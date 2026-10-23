# WordPress 站点批量备份工具

一个纯 Python + tkinter 的桌面小工具：扫描某个根目录下的所有 WordPress 站点，批量导出数据库并用 `mysqldump` 生成 sql，再把 `wp-content` + `wp-config.php` + sql 打成一个 zip。

## 环境要求

- Windows + Python 3.9+（已在 Python 3.13 验证）
- `mysqldump`（phpstudy / xampp / wamp / laragon 自带，程序会自动探测）
- 无第三方 Python 依赖（tkinter 为标准库）

## 启动

双击 `run.bat`，或：

```bat
python app.py
```

## 使用步骤

1. **根目录**：选择包含多个 WordPress 站点的文件夹（如 `D:\wwwroot`）。
2. **深度**：默认 3 层，即 `根目录/站点名/wp-config.php`。扫描只找 `wp-config.php`，并跳过 `wp-admin`、`wp-includes`、`wp-content`、`node_modules`、`.git` 等目录，不会遍历所有文件。
3. **备份目录**：选择 zip 与 sql 的存放位置（默认会建议 `根目录/site_backups`）。
4. 点 **扫描网站**，列表会显示：网站名、目录、创建时间、磁盘占用（后台异步统计，可关闭）、数据库、状态、进度、结果。
   点击表头可排序（再点一次反向）：
   - **网站**：按名称 A→Z / Z→A（不区分大小写）
   - **创建时间**：按新→旧 / 旧→新
   - **磁盘占用**：按小→大 / 大→小（体积尚未算出或无法读取的排在最后）
5. 勾选站点（点击「选择」列切换 ☑/☐，或用全选/反选），然后：
   - **开始备份**：导出 sql + 打包 zip
   - **仅导出 SQL**：只导出数据库
6. 每行的「状态 / 进度 / 结果」实时更新；双击任意行可查看详情（数据库配置、输出文件、错误与警告）。
7. **停止**：安全中断当前任务；已生成的文件不会残留半成品。
8. **删除已备份源目录**：勾选站点后点该按钮，可一键删除已完成备份的源目录。默认移到回收站（可恢复），也可在确认框中取消勾选改为永久删除。

### 删除源目录的安全规则

只有同时满足以下条件的站点才会被删除，其余会在确认框中列出原因并跳过：

- 本程序为该站点生成过 zip 备份，且备份文件仍存在、可正常读取
- 备份包内包含 `{站点名}/wp-config.php`
- 若站点配置了数据库，sql 备份文件必须仍然存在
- 备份包小于 400MB 时会重新做一次 CRC 校验（更大的包依据备份时的校验记录）
- 站点目录下确实存在 `wp-config.php`（防止误删普通目录）
- 备份目录**不在**站点目录内部（否则会连备份一起删掉）
- 站点目录不是扫描根目录本身

删除过程中会再次逐项校验；某个站点校验失败只影响该站点，其余继续。删除成功后会释放磁盘空间，行状态变为「已删除」并取消勾选，备份文件保留不动。

## 备份产物

```
{备份目录}/
└─ site-a/
   ├─ site-a_wp_site_a_20261022_143000.sql     ← 数据库导出（含日期）
   └─ site-a_20261022_143000.zip
      └─ site-a/                                ← 顶层文件夹 = WordPress 目录名
         ├─ wp-content/                          ← 整棵 wp-content
         ├─ site-a_wp_site_a_20261022_143000.sql ← 与 wp-config.php 同级
         └─ wp-config.php
```

- sql 与 zip 的文件名都带备份日期（`YYYYmmdd_HHmmss`），同一天反复备份不会互相覆盖。
- **sql 不会写入线上站点的 wp-content**，工具对站点目录只读；sql 落盘在备份目录的站点子文件夹中，并在 zip 内放到与 `wp-config.php` 同级的位置。
- 解压 zip 得到一个可直接使用的站点目录。

## 行为说明

| 场景 | 结果 |
|---|---|
| wp-config.php 没有 DB_NAME | 跳过数据库，仍打包，状态「部分成功」 |
| mysqldump 失败（密码错、服务未启动、库不存在等） | 仍打包网站，状态「部分成功」，错误写入结果与日志 |
| 打包失败（磁盘满、文件全被占用等） | 状态「失败」或「部分成功」（sql 成功时） |
| 单个文件被占用 / 无权限 | 跳过该文件并记录警告，其余照常打包 |
| 打包前磁盘空间不足 | 该站直接失败，不产生半成品 |
| 用户点「停止」 | 中止 mysqldump 进程 / 删除 `.part` 临时包 |
| 删除源目录失败（文件被占用、权限不足） | 该站标记「删除失败」，其余站点继续 |

其他细节：

- mysqldump 参数：`--single-transaction --quick --default-character-set=utf8mb4 --routines --triggers --events --no-tablespaces`。若因权限或版本不认某些参数失败，会自动用简化参数重试一次。
- 数据库密码通过临时 `--defaults-extra-file` 传递，不出现在命令行，用完即删。
- 打包后自动做 CRC 校验（小于 400MB 时全量校验，更大则跳过并注明）。
- 支持长路径（`\\?\` 前缀）与中文文件名（zip 使用 UTF-8 标志）。
- 默认打包排除：`.git`、`.svn`、`node_modules`、`cache`、`upgrade`、`*.tmp`、`*.log`、`Thumbs.db`、`.DS_Store`（可在 `config.json` 调整）。

## 配置

程序目录下的 `config.json`（退出时自动保存）：

| 键 | 说明 |
|---|---|
| `root_dir` / `backup_dir` | 上次使用的目录 |
| `mysqldump_path` | 手动指定的 mysqldump.exe |
| `depth` | 扫描深度 |
| `calc_size` | 是否统计磁盘占用 |
| `compresslevel` | zip 压缩级别 0-9（默认 6） |
| `verify_zip` | 是否做 CRC 校验 |
| `exclude_dirs` / `exclude_globs` | 打包排除规则 |

日志：`logs/app.log`（5MB × 5 轮转）。

## 安全提醒

zip 内包含 `wp-config.php`，其中**数据库账号密码是明文**。请把备份目录放在可信位置，不要随手上传到公开网盘。

## 测试

```bat
python tests\smoke_test.py
```

覆盖：wp-config 解析（单/双引号、GBK、host:port、无 DB_NAME）、扫描深度与目录剪枝、体积统计、zip 结构与排除规则、CRC 校验、数据库失败仍打包的逻辑、删除前的多重校验与删除流程。

## 目录结构

```
site_bakup/
├─ app.py                 入口
├─ run.bat                一键启动
├─ core/
│  ├─ config.py           配置读写
│  ├─ wpconfig.py         wp-config.php 解析
│  ├─ scanner.py          扫描 + 体积统计
│  ├─ dbbackup.py         mysqldump 导出
│  ├─ zipbackup.py        流式打包 + 校验
│  ├─ deleter.py          删除已备份源目录（多重校验）
│  ├─ jobs.py             批量任务队列
│  ├─ logsetup.py         日志
│  └─ util.py             路径/大小/编码工具
├─ ui/main_window.py      主界面
└─ tests/smoke_test.py    冒烟测试
```
