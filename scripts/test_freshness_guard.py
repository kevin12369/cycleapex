"""同截止日重跑护栏（should_keep_previous）的单元测试。

用真实结构的最小 payload 断言四种情形，不依赖网络。
运行：python scripts/_test_keep_prev.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_data import payload_coverage, should_keep_previous  # noqa: E402


def mk(as_of, dims, heat):
    return {
        "data_freshness": {"as_of": as_of},
        "professional": {"dimensions": {f"d{i}": 0.0 for i in range(dims)}},
        "heatmap": [["维度", f"子{i}", 0.0] for i in range(heat)],
    }


def write(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def main():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "market.json")
    fails = []

    def chk(name, got, want):
        ok = got == want
        print(("  PASS  " if ok else "  FAIL  ") + name)
        if not ok:
            fails.append(name)

    # ① 无旧文件 → 允许写
    if os.path.exists(p):
        os.remove(p)
    chk("无旧文件时写入", should_keep_previous(p, mk("2026-10-30", 7, 27), "2026-10-30"), False)

    # ② 同截止日、旧覆盖更全 → 拒绝覆盖（本次是退化重跑）
    write(p, mk("2026-10-30", 7, 27))
    chk("同日退化重跑被拦截", should_keep_previous(p, mk("2026-10-30", 7, 18), "2026-10-30"), True)

    # ③ 同截止日、旧维度更少（如 9 维时代） → 允许覆盖
    write(p, mk("2026-10-30", 7, 18))
    chk("同日更优结果可覆盖", should_keep_previous(p, mk("2026-10-30", 7, 27), "2026-10-30"), False)

    # ④ 截止日已推进 → 一律允许写（不能把旧一天的数据锁死）
    write(p, mk("2026-10-29", 7, 27))
    chk("截止日推进时放行", should_keep_previous(p, mk("2026-10-30", 7, 5), "2026-10-30"), False)

    # ⑤ 覆盖度完全相同 → 允许覆盖（权重/文案微调应当能生效）
    write(p, mk("2026-10-30", 7, 27))
    chk("覆盖度相同时放行", should_keep_previous(p, mk("2026-10-30", 7, 27), "2026-10-30"), False)

    # ⑥ 旧文件损坏 → 不阻塞
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    chk("旧文件损坏时不阻塞", should_keep_previous(p, mk("2026-10-30", 7, 18), "2026-10-30"), False)

    # ⑦ as_of 缺失 → 不敢判定，放行
    write(p, mk("2026-10-30", 7, 27))
    chk("as_of 缺失时放行", should_keep_previous(p, mk(None, 7, 1), None), False)

    # ⑧ payload_coverage 的取值符合预期
    chk("coverage 计算", payload_coverage(mk("2026-10-30", 7, 27)), (7, 27))

    print()
    if fails:
        print(f"FAILED {len(fails)}: {fails}")
        return 1
    print("ALL PASS (8/8)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
