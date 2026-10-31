#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""综合分阈值标定。

## 要解决什么

加权口径的综合分长期贴着 0：2026-10-31 实测 −0.091，距 ±0.33 的判定阈值还差 24，
Hero 大字长期恒定「震荡」，信号全部由共识口径承载。
这个 ±0.33 是当初拍脑袋定的，从未用数据检验过。

## 怎么做

用**与线上完全同源**的评分公式（composite_score_at），把 7 个维度在长历史上
逐日重算，得到一条可回测的综合分序列，然后回答三个问题：

1. 综合分的分布长什么样（σ、分位）？固定 ±0.33 落在分布的哪个位置？
2. 综合分和未来 21 日收益有关系吗？（分十档看单调性，比单一阈值更抗噪）
3. 换成「±k·σ」动态阈值，触发率与方向准确率如何？

## 局限（不要当成没写）

- **情绪维度没有历史序列**（恐贪指数只有当日快照），长历史口径只有 6 维，
  权重按可用维度归一化。所以这里算出的 σ 会比线上略低，不能直接套到 7 维上。
- **债券维度**用的是 2026-10-31 的新口径（收益率 20 日变化量），
  与旧快照里的债券分不是一回事，两者不可混用。
- 前瞻收益取标的自身，不是「做多综合分」的策略收益——没有考虑交易成本、滑点、仓位。

用法：
    python scripts/calibrate.py                 # 默认 2015 年起，21 个交易日前瞻
    python scripts/calibrate.py --years 8 --horizon 21
"""
import argparse
import datetime as dt
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_data import (  # noqa: E402
    WEIGHTS, OUT_DIR, bars_ok, build_series, clamp,
    composite_score_at, fetch_eastmoney_kline, fetch_yahoo,
)

# 维度 → 底层的日线序列。
# "em" 形态是 ("em", 东财 secid, Yahoo symbol)，顺序不能反：
# 首次跑批把韩股/黄金/套息写成了 ("em", yahoo代码, 东财代码)，结果东财拿着
# "^KS11" 报错、回退时又把 "100.KS11" 当 Yahoo 代码请求，两条腿一起死。
DIM_SPECS = {
    "us_stocks": [("yahoo", "^GSPC"), ("yahoo", "^IXIC")],
    "cn_stocks": [("em", "1.000001", "000001.SS"), ("em", "1.000300", "000300.SS"),
                  ("em", "1.000688", "000688.SS"), ("em", "0.399006", "399006.SZ")],
    "kr_stocks": [("em", "100.KS11", "^KS11")],
    "gold": [("em", "101.GC00Y", "GC=F")],
    "bonds": [("yahoo", "^TNX")],
    "yen_carry": [("em", "133.USDJPY", "JPY=X")],
}
DIM_LABEL = {"us_stocks": "美股", "cn_stocks": "A股", "kr_stocks": "韩股",
             "bonds": "债券", "gold": "黄金", "yen_carry": "日元套息"}

BOND_WINDOW = 20        # 债券：收益率 20 日变化量，25bp = 0.25 个百分点
BOND_SCALE_BP = 25.0
FX_WINDOW = 20          # 套息：USD/JPY 20 日动量，±5% 打满


def load(spec, years):
    """按 spec 取一条长历史日线，返回 build_series 结果或 None。"""
    rng = f"{years}y"
    if spec[0] == "yahoo":
        sym = spec[1]
        raw = fetch_yahoo(sym, rng=rng)
        name, code = sym, sym
    else:
        em_secid, yh = spec[1], spec[2]
        raw = fetch_eastmoney_kline(em_secid, beg="20050101", lmt=100000)
        if not bars_ok(raw):
            raw = fetch_yahoo(yh, rng=rng)
        name, code = yh, em_secid
    if not bars_ok(raw):
        print(f"  [SKIP] {name} 取不到足够历史（{len((raw or {}).get('close') or [])} 根）")
        return None
    s = build_series(name, code, raw)
    if not s:
        return None
    print(f"  [OK] {name} {len(s['close'])} 根 {s['dates'][0]} ~ {s['dates'][-1]}")
    return s


def price_dim_scores(s):
    """一条价格序列 → {日期: 综合分}。用与线上同一个 composite_score_at。"""
    out = {}
    c = s["close"]
    for i in range(len(c)):
        if s["ma20"][i] is None or s["rsi"][i] is None:
            continue
        out[s["dates"][i]] = composite_score_at(
            c, s["ma20"], s["ma60"], s["ma200"], s["rsi"], s["macdHist"], i)
    return out


def bond_dim_scores(s):
    """债券：收益率 20 日变化量。下行 25bp → +1（债牛）。"""
    out = {}
    c = s["close"]
    for i in range(BOND_WINDOW, len(c)):
        delta = c[i] - c[i - BOND_WINDOW]
        out[s["dates"][i]] = round(clamp(-delta / (BOND_SCALE_BP / 100.0)), 3)
    return out


def yen_dim_scores(s):
    """套息：USD/JPY 20 日动量 / 5%，日元贬值（USDJPY 上行）= 套息顺畅 = risk-on。"""
    out = {}
    c = s["close"]
    for i in range(FX_WINDOW, len(c)):
        mom = c[i] / c[i - FX_WINDOW] - 1
        out[s["dates"][i]] = round(clamp(mom / 0.05), 3)
    return out


def build_dim_series(years):
    """返回 ({维度: {日期: 分}}, {维度: 底层序列数}, {Yahoo代码: series})。

    第三个返回值是缓存：评估标的（沪深300 / 标普500）用的正是这里已经拉过的序列，
    再请求一次毫无意义，还会因为东财抖动而拿到不一致的结果。
    首跑就是栽在这上面：沪深300 的 dim 已成功拉到 5299 根，到了评估环节
    重新请求时东财断连、Yahoo 也没给数据，整个 A股 标的直接跳过。"""
    dims, used, cache = {}, {}, {}
    for key, specs in DIM_SPECS.items():
        per_sub = []
        for spec in specs:
            s = load(spec, years)
            if not s:
                continue
            cache[spec[-1]] = s          # spec[-1] 恒为 Yahoo 代码
            if key == "bonds":
                per_sub.append(bond_dim_scores(s))
            elif key == "yen_carry":
                per_sub.append(yen_dim_scores(s))
            else:
                per_sub.append(price_dim_scores(s))
        if not per_sub:
            print(f"  [WARN] {DIM_LABEL[key]} 无可用底层序列，该维度不参与历史重建")
            continue
        # 同维度多个指数：按日期等权平均（与线上 avg_usable 同义）
        merged = {}
        for d in set().union(*[set(p) for p in per_sub]):
            vals = [p[d] for p in per_sub if d in p]
            if vals:
                merged[d] = round(sum(vals) / len(vals), 3)
        dims[key] = merged
        used[key] = len(per_sub)
    return dims, used, cache


def composite_series(dims):
    """逐日按可用维度归一化加权。与线上 computeOverall 同规则。"""
    all_dates = sorted(set().union(*[set(v) for v in dims.values()])) if dims else []
    out = {}
    for d in all_dates:
        acc = tw = 0.0
        for k, series in dims.items():
            if d in series:
                w = WEIGHTS.get(k, 0)
                acc += w * series[d]
                tw += w
        if tw > 0:
            out[d] = round(acc / tw, 4)
    return out


def corr(a, b):
    if len(a) < 3:
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    return round(num / (da * db), 3) if da and db else None


def analyse(comp, target, horizon):
    """把综合分与标的的前瞻收益对齐，输出分布、十档、阈值扫描。"""
    dates = target["dates"]
    close = target["close"]
    idx_of = {d: i for i, d in enumerate(dates)}

    pairs = []
    for d, sc in comp.items():
        i = idx_of.get(d)
        if i is None:
            continue
        fr = None
        j = i + horizon
        if j < len(close) and close[i]:
            fr = close[j] / close[i] - 1
        if fr is not None:
            pairs.append((d, sc, fr))
    if len(pairs) < 200:
        return {"error": f"样本不足（{len(pairs)} 天），无法标定"}

    scores = [p[1] for p in pairs]
    fwds = [p[2] for p in pairs]
    sigma = statistics.pstdev(scores)

    # 固定阈值 ±0.33 的落点
    hit33 = sum(1 for s in scores if abs(s) >= 0.33) / len(scores)

    # 十档：按综合分排序后等分，看前瞻收益是否单调
    order = sorted(range(len(pairs)), key=lambda i: scores[i])
    buckets = []
    ten = max(1, len(order) // 10)
    for b in range(10):
        sel = order[b * ten:(b + 1) * ten] if b < 9 else order[9 * ten:]
        if not sel:
            continue
        buckets.append({
            "bucket": b + 1,
            "score_mean": round(statistics.mean(scores[i] for i in sel), 3),
            "score_min": round(min(scores[i] for i in sel), 3),
            "score_max": round(max(scores[i] for i in sel), 3),
            "fwd_mean_pct": round(statistics.mean(fwds[i] for i in sel) * 100, 2),
            "win_rate_pct": round(sum(1 for i in sel if fwds[i] > 0) / len(sel) * 100, 1),
            "n": len(sel),
        })

    # 固定阈值扫描
    scan = []
    for t in (0.2, 0.25, 0.3, 0.33, 0.4, 0.5, 0.6):
        bull = [(s, f) for _, s, f in pairs if s >= t]
        bear = [(s, f) for _, s, f in pairs if s <= -t]
        def stat(g):
            if not g:
                return None
            return {
                "n": len(g),
                "share_pct": round(len(g) / len(pairs) * 100, 1),
                "fwd_mean_pct": round(statistics.mean(f for _, f in g) * 100, 2),
                "win_rate_pct": round(sum(1 for _, f in g if f > 0) / len(g) * 100, 1),
            }
        scan.append({"threshold": t, "bull": stat(bull), "bear": stat(bear),
                     "gap_pct": (round((stat(bull)["fwd_mean_pct"]
                                        - stat(bear)["fwd_mean_pct"]), 2)
                                 if bull and bear else None)})

    # 动态阈值：±k·σ
    dyn = []
    for k in (0.75, 1.0, 1.25, 1.5):
        t = round(k * sigma, 3)
        bull = [(s, f) for _, s, f in pairs if s >= t]
        bear = [(s, f) for _, s, f in pairs if s <= -t]
        dyn.append({
            "k": k, "threshold": t,
            "trigger_pct": round((len(bull) + len(bear)) / len(pairs) * 100, 1),
            "bull_fwd_pct": round(statistics.mean(f for _, f in bull) * 100, 2) if bull else None,
            "bear_fwd_pct": round(statistics.mean(f for _, f in bear) * 100, 2) if bear else None,
        })

    return {
        "n_days": len(pairs),
        "date_range": [pairs[0][0], pairs[-1][0]],
        "composite_sigma": round(sigma, 4),
        "composite_mean": round(statistics.mean(scores), 4),
        "composite_min": round(min(scores), 3),
        "composite_max": round(max(scores), 3),
        "fixed_033_trigger_pct": round(hit33 * 100, 1),
        "corr_with_fwd": corr(scores, fwds),
        "deciles": buckets,
        "fixed_scan": scan,
        "dynamic_scan": dyn,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--horizon", type=int, default=21)
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "calibration.json"))
    args = ap.parse_args()

    print(f"== 阈值标定：{args.years} 年历史，前瞻 {args.horizon} 个交易日 ==\n")
    print("-- 抓取底层序列 --")
    dims, used, cache = build_dim_series(args.years)
    print(f"\n-- 可用维度 {len(dims)}/{len(WEIGHTS)}：{'、'.join(DIM_LABEL.get(k, k) for k in dims)} --")

    comp = composite_series(dims)
    print(f"-- 重建综合分 {len(comp)} 个交易日 "
          f"（{min(comp) if comp else '—'} ~ {max(comp) if comp else '—'}） --")

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "years": args.years,
        "horizon_days": args.horizon,
        "dims_rebuilt": {k: {"sub_series": used.get(k, 0), "days": len(v)}
                         for k, v in dims.items()},
        "dims_missing": [k for k in WEIGHTS if k not in dims],
        "note": ("情绪维度（恐贪指数）无历史序列，长历史口径仅 6 维，权重按可用维度归一化；"
                 "因此这里算出的 σ 会低于线上 7 维口径，不能直接照搬阈值。"),
        "targets": {},
    }

    # 两个标的各算一遍：A股看沪深300（拿不到就退到上证），美股看标普500。
    # 东财对单个 secid 的抖动是随机的（同一轮里 000001.SS 成功、000300.SS 只给 1 根），
    # 所以标的必须有降级链，否则「A股 没有结论」会变成常态。
    for label, chain in (
        ("沪深300", [("1.000300", "000300.SS"), ("1.000001", "000001.SS")]),
        ("标普500", [(None, "^GSPC")]),
    ):
        print(f"\n-- 评估标的：{label} --")
        t = None
        for secid, yh in chain:
            # 优先复用手上已有的序列，避免重复请求（也避免两次抓取拿到不同结果）
            t = cache.get(yh)
            if t is not None:
                break
            raw = None
            if secid:
                raw = fetch_eastmoney_kline(secid, beg="20050101", lmt=100000)
            if not bars_ok(raw):
                raw = fetch_yahoo(yh, rng=f"{args.years}y")
            if bars_ok(raw):
                t = build_series(label, secid or yh, raw)
                break
            print(f"  [WARN] {yh} 不可用，尝试下一个标的")
        if t is None:
            print(f"  [SKIP] {label} 取不到历史，跳过该标的的评估")
            continue
        res = analyse(comp, t, args.horizon)
        out["targets"][label] = res
        if "error" in res:
            print(f"  {res['error']}")
            continue
        print(f"  样本 {res['n_days']} 天  σ={res['composite_sigma']}  "
              f"固定±0.33 触发率 {res['fixed_033_trigger_pct']}%  "
              f"与前瞻收益相关 r={res['corr_with_fwd']}")
        print("  十档（按综合分从低到高）：")
        for b in res["deciles"]:
            print(f"    第{b['bucket']:2d}档 分 {b['score_min']:+.2f}~{b['score_max']:+.2f}  "
                  f"fwd{b['fwd_mean_pct']:+.2f}%  胜率 {b['win_rate_pct']:>5.1f}%  n={b['n']}")
        print("  动态阈值：")
        for d in res["dynamic_scan"]:
            print(f"    ±{d['k']}σ = {d['threshold']:+.3f}  触发 {d['trigger_pct']}%  "
                  f"多头组 fwd {d['bull_fwd_pct']}%  空头组 fwd {d['bear_fwd_pct']}%")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n== 已写出 {args.out} ==")


if __name__ == "__main__":
    main()
