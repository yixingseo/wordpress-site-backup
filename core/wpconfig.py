"""解析 wp-config.php 中的数据库配置。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .util import decode_text_file

_DEFINE_HEAD = re.compile(r"define\s*\(\s*")
_TABLE_PREFIX = re.compile(r"\$table_prefix\s*=\s*(['\"])(.*?)\1", re.S)


@dataclass
class DbConfig:
    name: str
    user: str
    password: str
    host: str
    port: int
    socket: str
    prefix: str

    @property
    def summary(self) -> str:
        if self.socket:
            return f"{self.name} @ socket"
        return f"{self.name} @ {self.host}:{self.port}"


def _read_quoted(text: str, i: int):
    """从 text[i] 的引号开始读取一个 PHP 字符串，返回 (值, 结束位置)。"""
    quote = text[i]
    i += 1
    buf = []
    mapping = {
        "n": "\n", "t": "\t", "r": "\r", "\\": "\\",
        "'": "'", '"': '"', "$": "$", "0": "\0",
    }
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            buf.append(mapping.get(nxt, nxt))
            i += 2
            continue
        if c == quote:
            return "".join(buf), i + 1
        buf.append(c)
        i += 1
    raise ValueError("unterminated string")


def parse_defines(text: str) -> dict:
    """提取 define('KEY', 'value') 中的所有键值对。"""
    result = {}
    for m in _DEFINE_HEAD.finditer(text):
        i = m.end()
        if i >= len(text) or text[i] not in "'\"":
            continue
        try:
            key, i = _read_quoted(text, i)
        except ValueError:
            continue
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i >= len(text) or text[i] != ",":
            continue
        i += 1
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        if i < len(text) and text[i] in "'\"":
            try:
                value, i = _read_quoted(text, i)
            except ValueError:
                continue
        else:
            j = text.find(")", i)
            if j < 0:
                continue
            value = text[i:j].strip()
        result.setdefault(str(key).strip(), value)
    return result


def split_host(raw: str):
    """解析 DB_HOST，返回 (host, port, socket)。"""
    raw = (raw or "").strip()
    if raw.startswith("p:"):
        raw = raw[2:]
    if not raw:
        return "localhost", 3306, ""
    # Unix socket 路径
    if raw.startswith("/") or raw.startswith("\\\\") or raw.endswith(".sock"):
        return "localhost", 3306, raw
    host, port, socket = raw, 3306, ""
    if ":" in raw:
        h, _, p = raw.rpartition(":")
        if p.isdigit():
            host, port = h or "localhost", int(p)
        else:
            host = raw
    return host, port, socket


def load_db_config(path) -> tuple[DbConfig | None, str]:
    """返回 (数据库配置, 说明)。未配置 DB_NAME 时返回 (None, 原因)。"""
    try:
        text = decode_text_file(Path(path))
    except OSError as exc:
        return None, f"读取 wp-config.php 失败: {exc}"

    defines = parse_defines(text)
    name = str(defines.get("DB_NAME", "") or "").strip()
    if not name:
        return None, "wp-config.php 未指定 DB_NAME"

    host, port, socket = split_host(str(defines.get("DB_HOST", "localhost")))
    m = _TABLE_PREFIX.search(text)
    prefix = m.group(2) if m else "wp_"

    return DbConfig(
        name=name,
        user=str(defines.get("DB_USER", "") or ""),
        password=str(defines.get("DB_PASSWORD", "") or ""),
        host=host,
        port=port,
        socket=socket,
        prefix=prefix,
    ), ""
