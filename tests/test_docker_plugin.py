"""Unit tests for the Docker plugin's version logic.

We don't talk to the Docker daemon here; we exercise the pure functions
that decide whether a tag looks like a version and whether one version is
newer than another. These two functions are responsible for ~all of the
false positives the plugin can produce.
"""

from homelabsage.plugins.docker import _SEMVER_RE, DockerPlugin


def test_semver_re_accepts_clean_semver():
    for v in ["1.2.3", "v0.10.0", "10.20.30", "2.5", "v3.0"]:
        assert _SEMVER_RE.match(v), v


def test_semver_re_rejects_variant_tags():
    for bad in ["openvino", "latest", "main", "edge", "stable",
                "cuda", "ubuntu-22.04-full", "alpine", "release-1.30.0",
                "rocm", ""]:
        assert _SEMVER_RE.match(bad) is None, bad


def test_is_newer_returns_true_for_higher_version():
    assert DockerPlugin._is_newer("1.0.0", "1.0.1") is True
    assert DockerPlugin._is_newer("1.0.0", "2.0.0") is True
    assert DockerPlugin._is_newer("v1.2.3", "1.2.4") is True


def test_is_newer_returns_false_for_same_or_older():
    assert DockerPlugin._is_newer("1.2.3", "1.2.3") is False
    assert DockerPlugin._is_newer("2.0.0", "1.9.9") is False


def test_is_newer_refuses_to_compare_non_semver():
    # Previous behaviour fell back to string `!=` and produced false positives
    # like 'openvino' != '2.7.5' → True. The fix returns False instead.
    assert DockerPlugin._is_newer("openvino", "2.7.5") is False
    assert DockerPlugin._is_newer("latest", "2.7.5") is False
    assert DockerPlugin._is_newer("main", "1.0.0") is False
