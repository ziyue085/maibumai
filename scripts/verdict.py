"""规则码 → 结论解析器 + 输出审计。

核心主张：**结论必须是证据的函数，不是模型心情的函数。**

模型负责取证、追问、给理由码；结论由本模块按固定优先级解析。
这样"理由都指向暂缓，结论却写建议购买"这类自相矛盾在结构上无法发生。

两类问题分开报告，不混为一谈：
- ``integrity``  证据是否可核验（脚本在 evidence.py 报，带 gate="integrity"）
- ``coherence``  结论与理由是否自洽（本模块报，带 gate="coherence"）
- ``financial``  预算与硬约束（引擎在 mbm.py 强制注入，带 gate="financial"）
"""

from __future__ import annotations

from evidence import SOURCE_TIERS

# ---------------------------------------------------------------------------
# 规则码表
# ---------------------------------------------------------------------------
RULE_TIER = {
    # 硬拦截：出现即不建议购买
    "DUP_OWNED": "DONT_BUY",
    "BUDGET_BREACH": "DONT_BUY",
    "UNAFFORDABLE_DEBT": "DONT_BUY",
    "SCAM_SIGNAL": "DONT_BUY",
    "PATTERN_REPEAT": "DONT_BUY",
    "CONSTRAINT_VIOLATION": "DONT_BUY",
    # 换个买法：需求成立，但这个商品/这个价格不成立
    "SUBSTITUTE_ADEQUATE": "SWITCH",
    "SPEC_MISMATCH": "SWITCH",
    "PRICE_ABOVE_FAIR": "SWITCH",
    # 暂缓：缺信息 / 时机不对 / 预算节奏不对
    "IMPULSE_SIGNAL": "DEFER",
    "TIMING_ADVERSE": "DEFER",
    "PRICE_STALE": "DEFER",
    "PRICE_UNVERIFIED": "DEFER",
    "INFO_GAP": "DEFER",
    "USAGE_IMPLAUSIBLE": "DEFER",
    "BUDGET_TIGHT": "DEFER",
    # 正向：支撑购买
    "NEED_REAL": "BUY",
    "SUBSTITUTE_INADEQUATE": "BUY",
    "PRICE_FAIR": "BUY",
    "AFFORDABLE_WITHIN_BUDGET": "BUY",
    "EMOTIONAL_VALUE_LEGIT": "BUY",
    "DURABILITY_GAIN": "BUY",
    "LOW_REGRET_RISK": "BUY",
}

RULE_ZH = {
    "DUP_OWNED": "已有可用的同类物品",
    "BUDGET_BREACH": "超过单笔承受上限",
    "UNAFFORDABLE_DEBT": "需要借贷或动用应急金",
    "SCAM_SIGNAL": "渠道或价格存在诈骗/假货信号",
    "PATTERN_REPEAT": "同品类已有多次买了不用的记录",
    "CONSTRAINT_VIOLATION": "违反你设定的硬约束",
    "SUBSTITUTE_ADEQUATE": "更便宜的路径能满足主要需求",
    "SPEC_MISMATCH": "规格与自述用途不匹配",
    "PRICE_ABOVE_FAIR": "现价高于同类合理区间",
    "IMPULSE_SIGNAL": "动机来自价格刺激/稀缺感/他人影响",
    "TIMING_ADVERSE": "换代或大促窗口在即",
    "PRICE_STALE": "价格证据已过期",
    "PRICE_UNVERIFIED": "价格未经核实",
    "INFO_GAP": "关键信息缺失",
    "USAGE_IMPLAUSIBLE": "使用场景与真实使用模式冲突",
    "BUDGET_TIGHT": "需跨月支出，挤占其他消费",
    "NEED_REAL": "需求先于接触商品而存在且具体",
    "SUBSTITUTE_INADEQUATE": "现有替代方案确实不够用",
    "PRICE_FAIR": "现价处于合理区间",
    "AFFORDABLE_WITHIN_BUDGET": "在月可支配预算内",
    "EMOTIONAL_VALUE_LEGIT": "情绪价值明确且能说清",
    "DURABILITY_GAIN": "替换失效旧物，服务周期长",
    "LOW_REGRET_RISK": "可退换或残值高，后悔成本低",
}

TIERS = ("DONT_BUY", "SWITCH", "DEFER", "BUY")

VERDICT_ZH = {
    "BUY": "建议购买",
    "SWITCH": "建议替换方案",
    "DEFER": "建议暂缓",
    "DONT_BUY": "不建议购买",
}

#: 越靠后越"推荐买"。人工驳回只允许往更保守的方向走。
VERDICT_RANK = {"DONT_BUY": 0, "DEFER": 1, "SWITCH": 2, "BUY": 3}
LEVEL_RANK = {"L1": 1, "L2": 2, "L3": 3}

BUY_ANCHORS = ("NEED_REAL", "EMOTIONAL_VALUE_LEGIT", "DURABILITY_GAIN")

MIN_REASONS = {"L1": 1, "L2": 2, "L3": 3}
MIN_NOTE_CHARS = 6

VAGUE_PHRASES = (
    "看你自己", "因人而异", "根据自身情况", "取决于你的需求", "建议综合考虑",
    "量力而行", "自行判断", "看你个人", "适合自己的才是最好的",
)

SUPERLATIVES = ("历史最低", "史上最低", "全网最低", "最低价", "最便宜", "破价", "史低")

#: L1（≤100 元）不要求价格证据：为 30 元的东西要求一条带链接的报价，等于堵死快判通道。
L1_EXEMPT_EVIDENCE = True

#: 引擎已用硬约束否决时，放宽"必须给够理由"。结论已经定了，再要三条理由只会让记录写不进去。
ENFORCED_WAIVES_REASON_COUNT = True


def _reason_items(reasons) -> list:
    """统一成内部结构。``enforced`` 必须保留——它是"这条是引擎判的"的唯一标记，
    丢了就无法在账本里区分人工判断与系统强制。"""
    out = []
    for r in reasons or []:
        if isinstance(r, dict):
            out.append({
                "code": r.get("code"),
                "note": r.get("note") or "",
                "enforced": bool(r.get("enforced")),
            })
        else:
            out.append({"code": str(r), "note": "", "enforced": False})
    return out


def _codes(reasons) -> list:
    return [r["code"] for r in _reason_items(reasons) if r["code"]]


def _valid_alternative(alt) -> bool:
    if not isinstance(alt, dict):
        return False
    return bool(str(alt.get("name") or "").strip())


def resolve(reasons, alternative=None, override=None):
    """按固定优先级把规则码解析成结论。

    优先级：硬拦截 > 换方案 > 暂缓 > 购买。任何一级命中即短路。
    """
    items = _reason_items(reasons)
    codes = [i["code"] for i in items if i["code"]]
    unknown = [c for c in codes if c not in RULE_TIER]
    tiers = {t: [] for t in TIERS}
    enforced = [i for i in items if i.get("enforced")]
    for item in items:
        tier = RULE_TIER.get(item["code"])
        if tier:
            tiers[tier].append(item["code"])

    def result(verdict, note="", overridden=False):
        return {
            "verdict": verdict,
            "verdict_zh": VERDICT_ZH[verdict],
            "matched": {t: sorted(set(v)) for t, v in tiers.items()},
            "note": note,
            "overridden": overridden,
            "enforced": [e["code"] for e in enforced],
            "unknown_codes": unknown,
        }

    base = (
        "DONT_BUY" if tiers["DONT_BUY"]
        else "SWITCH" if tiers["SWITCH"] and _valid_alternative(alternative)
        else "DEFER" if tiers["SWITCH"]
        else "DEFER" if tiers["DEFER"]
        else "BUY" if tiers["BUY"] and any(c in BUY_ANCHORS for c in tiers["BUY"])
        else "DEFER"
    )

    if override and override.get("verdict") in VERDICT_ZH:
        target = override["verdict"]
        # 只能往更保守的方向走：不能靠驳回把"暂缓"变成"购买"
        if VERDICT_RANK[target] > VERDICT_RANK[base]:
            return result(
                base,
                f"驳回被忽略：不能把「{VERDICT_ZH[base]}」上调为「{VERDICT_ZH[target]}」，"
                "要改结论请改理由码",
            )
        if tiers["DONT_BUY"] and target != "DONT_BUY":
            return result("DONT_BUY", "硬拦截不可被驳回：" + "、".join(tiers["DONT_BUY"]))
        return result(target, f"人工下调：{override.get('reason') or '未说明理由'}", overridden=True)

    if tiers["DONT_BUY"]:
        return result("DONT_BUY", "硬拦截：" + "、".join(tiers["DONT_BUY"]))

    if tiers["SWITCH"]:
        if _valid_alternative(alternative):
            return result("SWITCH")
        return result(
            "DEFER", "命中换方案条件但未给出具体替代品，只能先暂缓：" + "、".join(tiers["SWITCH"])
        )

    if tiers["DEFER"]:
        return result("DEFER")

    if tiers["BUY"]:
        if any(c in BUY_ANCHORS for c in tiers["BUY"]):
            return result("BUY")
        return result("DEFER", "只有价格类理由、缺'为什么需要'的锚点，不足以支持购买")

    return result("DEFER", "没有任何有效规则码支撑，按信息不足处理")


# ---------------------------------------------------------------------------
# 输出审计（gate = coherence）
# ---------------------------------------------------------------------------
def _finding(code, severity, message, detail=None):
    return {
        "code": code,
        "severity": severity,
        "gate": "coherence",
        "message": message,
        "detail": detail or {},
    }


def _evidence_kinds(evidence) -> set:
    return {str(e.get("kind") or "") for e in (evidence or []) if isinstance(e, dict)}


def audit(decision, evidence_result=None, computed_level=None, enforced_codes=(), declared_level=None):
    """检查一条决策记录是否自洽、具体、可核验。

    computed_level：引擎按价格与品类算出的等级。
    declared_level：调用方自报的等级。低于 computed 时判为降级绕过。
    enforced_codes：引擎强制注入的码（预算、约束）。只参与一致性判断，
    **不计入理由数量、不参与"是否有具体依据"的检查**——那是模型自己的功课。
    """
    findings = []
    verdict = decision.get("verdict")
    level = decision.get("level") or "L2"
    items = _reason_items(decision.get("reasons"))
    own_codes = [i["code"] for i in items if i["code"]]
    codes = list(dict.fromkeys(own_codes + [c for c in enforced_codes if c]))
    product = decision.get("product") or {}
    evidence = decision.get("evidence") or []
    alt = decision.get("alternative")

    if verdict not in VERDICT_ZH:
        findings.append(_finding("AUD_VERDICT_INVALID", "BLOCK", f"结论必须是四选一，收到 {verdict!r}"))
        return _pack(findings)

    claimed = declared_level or level
    if computed_level and claimed in LEVEL_RANK and computed_level in LEVEL_RANK:
        if LEVEL_RANK[claimed] < LEVEL_RANK[computed_level]:
            findings.append(
                _finding(
                    "AUD_LEVEL_DOWNGRADED", "BLOCK",
                    f"声明等级 {claimed} 低于系统判定 {computed_level}：不能靠降低等级绕过审核",
                )
            )
    if level not in LEVEL_RANK:
        findings.append(_finding("AUD_LEVEL_INVALID", "BLOCK", f"未知等级 {level!r}"))

    unknown = [c for c in codes if c not in RULE_TIER]
    if unknown:
        findings.append(
            _finding("AUD_REASON_UNKNOWN", "BLOCK", f"存在未定义的规则码：{unknown}", {"codes": unknown})
        )

    minimum = MIN_REASONS.get(level, 3)
    # 否决类结论只需要一条站得住的硬理由：结论已经被硬约束定死了，
    # 再要求凑三条只会让记录写不进去（引擎判的和模型判的都一样）。
    hard_enforced = bool(ENFORCED_WAIVES_REASON_COUNT) and (
        verdict == "DONT_BUY"
        or any(RULE_TIER.get(c) == "DONT_BUY" for c in enforced_codes)
    )
    if hard_enforced:
        minimum = min(minimum, 1)
    own_known = {c for c in own_codes if c in RULE_TIER}
    if len(own_known) < minimum:
        findings.append(
            _finding(
                "AUD_REASONS_FEW", "WARN" if level == "L1" else "BLOCK",
                f"{level} 级决策至少需要 {minimum} 条自己给出的理由，当前 {len(own_known)} 条"
                "（引擎自动判定的预算项不算）",
            )
        )

    # 只有码、没有具体依据 → 不可复核，也不算理由
    if level != "L1":
        empty = [
            i["code"] for i in items
            if i["code"] in RULE_TIER
            and len(str(i["note"]).strip()) < MIN_NOTE_CHARS
        ]
        if empty:
            findings.append(
                _finding(
                    "AUD_REASON_NO_NOTE", "BLOCK",
                    f"这些理由只有码没有具体依据（至少 {MIN_NOTE_CHARS} 字）：{sorted(set(empty))}",
                    {"codes": sorted(set(empty))},
                )
            )
        echoed = [
            i["code"] for i in items
            if i["code"] in RULE_ZH and str(i["note"]).strip() == RULE_ZH[i["code"]]
        ]
        if echoed:
            findings.append(
                _finding("AUD_REASON_ECHO", "BLOCK",
                         f"理由只是把规则名重复一遍，没有具体事实：{sorted(set(echoed))}")
            )

    tiers = {t: [c for c in codes if RULE_TIER.get(c) == t] for t in TIERS}

    if tiers["DONT_BUY"] and verdict != "DONT_BUY":
        findings.append(
            _finding("AUD_TIER_CONFLICT", "BLOCK",
                     f"存在硬拦截 {tiers['DONT_BUY']}，结论只能是「不建议购买」")
        )
    if verdict == "DONT_BUY" and not tiers["DONT_BUY"]:
        findings.append(
            _finding("AUD_DONTBUY_NO_BLOCK", "BLOCK", "不建议购买必须由至少一条硬拦截理由支撑")
        )
    if tiers["SWITCH"] and not tiers["DONT_BUY"] and verdict != "SWITCH":
        downgraded_for_missing_alt = verdict == "DEFER" and not _valid_alternative(alt)
        if not downgraded_for_missing_alt:
            findings.append(
                _finding("AUD_TIER_CONFLICT", "BLOCK",
                         f"存在换方案理由 {tiers['SWITCH']}，结论应为「建议替换方案」")
            )
    if verdict == "BUY" and any(RULE_TIER.get(c) == "DEFER" for c in codes):
        findings.append(
            _finding("AUD_TIER_CONFLICT", "BLOCK",
                     f"存在暂缓类理由 {tiers['DEFER']}，不能给出建议购买")
        )
    if verdict == "BUY" and not any(c in BUY_ANCHORS for c in codes):
        findings.append(
            _finding("AUD_BUY_NO_ANCHOR", "BLOCK",
                     "建议购买缺少 NEED_REAL / EMOTIONAL_VALUE_LEGIT / DURABILITY_GAIN 之一")
        )

    if verdict == "SWITCH":
        if not _valid_alternative(alt):
            findings.append(_finding("AUD_SWITCH_NO_ALT", "BLOCK", "建议替换方案必须给出替代品的名称"))
        else:
            if not isinstance(alt.get("price"), (int, float)) or alt.get("price") <= 0:
                if not str(alt.get("price_note") or "").strip():
                    findings.append(
                        _finding("AUD_ALT_NO_PRICE", "BLOCK",
                                 "替代方案没有价格：用户无法判断换过去是否真的省钱")
                    )
            if not str(alt.get("source") or "").strip():
                findings.append(
                    _finding("AUD_ALT_NO_SOURCE", "BLOCK", "替代方案没有来源：无法核实是否真的存在")
                )
            # 替代品也要过同一套预算与证据检查——否则"换方案"可能换成更买不起的
            alt_info = (decision.get("numbers") or {}).get("alternative") or {}
            if (alt_info.get("budget") or {}).get("status") == "breach":
                findings.append(
                    _finding(
                        "AUD_ALT_OVER_BUDGET", "BLOCK",
                        "替代方案同样超出你的承受能力，换过去并不能解决问题",
                        {"budget": alt_info.get("budget")},
                    )
                )
            alt_price = alt.get("price")
            if isinstance(alt_price, (int, float)) and alt_price > 0:
                supported = any(
                    isinstance(e, dict)
                    and e.get("kind") in ("price", "promo")
                    and str(e.get("subject") or "target").strip().lower() in ("alternative", "reference")
                    and isinstance(e.get("value"), (int, float))
                    and abs(e["value"] - alt_price) / alt_price <= 0.05
                    for e in evidence
                )
                if not supported:
                    findings.append(
                        _finding(
                            "AUD_ALT_UNVERIFIED_PRICE", "WARN",
                            "替代品的价格没有独立证据支撑，建议补一条带链接的报价",
                        )
                    )
            if alt_info.get("period_missing"):
                findings.append(
                    _finding("AUD_ALT_PERIOD_MISSING", "WARN",
                             "替代品是订阅但没写计费周期，年化成本无法比较")
                )

    if verdict == "DEFER" and not str(decision.get("recheck_condition") or "").strip():
        findings.append(
            _finding("AUD_DEFER_NO_RECHECK", "BLOCK",
                     "暂缓必须给出复查条件（时点或触发条件），否则等于没答")
        )

    kinds = _evidence_kinds(evidence)
    ev = evidence_result or {}
    price = product.get("price")
    # L1 是"30 块钱的东西不用查证"，不要求价格证据；否则快判通道形同虚设
    is_l1 = level == "L1" and bool(L1_EXEMPT_EVIDENCE)
    if verdict in ("BUY", "SWITCH"):
        price_items = [
            e for e in evidence
            if isinstance(e, dict)
            and e.get("kind") in ("price", "promo")
            and str(e.get("subject") or "target").strip().lower() == "target"
        ]
        if not isinstance(price, (int, float)) or price <= 0:
            findings.append(
                _finding("AUD_BUY_NO_PRICE", "BLOCK", "购买类结论必须知道确切价格")
            )
        if not is_l1:
            if not price_items:
                findings.append(_finding("AUD_NO_PRICE_EVIDENCE", "BLOCK", "购买类结论必须附价格证据"))
            else:
                best_tier = min(SOURCE_TIERS.get(str(e.get("source_type") or ""), 5) for e in price_items)
                if best_tier > 3:
                    findings.append(
                        _finding("AUD_PRICE_UNVERIFIED", "BLOCK",
                                 "价格只有用户口述或社区来源：不能据此断言「价格合理」，须先查到公开报价")
                    )
            if ev and not ev.get("ok", True):
                findings.append(
                    _finding("AUD_EVIDENCE_BLOCKING", "BLOCK",
                             f"证据校验未通过（{ev.get('blocking', 0)} 项阻断）")
                )

    if isinstance(price, (int, float)) and price > 0 and "price" in kinds:
        ev_prices = [
            e.get("value") for e in evidence
            if e.get("kind") in ("price", "promo")
            and str(e.get("subject") or "target").strip().lower() == "target"
            and isinstance(e.get("value"), (int, float)) and e.get("value") > 0
        ]
        if ev_prices:
            near = [p for p in ev_prices if abs(p - price) / price <= 0.05]
            if not near:
                findings.append(
                    _finding(
                        "AUD_PRICE_MISMATCH", "BLOCK" if verdict in ("BUY", "SWITCH") else "WARN",
                        f"商品价格 {price} 与证据价格 {ev_prices} 不一致（>5%），须说明口径",
                        {"stated": price, "evidence": ev_prices},
                    )
                )

    numbers = decision.get("numbers") or {}
    if numbers.get("cost_per_use") is not None and not (
        isinstance(price, (int, float))
        and isinstance((decision.get("prediction") or {}).get("uses_per_month"), (int, float))
    ):
        findings.append(
            _finding("AUD_NUMBER_UNSUPPORTED", "BLOCK", "单次成本缺少价格或使用频率输入，数字不可推导")
        )

    if verdict in ("BUY", "SWITCH") and numbers.get("annual_cost") is None:
        kind_note = str(decision.get("kind") or "")
        if kind_note == "subscription" or "订阅" in str(product.get("category") or ""):
            findings.append(
                _finding("AUD_SUBSCRIPTION_PERIOD", "BLOCK",
                         "订阅类必须说明计费周期（month / quarter / year），否则年度成本不可信")
            )

    # 已有资源：不否决，但不能装作没看见
    res = (decision.get("numbers") or {}).get("resources") or {}
    similar = res.get("similar") or []
    if verdict == "BUY" and similar:
        addressed = {"DUP_OWNED", "SUBSTITUTE_ADEQUATE", "SUBSTITUTE_INADEQUATE"} & set(codes)
        if not addressed:
            names = "、".join(str(s.get("name")) for s in similar[:3])
            findings.append(
                _finding(
                    "AUD_RESOURCE_IGNORED", "WARN",
                    f"你已经有同类资源（{names}），但结论里没有回应『是否重复』",
                    {"similar": similar},
                )
            )

    text = " ".join(
        [str(decision.get("summary") or ""), str(decision.get("advice") or "")]
        + [str(i["note"]) for i in items]
    )
    hit = [p for p in VAGUE_PHRASES if p in text]
    if hit:
        findings.append(_finding("AUD_VAGUE_PHRASE", "BLOCK", f"出现把判断推回给用户的表述：{hit}", {"hits": hit}))

    sup = [s for s in SUPERLATIVES if s in text]
    if sup and "price_history" not in kinds:
        findings.append(
            _finding("AUD_UNSUPPORTED_SUPERLATIVE", "BLOCK",
                     f"使用了最高级表述 {sup} 但没有历史价格证据", {"hits": sup})
        )

    return _pack(findings)


def _pack(findings):
    blocking = sum(1 for f in findings if f["severity"] == "BLOCK")
    return {
        "ok": blocking == 0,
        "blocking": blocking,
        "warnings": sum(1 for f in findings if f["severity"] == "WARN"),
        "findings": findings,
    }
