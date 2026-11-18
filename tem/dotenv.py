"""極簡 .env 載入器（不覆蓋既有環境變數，無第三方依賴）。"""

from __future__ import annotations

import os
from pathlib import Path

_LOADED: set[str] = set()


def load_env_file(path: Path | None = None) -> None:
    from .paths import ROOT

    target = path or (ROOT / ".env")
    key = str(target.resolve())
    if key in _LOADED or not target.exists():
        return
    _LOADED.add(key)
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


def save_env_values(updates: dict[str, str], path: Path | None = None) -> Path:
    """更新本機 .env，同時立即套用到目前程序。"""
    from .paths import ROOT

    target = path or (ROOT / ".env")
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    keys = set(updates)
    lines: list[str] = []
    for raw in existing:
        line = raw.strip()
        name = line.partition("=")[0].strip() if "=" in line and not line.startswith("#") else ""
        if name not in keys:
            lines.append(raw)
    for name, value in updates.items():
        clean = str(value).replace("\r", "").replace("\n", "")
        lines.append(f"{name}={clean}")
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    for name, value in updates.items():
        os.environ[name] = str(value).replace("\r", "").replace("\n", "")
    _LOADED.add(str(target.resolve()))
    return target
