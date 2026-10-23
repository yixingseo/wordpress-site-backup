"""批量备份任务调度（后台线程 + 事件队列）。"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .dbbackup import dump_database
from .util import Cancelled, now_stamp, safe_name
from .zipbackup import build_zip


@dataclass
class Settings:
    backup_dir: str
    mysqldump_path: str
    compresslevel: int = 6
    verify_zip: bool = True
    exclude_dirs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)


@dataclass
class SiteJob:
    site: object
    mode: str = "full"  # full = 数据库 + 打包；db = 只导出 sql


class BackupRunner(threading.Thread):
    """顺序处理站点任务，所有 UI 更新通过 events 队列回传。"""

    def __init__(self, jobs, settings: Settings, events, cancel: threading.Event, logger=None):
        super().__init__(daemon=True, name="BackupRunner")
        self.jobs = jobs
        self.settings = settings
        self.events = events
        self.cancel = cancel
        self.log = logger or logging.getLogger("wpbackup")

    def _emit(self, kind, *args):
        self.events.put((kind,) + args)

    def _log(self, level: str, msg: str):
        self._emit("log", level, msg)

    def run(self):
        total = len(self.jobs)
        self._emit("overall", 0, total)
        summary = {"ok": 0, "partial": 0, "fail": 0, "stopped": 0}

        for index, job in enumerate(self.jobs, 1):
            site = job.site
            if self.cancel.is_set():
                summary["stopped"] += 1
                self._emit("status", str(site.path), "已停止", "队列已停止")
                self._emit("result", str(site.path), "已停止", "队列已停止", {})
                continue

            self._run_one(job, index, total, summary)

        self._emit("finished", summary)

    def _run_one(self, job, index, total, summary):
        site = job.site
        path_key = str(site.path)
        started = time.time()
        stamp = now_stamp()
        site_dir = Path(self.settings.backup_dir) / safe_name(site.name, "site")
        notes: list[str] = []
        detail: dict = {"sql": "", "zip": "", "warnings": []}

        self._log("INFO", f"[{site.name}] 开始备份（{index}/{total}）")
        self._emit("current", path_key, f"{site.name}：准备中")

        sql_path = ""
        sql_result = None

        if job.mode in ("full", "db"):
            self._emit("status", path_key, "导出DB中", "")
            if site.db is None:
                msg = site.db_note or "未配置数据库"
                notes.append(msg)
                self._log("WARN", f"[{site.name}] {msg}，跳过数据库")
            else:
                self._emit(
                    "progress", path_key, None,
                    f"导出 {site.db.name}…",
                )

                def db_progress(written, _key=path_key):
                    self._emit("progress", _key, None, f"导出 DB {_fmt(written)}")

                sql_result = dump_database(
                    site, site_dir, self.settings.mysqldump_path,
                    stamp=stamp, cancel=self.cancel,
                    progress_cb=db_progress, logger=self.log,
                )
                if sql_result["ok"]:
                    sql_path = sql_result["path"]
                    detail["sql"] = sql_path
                    self._log("INFO", f"[{site.name}] {sql_result['message']}")
                else:
                    notes.append(sql_result["message"])
                    self._log("ERROR", f"[{site.name}] 数据库导出失败: {sql_result['message']}")
                    if job.mode == "db":
                        elapsed = round(time.time() - started, 2)
                        self._finish_site(
                            site, path_key, "失败", sql_result["message"],
                            detail, elapsed, summary, index, total,
                        )
                        return

        if job.mode == "db":
            elapsed = round(time.time() - started, 2)
            if sql_result and sql_result["ok"]:
                self._finish_site(
                    site, path_key, "成功", sql_result["message"],
                    detail, elapsed, summary, index, total,
                )
            else:
                msg = notes[0] if notes else "未导出数据库"
                self._finish_site(
                    site, path_key, "部分成功", msg,
                    detail, elapsed, summary, index, total,
                )
            return

        # 打包（db 失败也继续打包）
        self._emit("status", path_key, "打包中", "")
        self._emit("current", path_key, f"{site.name}：打包 wp-content")
        if self.cancel.is_set():
            self._finish_site(site, path_key, "已停止", "已取消", detail,
                              round(time.time() - started, 2), summary, index, total)
            return

        def zip_progress(done, total_bytes, current, _key=path_key):
            if total_bytes:
                pct = min(100.0, done * 100.0 / total_bytes)
                self._emit(
                    "progress", _key, pct,
                    f"{pct:.0f}% ({_fmt(done)}/{_fmt(total_bytes)})",
                )
            else:
                self._emit("progress", _key, None, _fmt(done))

        zip_result = build_zip(
            site, site_dir, stamp=stamp, sql_path=sql_path or None,
            exclude_dirs=self.settings.exclude_dirs,
            exclude_globs=self.settings.exclude_globs,
            compresslevel=self.settings.compresslevel,
            verify=self.settings.verify_zip,
            cancel=self.cancel, progress_cb=zip_progress, logger=self.log,
        )
        if zip_result.get("warnings"):
            detail["warnings"] = zip_result["warnings"]
            for warn in zip_result["warnings"][:10]:
                self._log("WARN", f"[{site.name}] {warn}")
            if len(zip_result["warnings"]) > 10:
                self._log("WARN", f"[{site.name}] 还有 {len(zip_result['warnings']) - 10} 条警告未显示")

        elapsed = round(time.time() - started, 2)

        if zip_result["ok"]:
            detail["zip"] = zip_result["path"]
            self._log("INFO", f"[{site.name}] {zip_result['message']}")
            if notes:
                msg = "；".join(notes) + "（已打包，无数据库文件）"
                status = "部分成功"
            else:
                msg = zip_result["message"]
                status = "成功"
            self._finish_site(site, path_key, status, msg, detail, elapsed,
                              summary, index, total)
        else:
            if zip_result["message"] == "已取消":
                status = "已停止"
            else:
                status = "部分成功" if sql_result and sql_result["ok"] else "失败"
            msg = zip_result["message"]
            if notes:
                msg = "；".join(notes + [msg])
            self._log("ERROR", f"[{site.name}] 打包失败: {zip_result['message']}")
            self._finish_site(site, path_key, status, msg, detail, elapsed,
                              summary, index, total)

    def _finish_site(self, site, path_key, status, message, detail, elapsed,
                     summary, index, total):
        detail = dict(detail)
        detail["elapsed"] = elapsed
        key = {"成功": "ok", "部分成功": "partial", "失败": "fail"}.get(status, "stopped")
        summary[key] += 1
        self._emit("result", path_key, status, message, detail)
        self._emit("overall", index, total)
        self._emit("current", path_key, "")


def _fmt(n) -> str:
    from .util import human_size
    return human_size(n)
