# 本地自动化任务说明（WorkBuddy 平台侧）

本文件用于**追溯**：数据更新任务的真实定义在 WorkBuddy 平台侧（`.workbuddy/` 已被 `.gitignore` 排除，不入库）。仓库里没有可执行的调度配置，删掉本文件不会影响运行，但会让"这个站每天是谁在更新"变成无据可查。

## 任务卡

| 项 | 值 |
|---|---|
| 名称 | 牛熊仪表盘-本地数据更新（上午9点） |
| 任务 id | `automation-1783826526827` |
| 调度 | `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0` → 工作日 **09:00 北京时间** |
| 状态 | ACTIVE |
| 工作目录 | `D:\Coder\CycleApex` |
| 解释器 | `C:/Users/Kevin/.workbuddy/binaries/python/envs/default/Scripts/python.exe`（受管 venv） |

## 任务 prompt（原文摘要，密钥以环境变量名引用，不含明文）

1. 用受管 Python 运行 `scripts/fetch_data.py`，产出 `data/market.json`，并由脚本自身归档 `data/history/YYYY-MM-DD.json`、写入 `data/status.json`。
2. 密钥（`FRED_KEY`、`TUSHARE_TOKEN`）从本地 `.env`（已被 gitignore）或本机环境变量读取，**不打印到日志、不提交仓库**。
3. 失败处理：
   - 脚本在「可用维度 < 2」时以非零退出码结束，且**不覆盖**旧 `market.json`；
   - 若退出码非 0 或 `status.json` 的 `status != "ok"`，则保留上一次成功的 `market.json` 并告警；
   - 成功则做 JSON 合法性校验。
4. 校验通过且内容有实质变化才 `git add data/market.json data/status.json data/history` → `commit` → `push`。
5. 汇报本次综合结论、可用维度数与分值。
6. 若所有数据源均不可达，停止并报告，不生成空文件。

## 双轨并存（这是容易困惑的地方）

站点实际有两条更新链路，都在写同一份 `data/`：

| 链路 | 触发 | 实际表现 |
|---|---|---|
| GitHub Actions `update.yml` | cron `0 21 * * 1-5`（21:00 UTC，即北京次日 05:00） | GitHub 常把定时任务**延迟 3 小时以上**，实际多在 UTC `00:0x–02:xx`（北京 08:xx–10:xx）执行 |
| WorkBuddy 本地自动化 | 工作日 09:00 北京时间 | 云端若已先跑过且数据无实质变化，本地这次**不提交**（符合 prompt 第 4 条） |

所以：**提交历史里看到的 `data:` 提交多来自 `github-actions[bot]`**，这不代表本地任务没跑，而是它判断"无变化、不提交"。核查方法见下节。

## 如何核查它到底有没有在跑

1. **看提交者**：`git log -3 --format='%an %s' -- data/market.json`。
2. **看时间点**：比对 `data/market.json` 的 `generated_at` 与 `gh run list --workflow=update.yml` 的 run 时间。若秒级吻合（如 run `00:16:55Z` vs `generated_at 00:17:18Z`），说明这份数据是云端产出的。
3. **看维度**：`data/status.json` 的 `dims_available`。**常态是 11/13**——本地任务读得到 `.env` 里的 `FRED_KEY` 时会是 13/13；云端没配 secret 时是 11/13（缺信用、宏观）。这也是判断"哪条链路产出的"最直接的信号。

## 失败判定的已知缺口

`fetch_data.py` 只有在「可用维度 < 2」时才非零退出。历史上出现过退出码 0、`status.json` 写 `ok`，但实际只有 **3/13** 维可用的情况（2026-07-15：Yahoo 全线 403 + 东方财富中断）。

→ **`status: ok` 只说明脚本没崩，不等于数据完整。** 判断当天数据能不能用，看 `dims_available`。

## 如何重建这个任务

平台侧新建一个定时任务，工作目录设为项目根，prompt 按上文字段填写，调度设为工作日 09:00。密钥只需放在项目根的 `.env`（参照 `.env.example`），不要写进 prompt。
