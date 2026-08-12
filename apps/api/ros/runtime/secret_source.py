"""In-memory secret source for the standalone runtime.

A `SecretStore` drop-in that resolves `secret://` refs from an in-memory map — the manifest's
run-scoped resolved secrets — instead of the master DB. This is how tool auth resolves at call time
on a VM WITHOUT shipping the master decryption key: master resolves the run's referenced secrets and
embeds them in the manifest; the runtime reads them from here. Read-only.
"""

from __future__ import annotations

from typing import Any

from ros.secrets.store import SecretNotFound


class InMemorySecretStore:
    def __init__(self, secrets: dict[str, Any] | None = None) -> None:
        self._secrets = secrets or {}

    async def read_ref(self, *, tenant_id: str, project_id: str, ref: str) -> Any:
        if ref in self._secrets:
            return self._secrets[ref]
        raise SecretNotFound(f"{ref} not present in the run manifest")
