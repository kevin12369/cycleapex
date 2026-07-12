# 中美金融市场牛熊仪表盘

一个 **纯静态、零后端、自动更新** 的中美金融市场牛熊状态展示站。由 **WorkBuddy 本地自动化**（首选）或 GitHub Actions 定时抓取多源免费公开数据，服务端计算牛熊指标后落盘为 JSON，GitHub Pages 仅负责渲染。密钥存于本机 `.env`（gitignore），不进仓库、不进日志。

## 架构

```
┌─────────────────┐   定时(cron)    ┌──────────────────────┐   提交 JSON   ┌─────────────────┐
│  GitHub Actions │ ───────────────▶ │  fetch_data.py (服务端) │ ────────────▶ │  data/market.json│
│  (Runner)       │                 │  抓取+牛熊判定+容错     │               └─────────────────┘
└─────────────────┘                 └──────────────────────┘                      │
       ▲                                                                           ▼
       │ 网页推送                                                                 渲染
 免密钥/可选密钥                                                                  ┌─────────────────┐
 数据源：                                                                         │  GitHub Pages   │
 • Yahoo Finance (美股/中债指数)      • 东方财富 push2his (A股)                   │  index.html +   │
 • FRED (美国宏观/中债10Y, 需Key)     • feargreedchart.com (恐惧贪婪)            │  assets/app.js  │
                                                                                  │  (ECharts)      │
                                                                                  └─────────────────┘
```

**核心约束**：GitHub Pages 是静态托管，浏览器**无法**直连 Yahoo / 东方财富（CORS / 反爬 / 密钥暴露）。
因此所有抓取与判定都在服务端（Actions Runner）完成，前端只渲染已生成的静态 JSON。这是本方案可行的关键前提。

## 目录结构

```
.
├── index.html              # 仪表盘页面
├── assets/
│   ├── app.js              # ECharts 渲染逻辑（仪表盘/K线/热力图/时间区间切换/对比）
│   └── style.css
├── scripts/
│   ├── fetch_data.py       # 数据抓取 + 牛熊判定（十维度加权，GitHub Actions / 本地自动化调用）
│   └── backtest.py         # 回测一致性校验（滚动判定 + 前瞻收益/命中率/相关性）
├── data/
│   ├── market.json         # 自动生成的静态数据（请勿手动编辑）
│   ├── paradigm.json       # 技术-经济范式周期（Pérez 框架，低频人工复核）
│   ├── status.json         # 运行状态（可用维度数/状态，供自动化告警读取）
│   ├── history/            # 历史快照归档（YYYY-MM-DD.json，供回看/回测基线/失败回滚）
│   ├── backtest.json       # 回测结果（单窗口，运行 backtest.py 后生成）
│   └── backtest_oos.json   # 样本外多窗口回测结果（运行 backtest.py --multi 后生成）
├── .github/workflows/
│   └── update.yml          # 云端替补定时更新工作流（可选）
├── .env.example            # 本地密钥模板（复制为 .env 填入，已被 gitignore）
└── report.html             # 技术可行性分析报告
```

## 本地运行

```bash
pip install requests
python scripts/fetch_data.py          # 生成 data/market.json
python -m http.server 8000            # 预览（必须用 http 服务，不能用 file://）
# 浏览器打开 http://localhost:8000
```

## 部署到 GitHub Pages

1. 将仓库推送至 GitHub（公共仓库免费，且 Actions 分钟数不限）。
2. **仓库 Settings → Secrets and variables → Actions → New repository secret**（可选）：
   - `FRED_KEY`：到 https://fredaccount.stlouisfed.org 免费注册获取，启用**美国宏观（CPI/失业率/GDP）**与**中债10Y**兜底。
   - `TUSHARE_TOKEN`：到 https://tushare.pro 注册获取，作为 A 股数据的额外兜底源。
3. **仓库 Settings → Pages → Build and deployment → Source 选择 "Deploy from a branch"**，
   Branch 选 `main`，目录选 `/ (root)`。
4. 工作流默认**每周一至周五 21:00 UTC** 运行（美股收盘后，覆盖当日 A 股数据）。可到 Actions 页手动 `Run workflow` 立即触发。
5. 每次运行会提交新的 `data/market.json`，GitHub Pages 自动重建并发布。

> 公共仓库若 **60 天无任何活动**，GitHub 会自动禁用定时任务。本工作流每次运行都会提交数据，
> 只要保持运行即可持续保持活跃，无需额外的 keep-alive 提交。

## 本地自动化（WorkBuddy，推荐）

需求要求"定时触发 Agent 脚本"，且密钥应在本地不易泄露。首选用 **WorkBuddy 自动化**在本机定时运行，密钥读取自本地 `.env`，永不离开本机：

1. 复制密钥模板：`cp .env.example .env`，填入 `FRED_KEY` / `TUSHARE_TOKEN`。
2. 本仓库已内置名为 **「牛熊仪表盘-本地数据更新」** 的自动化任务（工作日 21:00 触发），其 prompt 概要：
   - 用受管 Python 运行 `scripts/fetch_data.py` 生成 `data/market.json`；
   - 密钥从本地 `.env` 读取，**绝不打印日志、绝不提交仓库**；
   - 校验通过则提交并（可配置）推送到 GitHub Pages 分支；失败则保留上次成功文件。
3. 也可在 WorkBuddy 中手动「运行一次」立即刷新。

> **为何本地优先**：公共仓库 GitHub Secrets 本身是安全的，但维护者误打印日志 / fork 误配是真实泄露路径；本地 `.env` 从结构上规避了"密钥进远端"这一环节。代价是调度依赖本机 / WorkBuddy 在线——可配置开机自启，或叠加 GitHub Actions 作为云端替补实现 24/7 不中断。

## 牛熊判定算法

对每个价格序列计算三类子分后加权（详见 `fetch_data.py`）；并叠加专业维度，最终以**十维度加权综合**输出结论。

**价格序列技术分** `= 0.5·MA排列 + 0.25·RSI(14) + 0.25·MACD(12,26,9)`（债券维度对收益率取反：收益率上行=债市走熊）。

**专业维度（机构级牛熊补充层）**：

| 维度 | 数据源 | 打分逻辑 |
|------|--------|----------|
| 情绪（恐贪） | feargreedchart | `(FNG-50)/50` |
| 波动率（VIX） | Yahoo `^VIX` | `(20−VIX)/22`（低波动偏牛、高波动偏熊） |
| 信用（HY OAS / 曲线） | FRED `BAMLH0A0HYM2` / `T10Y2Y` | 利差收窄=偏牛；曲线倒挂(负)=偏熊 |
| 估值（CAPE / ERP） | multpl.com | CAPE 越高越贵；ERP=盈利收益率−10Y收益率，越高越具吸引力 |
| 量能（价量配合） | Yahoo / 东财 成交量 | 价涨放量=配合(+)、价跌放量=派发(−)，跨市场聚合 |
| 广度（A股） | 东方财富 全市场涨跌家数 | `(涨−跌)/(涨+跌+平)`，防指数被权重股绑架 |
| 范式相位（结构层） | `data/paradigm.json` | 佩雷斯框架静态标注 + 实时"结构信号"动态校验 |

**综合分（十维度加权，对"实际可用维度"归一化）**：
`0.18·美股 + 0.15·A股 + 0.10·债券 + 0.11·情绪 + 0.07·波动率 + 0.07·信用 + 0.08·估值 + 0.09·宏观 + 0.08·量能 + 0.07·广度`。
未配置/不可达数据源的维度（如未设 `FRED_KEY` 时的信用/宏观/曲线、东方财富代理抖动时的广度）会自动剔除并重新归一化权重，避免缺数据导致分数失真。
阈值：`≥ +0.33` 牛市，`≤ -0.33` 熊市，之间为震荡。

## 可配置权重面板

仪表盘提供**综合权重配置面板**（`assets/app.js` 渲染，页面"综合权重配置"卡片）：拖动十维度滑块即可按归一化权重实时重算综合结论，配置经 `localStorage` 持久化，刷新不丢。权重无需各档之和等于 100（前端会自动归一化）。点"恢复默认权重"即回退到服务端 `present_weights` 默认值。

## 历史快照与失败保护

- **历史快照**：`fetch_data.py` 每次成功运行都会将 `market.json` 归档到 `data/history/YYYY-MM-DD.json`，可用于历史回看、回测基线与失败回滚。
- **失败保护**：当"可用维度 < 2"时（疑似全源失效），脚本以非零退出码结束且**不覆盖**上一次 `market.json`；同时写入 `data/status.json`（`status: ok / 维度计数`）。
- **自动化告警**：WorkBuddy 自动化任务读取 `status.json`，若 `status != "ok"` 或进程退出码非 0，则保留上一快照并向用户告警，不会"开天窗"或写入空数据。

## 回测一致性校验

`scripts/backtest.py` 复用判定纯函数，对标的做"滚动判定"——在每个交易日用当时可得历史算出牛熊分，再考察其后 N 日前瞻收益，输出各结论态命中率与"分↔前瞻收益"相关系数，验证算法是否含真实信息（而非噪声）。

```bash
python scripts/backtest.py                  # 默认 ^GSPC 近3年，前瞻21日
python scripts/backtest.py --horizon 252    # 前瞻约1年
python scripts/backtest.py --symbol 000300.SS --years 5
python scripts/backtest.py --multi          # 样本外多窗口（含 2018/2020/2022 回撤），输出 backtest_oos.json
```

> 实测（标普500 近3年，552 个决策点）：牛态标签下 21 日上涨概率 66.3%、252 日 98.7%，说明状态判别有效；但分数与前瞻收益呈**负相关**（−0.29 / −0.45），反映动量透支后的短期/中期均值回归——故本分数应作"机制标签"使用，而非独立的短周期择时信号。
>
> **样本外校验（`--multi`）**：扩展至 2014–2026（覆盖 2018 抛售、2020 新冠崩盘、2022 熊市），合计 3550 个决策点，牛态命中率仍 66.2%、分↔前瞻收益持续负相关（−0.115）——证明算法在**牛/熊/震荡不同机制下均稳定含信息**，而非多头市特例。熊态标签前瞻收益反而为正（+2.59%），因多聚集在急跌末端后的均值回归反弹。

## 可行性要点

- ✅ **数据源免费且可用**：Yahoo / 东方财富 / feargreedchart / multpl 无需密钥即可运行核心维度；FRED 需免费 Key 解锁信用/宏观/曲线。
- ⚠️ **非官方 API 有稳定性风险**：Yahoo、东方财富均非官方接口，可能存在限流/临时封禁/字段改版。
  脚本已实现**多源容错**（A 股东财失败回退 Yahoo）、**估值代理兜底**（multpl 不可达时改用"价/200MA"）、与**指数退避重试**。
- ✅ **静态托管零成本**：GitHub Pages 对公共仓库免费；JSON 数据量小（约 200–350KB），远低于 1GB / 100GB 月带宽限制。
- ✅ **更新频率适配**：市场数据为日频，每日/每交易日一次完全够用；宏观为月/季频，信用/估值为日频，节奏由自动化任务统一控制。
- ⚠️ **宏观与中债10Y**：FRED 无中国宏观直采，需 Tushare/东方财富深接口；中债10Y 已做 FRED 兜底（需 Key）。估值主源 multpl 在受限网络下自动回退代理值，不影响其余维度。

## 免责声明

本站点所有牛熊结论均为算法的客观输出，**不构成任何投资建议**。市场有风险，决策需谨慎。
