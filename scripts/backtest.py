#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测一致性校验脚本
====================
复用 fetch_data.py 的纯函数（composite_score / sma / rsi / macd / verdict...），
对某一标的做「滚动判定」：在每个交易日用当时可得的历史数据算出牛熊分与结论，
再考察其后的前瞻收益，从而验证本算法的判定是否「含有信息」而非纯噪声。

两种模式：
  1) 单窗口（默认）：近 N 年滚动，输出 data/backtest.json
  2) 样本外多窗口（--multi）：覆盖牛/熊/震荡不同机制的历史区间
     （含 2018 抛售、2020 新冠崩盘、2022 熊市、近期强多头），
     聚合输出 data/backtest_oos.json，验证算法在熊市样本下仍非噪声。

用法：
    python scripts/backtest.py                  # 默认 ^GSPC 近3年
    python scripts/backtest.py --symbol 000300.SS --years 3
    python scripts/backtest.py --horizon 63     # 前瞻约3个月
    python scripts/backtest.py --multi           # 样本外多窗口（含回撤）
"""
import argparse
import json
import math
import os
import datetime as dt
from collections import defaultdict

from fetch_data import (sma, rsi, macd, composite_score,
                        verdict_from_score, fetch_yahoo,
                        fetch_eastmoney_kline, EM_SECID)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUT_FILE = os.path.join(OUT_DIR, "backtest.json")
OUT_FILE_OOS = os.path.join(OUT_DIR, "backtest_oos.json")

VERDICT_LABEL = {"bull": "牛市", "neutral": "震荡", "bear": "熊市"}
LONG_HIST_BEG = "20050101"   # 东财长历史起点（够覆盖 --multi 的全部窗口）

_last_source = ""


def fetch_long_history(symbol, years):
    """优先 Yahoo；Yahoo 403/缺数据时回退东财长历史（仅限 EM_SECID 里映射过的标的）。

    Yahoo 对非浏览器来源常年 403，回退前 backtest 会整条断掉——而历史数据本身
    与"今天是否能取到"无关，故加此兜底。返回 (raw, source)；均失败返回 (None, "")。
    """
    global _last_source
    raw = fetch_yahoo(symbol, rng=f"{years}y")
    if raw:
        _last_source = "yahoo"
        return raw, _last_source
    secid = EM_SECID.get(symbol)
    if not secid:
        print(f"  [WARN] {symbol} 无东财映射，无法回退长历史")
        return None, ""
    print(f"  [INFO] Yahoo 取 {symbol} 失败，回退东财长历史（{secid}，自 {LONG_HIST_BEG}）")
    raw = fetch_eastmoney_kline(secid, beg=LONG_HIST_BEG, lmt=100000)
    if raw:
        _last_source = "eastmoney"
        return raw, _last_source
    return None, ""


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return round(cov / (sx * sy), 3) if sx * sy else 0.0


def rolling_eval(close, dates, horizon):
    """对一段日线做滚动判定，返回 [(verdict, score, fwd_return)]。"""
    close = [None if v is None else float(v) for v in close]
    n = len(close)
    min_hist = 200
    records = []
    for i in range(min_hist, n):
        c = close[i]
        if c is None:
            continue
        sub = close[: i + 1]
        m20 = sma(sub, 20)
        m60 = sma(sub, 60)
        m200 = sma(sub, 200)
        r = rsi(sub, 14)
        _, _, h = macd(sub)
        score = composite_score(sub, m20, m60, m200, r, h)
        k = min(horizon, n - 1 - i)
        if k <= 0:
            continue
        nxt = close[i + k]
        if nxt is None:
            continue
        records.append((verdict_from_score(score), score, nxt / c - 1, dates[i]))
    return records


def aggregate(records):
    # 记录形如 (verdict, score, fwd_return, date)；用下标取值以便兼容不带日期的调用方
    groups = defaultdict(list)
    for rec in records:
        groups[rec[0]].append((rec[1], rec[2]))
    summary = {}
    for v, items in groups.items():
        scores = [x[0] for x in items]
        fwds = [x[1] for x in items]
        summary[v] = {
            "n": len(items),
            "avg_score": round(sum(scores) / len(scores), 3),
            "avg_fwd_return_pct": round(sum(fwds) / len(fwds) * 100, 2),
            "hit_rate_pct": round(sum(1 for f in fwds if f > 0) / len(fwds) * 100, 1),
        }
    corr = pearson([rec[1] for rec in records], [rec[2] for rec in records])
    overall_fwd = sum(rec[2] for rec in records) / len(records)
    return summary, corr, round(overall_fwd * 100, 2)


def dedupe_by_date(records):
    """按决策日去重，保留首次出现的记录。

    多窗口回测里后两段有重叠，同一交易日会被算两次；而两次的分数并不相同——
    因为每个窗口只用**窗口内**的历史算均线，同一天在更晚的窗口里历史更短。
    去重时保留先出现的（其窗口起点更早、用于判定的历史更长）。
    """
    seen = set()
    out = []
    for rec in records:
        d = rec[3] if len(rec) > 3 else None
        if d is not None:
            if d in seen:
                continue
            seen.add(d)
        out.append(rec)
    return out


def run(symbol, years, horizon):
    rng = f"{years}y"
    raw, src = fetch_long_history(symbol, years)
    if not raw:
        print(f"  [FAIL] {symbol} 抓取失败，无法回测")
        return None
    records = rolling_eval(raw["close"], raw["dates"], horizon)
    if not records:
        print("  [FAIL] 无有效回测记录")
        return None
    summary, corr, mean_fwd = aggregate(records)
    return {
        "symbol": symbol, "years": years, "horizon_days": horizon,
        "data_source": src or rng,
        "n_records": len(records),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary_by_verdict": summary,
        "score_forward_corr": corr,
        "mean_fwd_return_pct": mean_fwd,
    }


def run_oos(symbol, windows, horizon):
    """覆盖多个历史区间（含回撤窗口）的样本外校验。"""
    raw, src = fetch_long_history(symbol, 20)
    if not raw:
        print(f"  [FAIL] {symbol} 长历史抓取失败，无法做样本外回测")
        return None
    all_records = []
    window_reports = []
    for (y0, y1) in windows:
        idx = [i for i, d in enumerate(raw["dates"]) if y0 <= int(d[:4]) <= y1]
        if len(idx) < 260:
            print(f"  [SKIP] 窗口 {y0}-{y1} 样本 {len(idx)} 不足，跳过")
            continue
        sub_close = [raw["close"][i] for i in idx]
        sub_dates = [raw["dates"][i] for i in idx]
        recs = rolling_eval(sub_close, sub_dates, horizon)
        if not recs:
            continue
        all_records.extend(recs)
        summary, corr, mean_fwd = aggregate(recs)
        window_reports.append({
            "window": f"{y0}-{y1}", "n_records": len(recs),
            "summary_by_verdict": summary,
            "score_forward_corr": corr,
            "mean_fwd_return_pct": mean_fwd,
        })
        print(f"  [窗口 {y0}-{y1}] 决策点 {len(recs)}，"
              f"牛态命中 {summary.get('bull', {}).get('hit_rate_pct', '-')}%，"
              f"分↔前瞻相关 {corr}")
    if not all_records:
        return None
    summary, corr, mean_fwd = aggregate(all_records)
    uniq = dedupe_by_date(all_records)
    u_summary, u_corr, u_mean_fwd = aggregate(uniq)
    return {
        "symbol": symbol, "horizon_days": horizon,
        "data_source": src or "yahoo",
        "data_range": (f"{raw['dates'][0]} ~ {raw['dates'][-1]}"
                       if raw.get("dates") else None),
        "windows": window_reports,
        "combined": {
            "n_records": len(all_records),
            "n_records_unique": len(uniq),
            "summary_by_verdict": summary,
            "score_forward_corr": corr,
            "mean_fwd_return_pct": mean_fwd,
        },
        # 去重后（同一决策日只保留一次）的指标，用于判断结论是否被重叠放大
        "combined_unique": {
            "n_records": len(uniq),
            "summary_by_verdict": u_summary,
            "score_forward_corr": u_corr,
            "mean_fwd_return_pct": u_mean_fwd,
        },
        "overlap_note": ("combined 为各窗口记录直接拼接，后段窗口间存在重叠交易日，"
                         "同一决策日会被重复计入且因窗口内历史长度不同而得分略有差异；"
                         "combined_unique 为按决策日去重后的口径。"),
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def print_summary(title, res):
    if not res:
        return
    print(f"\n=== {title} ===")
    n = res.get("n_records")
    nu = res.get("n_records_unique")
    if nu is not None and nu != n:
        print(f"有效样本：{n} 个记录（含窗口重叠）｜按决策日去重后 {nu} 个")
    else:
        print(f"有效样本：{n} 个交易决策点")
    print(f"{'结论':<8}{'样本数':>8}{'平均分':>10}{'平均前瞻收益%':>16}{'上涨命中率%':>14}")
    for v in ("bull", "neutral", "bear"):
        s = res.get("summary_by_verdict", {}).get(v)
        if not s:
            continue
        print(f"{VERDICT_LABEL[v]:<8}{s['n']:>8}{s['avg_score']:>10}"
              f"{s['avg_fwd_return_pct']:>16}{s['hit_rate_pct']:>14}")
    print(f"---")
    print(f"牛熊分 ↔ 前瞻收益 相关系数：{res['score_forward_corr']}")
    print(f"全样本平均前瞻收益：{res['mean_fwd_return_pct']}%")


def interpret(res):
    bull = res.get("summary_by_verdict", {}).get("bull", {})
    hit = bull.get("hit_rate_pct")
    corr = res.get("score_forward_corr")
    print("解读：")
    if hit is not None:
        print(f"  · 牛态命中率 {hit}% (>50%) → 算法的「状态判别」有效，能区分当前所处机制")
    if corr is not None and corr < -0.1:
        print(f"  · 分↔前瞻收益 负相关({corr}) → 短周期存在「动量透支后的均值回归」；"
              f"本分数适合做「机制标签」而非短周期择时")
        print(f"    建议再用 --horizon 63 / 252 验证：中长周期相关性通常翻正（动量效应）")
    elif corr is not None and corr > 0.1:
        print(f"  · 分↔前瞻收益 正相关({corr}) → 分数在该周期具备一定方向性预见")
    else:
        print(f"  · 分↔前瞻收益 近零相关({corr}) → 该周期下分数主要反映状态而非方向")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="^GSPC")
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=21)
    ap.add_argument("--multi", action="store_true",
                    help="样本外多窗口回测（含 2018/2020/2022 回撤区间）")
    args = ap.parse_args()

    if args.multi:
        print(f"== 样本外回测：{args.symbol} 多窗口 / 前瞻{args.horizon}日 ==")
        # 覆盖牛/熊/震荡不同机制：2014-2019 含2018抛售；2019-2023 含2020崩盘+2022熊；
        # 2021-2026 近期强多头
        windows = [(2014, 2019), (2019, 2023), (2021, 2026)]
        res = run_oos(args.symbol, windows, args.horizon)
        if not res:
            return
        print_summary(f"样本外聚合 ({args.symbol})", res["combined"])
        if res.get("combined_unique"):
            print_summary(f"样本外聚合 · 按决策日去重 ({args.symbol})", res["combined_unique"])
        interpret(res["combined"])
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(OUT_FILE_OOS, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"== 已写入 {OUT_FILE_OOS} ==")
        return

    print(f"== 回测一致性校验：{args.symbol} 近{args.years}年 / 前瞻{args.horizon}日 ==")
    res = run(args.symbol, args.years, args.horizon)
    if not res:
        return
    print_summary(f"{args.symbol} 近{args.years}年", res)
    interpret(res)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"== 已写入 {OUT_FILE} ==")


if __name__ == "__main__":
    main()
