# 备份与恢复演练记录

> 目的：证明"**能**恢复"，而不是"应该能恢复"。备份没验证过，等于没有备份。

## 为什么不用复制文件

SQLite 在 WAL 模式下由**三个文件**共同描述状态（`name.sqlite3` / `-wal` / `-shm`）。
直接复制主库文件可能拿到不一致的快照——WAL 里还没合并的事务会丢，或者更糟：拿到半个事务。

演练用的是 SQLite 自己的**在线备份 API**（`Connection.backup`）：不锁库、不中断写入，
拿到的是一致的快照。它也是 `sqlite3 .backup` 背后的同一套东西。

## 演练做三件事

1. **在线备份**到工作目录；
2. **从备份恢复**到另一个目录——刻意不从原库复制：从原库复制证明不了备份可用；
3. **逐表核对**：行数 + 前 500 行的内容 SHA-256，外加 `PRAGMA integrity_check`。

一行命令：

```powershell
.\.venv\Scripts\python scripts/backup_restore_drill.py
```

## 实测记录（2026-09-20）

源库：`data/runs/runs.sqlite3`，**372,736 字节**

| 表 | 行数 |
|---|---:|
| `events` | 63 |
| `model_usage` | 310 |
| `runs` | 3 |
| `stage_outputs` | 30 |
| `stage_timings` | 0（该功能当天上线，本地库里还没有新运行产生的样本） |

结果：

```text
[OK] data\runs\runs.sqlite3  372736 字节

演练通过：1 个库全部备份并可恢复，逐表行数与内容摘要一致。
```

`agent.sqlite3` 与 `llm-cache.sqlite3` 本机尚不存在，脚本会跳过而不是报错——**没跑过
实时模型就没有缓存库**，那是正常状态，不该让演练失败。

## 校验逻辑本身也被测试守着

一条永远报"通过"的校验比没有校验更糟：它让人以为备份可用。所以
`tests/test_backup_drill.py` 不只测"一致时通过"，还测**四种不一致都必须被抓出来**：

- 少一行（"恢复出来是空的"正是最该抓到的失败）
- 行数一样但内容被改（只数行数的校验抓不到这个）
- 表缺失
- 演练确实经由备份文件（而不是从原库复制）

## 人工恢复流程

演练证明的是"备份文件可用"；真出事时的恢复步骤：

```powershell
# 1) 停掉写入方（web 与 worker），避免恢复过程中被写
docker compose stop web worker

# 2) 备份现场（哪怕它已经坏了——先留证再动手）
Copy-Item data/runs/runs.sqlite3 data/runs/runs.sqlite3.broken

# 3) 用演练产物里的备份文件恢复（或把生产备份复制过来）
Copy-Item output/backup-drill/backup/runs.sqlite3 data/runs/runs.sqlite3
Remove-Item data/runs/runs.sqlite3-wal, data/runs/runs.sqlite3-shm -ErrorAction SilentlyContinue

# 4) 校验
.\.venv\Scripts\python scripts/backup_restore_drill.py
docker compose start web worker
```

> 第 3 步删掉 `-wal`/`-shm` 是必要的：它们属于**被替换掉的那个库**，留着会被当成
> 新库的未提交事务。这是恢复流程里最容易漏的一步。

## 还没做的

- **没有定时备份**：演练用的是手动触发的脚本。真实部署应当由 cron 定期跑并把产物送到
  别的地方（本机备份挡不住磁盘故障）。
- **没有异地副本**：`output/` 目录在同一个磁盘上。
- **没有恢复时间目标（RTO）**：目前只能给"恢复要多久"，还没有面向业务的承诺值。
