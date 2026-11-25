"""IDs compuestos ``<alias>:<provider_id>``.

El alias identifica la cuenta dueña del objeto; así una respuesta o un envío
siempre salen de la cuenta correcta sin necesidad de una "cuenta default".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SEPARATOR = ":"


class InvalidIdError(ValueError):
    """El ID no tiene el formato ``<alias>:<provider_id>``."""


def validate_alias(alias: str) -> str:
    if not ALIAS_RE.match(alias):
        raise ValueError(
            f"Alias inválido {alias!r}: usá minúsculas, dígitos, '-' o '_' (máx. 32), "
            "empezando por letra o dígito."
        )
    if alias == "all":
        raise ValueError("'all' es una palabra reservada y no puede usarse como alias.")
    return alias


@dataclass(frozen=True, slots=True)
class CompositeId:
    alias: str
    provider_id: str

    def __str__(self) -> str:
        return format_id(self.alias, self.provider_id)


def format_id(alias: str, provider_id: str) -> str:
    validate_alias(alias)
    if not provider_id:
        raise InvalidIdError("provider_id vacío")
    return f"{alias}{SEPARATOR}{provider_id}"


def parse_id(value: str) -> CompositeId:
    """Separa en el primer ``:``; el provider_id puede contener ``:`` (no así el alias)."""
    if not isinstance(value, str) or SEPARATOR not in value:
        raise InvalidIdError(f"ID inválido {value!r}: se esperaba '<alias>:<id>'")
    alias, provider_id = value.split(SEPARATOR, 1)
    if not provider_id:
        raise InvalidIdError(f"ID inválido {value!r}: falta el id del proveedor")
    try:
        validate_alias(alias)
    except ValueError as exc:
        raise InvalidIdError(f"ID inválido {value!r}: {exc}") from exc
    return CompositeId(alias, provider_id)
