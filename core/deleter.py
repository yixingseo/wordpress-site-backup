"""删除已成功备份的站点源目录（带多重安全校验）。"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import stat
import sys
import threading
import time
import zipfile
from ctypes import wintypes
from pathlib import Path

from .util import Cancelled, human_size, long_path, safe_name

FO_DELETE = 3
FOF_ALLOWUNDO = 0x40
FOF_NOCONFIRMATION = 0x10
FOF_SILENT = 0x4
FOF_NOERRORUI = 0x400

VERIFY_LIMIT = 400 * 1024 * 1024


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_under(child, parent) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def can_delete(site) -> tuple[bool, str]:
    """判断站点是否具备删除条件（只做元数据判断，不动磁盘）。"""
    result = getattr(site, "result", None) or {}
    if getattr(site, "deleted", False):
        return False, "源目录已删除"
    if not result.get("zip"):
        return False, "没有成功的 zip 备份"
    if site.db is not None and not result.get("sql"):
        return False, "数据库未成功备份"
    return True, ""


def verify_backup(site) -> tuple[bool, str]:
    """删除前再次确认备份包存在且可读。"""
    ok, reason = can_delete(site)
    if not ok:
        return False, reason

    zip_path = (site.result or {}).get("zip") or ""
    if not os.path.isfile(zip_path):
        return False, f"备份文件不存在: {zip_path}"

    sql_path = (site.result or {}).get("sql") or ""
    if site.db is not None:
        if not sql_path or not os.path.isfile(sql_path):
            return False, f"数据库备份文件不存在: {sql_path or '未记录'}"

    root = safe_name(site.name, "site")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            if f"{root}/wp-config.php" not in names:
                return False, "备份包内缺少 wp-config.php"
            if os.path.getsize(zip_path) <= VERIFY_LIMIT:
                bad = zf.testzip()
                if bad:
                    return False, f"备份包校验失败: {bad}"
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        return False, f"备份包无法读取: {exc}"
    return True, ""


def _recycle(path: str) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "当前系统不支持回收站"
    target = os.path.abspath(path)
    if len(target) > 250:
        return False, "路径过长，回收站不支持，请改用永久删除"
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None  # type: ignore[assignment]
    op.wFunc = FO_DELETE
    op.pFrom = target + "\0"
    op.pTo = None  # type: ignore[assignment]
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    try:
        ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except OSError as exc:
        return False, f"调用回收站失败: {exc}"
    if ret != 0:
        return False, f"回收站操作返回错误码 {ret}"
    if op.fAnyOperationsAborted:
        return False, "操作被中止"
    return True, ""


def _retry_writable(func, target):
    try:
        os.chmod(target, stat.S_IWRITE)
        func(target)
    except OSError:
        pass


def _rmtree(path: str):
    kwargs = {}
    if sys.version_info >= (3, 12):
        kwargs["onexc"] = lambda func, target, exc: _retry_writable(func, target)
    else:
        kwargs["onerror"] = lambda func, target, exc_info: _retry_writable(func, target)
    shutil.rmtree(long_path(path), **kwargs)


def delete_directory(path, to_recycle_bin: bool = True) -> tuple[bool, str]:
    """删除站点目录。返回 (是否成功, 说明)。"""
    site_path = Path(path)
    if not site_path.is_dir():
        return False, "目录不存在"
    if not (site_path / "wp-config.php").is_file():
        return False, "目录下没有 wp-config.php，拒绝删除（可能不是站点目录）"

    if to_recycle_bin:
        ok, reason = _recycle(str(site_path))
        if ok:
            return True, "已移到回收站"
        return False, f"移到回收站失败：{reason}（可勾选“永久删除”重试）"

    try:
        _rmtree(str(site_path))
    except OSError as exc:
        return False, f"永久删除失败: {exc}"
    if site_path.exists():
        return False, "永久删除后目录仍然存在"
    return True, "已永久删除"


def precheck(site, backup_dir: str, scan_root: str) -> tuple[bool, str]:
    """删除前的路径安全检查。"""
    site_path = Path(site.path)
    if not site_path.is_dir():
        return False, "源目录不存在"

    if scan_root and _norm(site_path) == _norm(scan_root):
        return False, "该站点就是扫描根目录，禁止删除"

    if backup_dir and _is_under(backup_dir, site_path):
        return False, "备份目录位于该站点目录内，删除会连备份一起删除"

    return True, ""


class DeleteRunner(threading.Thread):
    """批量删除已备份的源目录。"""

    def __init__(self, sites, backup_dir: str, scan_root: str,
                 to_recycle_bin: bool, events, cancel: threading.Event, logger=None):
        super().__init__(daemon=True, name="DeleteRunner")
        self.sites = sites
        self.backup_dir = backup_dir
        self.scan_root = scan_root
        self.to_recycle_bin = to_recycle_bin
        self.events = events
        self.cancel = cancel
        self.log = logger or logging.getLogger("wpbackup")

    def _emit(self, kind, *args):
        self.events.put((kind,) + args)

    def run(self):
        total = len(self.sites)
        self._emit("del_overall", 0, total)
        summary = {"ok": 0, "fail": 0, "stopped": 0}

        for index, site in enumerate(self.sites, 1):
            path_key = str(site.path)
            if self.cancel.is_set():
                summary["stopped"] += 1
                self._emit("del_result", path_key, "已停止", "队列已停止")
                self._emit("del_overall", index, total)
                continue

            self._emit("del_status", path_key, "删除中", "校验备份…")
            ok, reason = precheck(site, self.backup_dir, self.scan_root)
            if ok:
                ok, reason = verify_backup(site)
            if ok:
                self._emit("del_status", path_key, "删除中", "正在删除源目录…")
                ok, reason = delete_directory(site.path, self.to_recycle_bin)

            if ok:
                summary["ok"] += 1
                size_text = human_size(site.size_bytes) if site.size_bytes else ""
                message = reason + (f"（释放约 {size_text}）" if size_text else "")
                self._emit("del_result", path_key, "已删除", message)
                self.log.info("[%s] 删除源目录成功: %s", site.name, reason)
            else:
                summary["fail"] += 1
                self._emit("del_status", path_key, "删除失败", reason)
                self._emit("del_result", path_key, "删除失败", reason)
                self.log.warning("[%s] 删除源目录失败: %s", site.name, reason)

            self._emit("del_overall", index, total)

        self._emit("del_finished", summary)
