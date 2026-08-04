"""Encrypted secret storage (Fernet locally; Vault/KMS swap in prod)."""

from ros.secrets.store import SecretStore

__all__ = ["SecretStore"]
