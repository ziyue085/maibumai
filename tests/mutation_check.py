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


@mutation("把「换方案」当成可以直接买")
def _m05():
    return _item(verdict.RULE_TIER, "SUBSTITUTE_ADEQUATE", "BUY")


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
    return _set(evidence, "convert_amount",
                lambda value, cur, base, items, as_of=None: float(value))


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


# --- 第四轮审核新增：确认/日期边界、结论与财务门一致、替代品资格 -------------
@mutation("未确认的字段照用（只看保鲜期）")
def _m24():
    def stale(profile, key):
        spec = store.FACT_SPECS.get(key) or {}
        ttl = spec.get("ttl")
        if not ttl:
            return False
        age = store.fact_age(profile, key)
        return False if age is None else age > ttl
    return _set(store, "fact_stale", stale)


@mutation("分级门槛用未经校验的原始值")
def _m25():
    return _set(calc, "threshold_of", lambda profile: 99999)


@mutation("承受力没确认也照常给购买结论")
def _m26():
    # 参数与真实签名一致（5 个）；否则会以 TypeError 被"捕获"，
    # 那是把崩溃误当成断言命中。
    return _set(mbm, "_affordability_unverified",
                lambda fin, level, price, kind=None, period=None: None)


@mutation("替代方案只要有个名字就算合格")
def _m27():
    return _set(verdict, "alternative_problems",
                lambda alt: [] if isinstance(alt, dict) and alt.get("name") else ["ALT_NO_NAME"])


@mutation("替代品的预算状态一律当作没问题")
def _m28():
    orig = mbm._assess_alternative

    def fake(*args, **kwargs):
        out = orig(*args, **kwargs)
        out["problems"] = [p for p in out["problems"]
                           if p not in ("ALT_OVER_BUDGET", "ALT_NEEDS_TIME", "ALT_BUDGET_UNKNOWN")]
        return out
    return _set(mbm, "_assess_alternative", fake)


@mutation("替代品外币价不做换算")
def _m29():
    orig = mbm._assess_alternative

    def fake(*args, **kwargs):
        out = orig(*args, **kwargs)
        raw = (args[0] or {}).get("price") if args else None
        if out.get("price") is None and isinstance(raw, (int, float)):
            out["price"] = float(raw)
            out["problems"] = [p for p in out["problems"] if p != "ALT_PRICE_UNCONVERTED"]
        return out
    return _set(mbm, "_assess_alternative", fake)


@mutation("账本直接写原始输入")
def _m30():
    def raw(draft, result):
        return {
            "type": "decision", "id": draft.get("id") or "fixture",
            "level": result["level"]["level"], "product": draft.get("product"),
            "currency": result["currency"], "verdict": result["verdict"]["verdict"],
            "reasons": draft.get("reasons") or [],
            "enforced": result["verdict"].get("enforced") or [],
            "alternative": draft.get("alternative"), "numbers": result["numbers"],
            "evidence": draft.get("evidence") or [],
            "prediction": draft.get("prediction") or {},
            "recheck_condition": draft.get("recheck_condition"),
            "overridden": False, "notes": "",
        }
    return _set(mbm, "_ledger_record", raw)


@mutation("不检查替代品价格是否有出处")
def _m31():
    orig = mbm._assess_alternative

    def fake(*args, **kwargs):
        out = orig(*args, **kwargs)
        out["problems"] = [p for p in out["problems"] if p != "ALT_PRICE_UNVERIFIED"]
        return out
    return _set(mbm, "_assess_alternative", fake)


@mutation("快判上限可以被放大")
def _m32():
    return _set(calc, "DEFAULT_SMALL", 99999)


@mutation("所有硬拦截都能让位给替代方案（不止钱）")
def _m33():
    return _set(verdict, "MONEY_TIER_CODES",
                tuple(c for c, t in verdict.RULE_TIER.items() if t == "DONT_BUY"))


# --- 第五轮审核新增：等级边界、存量未知、替代品证据与画像时间 -----------------
@mutation("L2 不检查财务是否确认")
def _m34():
    orig = mbm._affordability_unverified
    return _set(mbm, "_affordability_unverified",
                lambda fin, level, price, kind=None, period=None:
                None if level == "L2" else orig(fin, level, price, kind, period))


@mutation("存量未知当成零")
def _m35():
    orig = store.financial_snapshot

    def snap(profile):
        out = orig(profile)
        if out.get("spendable") is None:
            out["spendable"] = 0.0
        return out
    return _set(store, "financial_snapshot", snap)


@mutation("替代品报价只比数值，不查来源与时效")
def _m36():
    def loose(items, converted, as_of, price_class):
        for e in items or []:
            if (isinstance(e, dict) and e.get("kind") in ("price", "promo")
                    and str(e.get("subject") or "").lower() == "alternative"
                    and isinstance(e.get("value"), (int, float))
                    and abs(e["value"] - converted) / converted <= 0.05):
                return None
        return "ALT_PRICE_UNVERIFIED"
    return _set(mbm, "_alt_price_problem", loose)


@mutation("替代品预算不看未确认的结余上界")
def _m37():
    orig = mbm._assess_alternative

    def fake(*args, **kwargs):
        out = orig(*args, **kwargs)
        out["problems"] = [p for p in out["problems"] if p != "ALT_BUDGET_UNKNOWN"]
        return out
    return _set(mbm, "_assess_alternative", fake)


@mutation("写入画像时一律盖今天的日期")
def _m38():
    orig = store._set_fact_raw

    def stamp_today(profile, key, value, note="", source="", confirmed=True, updated_at=None):
        orig(profile, key, value, note=note, source=source, confirmed=confirmed,
             updated_at=store.today_str())
    return _set(store, "_set_fact_raw", stamp_today)


@mutation("未来日期当作有效")
def _m39():
    orig = store.fact_age
    return _set(store, "fact_age",
                lambda profile, key: abs(orig(profile, key) or 0))


def main() -> int:
    baseline = rr.run_all()
    base_fail = [r for r in baseline if not r["ok"]]
    if base_fail:
        print("基线就有失败用例，先修好再谈变异测试：")
        for r in base_fail:
            print(f"  [FAIL] {r['suite']} {r['id']} {r['detail']}")
        return 2

    print(f"基线：{len(baseline)} 条用例全部通过\n")

    survived, crashes_only = [], []
    for name, (apply, restore) in MUTATIONS:
        apply()
        try:
            results = rr.run_all()
        finally:
            restore()
        fails = [r for r in results if not r["ok"]]
        semantic = [r for r in fails if not r.get("error")]
        crashed = [r for r in fails if r.get("error")]

        if semantic:
            hit = sorted({r["id"] for r in semantic})
            print(f"[KILLED] {name}")
            print(f"          被捕获于：{', '.join(hit)}")
            if crashed:
                print(f"          （另有 {len(crashed)} 条因异常失败，不作为捕获依据）")
        elif crashed:
            # 只是把代码改崩了：断言并没有抓到错误行为，算假阳性
            print(f"[SURVIVED·崩溃] {name}")
            print(f"          只有异常，没有语义断言失败：{', '.join(sorted({r['id'] for r in crashed}))}")
            survived.append(name)
            crashes_only.append(name)
        else:
            print(f"[SURVIVED] {name}")
            survived.append(name)

    print("-" * 56)
    print(f"变异 {len(MUTATIONS)} 个，被捕获 {len(MUTATIONS) - len(survived)} 个")
    if crashes_only:
        print(f"其中 {len(crashes_only)} 个只靠崩溃暴露——说明对应用例没有真正断言行为：")
        for s in crashes_only:
            print("  -", s)
    if survived:
        print("未被捕获（说明对应用例缺失）：")
        for s in survived:
            print("  -", s)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
