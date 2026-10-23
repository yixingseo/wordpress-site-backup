"""通用工具函数。"""
from __future__ import annotations

import os
import time


class Cancelled(Exception):
    """任务被用户取消。"""


def long_path(p) -> str:
    """Windows 下给路径加 \\\\?\\ 前缀，绕过 260 字符限制。"""
    s = str(p)
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    return "\\\\?\\" + s


def strip_long(s: str) -> str:
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


def human_size(n) -> str:
    if n is None:
        return ""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_time(ts) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OSError, ValueError, OverflowError):
        return ""


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def safe_name(s: str, fallback: str = "site") -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if (c in bad or ord(c) < 32) else c for c in str(s))
    out = out.strip(" .")
    return out or fallback


def decode_bytes(b: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def decode_text_file(path) -> str:
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def tail_contains(path, marker: bytes, window: int = 16384) -> bool:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > window:
                f.seek(-window, os.SEEK_END)
            return marker in f.read()
    except OSError:
        return False


def remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass
