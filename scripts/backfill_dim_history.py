#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 data/history/*.json 重建 data/dim_history.json（维度卡折线的数据源）。

为什么需要这个脚本（两个用途，第二个才是重点）：

  1. **首次回填**：dim_history 是从 2026-11-02 才开始写的，而折线要 60 个点才看得出
     形状。不回填的话，上线那天只画得出一个点。
  2. **灾备重建**：dim_history.json 是**派生文件**，真正的源是 history/ 下的逐日快照。
     它丢了、坏了、被误删了、schema 升级了，跑这一条命令即可重建，RPO ≈ 0。

用法：
  python scripts/backfill_dim_history.py              # 增量并入现有 dim_history.json
  python scripts/backfill_dim_history.py --reset       # 丢掉现有文件，从快照全量重建
  python scripts/backfill_dim_history.py --dry-run     # 只打印将要写入什么，不落盘

规则（与 fetch_data.write_dim_history 完全共用同一套逻辑，不另写一遍）：
  · 行主键 = 该快照的 **as_of（数据截止日）**；缺失时退回 generated_at 的日期部分。
    绝不用快照文件名/跑批日 —— 周末与长假的跑批拿的是前一个交易日的数据，
    用跑批日会把同一根 K 线记成好几个点。
  · 同一 as_of 出现多份快照时，**后跑的那份胜出**（"对某交易日最新的计算"）。
    实测 2026-10-30 就有两份：10-30 跑的（债券旧口径 −0.050）与 10-31 跑的
    （同一组 K 线、新口径 +1.000），取后者。
  · 口径断点不单独维护，由 dim_breaks() 从行数据反推，因此不会与行数据漂移。
"""

import argparse
import datetime as dt
import glob
import json
import os
import shutil
import sys

# 复用抓取脚本里的全部实现（DIM_HIST_DIMS / dim_row / write_dim_history / read_dim_history）。
# 不复制一份逻辑：两处实现一旦分叉，"回填出来的表"和"每天增量写的表"就会长得不一样，
# 而这种差异要等到前端画出怪折线时才被发现。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data as fd  # noqa: E402


def snapshot_as_of(doc, path):
    """一份历史快照的「数据截止日」。

    优先级：data_freshness.as_of（2026-10-31 起才有）→ generated_at 的日期部分
    → 文件名。三级兜底不是防御性编程的洁癖：早期 19 份快照没有 data_freshness，
    而 generated_at 是 UTC 时间戳，直接切前 10 位正好是 runner 的 UTC 日期。
    """
    a = (doc.get("data_freshness") or {}).get("as_of")
    if a:
        return str(a)
    g = doc.get("generated_at") or ""
    if len(g) >= 10 and g[4] == "-" and g[7] == "-":
        return g[:10]
    base = os.path.basename(path)
    return base[:10] if len(base) >= 15 else None


def load_snapshots(hist_dir):
    """按文件名升序读入所有快照，返回 [(as_of, doc, path), ...]。"""
    out, bad = [], []
    for path in sorted(glob.glob(os.path.join(hist_dir, "*.json"))):
        # 只认 YYYY-MM-DD.json，别的文件（含我们自己写的 .bak）一律跳过
        base = os.path.basename(path)
        if not (len(base) == 15 and base.endswith(".json")):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception as e:  # noqa
            bad.append((base, str(e)))
            continue
        as_of = snapshot_as_of(doc, path)
        if not as_of:
            bad.append((base, "无法确定数据截止日"))
            continue
        out.append((as_of, doc, path))
    return out, bad


def main():
    ap = argparse.ArgumentParser(description="从 history 快照重建 dim_history.json")
    ap.add_argument("--history", default=os.path.join(fd.OUT_DIR, "history"),
                    help="快照目录（默认 data/history）")
    ap.add_argument("--out", default=fd.DIM_HIST_FILE,
                    help="输出文件（默认 data/dim_history.json）")
    ap.add_argument("--reset", action="store_true",
                    help="丢弃现有输出文件，从快照全量重建")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()

    snaps, bad = load_snapshots(args.history)
    if not snaps:
        print(f"== 在 {args.history} 下没找到任何快照，退出 ==")
        return 2
    print(f"== 读到 {len(snaps)} 份快照"
          + (f"，{len(bad)} 份跳过（{'、'.join(b for b, _ in bad[:3])}…）" if bad else "")
          + " ==")

    # 同一 as_of 多份时后者胜出：先按 as_of 归并，保留最后出现的那份 payload。
    # 用有序字典语义（dict 在 py3.7+ 保序），最后再按日期排序写盘。
    by_as_of = {}
    for as_of, doc, path in snaps:
        if as_of in by_as_of:
            print(f"  [重复截止日] {as_of}：{os.path.basename(path)} 覆盖 "
                  f"{os.path.basename(by_as_of[as_of][1])}")
        by_as_of[as_of] = (doc, path)

    if args.reset and os.path.exists(args.out) and not args.dry_run:
        os.replace(args.out, args.out + ".bak")
        print(f"== --reset：旧文件已备份为 {os.path.basename(args.out)}.bak ==")

    tmp = args.out + ".tmp" if args.dry_run else args.out
    if args.dry_run:
        # dry-run 用一个临时副本喂同一套合并逻辑，跑完删掉；
        # 结束时打印的"文件大小/行数"仍然成立。
        if os.path.exists(tmp):
            os.remove(tmp)
        if os.path.exists(args.out) and not args.reset:
            shutil.copyfile(args.out, tmp)

    added = replaced = 0
    for as_of in sorted(by_as_of):
        doc, path = by_as_of[as_of]
        payload = {"generated_at": doc.get("generated_at"), "markets": doc.get("markets") or {}}
        r = fd.write_dim_history(payload, as_of, tmp)
        if not r:
            print(f"  [SKIP] {as_of}（{os.path.basename(path)}）抽不出维度分")
            continue
        if r["added"]:
            added += 1
        else:
            replaced += 1

    full = fd.read_dim_history(tmp)
    rows = full.get("rows") or []
    # generated_at 改成"这次重建的时间"，并标注来源，方便日后判断这份表是谁造的。
    # 保留 rows / breaks / calibers，它们由 write_dim_history 算好。
    full["generated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    full["source"] = "backfill"
    if not args.dry_run:
        fd.write_json_compact(args.out, full)
        out_size = os.path.getsize(args.out)
    else:
        out_size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        if os.path.exists(tmp):
            os.remove(tmp)

    # ---- 报告 ----
    print(f"\n== 结果：{len(rows)} 行（新增 {added} / 覆盖 {replaced}）==")
    print(f"   文件：{args.out}（{out_size/1024:.1f} KB"
          + ("，dry-run 未落盘）" if args.dry_run else "）"))
    print(f"   截止日：{rows[0][0]} ~ {rows[-1][0]}" if rows else "   截止日：—")
    print(f"   口径：{full.get('calibers')}")

    # 每维度的首末值与覆盖情况，用来一眼看出"某维度是不是整段缺失"
    print("\n   维度      首值     末值     缺失行")
    for i, k in enumerate(fd.DIM_HIST_DIMS):
        vals = [r[i + 1] if i + 1 < len(r) else None for r in rows]
        nn = [v for v in vals if v is not None]
        miss = sum(1 for v in vals if v is None)
        first = f"{nn[0]:+.3f}" if nn else "  —  "
        last = f"{nn[-1]:+.3f}" if nn else "  —  "
        print(f"   {k:<10} {first}  {last}  {miss:>6}")

    if full.get("breaks"):
        print("\n   口径断点（折线在此处必须断开）：")
        for d, k, note in full["breaks"]:
            print(f"     {d}  {k}  {note}")
    else:
        print("\n   口径断点：无")

    # 「与上一行完全相同」的维度 —— 通常是休市期间被冻结的数据，折线会画成一段
    # 水平线。这里只统计、不处理：保留行是对的（那几天确实没有新信息），
    # 需要的是前端标注"休市沿用"，而不是把点删掉（见规划 §3.6）。
    # 判据用「相邻行同值」而不是「整行同值」：整行同时不变几乎不会发生，
    # 实测国庆期间只有 A股 一维被冻结，其余六维照常变化。
    frozen = {}
    for i, k in enumerate(fd.DIM_HIST_DIMS):
        n = 0
        for a, b in zip(rows, rows[1:]):
            if i + 1 < len(a) and i + 1 < len(b) and a[i + 1] is not None \
                    and a[i + 1] == b[i + 1]:
                n += 1
        if n >= 2:
            frozen[k] = n
    if frozen:
        print("\n   注意：以下维度存在连续同值段（多为休市沿用，前端需标注，"
              "否则会被读成横盘）：")
        for k, n in frozen.items():
            print(f"     {k:<10} {n} 处与上一行同值")
    return 0


if __name__ == "__main__":
    sys.exit(main())
