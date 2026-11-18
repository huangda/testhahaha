"""專案路徑解析。"""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    env = os.environ.get("TEM_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


ROOT = project_root()
CONFIG_DIR = ROOT / "config"
DATA_DIR = Path(os.environ.get("TEM_DATA_DIR") or (ROOT / "data"))
REPORT_DIR = DATA_DIR / "reports"
EXPORT_DIR = DATA_DIR / "exports"
STATIC_DIR = ROOT / "web" / "static"


def ensure_dirs() -> None:
    for d in (DATA_DIR, REPORT_DIR, EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
