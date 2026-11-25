"""Secretos en el keychain del SO vía ``keyring``.

Cada cuenta tiene una entrada con clave ``mail-multi-mcp:<email>``. Los valores
largos (p. ej. la cache de MSAL) se parten en trozos porque algunos backends
(Windows Credential Manager) limitan el tamaño de cada secreto.
"""

from __future__ import annotations

import contextlib

import keyring
import keyring.errors

PREFIX = "mail-multi-mcp"
USERNAME = "token"
CHUNK_SIZE = 1000
_CHUNK_MARK = "__chunks__:"


def account_key(email: str) -> str:
    return f"{PREFIX}:{email.lower()}"


def client_secret_key(provider: str) -> str:
    return f"{PREFIX}:oauth-client:{provider}"


class TokenStore:
    def __init__(self, username: str = USERNAME) -> None:
        self._username = username

    def get(self, key: str) -> str | None:
        value = keyring.get_password(key, self._username)
        if value is None or not value.startswith(_CHUNK_MARK):
            return value
        count = int(value[len(_CHUNK_MARK) :])
        parts: list[str] = []
        for i in range(count):
            part = keyring.get_password(f"{key}#{i}", self._username)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)

    def set(self, key: str, value: str) -> None:
        self.delete(key)
        if len(value) <= CHUNK_SIZE:
            keyring.set_password(key, self._username, value)
            return
        chunks = [value[i : i + CHUNK_SIZE] for i in range(0, len(value), CHUNK_SIZE)]
        for i, chunk in enumerate(chunks):
            keyring.set_password(f"{key}#{i}", self._username, chunk)
        keyring.set_password(key, self._username, f"{_CHUNK_MARK}{len(chunks)}")

    def delete(self, key: str) -> None:
        value = keyring.get_password(key, self._username)
        if value is None:
            return
        if value.startswith(_CHUNK_MARK):
            for i in range(int(value[len(_CHUNK_MARK) :])):
                self._safe_delete(f"{key}#{i}")
        self._safe_delete(key)

    def has(self, key: str) -> bool:
        return keyring.get_password(key, self._username) is not None

    def _safe_delete(self, key: str) -> None:
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(key, self._username)
