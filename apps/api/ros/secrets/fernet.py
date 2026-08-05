"""Fernet master-key management + symmetric encrypt/decrypt with rotation (A/C1).

Key material is loaded once, ORDERED with the primary first, from (highest precedence first):
  1. `ROS_SECRET_KEY`            - primary key inline (secret-manager / KMS / Vault friendly)
  2. `ROS_SECRET_KEYS_FALLBACK`  - comma-separated OLD keys, decrypt-only (rotation)
  3. `ROS_SECRET_KEY_FILE`       - file with one base64 key per line (first = primary)
  4. auto-generate to the key file (DEV ONLY) - logs a loud WARNING, because on autoscaled or
     ephemeral-disk replicas each process generates a DIFFERENT key and cannot decrypt its
     peers' secrets (Fernet InvalidToken).

`encrypt()` always uses the PRIMARY key; `decrypt()` tries every known key (MultiFernet), so a
key rotates with zero downtime: promote a new primary, keep the old key as a fallback, lazily
`rotate()` existing tokens, then drop the old key. In prod, source `ROS_SECRET_KEY` from KMS/Vault
(envelope encryption: the KMS-wrapped DEK is unwrapped at boot and exported here) and keep this
interface - the pluggable KMS/Vault KEK provider is a cloud-edition follow-up.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet

from ros.config import settings

log = logging.getLogger("ros.secrets")


def _valid_key(raw: bytes) -> bytes:
    """Return a validated base64 Fernet key. Constructing Fernet raises ValueError on a
    malformed/short key, so a misconfigured key fails fast at boot rather than surfacing as an
    InvalidToken on the first decrypt."""
    Fernet(raw)  # validates length + base64; raises ValueError otherwise
    return raw


def _load_key_material() -> list[bytes]:
    """Ordered key list, PRIMARY first (see module docstring for precedence). Dedupes while
    preserving order so listing the same key twice (e.g. as both primary and fallback) is safe."""
    keys: list[bytes] = []
    seen: set[bytes] = set()

    def _add(raw: bytes) -> None:
        raw = raw.strip()
        if raw and raw not in seen:
            _valid_key(raw)
            seen.add(raw)
            keys.append(raw)

    if settings.secret_key:  # 1. inline primary (secret manager / KMS / Vault)
        _add(settings.secret_key.encode())
    for fallback in settings.secret_keys_fallback:  # 2. inline old keys (decrypt-only)
        _add(fallback.encode())
    path = Path(settings.secret_key_file)
    if path.exists():  # 3. file: one key per line, first is primary if none set above
        for line in path.read_bytes().splitlines():
            _add(line)
    if not keys:  # 4. dev fallback: generate + persist, and WARN (multi-replica hazard)
        generated = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(generated)
        try:
            os.chmod(path, 0o600)  # best-effort; no-op semantics on Windows
        except OSError:
            pass
        log.warning(
            "SECURITY: no ROS_SECRET_KEY or key file found - generated an EPHEMERAL master key "
            "at %s. DEV ONLY: on multiple/autoscaled replicas each generates a DIFFERENT key and "
            "cannot decrypt its peers' secrets. Set ROS_SECRET_KEY (from KMS/Vault) in production.",
            path,
        )
        keys.append(generated)
    return keys


@lru_cache(maxsize=1)
def _mf() -> MultiFernet:
    # MultiFernet encrypts with the FIRST key and decrypts by trying each in order.
    return MultiFernet([Fernet(k) for k in _load_key_material()])


def encrypt(plaintext: str) -> bytes:
    return _mf().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    return _mf().decrypt(token).decode("utf-8")


def rotate(token: bytes) -> bytes:
    """Re-encrypt an existing token under the CURRENT primary key without changing the plaintext.
    Use for lazy/background re-encryption after a key rotation so old-key tokens can be retired."""
    return _mf().rotate(token)
