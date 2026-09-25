"""Small local environment-file loader shared by command-line apps."""

from __future__ import annotations

import os
from pathlib import Path


def load_local_environment(path: str | Path = ".env") -> None:
    """Load simple KEY=value entries without overriding process settings."""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip("\"'")
        if name and name not in os.environ:
            os.environ[name] = value
