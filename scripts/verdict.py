"""规则码 → 结论解析器 + 输出审计。

核心主张：**结论必须是证据的函数，不是模型心情的函数。**

模型负责取证、追问、给理由码；结论由本模块按固定优先级解析。
这样"理由都指向暂缓，结论却写建议购买"这类自相矛盾在结构上无法发生。
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

#: 给出"建议购买"必须至少命中一个"为什么值得"的锚点。
BUY_ANCHORS = ("NEED_REAL", "EMOTIONAL_VALUE_LEGIT", "DURABILITY_GAIN")

#: 结论必须带的具体化要求。宁可 3 条实的，不要 5 条凑的。
MIN_REASONS = {"L1": 1, "L2": 2, "L3": 3}

#: 典型的"把判断推回给用户"的废话。出现即不合格。
VAGUE_PHRASES = (
    "看你自己",
    "因人而异",
    "根据自身情况",
    "取决于你的需求",
    "建议综合考虑",
    "量力而行",
    "自行判断",
    "看你个人",
    "适合自己的才是最好的",
)

#: 无价格历史数据时不得使用的最高级表述。
SUPERLATIVES = ("历史最低", "史上最低", "全网最低", "最低价", "最便宜", "破价", "史低")


def _codes(reasons) -> list:
    out = []
    for r in reasons or []:
        code = r.get("code") if isinstance(r, dict) else str(r)
        if code:
            out.append(code)
    return out


def _valid_alternative(alt) -> bool:
    if not isinstance(alt, dict):
        return False
    name = str(alt.get("name") or "").strip()
    return bool(name)


def resolve(reasons, alternative=None, override=None):
    """按固定优先级把规则码解析成结论。

    优先级：硬拦截 > 换方案 > 暂缓 > 购买。
    任何一级命中即短路，后面的正向理由不能翻盘。

    返回 dict：verdict / matched / note / overridden
    """
    codes = _codes(reasons)
    unknown = [c for c in codes if c not in RULE_TIER]
    tiers = {t: [] for t in TIERS}
    for code in codes:
        tier = RULE_TIER.get(code)
        if tier:
            tiers[tier].append(code)

    def result(verdict, note="", overridden=False):
        return {
            "verdict": verdict,
            "verdict_zh": VERDICT_ZH[verdict],
            "matched": {t: sorted(set(v)) for t, v in tiers.items()},
            "note": note,
            "overridden": overridden,
            "unknown_codes": unknown,
        }

    if override and override.get("verdict") in VERDICT_ZH:
        if tiers["DONT_BUY"]:
            return result("DONT_BUY", "硬拦截不可被驳回：" + "、".join(tiers["DONT_BUY"]))
        return result(
            override["verdict"],
            f"人工驳回：{override.get('reason') or '未说明理由'}",
            overridden=True,
        )

    if tiers["DONT_BUY"]:
        return result("DONT_BUY", "硬拦截：" + "、".join(tiers["DONT_BUY"]))

    if tiers["SWITCH"]:
        if _valid_alternative(alternative):
            return result("SWITCH")
        return result(
            "DEFER",
            "命中换方案条件但未给出具体替代品，只能先暂缓：" + "、".join(tiers["SWITCH"]),
        )

    if tiers["DEFER"]:
        return result("DEFER")

    if tiers["BUY"]:
        if any(c in BUY_ANCHORS for c in tiers["BUY"]):
            return result("BUY")
        return result("DEFER", "只有价格类理由、缺'为什么需要'的锚点，不足以支持购买")

    return result("DEFER", "没有任何有效规则码支撑，按信息不足处理")


# ---------------------------------------------------------------------------
# 输出审计
# ---------------------------------------------------------------------------
def _finding(code, severity, message, detail=None):
    return {"code": code, "severity": severity, "message": message, "detail": detail or {}}


def _evidence_kinds(evidence) -> set:
    return {str(e.get("kind") or "") for e in (evidence or []) if isinstance(e, dict)}


def audit(decision, evidence_result=None):
    """检查一条决策记录是否自洽、具体、可核验。

    参数
    ----
    decision : dict
        至少含 verdict / reasons / level / product / evidence。
    evidence_result : dict | None
        ``evidence.check()`` 的返回；传入则复用其结论，避免二次误判。
    """
    findings = []
    verdict = decision.get("verdict")
    level = decision.get("level") or "L2"
    reasons = decision.get("reasons") or []
    codes = _codes(reasons)
    product = decision.get("product") or {}
    evidence = decision.get("evidence") or []
    alt = decision.get("alternative")

    if verdict not in VERDICT_ZH:
        findings.append(_finding("AUD_VERDICT_INVALID", "BLOCK", f"结论必须是四选一，收到 {verdict!r}"))
        return _pack(findings)

    unknown = [c for c in codes if c not in RULE_TIER]
    if unknown:
        findings.append(
            _finding("AUD_REASON_UNKNOWN", "BLOCK", f"存在未定义的规则码：{unknown}", {"codes": unknown})
        )

    minimum = MIN_REASONS.get(level, 3)
    if len(set(codes)) < minimum:
        findings.append(
            _finding(
                "AUD_REASONS_FEW",
                "WARN" if level == "L1" else "BLOCK",
                f"{level} 级决策至少需要 {minimum} 条不同理由，当前 {len(set(codes))} 条",
            )
        )

    tiers = {t: [c for c in codes if RULE_TIER.get(c) == t] for t in TIERS}

    # 结论必须与理由的层级一致
    if tiers["DONT_BUY"] and verdict != "DONT_BUY":
        findings.append(
            _finding(
                "AUD_TIER_CONFLICT",
                "BLOCK",
                f"存在硬拦截 {tiers['DONT_BUY']}，结论只能是「不建议购买」",
            )
        )
    if verdict == "DONT_BUY" and not tiers["DONT_BUY"]:
        findings.append(
            _finding("AUD_DONTBUY_NO_BLOCK", "BLOCK", "不建议购买必须由至少一条硬拦截理由支撑")
        )
    if tiers["SWITCH"] and not tiers["DONT_BUY"] and verdict != "SWITCH":
        downgraded_for_missing_alt = verdict == "DEFER" and not _valid_alternative(alt)
        if not downgraded_for_missing_alt:
            findings.append(
                _finding(
                    "AUD_TIER_CONFLICT",
                    "BLOCK",
                    f"存在换方案理由 {tiers['SWITCH']}，结论应为「建议替换方案」",
                )
            )
    if verdict == "BUY" and tier_has_defer(codes):
        findings.append(
            _finding("AUD_TIER_CONFLICT", "BLOCK", f"存在暂缓类理由 {tiers['DEFER']}，不能给出建议购买")
        )
    if verdict == "BUY" and not any(c in BUY_ANCHORS for c in codes):
        findings.append(
            _finding("AUD_BUY_NO_ANCHOR", "BLOCK", "建议购买缺少 NEED_REAL / EMOTIONAL_VALUE_LEGIT / DURABILITY_GAIN 之一")
        )

    if verdict == "SWITCH" and not _valid_alternative(alt):
        findings.append(_finding("AUD_SWITCH_NO_ALT", "BLOCK", "建议替换方案必须给出替代品的名称"))
    if verdict == "SWITCH" and isinstance(alt, dict):
        if not alt.get("price") and not alt.get("price_note"):
            findings.append(_finding("AUD_ALT_NO_PRICE", "WARN", "替代方案没有价格，用户无法比较"))
        if not alt.get("source"):
            findings.append(_finding("AUD_ALT_NO_SOURCE", "WARN", "替代方案没有来源，无法核实"))

    if verdict == "DEFER":
        cond = str(decision.get("recheck_condition") or "").strip()
        if not cond:
            findings.append(
                _finding("AUD_DEFER_NO_RECHECK", "BLOCK", "暂缓必须给出复查条件（时点或触发条件），否则等于没答")
            )

    # 价格主张必须可核验
    kinds = _evidence_kinds(evidence)
    ev = evidence_result or {}
    if verdict in ("BUY", "SWITCH"):
        price_items = [
            e for e in evidence if isinstance(e, dict) and e.get("kind") in ("price", "promo")
        ]
        if not price_items:
            findings.append(_finding("AUD_NO_PRICE_EVIDENCE", "BLOCK", "购买类结论必须附价格证据"))
        else:
            best_tier = min(SOURCE_TIERS.get(str(e.get("source_type") or ""), 5) for e in price_items)
            if best_tier > 3:
                findings.append(
                    _finding(
                        "AUD_PRICE_UNVERIFIED",
                        "BLOCK",
                        "价格只有用户口述或社区来源：不能据此断言“价格合理”，须先查到公开报价",
                    )
                )
        if ev and not ev.get("ok", True):
            findings.append(
                _finding("AUD_EVIDENCE_BLOCKING", "BLOCK", f"证据校验未通过（{ev.get('blocking', 0)} 项阻断）")
            )

    stated = product.get("price")
    if isinstance(stated, (int, float)) and "price" in kinds:
        ev_prices = [
            e.get("value")
            for e in evidence
            if e.get("kind") in ("price", "promo") and isinstance(e.get("value"), (int, float))
        ]
        if ev_prices:
            near = [p for p in ev_prices if stated and abs(p - stated) / max(stated, 1) <= 0.05]
            if not near:
                findings.append(
                    _finding(
                        "AUD_PRICE_MISMATCH",
                        "BLOCK",
                        f"商品价格 {stated} 与证据价格 {ev_prices} 不一致（>5%），须说明口径",
                        {"stated": stated, "evidence": ev_prices},
                    )
                )

    numbers = decision.get("numbers") or {}
    if numbers:
        if numbers.get("cost_per_use") is not None and not (
            isinstance(product.get("price"), (int, float))
            and isinstance((decision.get("prediction") or {}).get("uses_per_month"), (int, float))
        ):
            findings.append(
                _finding("AUD_NUMBER_UNSUPPORTED", "BLOCK", "单次成本缺少价格或使用频率输入，数字不可推导")
            )

    text = " ".join(
        [str(decision.get("summary") or ""), str(decision.get("advice") or "")]
        + [str(r.get("note") or "") for r in reasons if isinstance(r, dict)]
    )
    hit = [p for p in VAGUE_PHRASES if p in text]
    if hit:
        findings.append(_finding("AUD_VAGUE_PHRASE", "BLOCK", f"出现把判断推回给用户的表述：{hit}", {"hits": hit}))

    sup = [s for s in SUPERLATIVES if s in text]
    if sup and "price_history" not in kinds:
        findings.append(
            _finding(
                "AUD_UNSUPPORTED_SUPERLATIVE",
                "BLOCK",
                f"使用了最高级表述 {sup} 但没有历史价格证据",
                {"hits": sup},
            )
        )

    return _pack(findings)


def tier_has_defer(codes) -> bool:
    return any(RULE_TIER.get(c) == "DEFER" for c in codes)


def _pack(findings):
    blocking = sum(1 for f in findings if f["severity"] == "BLOCK")
    return {
        "ok": blocking == 0,
        "blocking": blocking,
        "warnings": sum(1 for f in findings if f["severity"] == "WARN"),
        "findings": findings,
    }
