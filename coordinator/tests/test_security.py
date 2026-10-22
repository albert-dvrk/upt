import pytest

from app.security import (
    check_password_policy, has_role, hash_password, hash_token, new_agent_token,
    sign_session, token_matches, verify_password, verify_session,
)


def test_password_hash_roundtrip_and_unique_salts():
    h1, h2 = hash_password("Correct-Horse-9"), hash_password("Correct-Horse-9")
    assert h1 != h2 and h1.startswith("scrypt$")
    assert verify_password("Correct-Horse-9", h1)
    assert not verify_password("correct-horse-9", h1)
    assert "Correct-Horse-9" not in h1


@pytest.mark.parametrize("stored", ["", "x", "scrypt$1$2", "md5$a$b$c$d$e", "scrypt$16384$8$1$!!$!!", "scrypt$abc$8$1$AAAA$AAAA"])
def test_garbage_hashes_never_verify_or_raise(stored):
    assert verify_password("anything", stored) is False


@pytest.mark.parametrize("pw,ok", [("short1A", False), ("alllowercase123", False), ("ALLUPPER12345", False), ("NoDigitsHereAtAll", False), ("Good-Passw0rd", True)])
def test_password_policy(pw, ok):
    assert (check_password_policy(pw) is None) == ok


def test_session_roundtrip_expiry_and_tampering():
    token, csrf = sign_session("s3cret", 7, "albert", 3600, now=1000)
    data = verify_session("s3cret", token, now=1500)
    assert data["uid"] == 7 and data["usr"] == "albert" and data["csrf"] == csrf
    assert verify_session("s3cret", token, now=5000) is None  # истёк
    assert verify_session("other", token, now=1500) is None  # чужой ключ
    body, sig = token.split(".")
    forged = body[:-2] + ("AA" if not body.endswith("AA") else "BB") + "." + sig
    assert verify_session("s3cret", forged, now=1500) is None
    for bad in (None, "", "abc", "a.b", "...", "%%%.%%%"):
        assert verify_session("s3cret", bad) is None


def test_agent_tokens():
    t = new_agent_token()
    assert len(t) >= 40 and t != new_agent_token()
    assert token_matches(t, hash_token(t)) and not token_matches(t + "x", hash_token(t))
    assert not token_matches(None, hash_token(t)) and not token_matches(t, None)
    assert len(hash_token(t)) == 64


def test_role_hierarchy():
    assert has_role(["administrator"], "viewer") and has_role(["operator"], "operator")
    assert not has_role(["viewer"], "operator") and not has_role([], "viewer")
    assert not has_role(["unknown"], "viewer")
