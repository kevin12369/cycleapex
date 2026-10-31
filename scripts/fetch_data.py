#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中美金融市场牛熊状态 - 数据抓取与牛熊判定脚本
=================================================
由 GitHub Actions（或本地）定时运行，抓取多源免费公开数据，
计算牛熊指标，输出静态 JSON 供 GitHub Pages 前端渲染。

数据源（全部免密钥）：
  - Yahoo Finance (query1/query2) : 美股/部分指数、美债10Y(^TNX)、VIX、韩股、黄金、日元、广度代理
  - 东方财富 push2his / push2      : A股指数 K 线、全市场涨跌家数、中美国债收益率
  - feargreedchart.com             : 美股恐惧贪婪指数 (CNN 同口径)
  - multpl.com                     : Shiller CAPE / 盈利收益率

注：本项目**不使用任何需要 API Key 的数据源**（原 FRED 信用/宏观维度已移除）。

关键架构约束：
  GitHub Pages 是纯静态托管，浏览器前端无法直连上述 API（CORS / 反爬 / 密钥暴露）。
  因此所有抓取与判定都在「服务端」（GitHub Actions Runner）完成，结果落盘为
  data/market.json，前端仅负责渲染。这也是本方案可行的核心前提。
"""

import json
import math
import os
import re
import sys
import time
import datetime as dt

try:
    import requests
except ImportError:
    sys.exit("缺少 requests 库，请先: pip install requests")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json,text/plain,*/*"})

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUT_FILE = os.path.join(OUT_DIR, "market.json")

# 历史快照保留天数：超过则删除（0 或负数表示不做清理）。
# 每次快照约 1.2MB，不设上限时仓库会无限膨胀（2026-10 已达 59 份/58MB）。
# 更早的快照请另行归档到仓库之外，不要留在 git 里。
def _env_int(name, default):
    """读整型环境变量；缺失或非法值都退回默认，避免一个拼错的值让整轮抓取崩掉。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"  [WARN] 环境变量 {name}={raw!r} 非整数，已改用默认值 {default}")
        return default


HISTORY_KEEP_DAYS = _env_int("HISTORY_KEEP_DAYS", 30)


# ----------------------------------------------------------------------------
# 通用 HTTP 工具：指数退避 + 限流友好
# ----------------------------------------------------------------------------
_SECRET_RE = re.compile(
    r"((?:api_key|apikey|token|key|access_token)=)([^&\s\"']+)", re.IGNORECASE
)


def mask_secrets(text):
    """把 URL 里可能出现的密钥参数值打码。

    本项目已不使用任何密钥数据源，但保留这层防护：日后若接入带 key 的源，
    异常日志里的 `api_key=xxx` 不会直接落到 Actions 日志或自动化输出里。
    """
    if not text:
        return text
    return _SECRET_RE.sub(r"\1***", str(text))


def http_get(url, params=None, headers=None, retries=3, timeout=20):
    last_err = None
    for attempt in range(retries):
        try:
            h = dict(SESSION.headers)
            if headers:
                h.update(headers)
            r = SESSION.get(url, params=params, headers=h, timeout=timeout)
            if r.status_code == 429:
                wait = (2 ** attempt) + 1
                time.sleep(wait)
                last_err = f"429 rate limited, retry {attempt}"
                continue
            r.raise_for_status()
            return r
        except Exception as e:  # noqa
            last_err = str(e)
            time.sleep((2 ** attempt) + 0.5)
    print(f"  [WARN] 请求失败 {mask_secrets(url)}: {mask_secrets(last_err)}")
    return None


# ----------------------------------------------------------------------------
# 指标计算（纯函数，便于单测与复用）
# ----------------------------------------------------------------------------
def sma(values, window, min_valid=None):
    """简单移动平均。

    只对窗口内的**有效值**（非 None）求平均，不再把缺失值当 0 计入。
    min_valid 为窗口内所需的最少有效值个数，默认等于 window（要求完整窗口）；
    有效值不足时返回 None。

    旧实现是 `s += v if v is not None else 0` 且分母固定为 window，
    一旦窗口里混入一个 None，就会产出一个"偏低但看起来正常"的假均线，
    进而让 MA 排列判定虚假转空 —— 静默错，比报错更难发现。
    """
    out = [None] * len(values)
    if window <= 0 or len(values) < window:
        return out
    need = window if min_valid is None else min_valid
    for i in range(window - 1, len(values)):
        chunk = [v for v in values[i - window + 1:i + 1] if v is not None]
        if len(chunk) >= need:
            out[i] = round(sum(chunk) / len(chunk), 4)
    return out


def rsi(values, period=14):
    """RSI(period)。

    先把 None 剔除再在有效序列上计算，结果按原下标回填。
    旧实现直接做 `values[i] - values[i-1]`，遇到 None 会抛 TypeError，
    没有兜底 —— 只要上游漏一个缺失值，整轮抓取就会崩。
    """
    out = [None] * len(values)
    idx = [i for i, v in enumerate(values) if v is not None]
    if len(idx) <= period:
        return out
    valid = [values[i] for i in idx]
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = valid[i] - valid[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    out[idx[period]] = _rsi_val(avg_gain, avg_loss)
    for k in range(period + 1, len(valid)):
        d = valid[k] - valid[k - 1]
        g = d if d >= 0 else 0
        l = -d if d < 0 else 0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[idx[k]] = _rsi_val(avg_gain, avg_loss)
    return out


def _rsi_val(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def macd(values, fast=12, slow=26, signal=9):
    """MACD(12,26,9)。

    注意 DEA 的算法：把 DIF 的 None 剔除后压成连续序列再算 EMA，然后按序回填原下标。
    这是为了保持"每个有效 DIF 对应一个 DEA"，不是严格的滑动窗口对齐；
    由于 _ema 用首个有效值做种子（不预热 skip 期），前 signal 根 DEA 偏敏感，
    属既有近似，不在本次修复范围内，仅在此标注以免被当成精确值引用。
    """
    ema_fast = _ema(values, fast)
    ema_slow = _ema(values, slow)
    dif = [None if (a is None or b is None) else round(a - b, 4)
           for a, b in zip(ema_fast, ema_slow)]
    dea = _ema([x for x in dif if x is not None], signal)
    # 对齐 dea 回原长度
    dea_full = [None] * len(values)
    j = 0
    for i, x in enumerate(dif):
        if x is not None:
            dea_full[i] = dea[j] if j < len(dea) else None
            j += 1
    hist = [None if (a is None or b is None) else round(a - b, 4)
            for a, b in zip(dif, dea_full)]
    return dif, dea_full, hist


def _ema(values, period):
    """指数移动平均。

    None 的处理是"沿用上一个有效 EMA 值"（hold-last），不参与递推、也不中断序列；
    序列开头若为 None 则保持 None，直到出现首个有效值做种子（不做预热 discard）。
    """
    out = [None] * len(values)
    k = 2 / (period + 1)
    prev = None
    started = False
    for i, v in enumerate(values):
        if v is None:
            out[i] = prev
            continue
        if not started:
            # 以首个有效值为种子
            prev = v
            out[i] = round(v, 4)
            started = True
        else:
            prev = v * k + prev * (1 - k)
            out[i] = round(prev, 4)
    return out


def clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def composite_score(close, ma20, ma60, ma200, rsi_arr, macd_hist):
    """综合 MA 排列 / RSI / MACD 得出 [-1,1] 的牛熊分。"""
    n = len(close)
    if n < 2:
        return 0.0
    i = n - 1
    # MA 排列分
    ma_score = 0.0
    c, m20, m60, m200 = close[i], ma20[i], ma60[i], ma200[i]
    if None in (m20, m60, m200) or c is None:
        ma_score = 0.0
    else:
        bull = sum([c > m20, m20 > m60, m60 > m200])
        ma_score = (bull / 3.0) * 2 - 1
    # RSI 分
    r = rsi_arr[i] if rsi_arr[i] is not None else 50
    rsi_score = clamp((r - 50) / 50)
    # MACD 分（用柱子/价格归一）
    h = macd_hist[i] if macd_hist[i] is not None else 0
    ref = c if c else 1
    macd_score = clamp(h / (0.02 * ref))
    return round(0.5 * ma_score + 0.25 * rsi_score + 0.25 * macd_score, 3)


def verdict_from_score(s):
    if s is None:
        return "unknown"
    if s >= 0.33:
        return "bull"
    if s <= -0.33:
        return "bear"
    return "neutral"


# ---- 专业维度评分（机构级牛熊分析补充层） ----
def vix_score(v):
    """VIX 波动率得分：低波动=平静偏牛，高波动=恐慌偏熊。"""
    return round(clamp((20 - v) / 22), 3)


def cape_score(c):
    """CAPE 估值得分：越高越贵，未来回报预期越低（风险叠加，非即时牛熊）。"""
    return round(clamp((25 - c) / 30), 3)


def erp_score(e):
    """股权风险溢价(ERP)得分：相对债券越有吸引力越偏牛。"""
    return round(clamp((e - 2) / 4), 3)


def vol_confirm_score(close, vol):
    """量能确认（价量配合）：价涨且放量→配合(正)；价跌且放量→派发(负)。
    价涨而缩量→动能不足(接近0)；纯量增不表方向，需结合价格方向。返回 [-1,1]。

    不可计算时返回 None（而不是 0.0）。0.0 的语义是"价量配合中性"，
    与"只有 1 根 K 线、根本没算出来"是两回事——混用会让数据不全的序列
    以"中性"身份稀释同维度里数据完整的序列。"""
    n = len(close) - 1
    if n < 1:
        return None
    vma = sma(vol, 20)
    if vma[n] in (None, 0) or vol[n] is None or close[n - 1] in (None, 0):
        return None
    ratio = vol[n] / vma[n]                       # 相对20日均量的倍数
    ret = close[n] - close[n - 1]
    direction = 1 if ret > 0 else (-1 if ret < 0 else 0)
    surge = clamp((ratio - 1) / 0.8)              # 量比 0.2→-1, 1.0→0, 1.8→+1
    return round(direction * surge, 3)


def bond_change_score(closes, window=20, scale_bp=25.0):
    """债券维度：只看「收益率 20 日变化量」，不看「收益率绝对水平」。

    旧口径（build_series invert=True）测的是收益率均线排列/RSI/MACD，
    即"收益率的绝对水平在往哪个方向走"。问题是长端收益率可能连续数月偏高，
    这时它会天天输出负分（实测历史 80 份快照中该维度从未为正，区间 −0.715~−0.001），
    等于把"期限溢价的结构性上移"误读成"今天债市在跌"——一个恒为空的格子不是信号。

    新口径：20 日下行 25bp → +1（债市走牛），上行 25bp → −1。
    单位说明：^TNX 与东财中债 10Y 的数值都是百分数（4.5 表示 4.5%），
    所以 25bp = 0.25 个百分点，scale_bp 按 25.0 传入后内部除以 100。

    不可计算（点数不足）返回 None，不返回 0.0。"""
    c = [x for x in closes if x is not None]
    if len(c) < window + 1:
        return None
    delta = c[-1] - c[-1 - window]        # 百分点，正=收益率上行
    return round(clamp(-delta / (scale_bp / 100.0)), 3)


def apply_bond_score(s):
    """把已构建好的债券序列的 score 换成「收益率变化量」口径。

    K 线、均线、RSI、MACD 全部保留（前端选到"美债10Y"仍能正常看图），
    只是这个序列对维度投票时用的是变化量分，而不是水平方向分。"""
    if s is None:
        return None
    chg = bond_change_score(s.get("close") or [])
    s["score"] = chg
    s["verdict"] = verdict_from_score(chg)
    s["scoreBasis"] = "收益率20日变化"
    return s


def breadth_score(up, down, flat=0):
    """市场广度得分：(涨-跌)/(涨+跌+平) ∈ [-1,1]，>0 普涨、<0 普跌。"""
    tot = up + down + flat
    if tot <= 0:
        return 0.0
    return round(clamp((up - down) / tot), 3)


# ----------------------------------------------------------------------------
# 数据源：Yahoo Finance
# ----------------------------------------------------------------------------
def fetch_yahoo(symbol, rng="1y"):
    """返回 {dates, open, high, low, close} 或 None。"""
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = http_get(url, params={"range": rng, "interval": "1d",
                                  "includePrePost": "false", "events": "div"})
        if not r:
            continue
        try:
            j = r.json()
            res = j["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
            o, h, l, c = q["open"], q["high"], q["low"], q["close"]
            v = q.get("volume", [0] * len(c)) or [0] * len(c)
            dates = [dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).strftime("%Y-%m-%d")
                     for t in ts]
            out = {"dates": dates,
                   "open": [round(x, 3) if x is not None else None for x in o],
                   "high": [round(x, 3) if x is not None else None for x in h],
                   "low": [round(x, 3) if x is not None else None for x in l],
                   "close": [round(x, 3) if x is not None else None for x in c],
                   "vol": [int(x) if x is not None else 0 for x in v]}
            if out["close"]:
                return out
        except Exception as e:  # noqa
            print(f"  [WARN] Yahoo 解析 {symbol} 失败: {e}")
    return None


# ----------------------------------------------------------------------------
# 数据源：东方财富 K 线（A股指数 / 中概）
# ----------------------------------------------------------------------------
def fetch_eastmoney_kline(secid, beg="20240101", lmt=300):
    """东财日线。lmt 为返回的最大根数；长历史回测可显式传大值（如 100000）。"""
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "klt": "101",        # 日线
        "fqt": "1",          # 前复权
        "beg": beg,
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "lmt": str(lmt),
    }
    r = http_get(url, params=params)
    if not r:
        return None
    try:
        j = r.json()
        kl = j["data"]["klines"]
        dates, o, h, l, c, vol = [], [], [], [], [], []
        for row in kl:
            parts = row.split(",")
            # 顺序: 日期,开,收,高,低,量
            dates.append(parts[0])
            o.append(float(parts[1]))
            c.append(float(parts[2]))
            h.append(float(parts[3]))
            l.append(float(parts[4]))
            vol.append(float(parts[5]) if parts[5] not in ("-", "", "None") else 0)
        return {"dates": dates,
                "open": o, "high": h, "low": l, "close": c, "vol": [int(x) for x in vol]}
    except Exception as e:  # noqa
        print(f"  [WARN] 东财 K线解析 {secid} 失败: {e}")
        return None


# 东财多市场 secid 映射：Yahoo 403 时用东财接管美股/海外指数/商品/汇率
EM_SECID = {
    "^GSPC": "100.SPX",    # 标普500
    "^IXIC": "100.NDX",    # 纳斯达克100（近似纳指）
    "^KS11": "100.KS11",   # 韩国KOSPI
    "GC=F": "101.GC00Y",   # COMEX 黄金主力
    "JPY=X": "133.USDJPY", # 美元兑日元
}


def fetch_quote(yahoo_symbol=None, em_secid=None, rng="1y"):
    """多源容错抓取：东财优先（规避 Yahoo 403 IP 封禁），失败回退 Yahoo。
    返回与 fetch_yahoo/fetch_eastmoney_kline 一致的 {dates,open,high,low,close,vol} 或 None。"""
    if em_secid:
        raw = fetch_eastmoney_kline(em_secid)
        if raw and raw.get("close"):
            return raw
    if yahoo_symbol:
        raw = fetch_yahoo(yahoo_symbol, rng=rng)
        if raw:
            return raw
    return None


def fetch_china_10y_yield():
    """东方财富「中美国债收益率」接口（best-effort，可能随页面改版失效）。"""
    url = "https://datacenter.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPTA_WEB_TREASURY_YIELD",
        "columns": "ALL",
        "filter": "(m1=10)",
        "sortColumns": "date",
        "sortTypes": "-1",
        "pageSize": "300",
        "pageNumber": "1",
        "client": "WEB",
        "source": "WEB",
        "token": "894050c76af8597a853f5b408b759f5d",
    }
    r = http_get(url, params=params)
    if not r:
        return None
    try:
        j = r.json()
        data = j["result"]["data"]
        dates, vals = [], []
        # 字段名随版本变动，做一次容错匹配
        for row in data:
            d = row.get("date") or row.get("DATETIME")
            v = (row.get("zgitgzos") or row.get("em013")
                 or row.get("china_10y") or row.get("CN_10Y"))
            if d and v not in (None, "--", ""):
                dates.append(str(d)[:10])
                vals.append(float(v))
        if dates:
            return {"dates": dates, "close": vals}
    except Exception as e:  # noqa
        print(f"  [WARN] 中债10Y解析失败: {e}")
    return None


def fetch_eastmoney_breadth():
    """A股全市场广度（沪深京A股涨跌家数聚合，东方财富 clist 一次性取聚合行）。
    返回 {up, down, flat} 或 None。best-effort，沙箱代理抖动时优雅跳过。"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": "1", "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",  # 沪A/深A/京A 等全部A股
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f104,f105,f106",   # f104=上涨家数 f105=下跌家数 f106=平盘家数
    }
    r = http_get(url, params=params)
    if not r:
        return None
    try:
        data = r.json()["data"]["diff"]
        row = data[0] if isinstance(data, list) else data
        up = int(row.get("f104") or 0)
        down = int(row.get("f105") or 0)
        flat = int(row.get("f106") or 0)
        if up + down + flat <= 0:
            return None
        return {"up": up, "down": down, "flat": flat}
    except Exception as e:  # noqa
        print(f"  [WARN] A股广度解析失败: {e}")
        return None


def fetch_us_breadth():
    """美股广度代理：RSP(等权S&P500) vs SPY(市值权重)。
    等权跑赢市值权重 => 宽度扩散(健康,偏牛)；等权落后 => 宽度收窄(偏熊)。
    返回 {score, ratio_last, series} 或 None。best-effort。"""
    rsp = fetch_yahoo("RSP", rng="6mo")
    spy = fetch_yahoo("SPY", rng="6mo")
    if not rsp or not spy:
        return None
    d_r = {d: c for d, c in zip(rsp["dates"], rsp["close"]) if c}
    d_s = {d: c for d, c in zip(spy["dates"], spy["close"]) if c}
    dates = sorted(set(d_r) & set(d_s))
    if len(dates) < 22:
        return None
    ratio = [d_r[d] / d_s[d] for d in dates]
    base = ratio[0]
    norm = [x / base * 100 for x in ratio]
    # 近 21 交易日(约1月)动量：等权相对市值的相对强弱
    mom = norm[-1] / norm[-22] - 1 if len(norm) >= 22 else norm[-1] / norm[0] - 1
    score = clamp(mom / 0.05)   # ±5% 月度动量 => ±1
    ma20 = sma(norm, 20); ma60 = sma(norm, 60); ma200 = sma(norm, 200)
    rsi_arr = rsi(norm, 14)
    dif, dea, hist = macd(norm)
    series = {
        "name": "美股广度(等权/市值)", "symbol": "RSP/SPY", "dates": dates,
        "open": list(norm), "high": list(norm), "low": list(norm), "close": list(norm),
        "vol": [0] * len(norm),
        "ma20": ma20, "ma60": ma60, "ma200": ma200,
        "rsi": rsi_arr, "macd": dif, "macdSignal": dea, "macdHist": hist,
        "volMa20": [0] * len(norm), "volScore": 0.0,
        "latest": norm[-1], "score": round(score, 3),
        "verdict": verdict_from_score(score),
    }
    return {"score": round(score, 3), "ratio_last": round(ratio[-1], 4), "series": series}


# ----------------------------------------------------------------------------
# 数据源：恐惧贪婪指数（美股，CNN 同口径）
# ----------------------------------------------------------------------------
def fetch_fng():
    r = http_get("https://feargreedchart.com/api/?action=all")
    if not r:
        return None
    try:
        j = r.json()
        score = j["score"]["score"]
        comps = {c["name"]: {"val": c.get("val"), "wt": c.get("wt")}
                 for c in j["score"].get("components", [])}
        return {"value": score, "components": comps}
    except Exception as e:  # noqa
        print(f"  [WARN] FNG 解析失败: {e}")
        return None


# ----------------------------------------------------------------------------
# 数据源：multpl.com（Shiller CAPE / 盈利收益率，免密钥抓取）
# ----------------------------------------------------------------------------
def fetch_multpl(metric_slug):
    """抓取 multpl.com 某指标的最新值（best-effort，HTML 解析）。"""
    import re
    url = f"https://www.multpl.com/{metric_slug}/table/by-month"
    r = http_get(url, headers={"Accept": "text/html"}, timeout=25)
    if not r:
        return None
    try:
        m = re.search(r"<td>([A-Za-z]{3}\s+\d{1,2},\s+\d{4})</td>\s*<td>([\d.]+)</td>", r.text)
        if m:
            d = dt.datetime.strptime(m.group(1), "%b %d, %Y").strftime("%Y-%m-%d")
            return {"date": d, "value": float(m.group(2))}
    except Exception as e:  # noqa
        print(f"  [WARN] multpl {metric_slug} 解析失败: {e}")
    return None


# ----------------------------------------------------------------------------
# 构建单序列对象（含指标与最新分）
# ----------------------------------------------------------------------------
def build_series(name, symbol, raw, invert=False):
    # 清洗空值行（Yahoo 末/首根偶有 null），保证 OHLC 与指标对齐。
    # 东财中债10Y 只给 dates+close，没有 OHLCV，这里用收盘价补齐，
    # 否则 zip(raw["open"], ...) 会直接 KeyError 把整个脚本带崩。
    _c = raw.get("close") or []
    _o = raw.get("open") or _c
    _h = raw.get("high") or _c
    _l = raw.get("low") or _c
    dates, o, h, l, c, vol = [], [], [], [], [], []
    for d, op, hp, lp, cp, vp in zip(raw["dates"], _o, _h, _l, _c, raw.get("vol") or [0] * len(_c)):
        if None in (op, hp, lp, cp):
            continue
        dates.append(d); o.append(op); h.append(hp); l.append(lp); c.append(cp)
        vol.append(vp if vp is not None else 0)
    if not c:
        return None
    close = c
    ma20 = sma(close, 20)
    ma60 = sma(close, 60)
    ma200 = sma(close, 200)
    rsi_arr = rsi(close, 14)
    dif, dea, hist = macd(close)
    i_last = len(close) - 1

    # 可判定性：至少要能算出 MA20 与 RSI(14)（≥20 根 K 线）。
    # 达不到就说明这一条序列只抓到一个快照（多源都挂了、只剩当日收盘），
    # 此时 composite_score 会返回"数据不足的 0.0"，而 0.0 在下游会被读成"中性"。
    # 必须显式标出来，否则同维度里数据完整的序列会被这个假中性稀释。
    usable = (ma20[i_last] is not None and rsi_arr[i_last] is not None)

    score = composite_score(close, ma20, ma60, ma200, rsi_arr, hist) if usable else None
    if score is not None and invert:      # 债券收益率：收益率上行=债市走熊
        score = round(-score, 3)
    vol_score = vol_confirm_score(close, vol)   # 不足 21 根时为 None，不是 0
    return {
        "name": name, "symbol": symbol, "dates": dates,
        "open": o, "high": h, "low": l,
        "close": close, "vol": vol,
        "ma20": ma20, "ma60": ma60, "ma200": ma200,
        "rsi": rsi_arr, "macd": dif, "macdSignal": dea, "macdHist": hist,
        "volMa20": sma(vol, 20),
        "volScore": vol_score,
        "latest": close[-1] if close else None,
        "bars": len(close),
        "usable": usable,
        "score": score,
        "verdict": verdict_from_score(score),
    }


def avg_usable(series):
    """对「可判定」的序列求平均分，返回 (平均分, 被排除的序列名)。

    只有一个可用序列时就等于它自己的分；一个都没有则返回 (None, 全部)。
    这是"按可用项归一化"原则下沉到序列级——算不出来的指数不该投票。"""
    ok = [s for s in series if s.get("usable") and s.get("score") is not None]
    skipped = [s["name"] for s in series if not (s.get("usable") and s.get("score") is not None)]
    if not ok:
        return None, skipped
    return round(sum(s["score"] for s in ok) / len(ok), 3), skipped


def series_indicators_block(s):
    """拆出 MA/RSI/MACD 三项子分，用于热力图。

    算不出来就返回 None。原先用 0 / 50 / 0 兜底，会让热力图上出现一个
    "正好落在中轴"的点——那是缺失值伪装成中性，比留空更容易误读。"""
    if not s.get("usable"):
        # 整条序列都不可判定（K 线不足 20 根），三个子项一起留空。
        # 注意 MACD 对单根序列在数学上会返回 0.0（DIF 与 DEA 同起点），
        # 数值合法但不携带任何信息，所以这里按"不可用"处理。
        return {"ma": None, "rsi": None, "macd": None}
    n = len(s["close"]) - 1
    c = s["close"][n]; m20 = s["ma20"][n]; m60 = s["ma60"][n]; m200 = s["ma200"][n]
    if None in (c, m20, m60, m200):
        ma_s = None
    else:
        bull = sum([c > m20, m20 > m60, m60 > m200])
        ma_s = (bull / 3) * 2 - 1
    r = s["rsi"][n]
    rsi_s = None if r is None else clamp((r - 50) / 50)
    h = s["macdHist"][n]
    macd_s = None if h is None else clamp(h / (0.02 * (c or 1)))

    def _r2(x):
        return None if x is None else round(x, 3)
    return {"ma": _r2(ma_s), "rsi": _r2(rsi_s), "macd": _r2(macd_s)}


def prune_history(hist_dir, keep_days):
    """删除 hist_dir 中早于 keep_days 天的 YYYY-MM-DD.json，返回被删文件名列表。

    只认严格匹配日期的文件名，其它文件一律不动。keep_days <= 0 时直接跳过，
    避免因配置缺失把整个历史目录清空。
    """
    removed = []
    if keep_days <= 0:
        return removed
    cutoff = dt.date.today() - dt.timedelta(days=keep_days)
    for fn in sorted(os.listdir(hist_dir)):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.json", fn)
        if not m:
            continue
        try:
            d = dt.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if d < cutoff:
            try:
                os.remove(os.path.join(hist_dir, fn))
                removed.append(fn)
            except OSError as e:
                print(f"  [WARN] 删除过期快照 {fn} 失败: {e}")
    return removed


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    print("== 抓取市场数据（专业维度扩展版） ==")
    markets = {}
    heatmap = []
    dims = {}   # 维度分 -> 用于加权综合

    def add_dim(key, label, score, verdict, sub, extra=None):
        # 统一闸门：分数算不出来就不产出这个维度。
        # 若放行 None，下游加权会 TypeError；若放行 0.0，会变成"假中性"稀释综合分。
        if score is None:
            print(f"  [SKIP] {label} 分数不可计算（数据不足），该维度整体不出")
            return
        markets[key] = {"label": label, "score": score, "verdict": verdict, "sub": sub}
        if extra:
            markets[key].update(extra)
        dims[key] = score

    def add_heat(group, name, score):
        """热力图子指标。算不出来的不写入，避免在图上落一个"正好在中轴"的假点。"""
        if score is not None:
            heatmap.append([group, name, score])

    # ---------- 美股 ----------
    us_series = []
    for sym, nm in (("^GSPC", "标普500"), ("^IXIC", "纳斯达克")):
        raw = fetch_quote(sym, EM_SECID.get(sym))
        s = build_series(nm, sym, raw) if raw else None
        if s is None:
            print(f"  [SKIP] {nm} 抓取失败或清洗后无有效 K 线")
            continue
        us_series.append(s)
        blk = series_indicators_block(s)
        add_heat("美股", f"{nm}-均线", blk["ma"])
        add_heat("美股", f"{nm}-RSI", blk["rsi"])
        add_heat("美股", f"{nm}-MACD", blk["macd"])
        if s["usable"]:
            print(f"  [OK] {nm} 最新 {s['latest']} 分 {s['score']} {s['verdict']}")
        else:
            print(f"  [WARN] {nm} 仅 {s['bars']} 根 K 线，无法算指标，不参与维度平均")

    if us_series:
        us_score, us_skipped = avg_usable(us_series)
        if us_score is not None:
            us_sub = f"标普/纳指综合 {round(us_score*100)}"
            if us_skipped:
                us_sub += f"（{'、'.join(us_skipped)} 数据不足已排除）"
            add_dim("us_stocks", "美股", us_score, verdict_from_score(us_score),
                    us_sub, {"series": us_series, "skipped": us_skipped})

    # ---------- 美股恐惧贪婪（情绪维度） ----------
    fng = fetch_fng()
    if fng:
        fng_score = clamp((fng["value"] - 50) / 50)
        add_dim("sentiment", "情绪", fng_score, verdict_from_score(fng_score),
                f"恐贪指数 {fng['value']} · {round(fng_score*100)}", {"fng": fng})
        add_heat("情绪", "恐惧贪婪指数", round(fng_score, 3))
        print(f"  [OK] 情绪 恐贪 {fng['value']} 分 {fng_score}")

    # ---------- 美债10Y ----------
    # 分数口径 = 收益率 20 日变化量（见 bond_change_score 的说明）。
    # 序列本身仍完整构建（K 线/均线/RSI 照旧），只是 score 换成变化量口径。
    us10y_raw = fetch_yahoo("^TNX")
    us10y = None
    if us10y_raw:
        us10y = build_series("美债10Y", "^TNX", us10y_raw)
        if us10y:
            us10y = apply_bond_score(us10y)
            print(f"  [OK] 美债10Y 20日变化 分 {us10y['score']}")
        else:
            print("  [WARN] 美债10Y 清洗后无有效 K 线，已跳过")

    # ---------- A股（东方财富优先，失败回退 Yahoo；含科技成长指数） ----------
    cn_series = []
    for secid, yh, nm in (("1.000001", "000001.SS", "上证指数"),
                         ("1.000300", "000300.SS", "沪深300"),
                         ("1.000688", "000688.SS", "科创50"),
                         ("0.399006", "399006.SZ", "创业板指")):
        raw = fetch_eastmoney_kline(secid)
        if raw is None:
            raw = fetch_yahoo(yh)   # 多源容错：东财不可达时回退
            if raw:
                print(f"  [FALLBACK] {nm} 改用 Yahoo {yh}")
        s = build_series(nm, secid, raw) if raw else None
        if s is None:
            print(f"  [SKIP] {nm} 抓取失败或清洗后无有效 K 线")
            continue
        cn_series.append(s)
        blk = series_indicators_block(s)
        add_heat("A股", f"{nm}-均线", blk["ma"])
        add_heat("A股", f"{nm}-RSI", blk["rsi"])
        add_heat("A股", f"{nm}-MACD", blk["macd"])
        if s["usable"]:
            print(f"  [OK] {nm} 最新 {s['latest']} 分 {s['score']} {s['verdict']}")
        else:
            print(f"  [WARN] {nm} 仅 {s['bars']} 根 K 线，无法算指标，不参与维度平均")
    if cn_series:
        cn_score, cn_skipped = avg_usable(cn_series)
        if cn_score is None:
            print("  [SKIP] A股 全部指数 K 线不足 20 根，无法判定，维度整体不出")
        else:
            cn_sub = "上证/沪深300/科创50/创业板指综合"
            if cn_skipped:
                cn_sub += f"（{'、'.join(cn_skipped)} 数据不足已排除）"
            add_dim("cn_stocks", "A股", cn_score, verdict_from_score(cn_score),
                    cn_sub, {"series": cn_series, "skipped": cn_skipped})
            if cn_skipped:
                print(f"  [WARN] A股 排除 {len(cn_skipped)} 条数据不足的指数："
                      f"{'、'.join(cn_skipped)}（仅用剩余序列平均，不再按 0 分计入）")

    # ---------- 量能（已降级为参考，不产出维度） ----------
    # 原 volume 维度：把美股+A股所有序列的 volScore 等权平均。
    # 实测 80 份快照 σ=0.180、86 天翻转 42 次、与综合分 r=−0.12 —— 是噪声不是信号。
    # 量价配合在指数层面本来就不稳（ETF 申赎、衍生品对冲都会扭曲成交量），
    # 与其留一个每天乱跳的 7% 权重，不如把 volScore 保留在序列里供 K 线图看。
    all_vol = [s["volScore"] for s in (us_series + cn_series) if s.get("volScore") is not None]
    if all_vol:
        print(f"  [--] 量价配合参考值 {round(sum(all_vol) / len(all_vol), 3)}（不参与综合分）")

    # ---------- 广度维度（跨市场：A股涨跌家数 + 美股等权-市值差） ----------
    ab = fetch_eastmoney_breadth()
    ub = fetch_us_breadth()
    br_a = breadth_score(ab["up"], ab["down"], ab["flat"]) if ab else None
    br_us = ub["score"] if ub else None
    if ab:
        print(f"  [OK] A股广度 涨{ab['up']}/跌{ab['down']}/平{ab['flat']} 分 {br_a}")
    else:
        print("  [WARN] A股广度抓取失败（东方财富可达性）")
    if br_us is not None:
        print(f"  [OK] 美股广度(等权-市值) 分 {br_us} 比率 {ub['ratio_last']}")
    else:
        print("  [WARN] 美股广度抓取失败（Yahoo RSP/SPY）")

    if br_a is not None or br_us is not None:
        # 跨市场等权复合：A股单日A/D + 美股1月动量（等权相对市值）
        bs = (round(0.5 * br_a + 0.5 * br_us, 3)
              if (br_a is not None and br_us is not None) else (br_a if br_us is None else br_us))
        sub = []
        if br_a is not None:
            sub.append(f"A股涨{ab['up']}/跌{ab['down']}")
        if br_us is not None:
            sub.append(f"美股等权-市值 {br_us}")
        extra = {"up": ab["up"] if ab else None, "down": ab["down"] if ab else None,
                 "flat": ab["flat"] if ab else None,
                 "us_score": br_us, "us_ratio": (ub["ratio_last"] if ub else None)}
        if ub and ub.get("series"):
            extra["series"] = [ub["series"]]
        add_dim("breadth", "广度", bs, verdict_from_score(bs), "；".join(sub), extra)
        if br_a is not None:
            add_heat("广度", "A股涨跌家数", round(br_a, 3))
        if br_us is not None:
            add_heat("广度", "美股等权-市值", round(br_us, 3))
        print(f"  [OK] 广度综合 分 {bs}")
    else:
        print("  [SKIP] 广度抓取失败（A股+美股均不可达）")

    # ---------- 债券维度（美债10Y + 中债10Y，口径=收益率20日变化量） ----------
    bond_series = []
    if us10y:
        bond_series.append(us10y)
    cn10y_raw = fetch_china_10y_yield()
    if cn10y_raw:
        cn10y = build_series("中债10Y", "CN_10Y", cn10y_raw)
        if cn10y:
            cn10y = apply_bond_score(cn10y)
            bond_series.append(cn10y)
            print(f"  [OK] 中债10Y 20日变化 分 {cn10y['score']}")
        else:
            print("  [WARN] 中债10Y 清洗后无有效 K 线，已跳过")
    if bond_series:
        bond_score, bond_skipped = avg_usable(bond_series)
        if bond_skipped:
            print(f"  [WARN] 债券维度排除数据不足序列：{'、'.join(bond_skipped)}")
        add_dim("bonds", "债券", bond_score, verdict_from_score(bond_score),
                "中美10Y收益率20日变化(下行=债牛)", {"series": bond_series, "skipped": bond_skipped})
        add_heat("债券", "美债10Y-20日变化", us10y["score"] if us10y else None)
        if cn10y_raw:
            add_heat("债券", "中债10Y-20日变化", bond_series[-1]["score"])

    # ---------- 风险温度计（VIX 优先；Yahoo 403 时用标普已实现波动率代理） ----------
    # 注意：这里【不再】产出 volatility 维度。
    # 理由见 WEIGHTS 注释：VIX 与美股 r=+0.82、与情绪 r=+0.82，作为投票项纯属重复计票。
    # 但它作为「风险温度」仍然有用（估值泡沫判断、structural_signal 都要它），
    # 所以保留抓取、保留数值与状态，只是不参与综合分。
    vix_raw = fetch_yahoo("^VIX")
    vix_val = None
    vix_src = "VIX"
    if vix_raw:
        cl = [x for x in vix_raw["close"] if x is not None]
        if cl:
            vix_val = cl[-1]
    if vix_val is None:
        # 回退：标普500 近20日对数收益的已实现波动率(年化%)近似 VIX
        spx_raw = fetch_quote("^GSPC", EM_SECID.get("^GSPC"))
        if spx_raw:
            cl = [x for x in spx_raw["close"] if x is not None]
            if len(cl) >= 21:
                rets = [math.log(cl[i] / cl[i - 1]) for i in range(len(cl) - 20, len(cl))]
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
                vix_val = (var ** 0.5) * (252 ** 0.5) * 100
                vix_src = "标普20日已实现波动率代理"
    risk_gauge = None
    if vix_val is not None:
        vix_regime = ("极端恐慌" if vix_val > 30 else "恐慌" if vix_val > 25 else
                      "谨慎" if vix_val > 20 else "正常" if vix_val > 15 else "平静")
        risk_gauge = {"vix": round(vix_val, 2), "regime": vix_regime, "source": vix_src}
        print(f"  [OK] 风险温度 {vix_src} {vix_val:.1f}（{vix_regime}）· 不参与综合分")
    else:
        print("  [SKIP] VIX 抓取失败（风险温度计不可用）")

    # ---------- 估值维度（CAPE + ERP，multpl 免密钥；失败回退价格代理） ----------
    cape_d = fetch_multpl("shiller-pe")
    ey_d = fetch_multpl("s-p-500-earnings-yield")
    val_parts = []
    cape = cape_d["value"] if cape_d else None
    erp = None
    val_method = "CAPE"
    if cape_d or ey_d:
        us10y_val = us10y["latest"] if us10y else None
        ey = ey_d["value"] if ey_d else None
        if cape:
            cs = cape_score(cape)
            val_parts.append(cs)
            add_heat("估值", "CAPE", round(cs, 3))
            print(f"  [OK] 估值 CAPE {cape:.1f} 分 {cs}")
        if ey and us10y_val:
            erp = ey - us10y_val   # 股权风险溢价 = 盈利收益率 - 10Y国债收益率
            cs = erp_score(erp)
            val_parts.append(cs)
            add_heat("估值", "股权风险溢价", round(cs, 3))
            print(f"  [OK] 估值 ERP {erp:.2f}% 分 {cs}")
    else:
        # 兜底：标普500 价格 / 200日均线 水平代理（价格远高于长期均值=偏贵）
        spx = next((s for s in us_series if s["symbol"] == "^GSPC"), None) if us_series else None
        if spx and spx["ma200"][-1]:
            ratio = spx["close"][-1] / spx["ma200"][-1]
            cs = round(clamp(-(ratio - 1) / 0.25), 3)   # 高于均值25%→-1，低于→+1
            val_parts.append(cs)
            val_method = "价格/200MA代理"
            add_heat("估值", "估值代理", round(cs, 3))
            print(f"  [OK] 估值代理 价/200MA={ratio:.2f} 分 {cs}（multpl 不可达）")
        else:
            print("  [SKIP] 估值抓取失败（multpl 不可达且无标普序列）")

    if val_parts:
        val_score = round(sum(val_parts) / len(val_parts), 3)
        tag = "偏贵" if val_score < -0.2 else ("中性偏高" if val_score < 0 else "中性")
        add_dim("valuation", "估值", val_score, verdict_from_score(val_score),
                f"{val_method} · {tag}",
                {"cape": cape, "erp": erp, "method": val_method})

    # ---------- 韩国 KOSPI（全球风险 / 半导体周期领先指标，Yahoo 免密钥） ----------
    ks_raw = fetch_quote("^KS11", EM_SECID.get("^KS11"))
    if ks_raw:
        ks = build_series("韩国KOSPI", "^KS11", ks_raw)
        if ks is None:
            print("  [WARN] KOSPI 清洗后无有效 K 线，已跳过")
        else:
            blk = series_indicators_block(ks)
            add_heat("韩股", "KOSPI-均线", blk["ma"])
            add_heat("韩股", "KOSPI-RSI", blk["rsi"])
            add_heat("韩股", "KOSPI-MACD", blk["macd"])
            kr_score = ks["score"]
            if kr_score is None:
                print(f"  [WARN] KOSPI 仅 {ks['bars']} 根 K 线，无法判定，维度不出")
            else:
                add_dim("kr_stocks", "韩股", kr_score, verdict_from_score(kr_score),
                        f"KOSPI 最新 {ks['latest']}", {"series": [ks]})
                print(f"  [OK] 韩股 KOSPI 最新 {ks['latest']} 分 {kr_score}")
    else:
        print("  [SKIP] KOSPI 抓取失败")

    # ---------- 黄金（避险/实际利率代理；按自身价格趋势计综合分，徽章即金价真实方向） ----------
    gold_raw = fetch_quote("GC=F", EM_SECID.get("GC=F"))
    if gold_raw:
        g = build_series("黄金", "GC=F", gold_raw)
        if g is None:
            print("  [WARN] 黄金清洗后无有效 K 线，已跳过")
        else:
            blk = series_indicators_block(g)
            add_heat("黄金", "金价-均线", blk["ma"])
            add_heat("黄金", "金价-RSI", blk["rsi"])
            add_heat("黄金", "金价-MACD", blk["macd"])
            gold_score = g["score"]
            if gold_score is None:
                print(f"  [WARN] 黄金仅 {g['bars']} 根 K 线，无法判定，维度不出")
            else:
                add_dim("gold", "黄金(避险)", gold_score, verdict_from_score(gold_score),
                        f"COMEX黄金 最新 {g['latest']}（避险/实际利率代理）", {"series": [g]})
                print(f"  [OK] 黄金 最新 {g['latest']} 分 {gold_score}")
    else:
        print("  [SKIP] 黄金 抓取失败")

    # ---------- 日元套息交易（USD/JPY 动量 → 套息健康度） ----------
    jpy_raw = fetch_quote("JPY=X", EM_SECID.get("JPY=X"))
    if jpy_raw:
        cj = [x for x in jpy_raw["close"] if x is not None]
        fx_s = build_series("美元兑日元", "JPY=X", jpy_raw)
        fx_series = [fx_s] if fx_s else []
        # 必须满 22 个点才能算「20 日」动量。不足时原先会退到 cj[-1]/cj[0]-1
        # （2 个点 = 1 天涨跌）或直接给 0（假中性），两者都是把"没算出来"包装成结论。
        if len(cj) >= 22:
            mom = cj[-1] / cj[-21] - 1
            carry_score = round(clamp(mom / 0.05), 3)
            regime = ("套息顺畅·risk-on" if mom > 0.01 else
                      "套息承压·risk-off" if mom < -0.01 else "中性")
            add_dim("yen_carry", "日元/套息", carry_score, verdict_from_score(carry_score),
                    f"USD/JPY 20日变动 {mom*100:+.1f}% · {regime}",
                    {"usdjpy": round(cj[-1], 2), "mom20": round(mom, 4)})
            add_heat("套息", "日元套息健康度", carry_score)
            print(f"  [OK] 日元套息 USD/JPY {cj[-1]:.2f} 20日{mom*100:+.1f}% 分 {carry_score} ({regime})")
        else:
            print(f"  [WARN] USD/JPY 仅 {len(cj)} 个有效点，不足 22 算不出 20 日动量，套息维度不出")
        if fx_series:
            markets.setdefault("fx", {"label": "外汇", "series": fx_series})
    else:
        print("  [SKIP] USD/JPY 抓取失败")

    # ---------- 加权综合（按可用维度归一化） ----------
    # 9 个维度，合计 1.00。
    #
    # 演进史（勿删，避免下次又凭直觉把噪声维度加回来）：
    #   · credit(0.06) / macro(0.08)：随 FRED 依赖一并移除（本项目不使用任何密钥数据源）。
    #   · volume(0.07)：80 份快照实测 σ=0.180、86 天里翻转 42 次、与综合分 r=−0.12。
    #     "量价配合"在 A股/美股两个市场用同一套口径聚合，实际测到的是噪声而非趋势确认。
    #   · volatility(0.06)：σ=0.068（全部维度最低），与美股 r=+0.82、与情绪 r=+0.82。
    #     它几乎完全复述美股维度，却占 6% 权重把综合分往中间拉。
    #     VIX 本身有价值，但价值在"风险温度"而非"投票"，故改为不投票的 risk_gauge 展示。
    #
    # 删掉这两项后 leave-one-out 复核：σ 0.072 → 0.084（+17%），振幅不再被均值回归压平。
    # 剩余 9 项按原相对比例放大到 1.00（不改变彼此的相对话语权）。
    WEIGHTS = {"us_stocks": 0.19, "cn_stocks": 0.17, "kr_stocks": 0.08,
               "bonds": 0.12, "gold": 0.07, "sentiment": 0.13,
               "valuation": 0.09, "breadth": 0.08, "yen_carry": 0.07}
    present = {k: v for k, v in dims.items() if k in WEIGHTS}
    wsum = sum(WEIGHTS[k] for k in present)
    overall = round(sum(dims[k] * WEIGHTS[k] for k in present) / wsum, 3) if wsum else 0.0

    # ---------- 共识计数（与加权平均并列，两条腿分开走） ----------
    # 加权平均的毛病：维度高度共线时（情绪↔估值 r=−0.89）正负互相抵消，
    # 结果永远贴着 0，阈值 ±0.33 几乎打不到，Hero 大字恒为"震荡"。
    # 共识计数不问"平均多偏"，只问"几个维度偏多、几个偏空"，
    # 不会被一两个极值维度吃掉，也不会被两两抵消抹平。
    # 两者分歧本身也是信息：平均偏多但票数不够 = 靠少数维度撑起来的牛市。
    bull_n = sum(1 for k in present if dims[k] >= 0.15)
    bear_n = sum(1 for k in present if dims[k] <= -0.15)
    n_dim = len(present)
    neutral_n = n_dim - bull_n - bear_n
    net_votes = bull_n - bear_n
    vote_ratio = round(net_votes / n_dim, 3) if n_dim else 0.0
    # 用「净票数 ≥ 可用维度 1/3」而非「比例 ≥ 0.34」判定。
    # 9 个维度时 net=3 的比例恰好是 0.3333…，若阈值写 0.34 就会被浮点四舍五入
    # 卡在门外（实测 2026-10-20、10-31 两天都是 1多/4中/4空，本该判空头共识却判成无共识）。
    # 直接比票数，规则可读、也没有边界歧义。最少 2 票，避免只剩 3 个维度时 1 票定生死。
    need = max(2, math.ceil(n_dim / 3)) if n_dim else 2
    consensus_verdict = ("bull" if net_votes >= need else
                         "bear" if net_votes <= -need else "neutral")
    consensus = {"bull": bull_n, "bear": bear_n, "neutral": neutral_n,
                 "total": n_dim, "net": net_votes, "need": need,
                 "ratio": vote_ratio, "verdict": consensus_verdict}

    # ---------- 结构信号（Pérez 范式叠加 + 广度校验） ----------
    val_mk = markets.get("valuation")
    cape_v = val_mk.get("cape") if val_mk else None
    vix_v = risk_gauge["vix"] if risk_gauge else None      # 风险温度计，来自上面的 VIX 抓取
    val_score = val_mk.get("score") if val_mk else None
    br_mk = markets.get("breadth")
    br_score = br_mk.get("score") if br_mk else None

    if val_score is not None:
        if (cape_v and cape_v > 35 or val_score < -0.4) and (vix_v is None or vix_v < 18):
            structural = ("估值极端 + 波动率低迷：呈现「金融资本主导·泡沫累积」特征，"
                          "符合导入期狂热(Frenzy)尾声，需警惕向转折点(Turning Point)过渡")
        elif (cape_v and cape_v > 30) or val_score < -0.2:
            structural = "估值偏高：处于导入期中后段，金融资本活跃，尚待进入协同(Synergy)展开期"
        else:
            structural = "估值中性：未显现明显泡沫特征"
    else:
        structural = "估值数据不足，结构信号以广度校验为准"

    if br_score is not None and br_score < -0.3:
        structural += "；广度显著转弱（普跌），与指数背离，警惕量价动能衰减。"
    elif br_score is not None and br_score > 0.3:
        structural += "；广度普涨配合，宽度健康。"

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "us_indices": "Yahoo Finance",
            "cn_indices": "东方财富 push2his / Yahoo 回退",
            "us_10y": "Yahoo Finance ^TNX",
            "cn_10y": "东方财富 datacenter (best-effort)",
            "vix": "Yahoo Finance ^VIX",
            "kr_indices": "Yahoo Finance ^KS11",
            "gold": "Yahoo Finance GC=F",
            "fx": "Yahoo Finance JPY=X",
            "fng": "feargreedchart.com",
            "valuation": "multpl.com (Shiller CAPE / 盈利收益率)",
            "breadth": "东方财富 push2 全市场涨跌家数 + Yahoo RSP/SPY 等权-市值差 (best-effort)",
        },
        "markets": markets,
        "overall": {"score": overall, "verdict": verdict_from_score(overall)},
        # 两条腿并列：加权平均看"平均多偏"，共识计数看"几个维度偏"。
        # 前端把两者都渲染出来，分歧本身即信息。
        "consensus": consensus,
        # 风险温度计（不投票）：VIX 数值与状态，供估值泡沫判断与页头展示
        "risk_gauge": risk_gauge,
        "heatmap": heatmap,
        "professional": {
            "weights": WEIGHTS,
            "present_weights": {k: WEIGHTS[k] for k in present},
            "dimensions": {k: round(dims[k], 3) for k in present},
            "method_notes": [
                "序列技术分 = 0.5·MA排列(MA20/60/200) + 0.25·RSI(14) + 0.25·MACD(12,26,9)",
                "债券维度改测「收益率 20 日变化量」而非「收益率绝对水平」：20日下行25bp→+1，上行→−1。原口径测的是水平方向，长端长期偏高时会给出一条恒为负的序列，等于把期限溢价的结构性上移误读成每日的债市下跌",
                "情绪维度 = (恐贪指数-50)/50",
                "估值维度 CAPE 越高越贵(未来回报预期越低)、ERP 越高股票越具吸引力",
                "广度维度 = 跨市场复合：A股(涨家数-跌家数)/(涨+跌+平) 等权 0.5 + 美股等权ETF(RSP)对市值ETF(SPY)的月度动量(等权跑赢=宽度扩散)，各0.5",
                "综合分（加权口径）= 各维度分按权重加权，并对「实际可用维度」归一化",
                "共识口径 = 偏多维度数 − 偏空维度数（|分|≥0.15 才计票）；净票数达到「可用维度数的 1/3」（最少 2 票）即判多头/空头共识，否则无共识。用于对冲加权口径在维度共线时的「正负抵消→恒贴零」缺陷",
                "波动率与量能不再作为维度投票：VIX 与美股 r=+0.82、量能 86 天翻转 42 次，前者复述美股、后者是噪声。VIX 以 risk_gauge 形式作为风险温度展示",
                "结构层：引入 Carlota Pérez 技术-经济范式周期，叠加广度背离校验，作为战术牛熊之上的制度背景",
                "韩国 KOSPI：高 Beta、半导体/出口敏感市场，作全球风险与 AI 硬件周期领先指标（kr_stocks）",
                "黄金维度作避险/实际利率代理，按自身价格趋势计综合分（金价走强常伴随避险需求/实际利率下行）",
                "日元套息维度 = clamp(USD/JPY 近20日变动 / 5%)：JPY 弱=套息顺畅=risk-on，JPY 急升=平仓=risk-off（yen_carry）",
            ],
        },
        "structural_signal": structural,
    }

    # ---------- 失败保护：可用维度不足则保留上一次快照，不覆盖 ----------
    if len(present) < 2:
        print(f"== FATAL: 可用维度仅 {len(present)} 个，疑似全源失效；"
              f"保留上一次 market.json，退出非零以便自动化告警 ==")
        sys.exit(2)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"== 完成，写入 {OUT_FILE} | 综合分 {overall} {payload['overall']['verdict']}"
          f" | 共识 {bull_n}多/{neutral_n}中/{bear_n}空 → {consensus_verdict} ==")

    # ---------- 历史快照归档（供历史回看 / 回测基线 / 失败回滚） ----------
    try:
        hist_dir = os.path.join(OUT_DIR, "history")
        os.makedirs(hist_dir, exist_ok=True)
        snap = os.path.join(hist_dir, dt.date.today().isoformat() + ".json")
        with open(snap, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"== 已归档快照 {snap} ==")
        removed = prune_history(hist_dir, HISTORY_KEEP_DAYS)
        if removed:
            print(f"== 快照保留 {HISTORY_KEEP_DAYS} 天，清理 {len(removed)} 份过期快照"
                  f"（{removed[0]} ~ {removed[-1]}）==")
    except Exception as e:  # noqa
        print(f"  [WARN] 快照归档失败（不影响主输出）: {e}")

    # ---------- 状态文件（供自动化告警读取） ----------
    try:
        status = {
            "status": "ok",
            "generated_at": payload["generated_at"],
            "overall": payload["overall"],
            "consensus": payload["consensus"],
            "risk_gauge": payload["risk_gauge"],
            "dims_available": len(present),
            "dims_total": len(WEIGHTS),
            "note": ("全部维度可用" if len(present) == len(WEIGHTS)
                     else "部分维度因数据源本次不可达而缺失，已按可用维度归一化"),
        }
        with open(os.path.join(OUT_DIR, "status.json"), "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa
        pass


if __name__ == "__main__":
    main()
