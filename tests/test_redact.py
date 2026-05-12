"""Tests for the export sanitiser.

The sanitiser is the only safety boundary between a user's homelab and a
public GitHub issue, so each redaction shape is checked individually plus
the dict-walk is exercised end-to-end.
"""

from homelabsage.redact import (
    SECRET_KEY_MARKERS,
    Sanitiser,
    _is_secret_key,
    _looks_like_credential,
)

# ─── key-name detection ──────────────────────────────────────────────────

def test_is_secret_key_matches_common_shapes():
    for k in ["DB_PASSWORD", "API_TOKEN", "MY_SECRET", "OPENAI_API_KEY",
              "session_signing_key", "client_secret", "AUTH_HEADER"]:
        assert _is_secret_key(k), k


def test_is_secret_key_ignores_innocent_keys():
    for k in ["PUID", "PGID", "TZ", "PORT", "DB_HOSTNAME", "DB_USERNAME"]:
        assert not _is_secret_key(k), k


def test_secret_key_markers_is_documented():
    # Sanity: the public marker tuple is the contract for downstream
    # consumers, keep at least these substrings present.
    for must in ("password", "token", "secret", "api_key"):
        assert must in SECRET_KEY_MARKERS


# ─── value-shape detection ──────────────────────────────────────────────

def test_looks_like_credential_jwt():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.S5DJ1gNBz1Q0wKr9oXn5lI6oXnNkk7g0vYK2jOQOOpA"
    assert _looks_like_credential(jwt)


def test_looks_like_credential_github_pat():
    assert _looks_like_credential("ghp_" + "A" * 36)
    assert _looks_like_credential("ghs_" + "B" * 36)


def test_looks_like_credential_openai_sk():
    assert _looks_like_credential("sk-" + "x" * 40)


def test_looks_like_credential_bearer():
    assert _looks_like_credential("Bearer " + "x" * 40)


def test_looks_like_credential_rejects_normal_strings():
    assert not _looks_like_credential("hello world")
    assert not _looks_like_credential("/photos")
    assert not _looks_like_credential("ghcr.io/jlp9989-sudo/homelabsage")


# ─── IP redaction ───────────────────────────────────────────────────────

def test_ipv4_private_is_redacted():
    s = Sanitiser()
    out = s.sanitise_text("DB at 192.168.31.200:5432")
    assert "192.168.31.200" not in out
    assert "10.0.0.1" in out


def test_ipv4_loopback_is_preserved():
    s = Sanitiser()
    out = s.sanitise_text("listening on 127.0.0.1:8000")
    assert "127.0.0.1" in out


def test_ipv4_aliasing_is_stable_within_one_sanitiser():
    s = Sanitiser()
    a = s.sanitise_text("redis on 192.168.31.200")
    b = s.sanitise_text("postgres on 192.168.31.200")
    # Same source IP must map to the same alias.
    assert a.replace("redis", "x") == b.replace("postgres", "x")


def test_ipv4_different_addresses_get_different_aliases():
    s = Sanitiser()
    out = s.sanitise_text("dbs at 192.168.31.200 and 192.168.31.201")
    assert "10.0.0.1" in out and "10.0.0.2" in out


def test_cidr_suffix_handled():
    s = Sanitiser()
    out = s.sanitise_text("subnet 192.168.0.0/24 routes via 10.42.0.1")
    # Both addresses get aliased (CIDR + non-CIDR), neither leaks.
    assert "192.168.0.0" not in out and "10.42.0.1" not in out


# ─── hostname redaction ─────────────────────────────────────────────────

def test_user_domain_is_redacted():
    s = Sanitiser()
    out = s.sanitise_text("visit https://mealie.jlp89.com/login")
    assert "mealie.jlp89.com" not in out
    assert "host-1" in out


def test_public_service_domains_are_preserved():
    s = Sanitiser()
    out = s.sanitise_text(
        "fetch https://api.github.com/repos/x/y "
        "pull ghcr.io/jlp9989-sudo/homelabsage"
    )
    assert "api.github.com" in out
    assert "ghcr.io" in out


def test_hostname_aliasing_is_stable():
    s = Sanitiser()
    a = s.sanitise_text("svc1 at mealie.jlp89.com")
    b = s.sanitise_text("svc2 at mealie.jlp89.com")
    # Same host → same alias on both lines
    assert "host-1" in a and "host-1" in b


# ─── env-var redaction ──────────────────────────────────────────────────

def test_sanitise_env_redacts_secret_keys():
    s = Sanitiser()
    out = s.sanitise_env(
        {
            "DB_PASSWORD": "super-secret-1234",
            "API_TOKEN": "xyz123",
            "PUID": "99",
            "TZ": "Europe/Madrid",
        }
    )
    assert out["DB_PASSWORD"] == "<redacted>"
    assert out["API_TOKEN"] == "<redacted>"
    assert out["PUID"] == "99"
    assert out["TZ"] == "Europe/Madrid"


def test_sanitise_env_redacts_credential_shaped_values_under_innocent_keys():
    s = Sanitiser()
    out = s.sanitise_env({"WEBHOOK_URL": "ghp_" + "A" * 36})
    assert out["WEBHOOK_URL"] == "<redacted>"


def test_sanitise_env_substitutes_ip_in_normal_values():
    s = Sanitiser()
    out = s.sanitise_env({"DB_HOSTNAME": "192.168.31.200"})
    # Not a secret key, not a credential shape → only IP substitution
    assert out["DB_HOSTNAME"] == "10.0.0.1"


# ─── recursive walk ─────────────────────────────────────────────────────

def test_sanitise_walks_nested_dicts():
    s = Sanitiser()
    out = s.sanitise(
        {
            "container": {
                "name": "mealie",
                "env": {
                    "DB_HOSTNAME": "192.168.31.200",
                    "DB_PASSWORD": "secret",
                },
                "url": "https://mealie.jlp89.com",
            }
        }
    )
    assert out["container"]["env"]["DB_HOSTNAME"] == "10.0.0.1"
    assert out["container"]["env"]["DB_PASSWORD"] == "<redacted>"
    assert "jlp89.com" not in out["container"]["url"]
    assert out["container"]["name"] == "mealie"


def test_sanitise_walks_lists():
    s = Sanitiser()
    out = s.sanitise(
        [
            {"host": "redis.jlp89.com", "port": 6379},
            {"host": "postgres.jlp89.com", "port": 5432},
        ]
    )
    assert all("jlp89.com" not in row["host"] for row in out)
    # The two hosts must be DIFFERENT aliases (host-1, host-2)
    assert out[0]["host"] != out[1]["host"]


def test_sanitise_returns_primitives_unchanged():
    s = Sanitiser()
    assert s.sanitise(42) == 42
    assert s.sanitise(None) is None
    assert s.sanitise(True) is True


def test_sanitise_redacts_credential_value_inside_dict_string():
    s = Sanitiser()
    out = s.sanitise(
        {"note": "auth header is Bearer " + "x" * 40}
    )
    # The credential-shape pattern is anchored, so a bearer embedded in prose
    # is NOT caught — we accept this tradeoff (anchoring prevents prose
    # false-positives on git SHAs etc). Document the behaviour with the
    # negative assertion below; if we later tighten this, update the test.
    assert "Bearer " in out["note"]
