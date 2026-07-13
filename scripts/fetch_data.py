#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中美金融市场牛熊状态 - 数据抓取与牛熊判定脚本
=================================================
由 GitHub Actions（或本地）定时运行，抓取多源免费公开数据，
计算牛熊指标，输出静态 JSON 供 GitHub Pages 前端渲染。

数据源（无需密钥即可运行核心部分）：
  - Yahoo Finance (query1/query2) : 美股/部分指数、美债10Y(^TNX)、中概指数
  - 东方财富 push2his              : A股指数(上证/沪深300) K线、中美国债收益率
  - FRED (api_key 可选)            : 美国宏观 CPI / 失业率 / GDP
  - feargreedchart.com             : 美股恐惧贪婪指数 (CNN 同口径)
  - Tushare (token 可选)           : A股指数兜底 + 中国宏观(需积分)

关键架构约束：
  GitHub Pages 是纯静态托管，浏览器前端无法直连上述 API（CORS / 反爬 / 密钥暴露）。
  因此所有抓取与判定都在「服务端」（GitHub Actions Runner）完成，结果落盘为
  data/market.json，前端仅负责渲染。这也是本方案可行的核心前提。
"""

import json
import os
import sys
import time
import datetime as dt
from urllib.parse import urlencode

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

def load_env_local():
    """无依赖加载项目根目录 .env，避免把密钥写进脚本或仓库。"""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

load_env_local()

FRED_KEY = os.environ.get("FRED_KEY", "").strip()
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "").strip()


# ----------------------------------------------------------------------------
# 通用 HTTP 工具：指数退避 + 限流友好
# ----------------------------------------------------------------------------
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
    print(f"  [WARN] 请求失败 {url}: {last_err}")
    return None


# ----------------------------------------------------------------------------
# 指标计算（纯函数，便于单测与复用）
# ----------------------------------------------------------------------------
def sma(values, window):
    out = [None] * len(values)
    if len(values) < window:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v if v is not None else 0
        if i >= window:
            s -= values[i - window] or 0
        if i >= window - 1:
            out[i] = round(s / window, 4)
    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_val(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        g = d if d >= 0 else 0
        l = -d if d < 0 else 0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = _rsi_val(avg_gain, avg_loss)
    return out


def _rsi_val(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def macd(values, fast=12, slow=26, signal=9):
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


def oas_score(o):
    """高收益债利差得分：利差越窄(信用风险低)越偏牛。"""
    return round(clamp((4.5 - o) / 4), 3)


def curve_score(s):
    """收益率曲线(10Y-2Y)得分：倒挂(负)=衰退风险偏熊，陡峭(正)=偏牛。"""
    return round(clamp(s / 2), 3)


def vol_confirm_score(close, vol):
    """量能确认（价量配合）：价涨且放量→配合(正)；价跌且放量→派发(负)。
    价涨而缩量→动能不足(接近0)；纯量增不表方向，需结合价格方向。返回 [-1,1]。"""
    n = len(close) - 1
    if n < 2:
        return 0.0
    vma = sma(vol, 20)
    if vma[n] in (None, 0) or vol[n] is None:
        return 0.0
    ratio = vol[n] / vma[n]                       # 相对20日均量的倍数
    ret = close[n] - close[n - 1]
    direction = 1 if ret > 0 else (-1 if ret < 0 else 0)
    surge = clamp((ratio - 1) / 0.8)              # 量比 0.2→-1, 1.0→0, 1.8→+1
    return round(direction * surge, 3)


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
def fetch_eastmoney_kline(secid, beg="20240101"):
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "klt": "101",        # 日线
        "fqt": "1",          # 前复权
        "beg": beg,
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "lmt": "300",
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
            vol.append(float(parts[5]))
        return {"dates": dates,
                "open": o, "high": h, "low": l, "close": c, "vol": [int(x) for x in vol]}
    except Exception as e:  # noqa
        print(f"  [WARN] 东财 K线解析 {secid} 失败: {e}")
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


# ----------------------------------------------------------------------------
# 数据源：FRED 宏观
# ----------------------------------------------------------------------------
def fetch_fred(series_id, api_key, obs_count=120):
    if not api_key:
        return None
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": api_key,
              "file_type": "json", "sort_order": "desc", "limit": obs_count}
    r = http_get(url, params=params)
    if not r:
        return None
    try:
        obs = r.json()["observations"]
        out = []
        for o in obs:
            v = o["value"]
            if v in (".", "", None):
                continue
            out.append({"date": o["date"], "value": float(v)})
        return list(reversed(out))
    except Exception as e:  # noqa
        print(f"  [WARN] FRED {series_id} 解析失败: {e}")
        return None


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
    # 清洗空值行（Yahoo 末/首根偶有 null），保证 OHLC 与指标对齐
    dates, o, h, l, c, vol = [], [], [], [], [], []
    for d, op, hp, lp, cp, vp in zip(raw["dates"], raw["open"], raw["high"],
                                     raw["low"], raw["close"], raw.get("vol", [])):
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
    score = composite_score(close, ma20, ma60, ma200, rsi_arr, hist)
    if invert:                       # 债券收益率：收益率上行=债市走熊
        score = round(-score, 3)
    vol_score = vol_confirm_score(close, vol) if len(close) >= 21 else 0.0
    return {
        "name": name, "symbol": symbol, "dates": dates,
        "open": o, "high": h, "low": l,
        "close": close, "vol": vol,
        "ma20": ma20, "ma60": ma60, "ma200": ma200,
        "rsi": rsi_arr, "macd": dif, "macdSignal": dea, "macdHist": hist,
        "volMa20": sma(vol, 20),
        "volScore": vol_score,
        "latest": close[-1] if close else None,
        "score": score,
        "verdict": verdict_from_score(score),
    }


def series_indicators_block(s):
    """拆出 MA/RSI/MACD 三项子分，用于热力图。"""
    n = len(s["close"]) - 1
    # 仅基于最新快照估算三项分的近似
    c = s["close"][n]; m20 = s["ma20"][n]; m60 = s["ma60"][n]; m200 = s["ma200"][n]
    if None not in (c, m20, m60, m200):
        bull = sum([c > m20, m20 > m60, m60 > m200])
        ma_s = (bull / 3) * 2 - 1
    else:
        ma_s = 0
    r = s["rsi"][n] if s["rsi"][n] is not None else 50
    rsi_s = clamp((r - 50) / 50)
    h = s["macdHist"][n] if s["macdHist"][n] is not None else 0
    macd_s = clamp(h / (0.02 * (c or 1)))
    return {"ma": round(ma_s, 3), "rsi": round(rsi_s, 3), "macd": round(macd_s, 3)}


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    print("== 抓取市场数据（专业维度扩展版） ==")
    markets = {}
    heatmap = []
    dims = {}   # 维度分 -> 用于加权综合

    def add_dim(key, label, score, verdict, sub, extra=None):
        markets[key] = {"label": label, "score": score, "verdict": verdict, "sub": sub}
        if extra:
            markets[key].update(extra)
        dims[key] = score

    # ---------- 美股 ----------
    us_series = []
    for sym, nm in (("^GSPC", "标普500"), ("^IXIC", "纳斯达克")):
        raw = fetch_yahoo(sym)
        if raw:
            s = build_series(nm, sym, raw)
            us_series.append(s)
            blk = series_indicators_block(s)
            heatmap.append(["美股", f"{nm}-均线", blk["ma"]])
            heatmap.append(["美股", f"{nm}-RSI", blk["rsi"]])
            heatmap.append(["美股", f"{nm}-MACD", blk["macd"]])
            print(f"  [OK] {nm} 最新 {s['latest']} 分 {s['score']} {s['verdict']}")
        else:
            print(f"  [SKIP] {nm} 抓取失败")

    if us_series:
        us_score = round(sum(x["score"] for x in us_series) / len(us_series), 3)
        add_dim("us_stocks", "美股", us_score, verdict_from_score(us_score),
                f"标普/纳指综合 {round(us_score*100)}", {"series": us_series})

    # ---------- 美股恐惧贪婪（情绪维度） ----------
    fng = fetch_fng()
    if fng:
        fng_score = clamp((fng["value"] - 50) / 50)
        add_dim("sentiment", "情绪", fng_score, verdict_from_score(fng_score),
                f"恐贪指数 {fng['value']} · {round(fng_score*100)}", {"fng": fng})
        heatmap.append(["情绪", "恐惧贪婪指数", round(fng_score, 3)])
        print(f"  [OK] 情绪 恐贪 {fng['value']} 分 {fng_score}")

    # ---------- 美债10Y ----------
    us10y_raw = fetch_yahoo("^TNX")
    us10y = None
    if us10y_raw:
        us10y = build_series("美债10Y", "^TNX", us10y_raw, invert=True)
        print(f"  [OK] 美债10Y 分 {us10y['score']}")

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
        if raw:
            s = build_series(nm, secid, raw)
            cn_series.append(s)
            blk = series_indicators_block(s)
            heatmap.append(["A股", f"{nm}-均线", blk["ma"]])
            heatmap.append(["A股", f"{nm}-RSI", blk["rsi"]])
            heatmap.append(["A股", f"{nm}-MACD", blk["macd"]])
            print(f"  [OK] {nm} 最新 {s['latest']} 分 {s['score']} {s['verdict']}")
        else:
            print(f"  [SKIP] {nm} 抓取失败")
    if cn_series:
        cn_score = round(sum(s["score"] for s in cn_series) / len(cn_series), 3)
        add_dim("cn_stocks", "A股", cn_score, verdict_from_score(cn_score),
                "上证/沪深300/科创50/创业板指综合", {"series": cn_series})

    # ---------- 量能维度（价量配合，跨市场聚合） ----------
    all_vol = [s["volScore"] for s in (us_series + cn_series) if s.get("volScore") is not None]
    if all_vol:
        vol_score = round(sum(all_vol) / len(all_vol), 3)
        add_dim("volume", "量能", vol_score, verdict_from_score(vol_score), "量价配合(跨市场)")
        for s in (us_series + cn_series):
            if s.get("volScore") is not None:
                heatmap.append(["量能", s["name"], round(s["volScore"], 3)])
        print(f"  [OK] 量能 综合 {vol_score}")

    # ---------- 广度维度（A股全市场涨跌家数，东方财富聚合） ----------
    br = fetch_eastmoney_breadth()
    if br:
        bs = breadth_score(br["up"], br["down"], br["flat"])
        add_dim("breadth", "广度", bs, verdict_from_score(bs),
                f"涨{br['up']}/跌{br['down']}/平{br['flat']}",
                {"up": br["up"], "down": br["down"], "flat": br["flat"]})
        heatmap.append(["广度", "A股涨跌家数", round(bs, 3)])
        print(f"  [OK] 广度 涨{br['up']}/跌{br['down']} 分 {bs}")
    else:
        print("  [SKIP] A股广度抓取失败（东方财富代理可达性，不影响其余维度）")

    # ---------- 债券维度（美债10Y + 中债10Y） ----------
    bond_series = []
    if us10y:
        bond_series.append(us10y)
    cn10y_raw = fetch_china_10y_yield()
    if cn10y_raw is None and FRED_KEY:
        fred_cn = fetch_fred("IRLTLT01CNM156N", FRED_KEY, 300)  # FRED 中国10Y国债收益率
        if fred_cn:
            cn10y_raw = {"dates": [x["date"] for x in fred_cn],
                         "close": [x["value"] for x in fred_cn]}
    if cn10y_raw:
        cn10y = build_series("中债10Y", "CN_10Y", cn10y_raw, invert=True)
        bond_series.append(cn10y)
        print(f"  [OK] 中债10Y 分 {cn10y['score']}")
    if bond_series:
        bond_score = round(sum(s["score"] for s in bond_series) / len(bond_series), 3)
        add_dim("bonds", "债券", bond_score, verdict_from_score(bond_score),
                "中美10Y国债(反向)", {"series": bond_series})
        heatmap.append(["债券", "美债10Y", us10y["score"] if us10y else 0])
        if cn10y_raw:
            heatmap.append(["债券", "中债10Y", bond_series[-1]["score"]])

    # ---------- 波动率维度（VIX，免密钥） ----------
    vix_raw = fetch_yahoo("^VIX")
    if vix_raw:
        cl = [x for x in vix_raw["close"] if x is not None]
        if cl:
            vix_val = cl[-1]
            vs = vix_score(vix_val)
            regime = ("极端恐慌" if vix_val > 30 else "恐慌" if vix_val > 25 else
                      "谨慎" if vix_val > 20 else "正常" if vix_val > 15 else "平静")
            add_dim("volatility", "波动率", vs, verdict_from_score(vs),
                    f"VIX {vix_val:.1f} · {regime}",
                    {"vix": {"value": round(vix_val, 2), "regime": regime}})
            heatmap.append(["风险", "VIX波动率", round(vs, 3)])
            print(f"  [OK] 波动率 VIX {vix_val:.1f} 分 {vs} ({regime})")
    else:
        print("  [SKIP] VIX 抓取失败")

    # ---------- 信用维度（HY OAS / 曲线，FRED 门控） ----------
    if FRED_KEY:
        hy = fetch_fred("BAMLH0A0HYM2", FRED_KEY, 250)   # ICE BofA 高收益债利差
        t102 = fetch_fred("T10Y2Y", FRED_KEY, 250)        # 10Y-2Y 利差(倒挂=衰退信号)
        credit_parts = []
        if hy and hy[-1]["value"] is not None:
            oas = hy[-1]["value"]
            cs = oas_score(oas)
            credit_parts.append(cs)
            heatmap.append(["风险", "HY信用利差", round(cs, 3)])
            print(f"  [OK] 信用 HY OAS {oas:.2f}% 分 {cs}")
        if t102 and t102[-1]["value"] is not None:
            spread = t102[-1]["value"]
            cs2 = curve_score(spread)
            credit_parts.append(cs2)
            heatmap.append(["风险", "收益率曲线", round(cs2, 3)])
            print(f"  [OK] 曲线 10Y-2Y {spread:.2f}% 分 {cs2}")
        if credit_parts:
            credit_score = round(sum(credit_parts) / len(credit_parts), 3)
            add_dim("credit", "信用", credit_score, verdict_from_score(credit_score),
                    "HY利差+曲线(倒挂)",
                    {"hy_oas": (hy[-1]["value"] if hy else None),
                     "curve": (t102[-1]["value"] if t102 else None)})
    else:
        print("  [INFO] 未配置 FRED_KEY，信用维度(HY利差/曲线)跳过（本地配置密钥后启用）")

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
            heatmap.append(["估值", "CAPE", round(cs, 3)])
            print(f"  [OK] 估值 CAPE {cape:.1f} 分 {cs}")
        if ey and us10y_val:
            erp = ey - us10y_val   # 股权风险溢价 = 盈利收益率 - 10Y国债收益率
            cs = erp_score(erp)
            val_parts.append(cs)
            heatmap.append(["估值", "股权风险溢价", round(cs, 3)])
            print(f"  [OK] 估值 ERP {erp:.2f}% 分 {cs}")
    else:
        # 兜底：标普500 价格 / 200日均线 水平代理（价格远高于长期均值=偏贵）
        spx = next((s for s in us_series if s["symbol"] == "^GSPC"), None) if us_series else None
        if spx and spx["ma200"][-1]:
            ratio = spx["close"][-1] / spx["ma200"][-1]
            cs = round(clamp(-(ratio - 1) / 0.25), 3)   # 高于均值25%→-1，低于→+1
            val_parts.append(cs)
            val_method = "价格/200MA代理"
            heatmap.append(["估值", "估值代理", round(cs, 3)])
            print(f"  [OK] 估值代理 价/200MA={ratio:.2f} 分 {cs}（multpl 不可达）")
        else:
            print("  [SKIP] 估值抓取失败（multpl 不可达且无标普序列）")

    if val_parts:
        val_score = round(sum(val_parts) / len(val_parts), 3)
        tag = "偏贵" if val_score < -0.2 else ("中性偏高" if val_score < 0 else "中性")
        add_dim("valuation", "估值", val_score, verdict_from_score(val_score),
                f"{val_method} · {tag}",
                {"cape": cape, "erp": erp, "method": val_method})

    # ---------- 宏观维度（FRED 门控） ----------
    macro = {}
    if FRED_KEY:
        cpi = fetch_fred("CPIAUCSL", FRED_KEY, 24)
        unr = fetch_fred("UNRATE", FRED_KEY, 13)
        gdp = fetch_fred("GDP", FRED_KEY, 12)
        parts = []
        notes = {}
        if cpi and len(cpi) >= 13:
            yoy = (cpi[-1]["value"] / cpi[-13]["value"] - 1) * 100
            parts.append(clamp(-abs(yoy - 2) / 5))
            notes["CPI同比"] = round(yoy, 2)
        if unr:
            uv = unr[-1]["value"]
            parts.append(clamp((6 - uv) / 4))
            notes["失业率"] = uv
        if gdp and len(gdp) >= 5:
            yoy = (gdp[-1]["value"] / gdp[-5]["value"] - 1) * 100
            parts.append(clamp((yoy - 2) / 4))
            notes["GDP同比"] = round(yoy, 2)
        if parts:
            us_macro_score = round(sum(parts) / len(parts), 3)
            macro["us"] = {"score": us_macro_score,
                           "verdict": verdict_from_score(us_macro_score),
                           "notes": notes}
            print(f"  [OK] 美国宏观 分 {us_macro_score} {notes}")
    else:
        print("  [INFO] 未配置 FRED_KEY，宏观维度跳过")

    if macro:
        mscore = round(sum(v["score"] for v in macro.values()) / len(macro), 3)
        markets["macro"] = {
            "label": "宏观", "score": mscore, "verdict": verdict_from_score(mscore),
            "sub": " · ".join(f"{k} {round(v['score']*100)}" for k, v in macro.items()),
            "detail": macro,
        }
        dims["macro"] = mscore   # 计入加权综合（修复：原仅写入 markets 未进 dims）

    # ---------- 韩国 KOSPI（全球风险 / 半导体周期领先指标，Yahoo 免密钥） ----------
    ks_raw = fetch_yahoo("^KS11")
    if ks_raw:
        ks = build_series("韩国KOSPI", "^KS11", ks_raw)
        blk = series_indicators_block(ks)
        heatmap.append(["韩股", "KOSPI-均线", blk["ma"]])
        heatmap.append(["韩股", "KOSPI-RSI", blk["rsi"]])
        heatmap.append(["韩股", "KOSPI-MACD", blk["macd"]])
        kr_score = ks["score"]
        add_dim("kr_stocks", "韩股", kr_score, verdict_from_score(kr_score),
                f"KOSPI 最新 {ks['latest']}", {"series": [ks]})
        print(f"  [OK] 韩股 KOSPI 最新 {ks['latest']} 分 {kr_score}")
    else:
        print("  [SKIP] KOSPI 抓取失败")

    # ---------- 黄金（避险/实际利率代理；按自身价格趋势计综合分，徽章即金价真实方向） ----------
    gold_raw = fetch_yahoo("GC=F")
    if gold_raw:
        g = build_series("黄金", "GC=F", gold_raw)
        blk = series_indicators_block(g)
        heatmap.append(["黄金", "金价-均线", blk["ma"]])
        heatmap.append(["黄金", "金价-RSI", blk["rsi"]])
        heatmap.append(["黄金", "金价-MACD", blk["macd"]])
        gold_score = g["score"]
        add_dim("gold", "黄金(避险)", gold_score, verdict_from_score(gold_score),
                f"COMEX黄金 最新 {g['latest']}（避险/实际利率代理）", {"series": [g]})
        print(f"  [OK] 黄金 最新 {g['latest']} 分 {gold_score}")
    else:
        print("  [SKIP] 黄金 抓取失败")

    # ---------- 日元套息交易（USD/JPY 动量 → 套息健康度） ----------
    jpy_raw = fetch_yahoo("JPY=X")
    if jpy_raw:
        cj = [x for x in jpy_raw["close"] if x is not None]
        fx_series = [build_series("美元兑日元", "JPY=X", jpy_raw)]
        if len(cj) >= 22:
            mom = cj[-1] / cj[-21] - 1
        elif len(cj) >= 2:
            mom = cj[-1] / cj[0] - 1
        else:
            mom = 0
        carry_score = round(clamp(mom / 0.05), 3)
        regime = ("套息顺畅·risk-on" if mom > 0.01 else
                  "套息承压·risk-off" if mom < -0.01 else "中性")
        add_dim("yen_carry", "日元/套息", carry_score, verdict_from_score(carry_score),
                f"USD/JPY 20日变动 {mom*100:+.1f}% · {regime}",
                {"usdjpy": round(cj[-1], 2), "mom20": round(mom, 4)})
        heatmap.append(["套息", "日元套息健康度", carry_score])
        markets.setdefault("fx", {"label": "外汇", "series": fx_series})
        print(f"  [OK] 日元套息 USD/JPY {cj[-1]:.2f} 20日{mom*100:+.1f}% 分 {carry_score} ({regime})")
    else:
        print("  [SKIP] USD/JPY 抓取失败")

    # ---------- 加权综合（按可用维度归一化） ----------
    WEIGHTS = {"us_stocks": 0.15, "cn_stocks": 0.13, "kr_stocks": 0.06,
               "bonds": 0.09, "gold": 0.05, "sentiment": 0.10,
               "volatility": 0.06, "credit": 0.06, "valuation": 0.07,
               "macro": 0.08, "volume": 0.07, "breadth": 0.06, "yen_carry": 0.05}
    present = {k: v for k, v in dims.items() if k in WEIGHTS}
    wsum = sum(WEIGHTS[k] for k in present)
    overall = round(sum(dims[k] * WEIGHTS[k] for k in present) / wsum, 3) if wsum else 0.0

    # ---------- 结构信号（Pérez 范式叠加 + 广度/量能校验） ----------
    val_mk = markets.get("valuation")
    cape_v = val_mk.get("cape") if val_mk else None
    vix_v = markets.get("volatility", {}).get("vix", {}).get("value")
    val_score = val_mk.get("score") if val_mk else None
    br_mk = markets.get("breadth")
    br_score = br_mk.get("score") if br_mk else None
    vol_mk = markets.get("volume")
    vol_score = vol_mk.get("score") if vol_mk else None

    if val_score is not None:
        if (cape_v and cape_v > 35 or val_score < -0.4) and (vix_v is None or vix_v < 18):
            structural = ("估值极端 + 波动率低迷：呈现「金融资本主导·泡沫累积」特征，"
                          "符合导入期狂热(Frenzy)尾声，需警惕向转折点(Turning Point)过渡")
        elif (cape_v and cape_v > 30) or val_score < -0.2:
            structural = "估值偏高：处于导入期中后段，金融资本活跃，尚待进入协同(Synergy)展开期"
        else:
            structural = "估值中性：未显现明显泡沫特征"
    else:
        structural = "估值数据不足，结构信号以广度/量能校验为准"

    if br_score is not None and br_score < -0.3:
        structural += "；广度显著转弱（普跌），与指数背离，警惕量价动能衰减。"
    elif br_score is not None and br_score > 0.3:
        structural += "；广度普涨配合，宽度健康。"
    if vol_score is not None and vol_score < -0.2:
        structural += " 量能转弱：缩量/派发，趋势确认度下降。"

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "us_indices": "Yahoo Finance",
            "cn_indices": "东方财富 push2his / Yahoo 回退",
            "us_10y": "Yahoo Finance ^TNX",
            "cn_10y": "东方财富 datacenter (best-effort) / FRED IRLTLT01CNM156N",
            "vix": "Yahoo Finance ^VIX",
            "kr_indices": "Yahoo Finance ^KS11",
            "gold": "Yahoo Finance GC=F",
            "fx": "Yahoo Finance JPY=X",
            "fng": "feargreedchart.com",
            "valuation": "multpl.com (Shiller CAPE / 盈利收益率)",
            "breadth": "东方财富 push2 全市场涨跌家数 (best-effort)",
            "credit_curve_macro": ("FRED (BAMLH0A0HYM2 / T10Y2Y / CPI / UNRATE / GDP)"
                                   if FRED_KEY else "未配置 FRED_KEY"),
        },
        "markets": markets,
        "overall": {"score": overall, "verdict": verdict_from_score(overall)},
        "heatmap": heatmap,
        "professional": {
            "weights": WEIGHTS,
            "present_weights": {k: WEIGHTS[k] for k in present},
            "dimensions": {k: round(dims[k], 3) for k in present},
            "method_notes": [
                "序列技术分 = 0.5·MA排列(MA20/60/200) + 0.25·RSI(14) + 0.25·MACD(12,26,9)",
                "债券维度对收益率取反（收益率上行=债市走熊）",
                "情绪维度 = (恐贪指数-50)/50",
                "波动率维度 VIX 低=平静偏牛、高=恐慌偏熊",
                "信用维度 HY利差收窄=偏牛、曲线倒挂=偏熊",
                "估值维度 CAPE 越高越贵(未来回报预期越低)、ERP 越高股票越具吸引力",
                "量能维度 = 价涨且放量→配合(正)、价跌且放量→派发(负)，跨美股/A股聚合",
                "广度维度 = (涨家数-跌家数)/(涨+跌+平)，反映全市场宽度而非指数被权重股绑架",
                "综合分 = 各维度分按权重加权，并对「实际可用维度」归一化（未配置源自动剔除）",
                "结构层：引入 Carlota Pérez 技术-经济范式周期，叠加广度/量能背离校验，作为战术牛熊之上的制度背景",
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
    print(f"== 完成，写入 {OUT_FILE} | 综合分 {overall} {payload['overall']['verdict']} ==")

    # ---------- 历史快照归档（供历史回看 / 回测基线 / 失败回滚） ----------
    try:
        hist_dir = os.path.join(OUT_DIR, "history")
        os.makedirs(hist_dir, exist_ok=True)
        snap = os.path.join(hist_dir, dt.date.today().isoformat() + ".json")
        with open(snap, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"== 已归档快照 {snap} ==")
    except Exception as e:  # noqa
        print(f"  [WARN] 快照归档失败（不影响主输出）: {e}")

    # ---------- 状态文件（供自动化告警读取） ----------
    try:
        status = {
            "status": "ok",
            "generated_at": payload["generated_at"],
            "overall": payload["overall"],
            "dims_available": len(present),
            "dims_total": len(WEIGHTS),
            "note": "部分维度可能因未配置密钥/网络受限而缺失，已自动归一化",
        }
        with open(os.path.join(OUT_DIR, "status.json"), "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa
        pass


if __name__ == "__main__":
    main()
