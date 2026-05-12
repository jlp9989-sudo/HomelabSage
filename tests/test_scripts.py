"""Tests for the universal cron / systemd / user-scripts probe layer.

Every probe is exercised against a tmp filesystem so the suite runs the
same on any host (Debian CI runner, my dev box, an Unraid array). No
real /etc, no real systemctl shell-out.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from homelabsage.scripts import (
    DiscoveryRoots,
    discover_scripts,
    probe_etc_cron_d,
    probe_etc_cron_periodic,
    probe_etc_crontab,
    probe_systemd_timers,
    probe_unraid_user_scripts,
    probe_user_crontabs,
)

# ─── /etc/crontab ─────────────────────────────────────────────────────────

def test_etc_crontab_parses_classic_lines(tmp_path: Path):
    (tmp_path / "crontab").write_text(
        "# comment\n"
        "SHELL=/bin/sh\n"
        "PATH=/usr/bin\n"
        "\n"
        "17 *  * * *  root  cd / && run-parts /etc/cron.hourly\n"
        "25 6  * * *  root  test -x /usr/sbin/anacron || /etc/cron.daily/run\n"
    )
    out = probe_etc_crontab(tmp_path)
    assert len(out) == 2
    assert out[0].source == "etc-crontab"
    assert out[0].schedule == "17 * * * *"
    assert out[0].user == "root"
    assert "run-parts" in out[0].command
    assert out[1].schedule == "25 6 * * *"


def test_etc_crontab_parses_vixie_specials(tmp_path: Path):
    (tmp_path / "crontab").write_text(
        "@daily root /usr/local/bin/backup\n"
        "@reboot www /usr/local/bin/announce-up\n"
    )
    out = probe_etc_crontab(tmp_path)
    assert [(s.schedule, s.user, s.command) for s in out] == [
        ("@daily", "root", "/usr/local/bin/backup"),
        ("@reboot", "www", "/usr/local/bin/announce-up"),
    ]


def test_etc_crontab_returns_empty_when_missing(tmp_path: Path):
    assert probe_etc_crontab(tmp_path) == []


def test_etc_crontab_returns_empty_when_etc_missing():
    assert probe_etc_crontab(Path("/nonexistent-prefix-xyzzy")) == []


# ─── /etc/cron.d/ ─────────────────────────────────────────────────────────

def test_cron_d_picks_up_dropins(tmp_path: Path):
    d = tmp_path / "cron.d"
    d.mkdir()
    (d / "logrotate").write_text("0 0 * * * root /usr/sbin/logrotate /etc/logrotate.conf\n")
    (d / "anacron").write_text("30 7 * * * root /usr/sbin/anacron -s\n")
    out = probe_etc_cron_d(tmp_path)
    assert {s.command for s in out} == {
        "/usr/sbin/logrotate /etc/logrotate.conf",
        "/usr/sbin/anacron -s",
    }
    assert all(s.source == "cron-d" for s in out)


def test_cron_d_ignores_invalid_filenames(tmp_path: Path):
    """cron silently skips files whose names aren't in [A-Za-z0-9_-]."""
    d = tmp_path / "cron.d"
    d.mkdir()
    (d / "file.with.dots").write_text("0 0 * * * root /bin/true\n")
    (d / "valid").write_text("0 0 * * * root /bin/false\n")
    (d / "spaced name").write_text("0 0 * * * root /bin/echo\n")
    out = probe_etc_cron_d(tmp_path)
    # `file.with.dots` and `valid` are both allowed; `spaced name` is not.
    names = [Path(s.path).name for s in out]
    assert "spaced name" not in names
    assert "valid" in names


# ─── /etc/cron.{hourly,daily,weekly,monthly} ──────────────────────────────

def test_cron_periodic_emits_one_per_script(tmp_path: Path):
    for sub in ("hourly", "daily", "weekly", "monthly"):
        d = tmp_path / f"cron.{sub}"
        d.mkdir()
        (d / "script1").write_text(f"#!/bin/sh\necho {sub}\n")
    out = probe_etc_cron_periodic(tmp_path)
    schedules = [s.schedule for s in out]
    assert schedules == ["@hourly", "@daily", "@weekly", "@monthly"]
    assert all(s.body.startswith("#!/bin/sh") for s in out)


def test_cron_periodic_skips_backup_files(tmp_path: Path):
    d = tmp_path / "cron.daily"
    d.mkdir()
    (d / "real").write_text("ok\n")
    (d / "real.bak").write_text("backup\n")
    (d / "real~").write_text("editor backup\n")
    (d / "real.dpkg-old").write_text("apt backup\n")
    out = probe_etc_cron_periodic(tmp_path)
    assert [Path(s.path).name for s in out] == ["real"]


# ─── user crontabs ────────────────────────────────────────────────────────

def test_user_crontab_debian_layout(tmp_path: Path):
    """Debian / Ubuntu: `/var/spool/cron/crontabs/<user>`."""
    d = tmp_path / "crontabs"
    d.mkdir()
    (d / "javi").write_text(
        "# my stuff\n"
        "0 9 * * * /usr/local/bin/morning-summary\n"
        "@weekly /home/javi/bin/heavy-task\n"
    )
    out = probe_user_crontabs(tmp_path)
    assert len(out) == 2
    assert out[0].user == "javi"
    assert out[0].schedule == "0 9 * * *"
    assert out[1].schedule == "@weekly"


def test_user_crontab_rhel_layout(tmp_path: Path):
    """RHEL / Fedora / Arch: bare `/var/spool/cron/<user>`, no `crontabs/` subdir."""
    (tmp_path / "root").write_text("*/15 * * * * /usr/local/bin/poll\n")
    out = probe_user_crontabs(tmp_path)
    assert len(out) == 1
    assert out[0].user == "root"
    assert out[0].schedule == "*/15 * * * *"


def test_user_crontab_missing_dir():
    assert probe_user_crontabs(Path("/nonexistent-prefix-xyzzy")) == []


# ─── systemd timers (mocked subprocess) ──────────────────────────────────

@dataclass
class _FakeCompleted:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_systemd_probe_returns_empty_when_no_systemctl():
    """Container / BSD / macOS: the probe must just no-op."""
    out = probe_systemd_timers(systemctl="/this/binary/does/not/exist")
    assert out == []


def test_systemd_probe_parses_typical_output(tmp_path: Path, monkeypatch):
    fake_output = (
        "Sun 2026-05-11 03:00:00 UTC 12h left  Sat 2026-05-10 03:00:00 UTC 11h ago "
        "logrotate.timer  logrotate.service\n"
        "Mon 2026-05-12 09:00:00 UTC 18h left  -                   -                 "
        "homelabsage-daily.timer homelabsage-daily.service\n"
    )

    def fake_runner(cmd, **kw):
        return _FakeCompleted(returncode=0, stdout=fake_output)

    # which() needs to say the binary exists; pretend ourselves.
    monkeypatch.setattr("homelabsage.scripts.shutil.which", lambda _: "/bin/systemctl")
    out = probe_systemd_timers(systemctl="/bin/systemctl", runner=fake_runner)
    timers = {s.name: s.extra["activates"] for s in out}
    assert timers == {
        "systemd-timer:logrotate.timer": "logrotate.service",
        "systemd-timer:homelabsage-daily.timer": "homelabsage-daily.service",
    }


def test_systemd_probe_returns_empty_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr("homelabsage.scripts.shutil.which", lambda _: "/bin/systemctl")

    def runner(*a, **k):
        return _FakeCompleted(returncode=1, stderr="oops")

    assert probe_systemd_timers("/bin/systemctl", runner=runner) == []


def test_systemd_probe_swallows_subprocess_error(monkeypatch):
    monkeypatch.setattr("homelabsage.scripts.shutil.which", lambda _: "/bin/systemctl")
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=10)
    assert probe_systemd_timers("/bin/systemctl", runner=boom) == []


# ─── Unraid User Scripts ──────────────────────────────────────────────────

def test_unraid_user_scripts_basic(tmp_path: Path):
    d = tmp_path / "nightly-backup"
    d.mkdir()
    (d / "script").write_text("#!/bin/bash\nrsync -a /mnt/user/data /backup/\n")
    (d / "name").write_text("Nightly backup\n")
    (d / "description").write_text("rsyncs /mnt/user/data to the backup share.\n")
    out = probe_unraid_user_scripts(tmp_path)
    assert len(out) == 1
    assert out[0].source == "unraid-user-scripts"
    assert "rsync" in out[0].body
    assert out[0].extra["display_name"] == "Nightly backup"
    assert "rsyncs" in out[0].purpose_hint


def test_unraid_user_scripts_ignores_empty_subdirs(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    (tmp_path / "no-script").mkdir()
    (tmp_path / "no-script" / "name").write_text("Just metadata, no script\n")
    assert probe_unraid_user_scripts(tmp_path) == []


def test_unraid_user_scripts_returns_empty_when_root_missing():
    assert probe_unraid_user_scripts(Path("/nonexistent-prefix-xyzzy")) == []


# ─── orchestrator ─────────────────────────────────────────────────────────

def test_discover_scripts_combines_sources(tmp_path: Path, monkeypatch):
    # Build a tiny mock filesystem under tmp_path/etc + tmp_path/spool.
    etc = tmp_path / "etc"
    (etc / "cron.d").mkdir(parents=True)
    (etc / "cron.d" / "logrotate").write_text("0 0 * * * root /usr/sbin/logrotate\n")
    (etc / "crontab").write_text("17 * * * * root /usr/bin/uptime\n")
    spool = tmp_path / "spool"
    (spool / "crontabs").mkdir(parents=True)
    (spool / "crontabs" / "javi").write_text("@daily /home/javi/bin/x\n")

    # No systemd, no Unraid (default flags + disabled).
    monkeypatch.setattr("homelabsage.scripts.shutil.which", lambda _: None)
    out = discover_scripts(DiscoveryRoots(etc=etc, cron_spool=spool))
    sources = {s.source for s in out}
    assert sources == {"etc-crontab", "cron-d", "user-crontab"}


def test_discover_scripts_unraid_opt_in(tmp_path: Path, monkeypatch):
    """`/boot` happens to exist on many distros (UEFI partition, pi rootfs);
    we must NOT probe it unless the caller explicitly asks."""
    unraid = tmp_path / "boot-scripts"
    unraid.mkdir()
    sub = unraid / "rogue"
    sub.mkdir()
    (sub / "script").write_text("oops\n")

    monkeypatch.setattr("homelabsage.scripts.shutil.which", lambda _: None)
    out_off = discover_scripts(
        DiscoveryRoots(etc=tmp_path / "missing", cron_spool=tmp_path / "missing",
                       unraid_user_scripts=unraid),
        enable_unraid=False,
    )
    out_on = discover_scripts(
        DiscoveryRoots(etc=tmp_path / "missing", cron_spool=tmp_path / "missing",
                       unraid_user_scripts=unraid),
        enable_unraid=True,
    )
    assert out_off == []
    assert len(out_on) == 1 and out_on[0].source == "unraid-user-scripts"


def test_discover_scripts_safe_on_clean_machine(tmp_path: Path, monkeypatch):
    """Pointing every root at empty dirs must produce [] without raising."""
    monkeypatch.setattr("homelabsage.scripts.shutil.which", lambda _: None)
    out = discover_scripts(DiscoveryRoots(
        etc=tmp_path / "empty1",
        cron_spool=tmp_path / "empty2",
        unraid_user_scripts=tmp_path / "empty3",
    ))
    assert out == []
