"""Rutas de configuración y datos del usuario (siempre fuera del repo)."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "mail-multi-mcp"
ENV_CONFIG_DIR = "MAIL_MULTI_MCP_CONFIG_DIR"
ENV_DATA_DIR = "MAIL_MULTI_MCP_DATA_DIR"


def config_dir() -> Path:
    return Path(os.environ.get(ENV_CONFIG_DIR) or user_config_dir(APP_NAME, appauthor=False))


def data_dir() -> Path:
    return Path(os.environ.get(ENV_DATA_DIR) or user_data_dir(APP_NAME, appauthor=False))


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} debe contener un objeto JSON")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Escritura atómica con permisos restringidos (0600) cuando el SO lo permite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    with contextlib.suppress(OSError):  # Windows / FS sin permisos POSIX
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
