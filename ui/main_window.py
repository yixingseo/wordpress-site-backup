"""主窗口界面。"""
from __future__ import annotations

import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.config import BASE_DIR, Config
from core.dbbackup import find_mysqldump
from core.deleter import DeleteRunner, can_delete, precheck
from core.jobs import BackupRunner, Settings, SiteJob
from core.logsetup import setup_logging
from core.scanner import dir_size, scan_sites
from core.util import Cancelled, fmt_time, human_size

CHECKED = "☑"
UNCHECKED = "☐"

COLUMNS = [
    ("sel", "选择", 48, "center", False),
    ("name", "网站", 150, "w", False),
    ("path", "目录", 360, "w", True),
    ("created", "创建时间", 120, "center", False),
    ("size", "磁盘占用", 90, "e", False),
    ("db", "数据库", 210, "w", False),
    ("status", "状态", 90, "center", False),
    ("progress", "进度", 170, "w", False),
    ("result", "结果", 320, "w", True),
]

STATUS_TAGS = {
    "成功": "ok",
    "部分成功": "partial",
    "失败": "fail",
    "已停止": "skip",
    "待备份": "",
    "导出DB中": "run",
    "打包中": "run",
    "删除中": "run",
    "已删除": "skip",
    "删除失败": "fail",
}


class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WordPress 站点批量备份工具")
        self.geometry("1320x860")
        self.minsize(1000, 660)

        self.cfg = Config()
        self.log = setup_logging(BASE_DIR)

        self.events: queue.Queue = queue.Queue()
        self.sites: list = []
        self.rows: dict[str, object] = {}
        self.by_path: dict[str, str] = {}
        self.checked: dict[str, bool] = {}

        self.scan_cancel = threading.Event()
        self.size_cancel = threading.Event()
        self.backup_cancel = threading.Event()
        self.delete_cancel = threading.Event()
        self.size_pool = ThreadPoolExecutor(max_workers=4)
        self.runner: BackupRunner | None = None
        self.deleter: DeleteRunner | None = None
        self.mysqldump = ""
        self._busy = False
        self._pb_indeterminate = False
        self.sort_key: str | None = None
        self.sort_desc = False
        self._calc_enabled = True
        self._delete_choice: bool | None = None

        self._build_vars()
        self._build_ui()
        self._load_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._poll_events)
        self.after(500, self._detect_dump)

    # ------------------------------------------------------------------ UI
    def _build_vars(self):
        self.var_root = tk.StringVar()
        self.var_backup = tk.StringVar()
        self.var_dump = tk.StringVar()
        self.var_depth = tk.IntVar(value=3)
        self.var_calc = tk.BooleanVar(value=True)
        self.var_status = tk.StringVar(value="就绪")
        self.var_current = tk.StringVar(value="")
        self.var_count = tk.StringVar(value="已选 0 / 共 0")

    def _build_ui(self):
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # 目录设置
        box = ttk.LabelFrame(outer, text="目录设置", padding=8)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="根目录").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(box, textvariable=self.var_root).grid(row=0, column=1, sticky="ew")
        ttk.Button(box, text="浏览…", width=8, command=self._browse_root).grid(
            row=0, column=2, padx=(6, 0))

        ttk.Label(box, text="备份目录").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(box, textvariable=self.var_backup).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        bf = ttk.Frame(box)
        bf.grid(row=1, column=2, padx=(6, 0), pady=(6, 0), sticky="e")
        ttk.Button(bf, text="浏览…", width=8, command=self._browse_backup).pack(side="left")
        ttk.Button(bf, text="打开", width=6, command=self._open_backup_dir).pack(side="left", padx=(4, 0))

        ttk.Label(box, text="mysqldump").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(box, textvariable=self.var_dump).grid(row=2, column=1, sticky="ew", pady=(6, 0))
        df = ttk.Frame(box)
        df.grid(row=2, column=2, padx=(6, 0), pady=(6, 0), sticky="e")
        ttk.Button(df, text="浏览…", width=8, command=self._browse_dump).pack(side="left")
        ttk.Button(df, text="自动检测", width=10, command=self._detect_dump).pack(side="left", padx=(4, 0))

        # 操作栏
        bar = ttk.Frame(outer)
        bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.btn_scan = ttk.Button(bar, text="扫描网站", command=self._scan)
        self.btn_scan.pack(side="left")
        ttk.Label(bar, text="深度").pack(side="left", padx=(10, 4))
        ttk.Spinbox(bar, from_=1, to=8, width=4, textvariable=self.var_depth).pack(side="left")
        ttk.Checkbutton(bar, text="计算体积", variable=self.var_calc).pack(side="left", padx=(10, 0))

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Button(bar, text="全选", width=6, command=lambda: self._check_all(True)).pack(side="left")
        ttk.Button(bar, text="反选", width=6, command=self._invert_check).pack(side="left", padx=(4, 0))
        ttk.Label(bar, textvariable=self.var_count).pack(side="left", padx=(10, 0))

        self.btn_stop = ttk.Button(bar, text="停止", width=8, command=self._stop_backup, state="disabled")
        self.btn_stop.pack(side="right")
        self.btn_delete = ttk.Button(bar, text="删除已备份源目录", width=17,
                                     command=self._start_delete)
        self.btn_delete.pack(side="right", padx=(6, 0))
        self.btn_db = ttk.Button(bar, text="仅导出 SQL", width=12,
                                 command=lambda: self._start_backup("db"))
        self.btn_db.pack(side="right", padx=(6, 0))
        self.btn_full = ttk.Button(bar, text="开始备份", width=12,
                                   command=lambda: self._start_backup("full"))
        self.btn_full.pack(side="right", padx=(6, 0))

        # 站点表格
        tree_frame = ttk.Frame(outer)
        tree_frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            tree_frame,
            columns=[c[0] for c in COLUMNS],
            show="headings",
            selectmode="extended",
        )
        for key, title, width, anchor, stretch in COLUMNS:
            if key in ("name", "created", "size"):
                self.tree.heading(key, text=title,
                                  command=lambda k=key: self._sort_by(k))
            else:
                self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=stretch, minwidth=40)
        self.tree.grid(row=0, column=0, sticky="nsew")

        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        xsb.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        self.tree.tag_configure("ok", foreground="#137333")
        self.tree.tag_configure("partial", foreground="#b06000")
        self.tree.tag_configure("fail", foreground="#c5221f")
        self.tree.tag_configure("run", foreground="#1a73e8")
        self.tree.tag_configure("skip", foreground="#80868b")

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        # 当前任务
        cur = ttk.LabelFrame(outer, text="当前任务", padding=6)
        cur.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        cur.columnconfigure(0, weight=1)
        ttk.Label(cur, textvariable=self.var_current, anchor="w").grid(row=0, column=0, sticky="ew")
        self.pb_current = ttk.Progressbar(cur, length=280, mode="determinate", maximum=100)
        self.pb_current.grid(row=0, column=1, sticky="e")

        # 日志
        log_box = ttk.LabelFrame(outer, text="日志", padding=4)
        log_box.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, height=8, wrap="none", state="disabled",
                                font=("Consolas", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.tag_configure("ERROR", foreground="#c5221f")
        self.log_text.tag_configure("WARN", foreground="#b06000")

        # 状态栏
        sb = ttk.Frame(outer)
        sb.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        self.pb_all = ttk.Progressbar(sb, length=220, mode="determinate", maximum=100)
        self.pb_all.pack(side="left")
        ttk.Label(sb, textvariable=self.var_status).pack(side="left", padx=(10, 0))

    # --------------------------------------------------------------- config
    def _load_config(self):
        self.var_root.set(self.cfg.get("root_dir", ""))
        self.var_backup.set(self.cfg.get("backup_dir", ""))
        self.var_dump.set(self.cfg.get("mysqldump_path", ""))
        try:
            self.var_depth.set(int(self.cfg.get("depth", 3)))
        except (TypeError, ValueError):
            self.var_depth.set(3)
        self.var_calc.set(bool(self.cfg.get("calc_size", True)))
        self._append_log("INFO", "程序已启动")

    def _save_config(self):
        self.cfg.set("root_dir", self.var_root.get().strip())
        self.cfg.set("backup_dir", self.var_backup.get().strip())
        self.cfg.set("mysqldump_path", self.var_dump.get().strip())
        try:
            self.cfg.set("depth", int(self.var_depth.get()))
        except (TypeError, ValueError):
            self.cfg.set("depth", 3)
        self.cfg.set("calc_size", bool(self.var_calc.get()))
        self.cfg.save()

    # --------------------------------------------------------------- browse
    def _browse_root(self):
        path = filedialog.askdirectory(title="选择包含 WordPress 站点的根目录")
        if path:
            self.var_root.set(os.path.normpath(path))

    def _browse_backup(self):
        path = filedialog.askdirectory(title="选择备份文件存放目录")
        if path:
            self.var_backup.set(os.path.normpath(path))

    def _browse_dump(self):
        path = filedialog.askopenfilename(
            title="选择 mysqldump.exe",
            filetypes=[("mysqldump", "mysqldump.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if path:
            self.var_dump.set(os.path.normpath(path))

    def _open_backup_dir(self):
        path = self.var_backup.get().strip()
        if not path:
            messagebox.showinfo("提示", "请先设置备份目录")
            return
        Path(path).mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    def _detect_dump(self):
        explicit = self.var_dump.get().strip()

        def work():
            found = find_mysqldump(explicit)
            self.events.put(("mysqldump", found or ""))

        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- scan
    def _scan(self):
        if self._busy:
            messagebox.showinfo("提示", "备份进行中，请先停止或等待完成")
            return
        root = self.var_root.get().strip()
        if not root or not Path(root).is_dir():
            messagebox.showwarning("提示", "请选择有效的根目录")
            return

        self.scan_cancel.clear()
        self.size_cancel.clear()
        self.tree.delete(*self.tree.get_children())
        self.sites.clear()
        self.rows.clear()
        self.by_path.clear()
        self.checked.clear()
        self._update_count()
        self.pb_all.configure(value=0)

        if not self.var_backup.get().strip():
            self.var_backup.set(str(Path(root) / "site_backups"))

        self.btn_scan.configure(state="disabled")
        self.var_status.set("扫描中…")
        self._append_log("INFO", f"开始扫描: {root}（深度 {self.var_depth.get()}）")

        threading.Thread(
            target=self._scan_worker,
            args=(root, int(self.var_depth.get()), bool(self.var_calc.get())),
            daemon=True,
        ).start()

    def _scan_worker(self, root, depth, calc):
        try:
            sites = scan_sites(
                root, depth,
                cancel=self.scan_cancel,
                log=lambda m: self.events.put(("log", "WARN", m)),
            )
        except Cancelled:
            self.events.put(("scan_cancelled",))
            return
        except Exception as exc:  # noqa: BLE001
            self.events.put(("scan_error", str(exc)))
            return
        self.events.put(("scan_done", sites, calc))

    def _populate(self, sites, calc):
        self.sites = sites
        self._calc_enabled = bool(calc)
        for site in sites:
            site.status = "待备份"
            site.progress_text = ""
            site.result = {}
        self._render_sites()
        self._update_count()
        self._append_log("INFO", f"扫描完成，共找到 {len(sites)} 个站点")
        self.var_status.set(f"扫描完成：{len(sites)} 个站点")
        self.btn_scan.configure(state="normal")

        if calc:
            for site in sites:
                self._submit_size(site)

    # --------------------------------------------------------------- 排序
    def _sort_by(self, key: str):
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key = key
            self.sort_desc = False
        self._refresh_headings()
        self._render_sites()

    def _refresh_headings(self):
        for key, title, *_ in COLUMNS:
            if key not in ("name", "created", "size"):
                continue
            text = title
            if key == self.sort_key:
                text += " ▼" if self.sort_desc else " ▲"
            self.tree.heading(key, text=text)

    def _sorted_sites(self):
        if not self.sort_key:
            return list(self.sites)
        key = self.sort_key

        def value(site):
            if key == "name":
                return site.name.lower()
            if key == "created":
                return site.created
            if key == "size":
                return site.size_bytes
            return 0

        known = [s for s in self.sites if value(s) is not None]
        unknown = [s for s in self.sites if value(s) is None]
        known.sort(key=value, reverse=self.sort_desc)
        return known + unknown

    def _render_sites(self):
        checked: dict[str, bool] = {}
        for iid, site in self.rows.items():
            checked[str(site.path)] = bool(self.checked.get(iid, True))

        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.by_path.clear()
        self.checked.clear()

        for site in self._sorted_sites():
            path_key = str(site.path)
            is_checked = checked.get(path_key, True)
            if site.size_bytes is not None:
                size_text = human_size(site.size_bytes)
            elif self._calc_enabled:
                size_text = "计算中…"
            else:
                size_text = ""
            message = (site.result or {}).get("message", "")
            iid = self.tree.insert("", "end", values=(
                CHECKED if is_checked else UNCHECKED,
                site.name,
                path_key,
                fmt_time(site.created),
                size_text,
                site.db_text,
                site.status,
                site.progress_text,
                message,
            ))
            self.rows[iid] = site
            self.by_path[path_key] = iid
            self.checked[iid] = is_checked
            self._apply_tag(iid, site.status)

    def _submit_size(self, site):
        def work():
            try:
                size = dir_size(site.path, self.size_cancel)
            except Cancelled:
                return
            except Exception:  # noqa: BLE001
                self.events.put(("size", str(site.path), None))
                return
            self.events.put(("size", str(site.path), size))

        self.size_pool.submit(work)

    # ------------------------------------------------------------- actions
    def _check_all(self, value: bool):
        for iid, site in self.rows.items():
            allowed = value and not getattr(site, "deleted", False)
            self.checked[iid] = allowed
            self.tree.set(iid, "sel", CHECKED if allowed else UNCHECKED)
        self._update_count()

    def _invert_check(self):
        for iid, site in self.rows.items():
            if getattr(site, "deleted", False):
                self.checked[iid] = False
                self.tree.set(iid, "sel", UNCHECKED)
                continue
            self.checked[iid] = not self.checked.get(iid, False)
            self.tree.set(iid, "sel", CHECKED if self.checked[iid] else UNCHECKED)
        self._update_count()

    def _update_count(self):
        total = len(self.rows)
        chosen = sum(1 for iid in self.rows if self.checked.get(iid))
        self.var_count.set(f"已选 {chosen} / 共 {total}")

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        site = self.rows.get(iid)
        if site is not None and getattr(site, "deleted", False):
            return "break"
        value = not self.checked.get(iid, False)
        self.checked[iid] = value
        self.tree.set(iid, "sel", CHECKED if value else UNCHECKED)
        self._update_count()
        return "break"

    def _on_tree_double_click(self, event):
        if self.tree.identify_column(event.x) == "#1":
            return None
        iid = self.tree.identify_row(event.y)
        if iid:
            self._show_detail(iid)
        return None

    # -------------------------------------------------------------- backup
    def _start_backup(self, mode: str):
        if self._busy:
            messagebox.showinfo("提示", "已有备份任务在运行")
            return
        selected = [
            self.rows[iid] for iid in self.rows
            if self.checked.get(iid) and not getattr(self.rows[iid], "deleted", False)
        ]
        if not selected:
            messagebox.showinfo("提示", "请先勾选要备份的站点")
            return

        backup_dir = self.var_backup.get().strip()
        if not backup_dir:
            messagebox.showwarning("提示", "请先设置备份目录")
            return
        try:
            Path(backup_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("备份目录不可用", str(exc))
            return

        dump = self.mysqldump or find_mysqldump(self.var_dump.get().strip())
        if not dump:
            if mode == "db":
                messagebox.showerror("缺少 mysqldump", "未找到 mysqldump，请手动指定 mysqldump.exe")
                return
            if not messagebox.askyesno(
                "缺少 mysqldump",
                "未找到 mysqldump，本次将无法导出数据库，但会继续打包网站。\n是否继续？",
            ):
                return
        else:
            self.mysqldump = dump
            if not self.var_dump.get().strip():
                self.var_dump.set(dump)

        self._save_config()
        self.backup_cancel.clear()

        for site in selected:
            iid = self.by_path.get(str(site.path))
            if iid:
                self.tree.set(iid, "status", "待备份")
                self.tree.set(iid, "progress", "")
                self.tree.set(iid, "result", "")
                self._apply_tag(iid, "待备份")
            site.result = {}
            site.status = "待备份"
            site.progress_text = ""

        settings = Settings(
            backup_dir=backup_dir,
            mysqldump_path=dump or "",
            compresslevel=int(self.cfg.get("compresslevel", 6) or 6),
            verify_zip=bool(self.cfg.get("verify_zip", True)),
            exclude_dirs=list(self.cfg.get("exclude_dirs", []) or []),
            exclude_globs=list(self.cfg.get("exclude_globs", []) or []),
        )
        jobs = [SiteJob(site, mode) for site in selected]

        self._set_busy(True)
        self.pb_all.configure(maximum=max(1, len(jobs)), value=0)
        self._set_current_progress(0, "")
        self.var_status.set(f"开始备份 {len(jobs)} 个站点…")
        self._append_log(
            "INFO",
            f"开始{'完整' if mode == 'full' else '仅SQL'}备份，共 {len(jobs)} 个站点",
        )

        self.runner = BackupRunner(jobs, settings, self.events, self.backup_cancel, self.log)
        self.runner.start()

    def _stop_backup(self):
        stopping = False
        if self.runner and self.runner.is_alive():
            self.backup_cancel.set()
            stopping = True
        if self.deleter and self.deleter.is_alive():
            self.delete_cancel.set()
            stopping = True
        if stopping:
            self.btn_stop.configure(state="disabled")
            self.var_status.set("正在停止…")
            self._append_log("WARN", "用户请求停止，等待当前步骤安全退出")

    # ---------------------------------------------------------- 删除源目录
    def _start_delete(self):
        if self._busy:
            messagebox.showinfo("提示", "已有任务在运行，请先等待完成或停止")
            return

        selected = [self.rows[iid] for iid in self.rows if self.checked.get(iid)]
        if not selected:
            messagebox.showinfo("提示", "请先勾选要删除的站点")
            return

        backup_dir = self.var_backup.get().strip()
        scan_root = self.var_root.get().strip()
        eligible = []
        skipped = []
        for site in selected:
            ok, reason = can_delete(site)
            if ok:
                ok, reason = precheck(site, backup_dir, scan_root)
            if ok:
                eligible.append(site)
            else:
                skipped.append(f"{site.name}：{reason}")

        if not eligible:
            messagebox.showwarning(
                "没有可删除的站点",
                "选中站点都不满足删除条件（需已成功备份并通过校验）：\n\n"
                + "\n".join(skipped[:15]),
            )
            return

        choice = self._confirm_delete(eligible, skipped)
        if choice is None:
            return
        to_recycle = bool(choice)
        if not to_recycle:
            if not messagebox.askyesno(
                "确认永久删除",
                "已选择永久删除，文件不会进入回收站，删除后无法恢复。\n\n确定继续？",
            ):
                return

        self.delete_cancel.clear()
        self.var_status.set(f"正在删除 {len(eligible)} 个站点的源目录…")
        self.pb_all.configure(maximum=max(1, len(eligible)), value=0)
        self._append_log(
            "WARN",
            f"开始删除 {len(eligible)} 个已备份站点的源目录"
            f"（{'回收站' if to_recycle else '永久删除'}）",
        )
        self._set_busy(True)
        self.deleter = DeleteRunner(
            eligible, backup_dir, scan_root, to_recycle,
            self.events, self.delete_cancel, self.log,
        )
        self.deleter.start()

    def _confirm_delete(self, sites, skipped) -> bool | None:
        win = tk.Toplevel(self)
        win.title("确认删除源目录")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        total = sum(s.size_bytes or 0 for s in sites)
        ttk.Label(
            frame,
            text=f"将删除 {len(sites)} 个已备份站点的源目录"
                 + (f"（共 {human_size(total)}）" if total else ""),
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="只有 zip 备份存在且校验通过的站点才会被删除。",
            foreground="#b06000",
        ).pack(anchor="w", pady=(4, 8))

        listing = tk.Text(frame, height=min(10, max(3, len(sites))), width=68,
                          wrap="none", font=("Consolas", 9))
        listing.pack(fill="both", expand=True)
        rows = []
        for site in sites[:20]:
            size = human_size(site.size_bytes) if site.size_bytes else "未知"
            rows.append(f"{site.name}\t{size}\t{site.path}")
        if len(sites) > 20:
            rows.append(f"…… 其余 {len(sites) - 20} 个站点")
        if skipped:
            rows.append("")
            rows.append("以下选中站点将被跳过：")
            rows.extend(skipped[:10])
        listing.insert("1.0", "\n".join(rows))
        listing.configure(state="disabled")

        var_recycle = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame, text="移到回收站（推荐，可恢复）", variable=var_recycle,
        ).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            frame,
            text="取消勾选 = 永久删除，无法恢复。",
            foreground="#80868b",
        ).pack(anchor="w", pady=(2, 10))

        btns = ttk.Frame(frame)
        btns.pack(fill="x")

        def on_ok():
            self._delete_choice = bool(var_recycle.get())
            win.destroy()

        def on_cancel():
            self._delete_choice = None
            win.destroy()

        ttk.Button(btns, text="取消", width=10, command=on_cancel).pack(side="right")
        ttk.Button(btns, text="删除", width=10, command=on_ok).pack(side="right", padx=(0, 6))

        win.protocol("WM_DELETE_WINDOW", on_cancel)
        self._delete_choice = None
        win.update_idletasks()
        win.wait_window()
        return self._delete_choice

    def _on_delete_finished(self, summary):
        self._set_busy(False)
        self.deleter = None
        ok = summary.get("ok", 0)
        fail = summary.get("fail", 0)
        stopped = summary.get("stopped", 0)
        self.var_status.set(f"删除完成：成功 {ok}，失败 {fail}，未执行 {stopped}")
        self._append_log("INFO", self.var_status.get())
        messagebox.showinfo(
            "删除完成",
            f"已删除：{ok}\n失败：{fail}\n未执行/已停止：{stopped}",
        )

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_scan.configure(state=state)
        self.btn_full.configure(state=state)
        self.btn_db.configure(state=state)
        self.btn_delete.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    # -------------------------------------------------------------- events
    def _poll_events(self):
        processed = 0
        while processed < 300:
            try:
                evt = self.events.get_nowait()
            except queue.Empty:
                break
            processed += 1
            try:
                self._handle_event(evt)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("处理事件失败: %s", exc)
        self.after(120, self._poll_events)

    def _handle_event(self, evt):
        kind = evt[0]

        if kind == "log":
            self._append_log(evt[1], evt[2])

        elif kind == "scan_done":
            self._populate(evt[1], evt[2])

        elif kind == "scan_cancelled":
            self.btn_scan.configure(state="normal")
            self.var_status.set("扫描已取消")

        elif kind == "scan_error":
            self.btn_scan.configure(state="normal")
            self._append_log("ERROR", f"扫描失败: {evt[1]}")
            messagebox.showerror("扫描失败", evt[1])

        elif kind == "size":
            self._on_size(evt[1], evt[2])

        elif kind == "mysqldump":
            self.mysqldump = evt[1]
            if evt[1]:
                if not self.var_dump.get().strip():
                    self.var_dump.set(evt[1])
                self._append_log("INFO", f"mysqldump: {evt[1]}")
            else:
                self._append_log("WARN", "未找到 mysqldump，请手动指定路径")

        elif kind == "status":
            iid = self.by_path.get(evt[1])
            if iid:
                self.tree.set(iid, "status", evt[2])
                self._apply_tag(iid, evt[2])
                site = self.rows.get(iid)
                if site is not None:
                    site.status = evt[2]

        elif kind == "progress":
            iid = self.by_path.get(evt[1])
            if iid:
                self.tree.set(iid, "progress", evt[3])
                site = self.rows.get(iid)
                if site is not None:
                    site.progress_text = evt[3] or ""
            self._set_current_progress(evt[2], evt[3])

        elif kind == "current":
            if evt[1] and self.by_path.get(evt[1]):
                site = self.rows[self.by_path[evt[1]]]
                self.var_current.set(f"{site.name}：{evt[2]}" if evt[2] else "")
            elif not evt[1]:
                self.var_current.set("")

        elif kind == "result":
            self._on_result(evt[1], evt[2], evt[3], evt[4])

        elif kind == "overall":
            total = max(1, evt[2])
            self.pb_all.configure(maximum=total, value=evt[1])
            self.var_status.set(f"总进度 {evt[1]}/{evt[2]}")

        elif kind == "finished":
            self._on_finished(evt[1])

        elif kind == "del_status":
            iid = self.by_path.get(evt[1])
            if iid:
                self.tree.set(iid, "status", evt[2])
                self.tree.set(iid, "progress", evt[3])
                self._apply_tag(iid, evt[2])
                site = self.rows.get(iid)
                if site is not None:
                    site.status = evt[2]
                    site.progress_text = evt[3] or ""

        elif kind == "del_result":
            iid = self.by_path.get(evt[1])
            if iid:
                status, message = evt[2], evt[3]
                self.tree.set(iid, "status", status)
                self.tree.set(iid, "progress", "")
                self.tree.set(iid, "result", message)
                self._apply_tag(iid, status)
                site = self.rows.get(iid)
                if site is not None:
                    site.status = status
                    site.progress_text = ""
                    site.deleted = (status == "已删除")
                    if not isinstance(site.result, dict):
                        site.result = {}
                    site.result["message"] = message
                    if site.deleted:
                        self.checked[iid] = False
                        self.tree.set(iid, "sel", UNCHECKED)
                self._update_count()

        elif kind == "del_overall":
            self.pb_all.configure(maximum=max(1, evt[2]), value=evt[1])
            self.var_status.set(f"删除进度 {evt[1]}/{evt[2]}")

        elif kind == "del_finished":
            self._on_delete_finished(evt[1])

    def _on_size(self, path, size):
        iid = self.by_path.get(path)
        if not iid:
            return
        site = self.rows.get(iid)
        if site is not None:
            site.size_bytes = size
        self.tree.set(iid, "size", human_size(size) if size is not None else "未知")

    def _on_result(self, path, status, message, detail):
        iid = self.by_path.get(path)
        if not iid:
            return
        self.tree.set(iid, "status", status)
        self.tree.set(iid, "result", message)
        if status == "成功":
            self.tree.set(iid, "progress", "100%")
        elif status in ("失败", "已停止"):
            self.tree.set(iid, "progress", "")
        self._apply_tag(iid, status)
        site = self.rows.get(iid)
        if site is not None:
            site.result = dict(detail or {})
            site.result["message"] = message
            site.status = status
            try:
                site.progress_text = self.tree.set(iid, "progress") or ""
            except tk.TclError:
                site.progress_text = ""

    def _on_finished(self, summary):
        self._set_busy(False)
        self._set_current_progress(0, "")
        self.var_current.set("")
        try:
            self.pb_current.stop()
        except tk.TclError:
            pass
        self._pb_indeterminate = False
        self.pb_current.configure(mode="determinate", value=0)
        self.var_status.set(
            f"完成：成功 {summary.get('ok', 0)}，部分成功 {summary.get('partial', 0)}，"
            f"失败 {summary.get('fail', 0)}，未执行 {summary.get('stopped', 0)}"
        )
        self._append_log("INFO", self.var_status.get())
        messagebox.showinfo(
            "备份完成",
            f"成功：{summary.get('ok', 0)}\n"
            f"部分成功：{summary.get('partial', 0)}\n"
            f"失败：{summary.get('fail', 0)}\n"
            f"未执行/已停止：{summary.get('stopped', 0)}",
        )

    def _set_current_progress(self, fraction, text):
        if fraction is None:
            if not self._pb_indeterminate:
                self.pb_current.stop()
                self.pb_current.configure(mode="indeterminate")
                self.pb_current.start(18)
                self._pb_indeterminate = True
            return
        if self._pb_indeterminate:
            self.pb_current.stop()
            self.pb_current.configure(mode="determinate")
            self._pb_indeterminate = False
        try:
            self.pb_current.configure(value=float(fraction))
        except (tk.TclError, ValueError):
            pass

    def _apply_tag(self, iid, status):
        tag = STATUS_TAGS.get(status, "")
        self.tree.item(iid, tags=(tag,) if tag else ())

    # ---------------------------------------------------------------- detail
    def _show_detail(self, iid):
        site = self.rows.get(iid)
        if site is None:
            return
        win = tk.Toplevel(self)
        win.title(f"站点详情 - {site.name}")
        win.geometry("820x600")
        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="word", font=("Consolas", 10))
        text.pack(fill="both", expand=True)

        db = site.db
        lines = [
            f"网站名称：{site.name}",
            f"网站目录：{site.path}",
            f"wp-config：{site.wp_config}",
            f"创建时间：{fmt_time(site.created)}",
            f"磁盘占用：{human_size(site.size_bytes) if site.size_bytes is not None else '未知'}",
            f"wp-content：{'存在' if site.has_wp_content else '缺失'}",
            "",
            "数据库配置：",
        ]
        if db:
            lines += [
                f"  库名：{db.name}",
                f"  用户：{db.user}",
                f"  密码：{'已设置' if db.password else '（空）'}",
                f"  主机：{db.host}:{db.port}" + (f"（socket: {db.socket}）" if db.socket else ""),
                f"  表前缀：{db.prefix}",
            ]
        else:
            lines.append(f"  {site.db_note or '未配置'}")

        result = site.result or {}
        lines += ["", f"最近状态：{site.status}"]
        if result.get("sql"):
            lines.append(f"SQL 文件：{result['sql']}")
        if result.get("zip"):
            lines.append(f"zip 文件：{result['zip']}")
        if result.get("elapsed"):
            lines.append(f"耗时：{result['elapsed']}s")
        warnings = result.get("warnings") or []
        if warnings:
            lines.append("")
            lines.append("警告：")
            lines += [f"  - {w}" for w in warnings]

        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")

    # ----------------------------------------------------------------- log
    def _append_log(self, level, message):
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp} [{level}] {message}\n", level if level in ("ERROR", "WARN") else ())
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 1200:
            self.log_text.delete("1.0", "400.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if level == "ERROR":
            self.log.error(message)
        elif level == "WARN":
            self.log.warning(message)
        else:
            self.log.info(message)

    # ---------------------------------------------------------------- close
    def _on_close(self):
        if self._busy:
            if not messagebox.askyesno("退出", "备份仍在进行，确定要退出吗？"):
                return
            self.backup_cancel.set()
            if self.runner:
                self.runner.join(timeout=3)
        self.scan_cancel.set()
        self.size_cancel.set()
        self.delete_cancel.set()
        if self.deleter and self.deleter.is_alive():
            self.deleter.join(timeout=2)
        try:
            self.size_pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self.size_pool.shutdown(wait=False)
        self._save_config()
        self.destroy()
