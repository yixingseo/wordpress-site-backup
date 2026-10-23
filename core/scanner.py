"""扫描站点目录：只找 wp-config.php，不遍历所有文件。"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import Cancelled, long_path, strip_long
from .wpconfig import DbConfig, load_db_config

WP_CONFIG_NAME = "wp-config.php"

# 这些目录里不可能有 wp-config.php，直接跳过以加速扫描
PRUNE_DIRS = {
    "wp-admin",
    "wp-includes",
    "wp-content",
    "node_modules",
    ".git",
    ".svn",
    ".hg",
}


@dataclass
class Site:
    path: Path
    name: str
    wp_config: Path
    created: float
    db: DbConfig | None = None
    db_note: str = ""
    has_wp_content: bool = False
    size_bytes: int | None = None
    status: str = "待备份"
    progress_text: str = ""
    progress: float | None = None
    result: dict = field(default_factory=dict)
    deleted: bool = False

    @property
    def db_text(self) -> str:
        return self.db.summary if self.db else "未配置"


def _make_site(dir_path: Path, wp_config: Path) -> Site:
    try:
        created = wp_config.stat().st_ctime
    except OSError:
        created = time.time()
    db, note = load_db_config(wp_config)
    return Site(
        path=dir_path,
        name=dir_path.name,
        wp_config=wp_config,
        created=created,
        db=db,
        db_note=note,
        has_wp_content=(dir_path / "wp-content").is_dir(),
    )


def scan_sites(root: str, max_depth: int = 3, cancel=None, on_found=None, log=None) -> list[Site]:
    """扫描 root 下最多 max_depth 层的目录，命中 wp-config.php 即视为站点。"""
    found: list[Site] = []
    seen: set[str] = set()

    def walk(dir_path: Path, depth: int):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            with os.scandir(long_path(dir_path)) as it:
                entries = list(it)
        except OSError as exc:
            if log:
                log(f"无法读取目录 {dir_path}: {exc}")
            return

        wp = None
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False) and entry.name.lower() == WP_CONFIG_NAME:
                    wp = Path(strip_long(entry.path))
                    break
            except OSError:
                continue

        if wp is not None:
            key = str(dir_path).lower()
            if key not in seen:
                seen.add(key)
                site = _make_site(dir_path, wp)
                found.append(site)
                if on_found:
                    on_found(site)
            return  # 命中后不再向下递归，避免嵌套误判

        if depth >= max_depth:
            return

        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name
            if name.startswith(".") or name.lower() in PRUNE_DIRS:
                continue
            walk(Path(strip_long(entry.path)), depth + 1)

    walk(Path(root), 0)
    found.sort(key=lambda s: (s.name.lower(), str(s.path).lower()))
    return found


def dir_size(path, cancel=None) -> int:
    """递归统计目录体积（字节）。"""
    total = 0
    stack = [long_path(path)]
    while stack:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total
