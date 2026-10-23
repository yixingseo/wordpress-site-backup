"""核心逻辑冒烟测试（不依赖真实 MySQL）。

用法: python tests/smoke_test.py
"""
from __future__ import annotations

import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.jobs import BackupRunner, Settings, SiteJob  # noqa: E402
from core.scanner import dir_size, scan_sites  # noqa: E402
from core.wpconfig import load_db_config, parse_defines  # noqa: E402
from core.zipbackup import build_zip  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name} {extra}")
        FAILED.append(name)


def write(path: Path, content: str, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode(encoding))


WP_A = """<?php
// 普通站点，单引号 + 带端口
define('DB_NAME', 'wp_site_a');
define('DB_USER', 'root');
define('DB_PASSWORD', 'p@ss\\'word#1');
define('DB_HOST', '127.0.0.1:3307');
define('DB_CHARSET', 'utf8mb4');
$table_prefix = 'wp_';
"""

WP_B = """<?php
// GBK 编码 + 双引号
define("DB_NAME", "wp_site_b");
define("DB_USER", "dbuser");
define("DB_PASSWORD", "简单的密码");
define("DB_HOST", "localhost");
$table_prefix = 'b_';
"""

WP_C = """<?php
// 没有数据库配置
define('AUTH_KEY', 'abc');
$table_prefix = 'wp_';
"""


def build_fixture(root: Path):
    # site_a
    write(root / "site_a" / "wp-config.php", WP_A)
    write(root / "site_a" / "wp-content" / "themes" / "twentytwenty" / "style.css", "body{}")
    write(root / "site_a" / "wp-content" / "uploads" / "2024" / "a.jpg", "x" * 2048)
    write(root / "site_a" / "wp-content" / "cache" / "page.html", "cached")
    write(root / "site_a" / "wp-admin" / "index.php", "<?php")
    write(root / "site_a" / "wp-admin" / "wp-config.php", "<?php // 伪造，不应被扫描")

    # site_b（GBK）
    write(root / "site_b" / "wp-config.php", WP_B, encoding="gbk")
    write(root / "site_b" / "wp-content" / "plugins" / "p.php", "<?php")

    # site_c（无数据库）
    write(root / "site_c" / "wp-config.php", WP_C)

    # 深层站点 root/deep/sub/site_d
    write(root / "deep" / "sub" / "site_d" / "wp-config.php", WP_A)
    write(root / "deep" / "sub" / "site_d" / "wp-content" / "index.php", "<?php")

    # 噪音：普通目录无 wp-config
    write(root / "noise" / "readme.txt", "nothing")


def test_wpconfig(tmp: Path):
    print("解析 wp-config.php")
    db, note = load_db_config(tmp / "site_a" / "wp-config.php")
    check("单引号站点解析成功", db is not None, note)
    if db:
        check("库名为 wp_site_a", db.name == "wp_site_a", db.name)
        check("密码转义正确", db.password == "p@ss'word#1", repr(db.password))
        check("host:port 拆分", (db.host, db.port) == ("127.0.0.1", 3307), f"{db.host}:{db.port}")
        check("表前缀", db.prefix == "wp_", db.prefix)

    db_b, note_b = load_db_config(tmp / "site_b" / "wp-config.php")
    check("GBK + 双引号解析成功", db_b is not None, note_b)
    if db_b:
        check("GBK 中文密码", db_b.password == "简单的密码", repr(db_b.password))
        check("默认端口 3306", db_b.port == 3306, str(db_b.port))

    db_c, note_c = load_db_config(tmp / "site_c" / "wp-config.php")
    check("无 DB_NAME 返回 None", db_c is None, str(db_c))
    check("无 DB_NAME 有说明", bool(note_c), note_c)

    defines = parse_defines(WP_A)
    check("define 解析数量", len(defines) >= 5, str(len(defines)))


def test_scan(tmp: Path):
    print("扫描站点")
    sites = scan_sites(str(tmp), max_depth=3)
    names = sorted(s.name for s in sites)
    check("扫描到 4 个站点", len(sites) == 4, str(names))
    check("站点名齐全", names == ["deep" if False else "site_a", "site_b", "site_c", "site_d"] or
          names == ["site_a", "site_b", "site_c", "site_d"], str(names))
    check("未把 wp-admin 里的伪 wp-config 当成站点",
          all("wp-admin" not in str(s.path) for s in sites))
    a = next(s for s in sites if s.name == "site_a")
    check("wp-content 探测", a.has_wp_content)
    check("创建时间有效", a.created > 0)

    depth1 = scan_sites(str(tmp), max_depth=1)
    check("深度 1 只扫描一层", len(depth1) == 3, str([s.name for s in depth1]))

    total = dir_size(a.path)
    check("目录体积统计 > 0", total > 2000, str(total))


def test_zip(tmp: Path):
    print("打包 zip")
    site = next(s for s in scan_sites(str(tmp), 3) if s.name == "site_a")
    out = tmp / "backup_out" / "site_a"
    sql_file = out / "site_a_wp_site_a_20260101_010101.sql"
    out.mkdir(parents=True, exist_ok=True)
    sql_file.write_text("-- MySQL dump\n-- Dump completed\n", encoding="utf-8")

    result = build_zip(
        site, out, stamp="20260101_010101", sql_path=sql_file,
        exclude_dirs=["cache"], exclude_globs=["*.tmp"], verify=True,
    )
    check("打包成功", result["ok"], result["message"])
    zip_path = Path(result["path"])
    check("zip 文件存在", zip_path.is_file(), str(zip_path))
    check("zip 命名含日期", zip_path.name == "site_a_20260101_010101.zip", zip_path.name)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        check("顶层目录为站点名", names[0].startswith("site_a/"), names[0])
        check("包含 wp-content", "site_a/wp-content/" in names)
        check("包含 wp-config.php", "site_a/wp-config.php" in names)
        check("sql 与 wp-config 同级",
              "site_a/site_a_wp_site_a_20260101_010101.sql" in names, str(names))
        check("sql 内容正确", "-- Dump completed" in zf.read(
            "site_a/site_a_wp_site_a_20260101_010101.sql").decode())
        check("cache 目录被排除", not any("/cache/" in n for n in names))
        check("uploads 文件保留",
              any(n.endswith("uploads/2024/a.jpg") for n in names), str(names))
        check("wp-admin 未被打包", not any("/wp-admin/" in n for n in names))
        check("CRC 校验通过", zf.testzip() is None)

    # 备份目录恰好位于 wp-content 内时，不能把旧备份再打进新包
    inner = site.path / "wp-content" / "my_backups"
    inner.mkdir(parents=True, exist_ok=True)
    (inner / "old_backup.zip").write_text("old", encoding="utf-8")
    res2 = build_zip(site, inner, stamp="20260102_020202", sql_path=None,
                     exclude_dirs=["cache"], exclude_globs=[])
    check("嵌套备份目录打包成功", res2["ok"], res2["message"])
    with zipfile.ZipFile(res2["path"]) as zf2:
        names2 = zf2.namelist()
    check("备份目录不自我打包", not any("my_backups" in n for n in names2),
          str([n for n in names2 if "my_backups" in n]))


def test_runner(tmp: Path):
    print("批量任务：数据库失败仍打包")
    sites = {s.name: s for s in scan_sites(str(tmp), 3)}
    job = SiteJob(sites["site_b"], "full")
    events = queue.Queue()
    cancel = threading.Event()
    settings = Settings(
        backup_dir=str(tmp / "runner_out"),
        mysqldump_path="",  # 故意不可用
        verify_zip=True,
        exclude_dirs=["cache"],
        exclude_globs=["*.tmp"],
    )
    runner = BackupRunner([job], settings, events, cancel)
    runner.start()
    runner.join(timeout=60)

    collected = []
    while True:
        try:
            collected.append(events.get_nowait())
        except queue.Empty:
            break

    kinds = [e[0] for e in collected]
    check("产生整体进度事件", "overall" in kinds)
    check("产生完成事件", "finished" in kinds)
    results = [e for e in collected if e[0] == "result"]
    check("产生结果事件", len(results) == 1, str(kinds))
    if results:
        status = results[0][2]
        check("数据库失败但打包成功 => 部分成功", status == "部分成功", status)
        detail = results[0][4]
        check("结果含 zip 路径", bool(detail.get("zip")), str(detail))
        check("结果不含 sql", not detail.get("sql"))
        if detail.get("zip"):
            with zipfile.ZipFile(detail["zip"]) as zf:
                check(
                    "zip 内只有一个站点文件夹",
                    all(n.startswith("site_b/") for n in zf.namelist()),
                )
                check("无 sql 时不硬造 sql 文件",
                      not any(n.endswith(".sql") for n in zf.namelist()))


def test_dump_paths(tmp: Path):
    """模拟 mysqldump 的成功/不完整输出，验证后处理逻辑。"""
    print("mysqldump 结果处理（模拟）")
    import core.dbbackup as dbmod

    site = next(s for s in scan_sites(str(tmp), 3) if s.name == "site_a")
    out = tmp / "sql_out"
    original = dbmod._run_dump
    calls = []

    def fake_ok(args, out_path, cancel=None, progress_cb=None):
        calls.append(args)
        Path(out_path).write_bytes(b"-- MySQL dump\n-- Dump completed on 2026-01-01\n")
        if progress_cb:
            progress_cb(64)
        return 0, "", False

    dbmod._run_dump = fake_ok
    try:
        res = dbmod.dump_database(site, out, r"C:\fake\mysqldump.exe",
                                  stamp="20260101_010101")
    finally:
        dbmod._run_dump = original

    check("导出成功", res["ok"], res["message"])
    sql_path = Path(res["path"])
    check("sql 命名含日期",
          sql_path.name == "site_a_wp_site_a_20260101_010101.sql", sql_path.name)
    check("sql 内容完整", "-- Dump completed" in sql_path.read_text(encoding="utf-8"))
    check("无 .part 残留", not list(out.glob("*.part")))
    check("命令行包含库名", any("wp_site_a" in a for a in calls[0]) if calls else False)
    check("密码不出现在命令行",
          all("p@ss" not in a and "password" not in a.lower() for a in calls[0]) if calls else False)

    def fake_incomplete(args, out_path, cancel=None, progress_cb=None):
        Path(out_path).write_bytes(b"-- MySQL dump\n-- cut off")
        return 0, "", False

    dbmod._run_dump = fake_incomplete
    out2 = tmp / "sql_out2"
    try:
        bad = dbmod.dump_database(site, out2, r"C:\fake\mysqldump.exe",
                                  stamp="20260101_010102")
    finally:
        dbmod._run_dump = original
    check("缺少结束标记判为失败", not bad["ok"], bad["message"])
    check("不完整文件已清理", not list(out2.glob("*.sql")))


def test_delete(tmp: Path):
    print("删除已备份源目录")
    from core.deleter import DeleteRunner, can_delete, delete_directory, precheck, verify_backup
    from core.scanner import Site
    from core.wpconfig import DbConfig
    from core.zipbackup import build_zip

    base = tmp / "delcase"
    site_root = base / "site_e"
    write(site_root / "wp-config.php", WP_A)
    write(site_root / "wp-content" / "themes" / "a" / "style.css", "body{}")
    site = Site(path=site_root, name="site_e", wp_config=site_root / "wp-config.php",
                created=time.time(), has_wp_content=True, size_bytes=4096)

    check("没备份时不可删除", can_delete(site)[0] is False)

    backup_dir = base / "backups"
    sql_file = backup_dir / "site_e_wp_site_a_20260103_030303.sql"
    sql_file.parent.mkdir(parents=True, exist_ok=True)
    sql_file.write_text("-- MySQL dump\n-- Dump completed\n", encoding="utf-8")
    zip_res = build_zip(site, backup_dir, stamp="20260103_030303", sql_path=sql_file)
    check("删除场景 zip 已生成", zip_res["ok"], zip_res["message"])
    site.result = {"zip": zip_res["path"], "sql": str(sql_file)}
    site.db = DbConfig(name="wp_site_a", user="root", password="x", host="localhost",
                       port=3306, socket="", prefix="wp_")

    ok, reason = can_delete(site)
    check("备份齐全可删除", ok, reason)
    ok, reason = verify_backup(site)
    check("zip 校验通过", ok, reason)
    check("备份目录在站点内则拒绝",
          precheck(site, str(site_root / "wp-content" / "bk"), str(tmp))[0] is False)
    check("扫描根目录等于站点则拒绝",
          precheck(site, str(backup_dir), str(site_root))[0] is False)

    site.result["sql"] = str(backup_dir / "missing.sql")
    check("sql 文件缺失则拒绝", verify_backup(site)[0] is False, verify_backup(site)[1])
    site.result["sql"] = str(sql_file)

    bad_zip = backup_dir / "broken.zip"
    bad_zip.write_bytes(b"PK\x03\x04broken")
    site.result["zip"] = str(bad_zip)
    check("损坏 zip 拒绝删除", verify_backup(site)[0] is False, verify_backup(site)[1])
    site.result["zip"] = zip_res["path"]

    victim = base / "victim"
    write(victim / "wp-config.php", WP_A)
    write(victim / "wp-content" / "x.txt", "x")
    ok, msg = delete_directory(victim, to_recycle_bin=False)
    check("永久删除成功", ok and not victim.exists(), msg)

    plain = base / "plain"
    plain.mkdir(parents=True, exist_ok=True)
    ok2, msg2 = delete_directory(plain, to_recycle_bin=False)
    check("无 wp-config 目录拒绝删除", not ok2 and plain.exists(), msg2)

    events = queue.Queue()
    runner = DeleteRunner([site], str(backup_dir), str(tmp), False,
                          events, threading.Event())
    runner.start()
    runner.join(30)
    check("DeleteRunner 删除源目录", not site_root.exists())

    drained = []
    while True:
        try:
            drained.append(events.get_nowait())
        except queue.Empty:
            break
    finished = [e for e in drained if e[0] == "del_finished"]
    check("产生 del_finished", len(finished) == 1)
    if finished:
        check("删除统计成功=1", finished[0][1].get("ok") == 1, str(finished[0][1]))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="wpbackup_test_"))
    try:
        build_fixture(tmp)
        test_wpconfig(tmp)
        test_scan(tmp)
        test_zip(tmp)
        test_dump_paths(tmp)
        test_runner(tmp)
        test_delete(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
