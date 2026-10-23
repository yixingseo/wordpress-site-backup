"""配置读写（JSON，放在程序目录下）。"""
from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "root_dir": "",
    "backup_dir": "",
    "depth": 3,
    "calc_size": True,
    "mysqldump_path": "",
    "compresslevel": 6,
    "verify_zip": True,
    "exclude_dirs": [".git", ".svn", "node_modules", "cache", "upgrade"],
    "exclude_globs": ["*.tmp", "*.log", "Thumbs.db", ".DS_Store"],
}


class Config:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged = dict(DEFAULT_CONFIG)
                merged.update(raw)
                self.data = merged
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def get(self, key, default=None):
        return self.data.get(key, DEFAULT_CONFIG.get(key, default))

    def set(self, key, value):
        self.data[key] = value
