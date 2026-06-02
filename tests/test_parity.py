"""Tests for the parity-aware notification gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from homelabsage import parity as parity_mod
from homelabsage.parity import (
    ParityState,
    check_mdcmd,
    check_mdstat,
    is_parity_running,
)

# Representative `cat /proc/mdstat` outputs.
MDSTAT_IDLE = """\
Personalities : [raid1] [raid6] [raid5] [raid4]
md0 : active raid1 sda1[0] sdb1[1]
      976630464 blocks super 1.2 [2/2] [UU]

unused devices: <none>
"""

MDSTAT_RECOVERING = """\
md0 : active raid1 sda1[0] sdb1[1]
      976630464 blocks super 1.2 [2/2] [UU]
      [=>...................]  recovery = 18.7% (3145728/976630464) finish=98.2min
"""

MDSTAT_RESHAPE = """\
md1 : active raid5 sda1[0] sdb1[1] sdc1[2]
      [=====>...............]  reshape = 32.0% (5000000/15600000) finish=42.0min
"""

MDSTAT_CHECK = """\
md0 : active raid1
      [======>..............]  check = 30% finish=120.0min
"""


def test_check_mdstat_returns_idle_on_clean_file(tmp_path: Path):
    f = tmp_path / "mdstat"
    f.write_text(MDSTAT_IDLE)
    assert not check_mdstat(f).running


def test_check_mdstat_detects_recovery(tmp_path: Path):
    f = tmp_path / "mdstat"
    f.write_text(MDSTAT_RECOVERING)
    state = check_mdstat(f)
    assert state.running
    assert "recovery" in state.reason.lower()
    assert "%" in state.reason


def test_check_mdstat_detects_reshape(tmp_path: Path):
    f = tmp_path / "mdstat"
    f.write_text(MDSTAT_RESHAPE)
    assert check_mdstat(f).running


def test_check_mdstat_detects_check_passes(tmp_path: Path):
    f = tmp_path / "mdstat"
    f.write_text(MDSTAT_CHECK)
    state = check_mdstat(f)
    assert state.running
    assert "check" in state.reason.lower()


def test_check_mdstat_absent_file_is_not_running(tmp_path: Path):
    assert not check_mdstat(tmp_path / "no-such-file").running


# ─── mdcmd ───────────────────────────────────────────────────────────────


def test_check_mdcmd_returns_none_when_binary_absent(monkeypatch):
    monkeypatch.setattr(parity_mod.shutil, "which", lambda name: None)
    assert check_mdcmd() is None


def test_check_mdcmd_parses_resync_in_progress(monkeypatch):
    monkeypatch.setattr(parity_mod.shutil, "which", lambda name: "/usr/local/sbin/mdcmd")

    class _R:
        returncode = 0
        stdout = "mdState=STARTED\nmdResync=12345\nmdResyncSize=20000000\n"
        stderr = ""

    monkeypatch.setattr(parity_mod.subprocess, "run", lambda *a, **kw: _R())
    state = check_mdcmd()
    assert state is not None and state.running
    assert "12345" in state.reason


def test_check_mdcmd_idle_when_resync_zero(monkeypatch):
    monkeypatch.setattr(parity_mod.shutil, "which", lambda name: "/usr/local/sbin/mdcmd")

    class _R:
        returncode = 0
        stdout = "mdState=STARTED\nmdResync=0\n"
        stderr = ""

    monkeypatch.setattr(parity_mod.subprocess, "run", lambda *a, **kw: _R())
    state = check_mdcmd()
    assert state is not None and not state.running


# ─── combined gate ──────────────────────────────────────────────────────


def test_is_parity_running_prefers_mdcmd_when_running(monkeypatch, tmp_path: Path):
    # mdstat says idle, mdcmd says running → result is running
    f = tmp_path / "mdstat"
    f.write_text(MDSTAT_IDLE)

    monkeypatch.setattr(
        parity_mod, "check_mdcmd",
        lambda: ParityState(True, "Unraid resync 50%"),
    )
    state = is_parity_running(mdstat_path=f)
    assert state.running
    assert "Unraid" in state.reason


def test_is_parity_running_falls_back_to_mdstat_when_mdcmd_clean(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(parity_mod, "check_mdcmd", lambda: ParityState(False, "mdcmd clean"))
    f = tmp_path / "mdstat"
    f.write_text(MDSTAT_RECOVERING)
    state = is_parity_running(mdstat_path=f)
    assert state.running


def test_is_parity_running_neither_source_running(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(parity_mod, "check_mdcmd", lambda: None)
    f = tmp_path / "mdstat"
    f.write_text(MDSTAT_IDLE)
    state = is_parity_running(mdstat_path=f)
    assert not state.running
    # Combined reason should mention both sources for diagnostics
    assert "no mdcmd" in state.reason
    assert "mdstat" in state.reason


# ─── engine integration ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_skips_push_outputs_when_gated(monkeypatch, tmp_path: Path):
    """Push outputs (is_push=True) are skipped while the gate is active."""

    from homelabsage.config import (
        Config,
        DiscordOutputConfig,
        NotionOutputConfig,
        OutputsConfig,
        ParityGateConfig,
    )
    from homelabsage.db import Database
    from homelabsage.engine import Engine
    from homelabsage.models import (
        Update,
    )
    from homelabsage.outputs import Output

    sent_to: list[str] = []

    class _FakePush(Output):
        id = "fake_push"
        is_push = True
        async def send(self, item):
            sent_to.append(self.id)

    class _FakePersistent(Output):
        id = "fake_persistent"
        is_push = False
        async def send(self, item):
            sent_to.append(self.id)

    class _FakePlugin:
        id = "fake_plugin"
        async def scan(self):
            return [Update(
                source="fake_plugin",
                subject="x",
                current_version="1.0",
                new_version="2.0",
            )]

    cfg = Config(
        outputs=OutputsConfig(
            notion=NotionOutputConfig(enabled=False),
            discord=DiscordOutputConfig(enabled=False),
        ),
        parity_gate=ParityGateConfig(enabled=True),
    )
    cfg.storage.database_path = str(tmp_path / "state.sqlite")
    cfg.llm.provider = "disabled"

    # Force the gate to report running
    monkeypatch.setattr(
        "homelabsage.engine.is_parity_running",
        lambda *, mdstat_path: ParityState(True, "test gate active"),
    )

    db = Database(cfg.storage.database_path)
    engine = Engine(cfg, db)
    engine.plugins = [_FakePlugin()]  # type: ignore[list-item]
    engine.outputs = [_FakePush(), _FakePersistent()]  # type: ignore[list-item]
    try:
        await engine.run_once()
    finally:
        engine.close()

    # Push gated → only persistent fired
    assert sent_to == ["fake_persistent"]
