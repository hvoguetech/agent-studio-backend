"""A/C1: master-key rotation (MultiFernet) + multi-source key loading + ephemeral guard."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from ros.config import settings
from ros.secrets import fernet


@pytest.fixture
def keys(monkeypatch):
    """Set key material and clear the cached MultiFernet before/after so each test is isolated
    (the module caches _mf across the shared test process)."""
    fernet._mf.cache_clear()

    def _set(*, key=None, fallback=(), key_file=None):
        monkeypatch.setattr(settings, "secret_key", key)
        monkeypatch.setattr(settings, "secret_keys_fallback", list(fallback))
        if key_file is not None:
            monkeypatch.setattr(settings, "secret_key_file", key_file)
        fernet._mf.cache_clear()

    yield _set
    fernet._mf.cache_clear()  # monkeypatch restores settings; drop the test's cached keys


def test_roundtrip_with_env_key(keys):
    keys(key=Fernet.generate_key().decode())
    token = fernet.encrypt("s3cr3t")
    assert fernet.decrypt(token) == "s3cr3t"


def test_fallback_key_still_decrypts_old_tokens(keys):
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()

    keys(key=old)  # token written under the OLD key
    old_token = fernet.encrypt("legacy")

    keys(key=new, fallback=[old])  # rotate: new primary, old retained decrypt-only
    assert fernet.decrypt(old_token) == "legacy"  # old token still readable

    # new writes use the NEW primary; the old key alone cannot decrypt them
    new_token = fernet.encrypt("fresh")
    with pytest.raises(InvalidToken):
        Fernet(old.encode()).decrypt(new_token)


def test_rotate_reencrypts_under_primary(keys):
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()

    keys(key=old)
    token = fernet.encrypt("v")

    keys(key=new, fallback=[old])
    rotated = fernet.rotate(token)
    assert fernet.decrypt(rotated) == "v"
    # after rotate the token is decryptable by the NEW key alone -> old key can be retired
    assert Fernet(new.encode()).decrypt(rotated).decode() == "v"


def test_invalid_key_fails_fast(keys):
    keys(key="not-a-valid-fernet-key")
    with pytest.raises(ValueError):
        fernet.encrypt("x")


def test_generates_ephemeral_key_when_unconfigured(keys, tmp_path):
    key_file = tmp_path / "master.key"
    keys(key=None, fallback=[], key_file=str(key_file))
    assert not key_file.exists()
    token = fernet.encrypt("dev")
    assert fernet.decrypt(token) == "dev"
    assert key_file.exists()  # persisted for this process


def test_startup_warning_flags_missing_managed_key(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", None)
    assert any("ROS_SECRET_KEY" in w for w in settings.startup_warnings())
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    assert not any("ROS_SECRET_KEY" in w for w in settings.startup_warnings())
