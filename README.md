# 中美金融市场牛熊仪表盘

[![Pages](https://img.shields.io/badge/Pages-live-brightgreen)](https://kevin12369.github.io/cycleapex/)
[![更新频率](https://img.shields.io/badge/%E6%9B%B4%E6%96%B0-%E5%B7%A5%E4%BD%9C%E6%97%A5%2009%3A00%20UTC%2B8-blue)](#数据更新频率与失败表现)
[![维度](https://img.shields.io/badge/%E7%BB%B4%E5%BA%A6-13%20%E7%BB%B4%E8%AE%BE%E8%AE%A1%20%C2%B7%20%E5%B8%B8%E6%80%81%2011%20%E5%8F%AF%E7%94%A8-informational)](#数据更新频率与失败表现)
[![License](https://img.shields.io/badge/License-MIT-yellow)](./LICENSE)

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
│   ├── fetch_data.py       # 数据抓取 + 牛熊判定（十三维度加权，GitHub Actions / 本地自动化调用）
│   └── backtest.py         # 回测一致性校验（滚动判定 + 前瞻收益/命中率/相关性）
├── data/
│   ├── market.json         # 自动生成的静态数据（请勿手动编辑）
│   ├── paradigm.json       # 技术-经济范式周期（Pérez 框架，低频人工复核）
│   ├── status.json         # 运行状态（可用维度数/状态，供自动化告警读取）
│   ├── history/            # 历史快照归档（YYYY-MM-DD.json，供回看/回测基线/失败回滚）
│   ├── backtest.json       # 回测结果（单窗口，运行 backtest.py 后生成）
│   └── backtest_oos.json   # 多窗口分段回测结果（运行 backtest.py --multi 后生成；窗口间有重叠）
├── .github/workflows/
│   ├── update.yml          # 云端替补定时更新工作流（可选）
│   └── pages.yml           # Pages 部署工作流（装配 dist，只发布站点必需文件）
├── requirements.txt        # Python 依赖（锁定版本）
├── LICENSE                 # MIT
├── .env.example            # 本地密钥模板（复制为 .env 填入，已被 gitignore）
└── report.html             # 技术可行性分析报告
```

## 本地运行

```bash
pip install -r requirements.txt
python scripts/fetch_data.py          # 生成 data/market.json
python -m http.server 8000            # 预览（必须用 http 服务，不能用 file://）
# 浏览器打开 http://localhost:8000
```

## 部署到 GitHub Pages

本站使用 **GitHub Actions 部署**（仓库 Pages 配置实测为 `build_type: workflow`），由 `.github/workflows/pages.yml` 负责发布。**不要**在 Settings 里选 "Deploy from a branch"——两种模式混用会造成"分支内容即站点"的误解，还会把 88MB 的 `data/history` 一起发布出去。

新仓库首次部署时按此操作：

1. 将仓库推送至 GitHub（公共仓库免费，且 Actions 分钟数不限）。
2. **Settings → Secrets and variables → Actions → New repository secret**（可选）：
   - `FRED_KEY`：到 https://fredaccount.stlouisfed.org 免费注册获取，启用**美国宏观（CPI/失业率/GDP）**与**中债10Y**兜底。
   - `TUSHARE_TOKEN`：到 https://tushare.pro 注册获取，作为 A 股数据的额外兜底源。
3. **Settings → Pages → Build and deployment → Source 选择 "GitHub Actions"**。
4. 两个工作流的分工：
   - `update.yml`：工作日 21:00 UTC 由 cron 触发抓取与判定，只提交 `data/`；
   - `pages.yml`：任意 push 到 `main` 后触发发布。
5. 可到 Actions 页手动 `Run workflow` 立即触发。

**`pages.yml` 发布的是装配出来的 `dist/`，不是仓库根目录**：只拷贝 `index.html`、`report.html`、`assets/app.js`、`assets/style.css`、`data/market.json`、`data/paradigm.json` 与 `.nojekyll`。因此 `data/history/`（80 余份快照、约 88MB）、`scripts/`、`.github/` 都不会上线——线上访问 `data/history/*.json` 返回 404 属**预期行为**，发布体积从 116MB 降到约 2MB。新增前端文件时，请同步更新 `pages.yml` 的拷贝清单。

> 公共仓库若 **60 天无任何活动**，GitHub 会自动禁用定时任务。`update.yml` 每次运行都会提交数据，
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

对每个价格序列计算三类子分后加权（详见 `fetch_data.py`）；并叠加专业维度，最终以**十三维度加权综合**输出结论。

**价格序列技术分** `= 0.5·MA排列 + 0.25·RSI(14) + 0.25·MACD(12,26,9)`（债券维度对收益率取反：收益率上行=债市走熊）。

**专业维度（机构级牛熊补充层）**：

| 维度 | 数据源 | 打分逻辑 |
|------|--------|----------|
| 情绪（恐贪） | feargreedchart | `(FNG-50)/50` |
| 波动率（VIX） | Yahoo `^VIX`（东财兜底时改用标普近20日已实现波动率代理） | `(20−VIX)/22`（低波动偏牛、高波动偏熊） |
| 信用（HY OAS / 曲线） | FRED `BAMLH0A0HYM2` / `T10Y2Y` | 利差收窄=偏牛；曲线倒挂(负)=偏熊 |
| 估值（CAPE / ERP） | multpl.com | CAPE 越高越贵；ERP=盈利收益率−10Y收益率，越高越具吸引力 |
| 量能（价量配合） | Yahoo / 东财 成交量 | 价涨放量=配合(+)、价跌放量=派发(−)，跨市场聚合 |
| 广度（跨市场） | 东财A股涨跌家数 / Yahoo `RSP`·`SPY` 等权-市值 | A股 `(涨−跌)/(涨+跌+平)` 与美股等权相对动量按 0.5 复合；防指数被权重股绑架 |
| 韩股（KOSPI） | Yahoo `^KS11`（东财兜底） | 价格序列技术分；AI 硬件/半导体周期领先指标 |
| 黄金（避险） | Yahoo `GC=F`（东财兜底） | 按金价自身趋势计综合分；避险/实际利率代理 |
| 日元套息 | Yahoo `JPY=X`（东财兜底） | `clamp(USD/JPY 近20日变动 / 5%)`；JPY 弱=套息顺畅=risk-on，JPY 急升=拆仓=risk-off |

> 注：**范式相位**（`data/paradigm.json`，佩雷斯框架）为**结构层叠加**，不参与加权；仅通过 `structural_signal` 文字输出参与判读。

**综合分（十三维度加权，对"实际可用维度"归一化）**：
`0.15·美股 + 0.13·A股 + 0.06·韩股 + 0.09·债券 + 0.05·黄金 + 0.10·情绪 + 0.06·波动率 + 0.06·信用 + 0.07·估值 + 0.08·宏观 + 0.07·量能 + 0.06·广度 + 0.05·日元套息`（13 项原始权重合计 1.03，运行时对可用维度归一化）。
未配置/不可达数据源的维度（如未设 `FRED_KEY` 时的信用/宏观/曲线、东方财富代理抖动时的广度）会自动剔除并重新归一化权重，避免缺数据导致分数失真。
阈值：`≥ +0.33` 牛市，`≤ -0.33` 熊市，之间为震荡。

## 可配置权重面板

仪表盘提供**综合权重配置面板**（`assets/app.js` 渲染，页面"综合权重配置"卡片）：拖动十三维度滑块即可按归一化权重实时重算综合结论，配置经 `localStorage` 持久化，刷新不丢。权重无需各档之和等于 100（前端会自动归一化）。点"恢复默认权重"即回退到服务端 `present_weights` 默认值。

## 历史快照与失败保护

- **历史快照**：`fetch_data.py` 在成功写出 `market.json` 后，会把它归档一份到 `data/history/YYYY-MM-DD.json`（见脚本「历史快照归档」段），可用于历史回看、回测基线与失败回滚。注意长名日期取 `dt.date.today()`——即**运行环境的本地时区**（GitHub Actions runner 为 UTC，本机运行时为北京时间），两者可能差一天。
- **失败保护**：当"可用维度 < 2"时（疑似全源失效），脚本以非零退出码结束且**不覆盖**上一次 `market.json`；同时写入 `data/status.json`（`status: ok / 维度计数`）。
- **自动化告警**：WorkBuddy 自动化任务读取 `status.json`，若 `status != "ok"` 或进程退出码非 0，则保留上一快照并向用户告警，不会"开天窗"或写入空数据。

## 数据更新频率与失败表现

**频率**：本地自动化按**工作日 09:00（北京时间）**触发；云端替代 `update.yml` 为 `0 21 * * 1-5`（UTC，即北京时间次日 05:00）。市场数据日频、宏观月/季频，一日一次足够。快照归档由脚本内置完成，云端与本机都会产生。

**为什么"周一没有快照、周六却有"**：这不是漏跑，而是**排期延迟 + 按 UTC 日期命名**叠加的结果。

1. 云端 cron 排期在 **21:00 UTC**，但 GitHub 的定时任务常被延迟数小时才实际执行；
2. 归档文件名取 `dt.date.today()`，在 runner 上是 **UTC 日期**；
3. 一旦延迟跨过 UTC 零点，本来属于"周一 21:00"的那次跑批就会落成"**周二**"的文件名——于是排期里的周一在文件名上消失，而周五那次会落到**周六**。

实测印证（80 份快照全部核对）：**文件名与 `generated_at` 的 UTC 日期 80/80 完全一致**；其中 **20 份**的 `generated_at` 落在 `00:00–03:00 UTC` 区间（即被延迟过零点的跑批）。7 月延迟仅约 50 分钟（如 `21:50Z`），文件名与排期同日；9 月底起延迟普遍超过 3 小时，所以 10 月的每个周一都"缺"了。

**实测覆盖（2026-07-12 → 2026-10-31，共 80 份快照）**：

- 6 份落在周末：07-12、07-25、08-29、10-03、10-10、10-31；
- 6 个工作日无快照：08-31、09-28、10-05、10-12、10-19、10-26（**全部是周一**，成因见上）；
- 缺口日没有留下任何降级快照，缺口前后快照的可用维度数也一致（都是 11/13），可佐证是"日期错位"而非"跑批失败"。

**维度覆盖度（最容易被误读的一处）**：`WEIGHTS` 设计了 13 维，但 80 份快照里只有 **4 份是 13/13**，**72 份是 11/13**，另有 3 份 10 维、1 份 9 维（早期版本）。常态缺失的是**信用**与**宏观**，原因是采集端未配置 `FRED_KEY`。缺失维度会被剔除并按可用维度重新归一化，首页顶部因此直接显示「可用维度 11 / 13」。

**一次真实失败（2026-07-15）**：脚本退出码 `0`、`status.json` 写 `ok`，但当天 Yahoo 全线 `403`、东方财富连接中断，**实际只有 3/13 维可用**。"可用维度 < 2 才判失败"的守卫没有触发——**这是守卫阈值偏松**：3 维也照样算一次"成功运行"。当天由人工按住不发布（`git checkout -- data/market.json data/status.json` 还原、删除降级快照）等网络恢复。

> 结论：**`status: ok` 只说明脚本没崩，不等于数据完整**。要判断当天数据可不可用，看 `status.json` 里的 `dims_available`。

## 回测一致性校验

`scripts/backtest.py` 复用判定纯函数，对标的做"滚动判定"——在每个交易日用当时可得历史算出牛熊分，再考察其后 N 日前瞻收益，输出各结论态命中率与"分↔前瞻收益"相关系数，验证算法是否含真实信息（而非噪声）。

```bash
python scripts/backtest.py                  # 默认 ^GSPC 近3年，前瞻21日
python scripts/backtest.py --horizon 252    # 前瞻约1年
python scripts/backtest.py --symbol 000300.SS --years 5
python scripts/backtest.py --multi          # 多窗口分段回测（含 2018/2020/2022 回撤；窗口间有重叠），输出 backtest_oos.json
```

> 实测（标普500 近 3 年、前瞻 **252 日**、552 个决策点）：牛态标签下 252 日上涨概率 **98.7%**，分数与前瞻收益呈**负相关**（**−0.45**，即 `score_forward_corr=−0.448`），反映动量透支后的中期均值回归——故本分数应作"机制标签"使用，而非独立的短周期择时信号。
>
> **多窗口分段回测（`--multi`，前瞻 21 日）**：覆盖 2014–2019 / 2019–2023 / 2021–2026 三段（含 2018 抛售、2020 新冠崩盘、2022 熊市），三段合计 3550 个决策点（**注：后两段约两年重叠，合计数含重复计数、非独立样本**）。牛/熊/震荡三档命中率接近（66.2% / 65.4% / 65.0%），分↔前瞻收益整体轻微负相关（−0.115）；说明本分数能**区分市场状态**，但命中率**未显著优于中性基准**，且熊态标签前瞻收益为正（+2.59%，均值回归）。故宜作机制标签，不宜作独立择时信号。

## 可行性要点

- ✅ **数据源免费且可用**：Yahoo / 东方财富 / feargreedchart / multpl 无需密钥即可运行核心维度；FRED 需免费 Key 解锁信用/宏观/曲线。
- ⚠️ **非官方 API 有稳定性风险**：Yahoo、东方财富均非官方接口，可能存在限流/临时封禁/字段改版。
  脚本已实现**多源容错**（A 股东财失败回退 Yahoo）、**估值代理兜底**（multpl 不可达时改用"价/200MA"）、与**指数退避重试**。
- ✅ **静态托管零成本**：GitHub Pages 对公共仓库免费；`data/market.json` 实测 **1.89 MiB**（2026-10-31，含 11 条序列的全量 K 线与指标数组），仍远低于单站 1GB / 月 100GB 带宽限制。
- ✅ **更新频率适配**：市场数据为日频，每日/每交易日一次完全够用；宏观为月/季频，信用/估值为日频，节奏由自动化任务统一控制。
- ⚠️ **宏观与中债10Y**：FRED 无中国宏观直采，需 Tushare/东方财富深接口；中债10Y 已做 FRED 兜底（需 Key）。估值主源 multpl 在受限网络下自动回退代理值，不影响其余维度。

## 免责声明

本站点所有牛熊结论均为算法的客观输出，**不构成任何投资建议**。市场有风险，决策需谨慎。

## 许可

代码以 [MIT License](./LICENSE) 发布。数据来自各公开数据源（Yahoo Finance、东方财富、FRED、feargreedchart.com、multpl.com），版权归原数据提供方，使用时请遵守其各自条款。
