"""Tests for the system-level curator (v0.4.1).

Probes are isolated functions, so most tests exercise them with monkeypatched
`subprocess.run` / `Path.read_text` substitutes. The render/fingerprint/write
layers are tested against real on-disk paths.
"""

from __future__ import annotations

from pathlib import Path

from homelabsage.curator import system as syscur
from homelabsage.curator.system import (
    SystemReport,
    _footer_fingerprint,
    curate_system,
    probe_cpu,
    probe_docker_info,
    probe_gpus,
    probe_kernel,
    probe_memory,
    probe_network_proxy,
    probe_unraid,
    probe_zpools,
    render,
    write_to_notes,
)

# ─── probe behaviour on absence ──────────────────────────────────────────


def test_probe_kernel_returns_none_when_nothing_available(monkeypatch, tmp_path):
    # uname not on PATH AND /etc/os-release missing
    monkeypatch.setattr(syscur, "_run", lambda *a, **kw: None)
    monkeypatch.setattr(syscur, "_read_file", lambda p: None)
    assert probe_kernel() is None


def test_probe_kernel_combines_uname_and_osrel(monkeypatch):
    monkeypatch.setattr(syscur, "_run", lambda cmd, **kw: "6.19.12" if cmd[0] == "uname" else None)
    monkeypatch.setattr(syscur, "_read_file", lambda p: (
        'PRETTY_NAME="Fedora Linux 43"\nID=fedora\nVERSION_ID=43\n' if str(p) == "/etc/os-release" else None
    ))
    out = probe_kernel()
    assert out == {"kernel": "6.19.12", "pretty_name": "Fedora Linux 43",
                   "id": "fedora", "version_id": "43"}


def test_probe_docker_info_parses_subset(monkeypatch):
    fake = '{"ServerVersion":"27.5.0","Driver":"overlay2","CgroupDriver":"systemd",' \
           '"DefaultRuntime":"runc","Runtimes":{"runc":{},"nvidia":{}},' \
           '"OperatingSystem":"Slackware","KernelVersion":"6.6.x","NCPU":24,"MemTotal":134217728}'
    monkeypatch.setattr(syscur, "_run", lambda *a, **kw: fake)
    info = probe_docker_info()
    assert info["server_version"] == "27.5.0"
    assert info["storage_driver"] == "overlay2"
    assert info["default_runtime"] == "runc"
    assert info["runtimes"] == ["nvidia", "runc"]


def test_probe_docker_info_returns_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(syscur, "_run", lambda *a, **kw: "not json")
    assert probe_docker_info() is None


def test_probe_cpu_finds_model_name(monkeypatch):
    cpuinfo = "processor\t: 0\nvendor_id\t: GenuineIntel\nmodel name\t: Intel Core Ultra 5 245K\n"
    monkeypatch.setattr(syscur, "_read_file", lambda p: cpuinfo if str(p) == "/proc/cpuinfo" else None)
    assert probe_cpu() == {"model": "Intel Core Ultra 5 245K"}


def test_probe_cpu_returns_none_without_proc(monkeypatch):
    monkeypatch.setattr(syscur, "_read_file", lambda p: None)
    assert probe_cpu() is None


def test_probe_memory_parses_meminfo(monkeypatch):
    meminfo = "MemTotal:      131072000 kB\nMemFree: 10000 kB\n"
    monkeypatch.setattr(syscur, "_read_file", lambda p: meminfo if str(p) == "/proc/meminfo" else None)
    assert probe_memory() == {"total_kb": 131072000}


def test_probe_gpus_nvidia_only(monkeypatch):
    def _run(cmd, **kw):
        if cmd[0] == "nvidia-smi":
            return "NVIDIA GeForce RTX 5060 Ti, 565.77, 16384"
        return None
    monkeypatch.setattr(syscur, "_run", _run)
    gpus = probe_gpus()
    assert gpus == [{
        "vendor": "nvidia",
        "name": "NVIDIA GeForce RTX 5060 Ti",
        "driver": "565.77",
        "memory_mib": "16384",
    }]


def test_probe_gpus_returns_none_when_no_tools(monkeypatch):
    monkeypatch.setattr(syscur, "_run", lambda *a, **kw: None)
    assert probe_gpus() is None


def test_probe_zpools_parses_tabular_output(monkeypatch):
    out = "tank\t10T\tONLINE\t1%\t42%\nfast\t1T\tONLINE\t0%\t11%"
    monkeypatch.setattr(syscur, "_run", lambda *a, **kw: out)
    pools = probe_zpools()
    assert pools and len(pools) == 2
    assert pools[0]["name"] == "tank"
    assert pools[0]["health"] == "ONLINE"


def test_probe_zpools_none_when_zfs_absent(monkeypatch):
    monkeypatch.setattr(syscur, "_run", lambda *a, **kw: None)
    assert probe_zpools() is None


def test_probe_unraid_requires_boot_config(monkeypatch, tmp_path):
    # Pretend /boot/config exists by patching Path.is_dir on that exact path
    real_is_dir = Path.is_dir
    def fake_is_dir(self):
        return str(self) == "/boot/config" or real_is_dir(self)
    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    monkeypatch.setattr(syscur, "_read_file", lambda p: 'version="7.2.0"' if str(p) == "/etc/unraid-version" else None)
    monkeypatch.setattr(syscur, "_run", lambda *a, **kw: None)
    out = probe_unraid()
    assert out and out["is_unraid"] is True
    assert out.get("version") == "7.2.0"


def test_probe_unraid_none_on_non_unraid(monkeypatch):
    # Force Path.is_dir to lie about /boot/config so we hit the negative path
    # regardless of where this test happens to run (CI vs Unraid).
    real_is_dir = Path.is_dir
    def fake_is_dir(self):
        if str(self) == "/boot/config":
            return False
        return real_is_dir(self)
    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    assert probe_unraid() is None


def test_probe_network_proxy_only_reports_present_binaries(monkeypatch):
    monkeypatch.setattr(
        syscur.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name in {"caddy", "tailscale"} else None,
    )
    out = probe_network_proxy()
    assert out == {"caddy": "/usr/bin/caddy", "tailscale": "/usr/bin/tailscale"}


# ─── render + fingerprint + write ────────────────────────────────────────


def _stub_report() -> SystemReport:
    return SystemReport(
        kernel={"pretty_name": "Fedora Linux 43", "kernel": "6.19.12", "id": "fedora", "version_id": "43"},
        docker={"server_version": "27.5.0", "storage_driver": "overlay2",
                "cgroup_driver": "systemd", "default_runtime": "runc",
                "runtimes": ["runc"]},
        cpu={"model": "Intel Core Ultra 5"},
        memory={"total_kb": 131072000},
        gpus=[{"vendor": "nvidia", "name": "RTX 5060 Ti", "driver": "565", "memory_mib": "16384"}],
        zpools=[{"name": "tank", "size": "10T", "health": "ONLINE", "fragmentation": "1%", "capacity": "42%"}],
        unraid=None,
        network={"caddy": "/usr/bin/caddy"},
    )


def test_render_includes_all_sections_when_populated():
    body = render(_stub_report())
    assert "# System" in body
    assert "Fedora Linux 43" in body
    assert "RTX 5060 Ti" in body
    assert "tank" in body
    assert "caddy" in body
    assert "Docker daemon" in body
    assert "<!-- curator: system@" in body  # footer
    assert body.endswith("-->")


def test_render_empty_report_explains_why():
    body = render(SystemReport())
    assert "No probes returned data" in body
    assert "<!-- curator: system@" in body


def test_fingerprint_is_deterministic_and_excludes_timestamp():
    r1 = _stub_report()
    r2 = _stub_report()
    # Different generated_at, same structural facts → same fingerprint
    r2.generated_at = r1.generated_at.replace(year=2030)
    assert r1.fingerprint() == r2.fingerprint()


def test_write_to_notes_creates_and_skips_when_unchanged(tmp_path):
    body = render(_stub_report())
    out = write_to_notes(tmp_path / "notes", body)
    assert out and out.exists()
    # mtime baseline
    mtime1 = out.stat().st_mtime_ns
    # Re-write with identical fingerprint → mtime should NOT advance
    out2 = write_to_notes(tmp_path / "notes", body)
    assert out2 == out
    assert out.stat().st_mtime_ns == mtime1


def test_write_to_notes_rewrites_on_fingerprint_change(tmp_path):
    body_v1 = render(_stub_report())
    write_to_notes(tmp_path / "notes", body_v1)
    # Mutate a structural fact → fingerprint changes → file is rewritten
    r2 = _stub_report()
    r2.docker["server_version"] = "27.6.0"
    body_v2 = render(r2)
    assert _footer_fingerprint(body_v1) != _footer_fingerprint(body_v2)
    out = write_to_notes(tmp_path / "notes", body_v2)
    assert "27.6.0" in out.read_text()


def test_write_to_notes_noop_with_empty_dir():
    assert write_to_notes("", "body") is None


def test_curate_system_returns_path_and_report(tmp_path, monkeypatch):
    # Force every probe to return a known value
    monkeypatch.setattr(syscur, "probe_kernel", lambda: {"kernel": "x"})
    monkeypatch.setattr(syscur, "probe_docker_info", lambda: None)
    monkeypatch.setattr(syscur, "probe_cpu", lambda: None)
    monkeypatch.setattr(syscur, "probe_memory", lambda: None)
    monkeypatch.setattr(syscur, "probe_gpus", lambda: None)
    monkeypatch.setattr(syscur, "probe_zpools", lambda: None)
    monkeypatch.setattr(syscur, "probe_unraid", lambda: None)
    monkeypatch.setattr(syscur, "probe_network_proxy", lambda: None)
    path, report = curate_system(tmp_path / "notes")
    assert path and path.name == "system.md"
    assert report.kernel == {"kernel": "x"}
