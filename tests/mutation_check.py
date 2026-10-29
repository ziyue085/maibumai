#!/usr/bin/env python3
"""变异测试：证明回归用例不是空转。

做法：故意把关键规则改坏，再跑全量用例。
每一次变异都必须导致至少一条用例失败；否则说明对应用例没起作用。

用法：
    python tests/mutation_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import evidence  # noqa: E402
import verdict   # noqa: E402
import run_regression as rr  # noqa: E402


def _set(obj, attr, value):
    def apply():
        setattr(obj, attr, value)

    def restore():
        setattr(obj, attr, _ORIGINAL[(id(obj), attr)])

    _ORIGINAL.setdefault((id(obj), attr), getattr(obj, attr))
    return apply, restore


def _item(mapping, key, value):
    def apply():
        mapping[key] = value

    def restore():
        mapping[key] = _ORIGINAL[(id(mapping), key)]

    _ORIGINAL.setdefault((id(mapping), key), mapping[key])
    return apply, restore


_ORIGINAL: dict = {}

MUTATIONS = []


def mutation(name, expect_suites):
    def deco(factory):
        MUTATIONS.append((name, factory(), expect_suites))
        return factory

    return deco


@mutation("把「已有同类」从硬拦截降级为可购买", ["verdict"])
def _m1():
    return _item(verdict.RULE_TIER, "DUP_OWNED", "BUY")


@mutation("把「价格过期」当作正向理由", ["verdict"])
def _m2():
    return _item(verdict.RULE_TIER, "PRICE_STALE", "BUY")


@mutation("取消「为什么需要」锚点要求", ["verdict"])
def _m3():
    return _set(verdict, "BUY_ANCHORS", ())


@mutation("把电子品价格保鲜期放宽到 999 天", ["evidence"])
def _m4():
    return _item(evidence.STALE_DAYS, "price_fast", 999)


@mutation("把价格冲突阈值放宽到 50%", ["evidence"])
def _m5():
    return _set(evidence, "_PRICE_CONFLICT_RATIO", 0.5)


@mutation("删掉废话检测词表", ["audit"])
def _m6():
    return _set(verdict, "VAGUE_PHRASES", ())


@mutation("删掉最高级表述检测", ["audit"])
def _m7():
    return _set(verdict, "SUPERLATIVES", ())


@mutation("允许一条理由就下结论", ["audit"])
def _m8():
    return _set(verdict, "MIN_REASONS", {"L1": 0, "L2": 0, "L3": 0})


@mutation("把用户口述价格当成官方报价", ["audit"])
def _m9():
    return _item(evidence.SOURCE_TIERS, "user_provided", 1)


@mutation("让空替代品也算有效替代方案", ["audit"])
def _m10():
    return _set(verdict, "_valid_alternative", lambda alt: True)


def main() -> int:
    baseline = rr.run_all()
    base_fail = [r for r in baseline if not r["ok"]]
    if base_fail:
        print("基线就有失败用例，先修好再谈变异测试：")
        for r in base_fail:
            print(f"  [FAIL] {r['suite']} {r['id']} {r['detail']}")
        return 2

    print(f"基线：{len(baseline)} 条用例全部通过\n")

    survived = []
    for name, (apply, restore), suites in MUTATIONS:
        apply()
        try:
            results = rr.run_all()
        finally:
            restore()
        fails = [r for r in results if not r["ok"]]
        hit = sorted({r["id"] for r in fails})
        ok = bool(fails)
        print(f"[{'KILLED' if ok else 'SURVIVED'}] {name}")
        if ok:
            print(f"          被捕获于：{', '.join(hit)}")
        else:
            survived.append(name)

    print("-" * 56)
    print(f"变异 {len(MUTATIONS)} 个，被捕获 {len(MUTATIONS) - len(survived)} 个")
    if survived:
        print("未被捕获（说明对应用例缺失）：")
        for s in survived:
            print("  -", s)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
