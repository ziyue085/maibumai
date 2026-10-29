#!/usr/bin/env python3
"""变异测试：证明回归用例不是空转。

做法：故意把关键规则改坏，再跑全量用例。
每一次变异都必须导致至少一条用例失败；否则说明对应用例没起作用。

第二轮审核后新增五条，专门盯住被审核者指出的漏洞：
财务强制、等级降级、理由依据、币种换算、订阅周期。

用法：
    python tests/mutation_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import calc      # noqa: E402
import evidence  # noqa: E402
import mbm       # noqa: E402
import store     # noqa: E402
import verdict   # noqa: E402
import run_regression as rr  # noqa: E402

_ORIGINAL: dict = {}
MUTATIONS = []


def _set(obj, attr, value):
    _ORIGINAL.setdefault((id(obj), attr), getattr(obj, attr))

    def apply():
        setattr(obj, attr, value)

    def restore():
        setattr(obj, attr, _ORIGINAL[(id(obj), attr)])

    return apply, restore


def _item(mapping, key, value):
    _ORIGINAL.setdefault((id(mapping), key), mapping[key])

    def apply():
        mapping[key] = value

    def restore():
        mapping[key] = _ORIGINAL[(id(mapping), key)]

    return apply, restore


def mutation(name):
    def deco(factory):
        MUTATIONS.append((name, factory()))
        return factory

    return deco


# --- 决策矩阵 -------------------------------------------------------------
@mutation("把「已有同类」从硬拦截降级为可购买")
def _m01():
    return _item(verdict.RULE_TIER, "DUP_OWNED", "BUY")


@mutation("把「价格过期」当作正向理由")
def _m02():
    return _item(verdict.RULE_TIER, "PRICE_STALE", "BUY")


@mutation("取消「为什么需要」锚点要求")
def _m03():
    return _set(verdict, "BUY_ANCHORS", ())


@mutation("允许一条理由就下结论")
def _m04():
    return _set(verdict, "MIN_REASONS", {"L1": 0, "L2": 0, "L3": 0})


@mutation("让空替代品也算有效替代方案")
def _m05():
    return _set(verdict, "_valid_alternative", lambda alt: True)


# --- 证据层 ---------------------------------------------------------------
@mutation("把电子品价格保鲜期放宽到 999 天")
def _m06():
    return _item(evidence.STALE_DAYS, "price_fast", 999)


@mutation("把价格冲突阈值放宽到 50%")
def _m07():
    return _set(evidence, "_PRICE_CONFLICT_RATIO", 0.5)


@mutation("把用户口述价格当成官方报价")
def _m08():
    return _item(evidence.SOURCE_TIERS, "user_provided", 1)


@mutation("外币直接当本币用（跳过换算）")
def _m09():
    return _set(evidence, "convert_amount", lambda value, cur, base, items: float(value))


# --- 输出审计 -------------------------------------------------------------
@mutation("删掉废话检测词表")
def _m10():
    return _set(verdict, "VAGUE_PHRASES", ())


@mutation("删掉最高级表述检测")
def _m11():
    return _set(verdict, "SUPERLATIVES", ())


@mutation("允许理由只有码没有依据")
def _m12():
    return _set(verdict, "MIN_NOTE_CHARS", 0)


# --- 财务与分级（第二轮新增） ---------------------------------------------
@mutation("预算超标阈值放宽到 999 倍（等于不拦）")
def _m13():
    return _set(calc, "BREACH_MULTIPLE", 999)


@mutation("让所有等级视作同一级（等级可被降级绕过）")
def _m14():
    return _set(verdict, "LEVEL_RANK", {"L1": 2, "L2": 2, "L3": 2})


@mutation("订阅周期一律当作年付")
def _m15():
    return _set(calc, "period_months", lambda p: 12)


# --- 第三轮审核新增：财务口径、分类、信任边界 ---------------------------
@mutation("过期财务数据照用（跳过时效检查）")
def _m16():
    return _set(store, "fact_fresh", store.fact)


@mutation("所有财务事实都视为未过期")
def _m17():
    return _set(store, "fact_stale", lambda profile, key: False)


@mutation("云服务器被判成耐用品（不做年化）")
def _m18():
    return _set(calc, "classify_category", lambda category: "durable")


@mutation("汇率不再检查时效")
def _m19():
    return _item(evidence.STALE_DAYS, "fx", 99999)


@mutation("允许外部传入引擎管辖的理由码")
def _m20():
    return _set(mbm, "ENGINE_CODES", set())


@mutation("取消 L1 免证据")
def _m21():
    return _set(verdict, "L1_EXEMPT_EVIDENCE", False)


@mutation("取消硬拦截时的理由数量豁免")
def _m22():
    return _set(verdict, "ENFORCED_WAIVES_REASON_COUNT", False)


@mutation("不检查替代品的预算")
def _m23():
    return _set(calc, "budget_status", lambda price, profile, monthly_equiv=None: {
        "status": "ok", "code": "AFFORDABLE_WITHIN_BUDGET", "detail": "夹具", "snapshot": {}})


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
    for name, (apply, restore) in MUTATIONS:
        apply()
        try:
            results = rr.run_all()
        finally:
            restore()
        fails = [r for r in results if not r["ok"]]
        hit = sorted({r["id"] for r in fails})
        print(f"[{'KILLED' if fails else 'SURVIVED'}] {name}")
        if fails:
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
