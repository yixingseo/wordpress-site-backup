"""把 wp-content + wp-config.php + sql 打成 zip。

zip 内结构::

    {站点名}/
        wp-content/...
        {站点名}_{库名}_{日期}.sql
        wp-config.php
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import time
import zipfile
from pathlib import Path

from .util import Cancelled, human_size, long_path, now_stamp, safe_name, strip_long

CHUNK = 1024 * 1024
VERIFY_LIMIT = 400 * 1024 * 1024  # 超过 400MB 不做全量 CRC 校验


def _excluded_file(name: str, globs) -> bool:
    low = name.lower()
    for pattern in globs:
        if fnmatch.fnmatch(low, pattern.lower()):
            return True
    return False


def _collect(base: Path, arc_base: str, exclude_dirs, exclude_globs, cancel):
    """返回 ([(源路径, zip内路径, 大小, 是否目录)], 总字节, 文件数, 警告列表)。"""
    items = []
    warnings = []
    total = 0
    files = 0
    dirs = {name.lower() for name in exclude_dirs}
    stack = [(long_path(base), arc_base)]

    while stack:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        current, arc = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError as exc:
            warnings.append(f"跳过无法读取的目录 {arc}: {exc}")
            continue

        subdirs = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() in dirs:
                        continue
                    subdirs.append((entry.path, f"{arc}/{entry.name}"))
                elif entry.is_file(follow_symlinks=False):
                    if _excluded_file(entry.name, exclude_globs):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    items.append((entry.path, f"{arc}/{entry.name}", stat.st_size, False))
                    total += stat.st_size
                    files += 1
            except OSError as exc:
                warnings.append(f"跳过 {entry.name}: {exc}")

        for path, sub_arc in subdirs:
            items.append((path, sub_arc + "/", 0, True))
            stack.append((path, sub_arc))

    return items, total, files, warnings


def build_zip(site, out_dir, stamp: str | None = None, sql_path=None,
              exclude_dirs=(), exclude_globs=(), compresslevel: int = 6,
              verify: bool = True, cancel=None, progress_cb=None, logger=None) -> dict:
    started = time.time()
    stamp = stamp or now_stamp()
    site_name = site.name
    root_name = safe_name(site_name, "site")

    def fail(msg):
        return {
            "ok": False,
            "path": "",
            "size": 0,
            "files": 0,
            "message": msg,
            "elapsed": round(time.time() - started, 2),
            "warnings": [],
        }

    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return fail(f"无法创建备份目录: {exc}")

    zip_name = f"{root_name}_{stamp}.zip"
    final = out_dir / zip_name
    tmp = out_dir / f"{zip_name}.part"

    items: list[tuple] = []
    warnings: list[str] = []
    total = 0
    file_count = 0

    wp_content = Path(site.path) / "wp-content"
    if wp_content.is_dir():
        items.append((long_path(wp_content), f"{root_name}/wp-content/", 0, True))
        try:
            part, part_total, part_files, part_warn = _collect(
                wp_content, f"{root_name}/wp-content", exclude_dirs, exclude_globs, cancel
            )
            # 备份目录若恰好位于 wp-content 内，必须排除，否则会把旧备份再次打包
            part, part_total, part_files = _drop_under(part, part_total, part_files, out_dir)
            items.extend(part)
            total += part_total
            file_count += part_files
            warnings.extend(part_warn)
        except Cancelled:
            return fail("已取消")
        except OSError as exc:
            warnings.append(f"读取 wp-content 失败: {exc}")
    else:
        warnings.append("站点没有 wp-content 目录")

    try:
        wp_config = Path(site.wp_config)
        cfg_size = wp_config.stat().st_size
        items.append((long_path(wp_config), f"{root_name}/wp-config.php", cfg_size, False))
        total += cfg_size
        file_count += 1
    except OSError as exc:
        warnings.append(f"wp-config.php 读取失败: {exc}")

    if sql_path:
        try:
            sql_file = Path(sql_path)
            sql_size = sql_file.stat().st_size
            items.append((long_path(sql_file), f"{root_name}/{sql_file.name}", sql_size, False))
            total += sql_size
            file_count += 1
        except OSError as exc:
            warnings.append(f"sql 文件加入失败: {exc}")

    try:
        free = shutil.disk_usage(str(out_dir)).free
        if total and free < total * 1.02 + 50 * 1024 * 1024:
            return fail(
                f"备份盘空间不足：需要约 {human_size(total)}，可用 {human_size(free)}"
            )
    except OSError:
        pass

    if progress_cb:
        try:
            progress_cb(0, total, "")
        except Exception:
            pass

    done = 0
    last_emit = 0.0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=compresslevel, allowZip64=True) as zf:
            for source, arc, size, is_dir in items:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()

                if is_dir:
                    info = zipfile.ZipInfo(arc)
                    info.external_attr = (0o40755 << 16) | 0x10
                    zf.writestr(info, b"")
                    continue

                try:
                    info = zipfile.ZipInfo.from_file(source, arc, strict_timestamps=False)
                except (OSError, ValueError) as exc:
                    warnings.append(f"跳过 {arc}: {exc}")
                    continue
                info.compress_type = zipfile.ZIP_DEFLATED

                try:
                    src = open(source, "rb")
                except OSError as exc:
                    warnings.append(f"跳过被占用/无权限的文件 {arc}: {exc}")
                    continue

                with src:
                    with zf.open(info, "w") as dst:
                        while True:
                            chunk = src.read(CHUNK)
                            if not chunk:
                                break
                            dst.write(chunk)
                            done += len(chunk)
                            now = time.time()
                            if progress_cb and now - last_emit >= 0.15:
                                last_emit = now
                                try:
                                    progress_cb(done, total, arc)
                                except Exception:
                                    pass
                if progress_cb:
                    try:
                        progress_cb(done, total, arc)
                    except Exception:
                        pass

        raw_size = os.path.getsize(tmp)

        verified = False
        if verify and raw_size <= VERIFY_LIMIT:
            with zipfile.ZipFile(tmp) as check:
                bad = check.testzip()
            if bad:
                raise OSError(f"校验失败，压缩包内文件损坏: {bad}")
            verified = True

        os.replace(long_path(tmp), long_path(final))
    except Cancelled:
        _cleanup(tmp)
        return fail("已取消")
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        _cleanup(tmp)
        return fail(f"打包失败: {exc}")

    elapsed = round(time.time() - started, 2)
    size = os.path.getsize(final)
    note = "已校验" if verified else "未做全量校验"
    return {
        "ok": True,
        "path": str(final),
        "size": size,
        "files": file_count,
        "message": f"打包完成（{file_count} 个文件，{human_size(size)}，{elapsed}s，{note}）",
        "elapsed": elapsed,
        "warnings": warnings[:50],
    }


def _cleanup(path):
    try:
        os.remove(long_path(path))
    except OSError:
        pass


def _drop_under(items, total, file_count, out_dir):
    """排除位于备份输出目录内的条目（避免把旧备份打进新包）。"""
    try:
        out_abs = os.path.normcase(os.path.abspath(str(out_dir)))
    except OSError:
        return items, total, file_count
    prefix = out_abs.rstrip("\\/") + os.sep
    kept = []
    new_total = 0
    new_files = 0
    for src, arc, size, is_dir in items:
        src_abs = os.path.normcase(os.path.abspath(strip_long(str(src))))
        if src_abs == out_abs or src_abs.startswith(prefix):
            continue
        kept.append((src, arc, size, is_dir))
        if not is_dir:
            new_total += size
            new_files += 1
    return kept, new_total, new_files
