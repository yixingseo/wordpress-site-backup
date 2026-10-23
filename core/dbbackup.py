"""用 mysqldump 导出数据库。"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .util import (
    decode_bytes,
    human_size,
    long_path,
    now_stamp,
    remove_quietly,
    safe_name,
    tail_contains,
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DUMP_MARKER = b"-- Dump completed"

_COMMON_PATTERNS = [
    r"D:\phpstudy_pro\Extensions\MySQL*\bin\mysqldump.exe",
    r"C:\phpstudy_pro\Extensions\MySQL*\bin\mysqldump.exe",
    r"E:\phpstudy_pro\Extensions\MySQL*\bin\mysqldump.exe",
    r"C:\xampp\mysql\bin\mysqldump.exe",
    r"D:\xampp\mysql\bin\mysqldump.exe",
    r"C:\wamp64\bin\mysql\mysql*\bin\mysqldump.exe",
    r"D:\wamp64\bin\mysql\mysql*\bin\mysqldump.exe",
    r"C:\laragon\bin\mysql\mysql-*\bin\mysqldump.exe",
    r"D:\laragon\bin\mysql\mysql-*\bin\mysqldump.exe",
    r"C:\Program Files\MySQL\MySQL Server *\bin\mysqldump.exe",
    r"C:\Program Files (x86)\MySQL\MySQL Server *\bin\mysqldump.exe",
]

_FULL_FLAGS = [
    "--single-transaction",
    "--quick",
    "--default-character-set=utf8mb4",
    "--routines",
    "--triggers",
    "--events",
    "--no-tablespaces",
]

_SAFE_FLAGS = [
    "--single-transaction",
    "--quick",
    "--default-character-set=utf8mb4",
]


def find_mysqldump(explicit: str = "") -> str | None:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
    which = shutil.which("mysqldump")
    if which:
        return which
    for pattern in _COMMON_PATTERNS:
        hits = sorted(glob.glob(pattern), reverse=True)
        if hits:
            return hits[0]
    return None


def _cnf_quote(value: str) -> str:
    value = str(value).replace("\r", "").replace("\n", "")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def _write_option_file(db) -> str:
    fd, name = tempfile.mkstemp(prefix="wpbackup_", suffix=".cnf")
    os.close(fd)
    lines = [
        "[client]",
        f"user={_cnf_quote(db.user)}",
        f"password={_cnf_quote(db.password)}",
    ]
    if db.socket:
        lines.append(f"socket={db.socket}")
    else:
        lines.append(f"host={db.host}")
        lines.append(f"port={db.port}")
    Path(name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return name


def _retryable(err: str) -> bool:
    low = err.lower()
    keys = (
        "unknown option",
        "unknown variable",
        "access denied",
        "access denied for user",
        "the used command is not allowed",
        "is not allowed with",
    )
    return any(k in low for k in keys)


def _run_dump(args, out_path, cancel=None, progress_cb=None):
    """执行 mysqldump，返回 (returncode, stderr_text, cancelled)。"""
    if progress_cb:
        try:
            progress_cb(0)
        except Exception:
            pass

    with open(long_path(out_path), "wb") as out:
        proc = subprocess.Popen(
            args,
            stdout=out,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        err_box: list[bytes] = []

        def _reader():
            try:
                err_box.append(proc.stderr.read())
            except Exception:
                pass

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        while True:
            code = proc.poll()
            if progress_cb:
                try:
                    progress_cb(os.path.getsize(out_path))
                except Exception:
                    pass
            if code is not None:
                break
            if cancel is not None and cancel.is_set():
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                reader.join(1)
                return -1, "已取消", True
            time.sleep(0.2)

        reader.join(2)
        err = decode_bytes(err_box[0]) if err_box else ""
    return code, err, False


def dump_database(site, out_dir, mysqldump: str, stamp: str | None = None,
                  cancel=None, progress_cb=None, logger=None) -> dict:
    """导出站点数据库，返回结果字典。"""
    started = time.time()
    stamp = stamp or now_stamp()
    db = site.db

    def fail(msg):
        return {
            "ok": False,
            "path": "",
            "size": 0,
            "message": msg,
            "elapsed": round(time.time() - started, 2),
        }

    if db is None:
        return fail(site.db_note or "wp-config.php 未指定数据库")
    if not mysqldump:
        return fail("未找到 mysqldump，可在界面中手动指定")

    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return fail(f"无法创建备份目录: {exc}")

    base = f"{safe_name(site.name)}_{safe_name(db.name)}_{stamp}"
    final = out_dir / f"{base}.sql"
    tmp = out_dir / f"{base}.sql.part"

    cnf_path = None
    try:
        cnf_path = _write_option_file(db)
        attempts = [_FULL_FLAGS, _SAFE_FLAGS]
        rc, err = -1, ""
        for index, flags in enumerate(attempts):
            args = [mysqldump, f"--defaults-extra-file={cnf_path}"] + flags + [db.name]
            if logger:
                logger.info("mysqldump 开始: 站点=%s 库=%s 尝试=%d", site.name, db.name, index + 1)
            rc, err, cancelled = _run_dump(args, tmp, cancel, progress_cb)
            if cancelled:
                remove_quietly(tmp)
                return fail("已取消")
            if rc == 0:
                break
            remove_quietly(tmp)
            if index + 1 < len(attempts) and _retryable(err):
                if logger:
                    logger.warning("mysqldump 失败，使用简化参数重试: %s", err.strip()[:300])
                continue
            break
    except FileNotFoundError:
        return fail(f"无法执行 mysqldump: {mysqldump}")
    except OSError as exc:
        return fail(f"导出失败: {exc}")
    finally:
        if cnf_path:
            remove_quietly(cnf_path)

    if rc != 0:
        detail = (err or "").strip()
        if len(detail) > 600:
            detail = detail[:600] + "…"
        return fail(f"mysqldump 退出码 {rc}。{detail}")

    try:
        size = os.path.getsize(tmp)
    except OSError:
        size = 0
    if size <= 0:
        remove_quietly(tmp)
        return fail("mysqldump 未产生任何数据")

    if not tail_contains(tmp, DUMP_MARKER):
        remove_quietly(tmp)
        return fail("导出文件缺少结束标记，可能不完整")

    try:
        os.replace(long_path(tmp), long_path(final))
    except OSError as exc:
        remove_quietly(tmp)
        return fail(f"重命名导出文件失败: {exc}")

    elapsed = round(time.time() - started, 2)
    return {
        "ok": True,
        "path": str(final),
        "size": size,
        "message": f"数据库 {db.name} 导出成功（{human_size(size)}，{elapsed}s）",
        "elapsed": elapsed,
    }
