#!/usr/bin/env python3
"""买不买 AI — 命令行入口。

典型用法（模型侧只需要这几条）：

    python scripts/mbm.py brief                    # 一页画像 + 已有物品
    python scripts/mbm.py profile gaps --limit 3   # 该补/该确认什么
    python scripts/mbm.py judge --input draft.json # 判定并落账（一步）
    python scripts/mbm.py outcome <id> --bought yes
    python scripts/mbm.py review  <id> --input review.json
    python scripts/mbm.py due
    python scripts/mbm.py stats

所有命令默认输出 JSON；``--human`` 输出中文摘要。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calc          # noqa: E402
import evidence as ev  # noqa: E402
import store         # noqa: E402
import verdict as vd  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass


#: 这些码由引擎判定，外部传入一律丢弃——否则模型可以自己"宣布"预算结论，
#: 也可以把自己的理由标成 enforced 来躲开理由数量与依据检查。
ENGINE_CODES = {
    "BUDGET_BREACH", "BUDGET_TIGHT", "AFFORDABLE_WITHIN_BUDGET",
    "CONSTRAINT_VIOLATION", "BUDGET_UNVERIFIED",
}

#: 结余是按"储蓄/投资未填"估出来的时候，留这么多余量才算还能接受。
#: 缺的两项要吃掉一半以上结余才会翻转结论，那时就不该再断言"买得起"。
ESTIMATE_TOLERANCE = 0.5


def _out(data, human: bool = False, human_fn=None) -> int:
    if human and human_fn:
        print(human_fn(data))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def _load_input(path: str | None) -> dict:
    if not path or path == "-":
        raw = sys.stdin.read()
    else:
        p = Path(path).expanduser()
        if not p.is_file():
            raise SystemExit(f"找不到输入文件：{p}\n提示：用 --input 指定 JSON 草稿路径，或传 - 从标准输入读。")
        raw = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"输入不是合法 JSON：{exc}\n行 {exc.lineno} 列 {exc.colno}")
    if not isinstance(data, dict):
        raise SystemExit("输入必须是 JSON 对象")
    return data


def _append_findings(result: dict, findings) -> None:
    existing = {f["code"] for f in result.get("findings") or []}
    incoming = [f for f in findings if f["code"] not in existing]
    result["findings"] = list(result.get("findings") or []) + incoming
    result["blocking"] = sum(1 for f in result["findings"] if f["severity"] == "BLOCK")
    result["warnings"] = sum(1 for f in result["findings"] if f["severity"] == "WARN")
    result["ok"] = result["blocking"] == 0


#: 报价审核终态 → 替代品该报的问题码。**目标商品和替代品共用** ev.assess_quotations，
#: 只有"出了该怎么说"这一层不同，不允许两边各写一套判断标准。
ALT_STATUS_PROBLEM = {
    "NO_EVIDENCE": "ALT_PRICE_UNVERIFIED",
    "NO_CREDIBLE": "ALT_PRICE_WEAK_SOURCE",
    "NO_LINK": "ALT_PRICE_UNVERIFIED",
    "STALE": "ALT_PRICE_STALE",
    "IDENTITY_AMBIGUOUS": "ALT_IDENTITY_AMBIGUOUS",
    "CONFLICT": "ALT_PRICE_CONFLICT",
    "BASIS_AMBIGUOUS": "ALT_PRICE_BASIS_AMBIGUOUS",
    "NO_VALUE": "ALT_PRICE_UNVERIFIED",
    "MISMATCH": "ALT_PRICE_MISMATCH",
}

ALT_PAGE_PROBLEM = {
    "PAGE_PRICE_INVALID": "ALT_PAGE_PRICE_INVALID",
    "QUOTE_CONTENT_MISMATCH": "ALT_PRICE_CONTENT_MISMATCH",
    "PAGE_TEXT_HASH_MISMATCH": "ALT_PAGE_TEXT_HASH_MISMATCH",
    "PAGE_TEXT_HASH_UNVERIFIABLE": "ALT_PAGE_TEXT_HASH_UNVERIFIABLE",
    "PAGE_TEXT_HASH_REQUIRED": "ALT_PAGE_TEXT_HASH_REQUIRED",
    "PAGE_TEXT_NO_QUOTE": "ALT_PAGE_TEXT_NO_QUOTE",
    "PAGE_TEXT_NOT_A_PRICE": "ALT_PAGE_TEXT_NOT_A_PRICE",
    "PAGE_TEXT_PRICE_AMBIGUOUS": "ALT_PAGE_TEXT_PRICE_AMBIGUOUS",
}


def _quotation_problems(assessment: dict, prefix: str) -> list:
    """把共用审核结果翻成"这一侧"的问题码。目标侧用 ``from``，替代侧用 ALT_*。"""
    status = assessment["status"]
    if status == "OK":
        return []
    if status == "PAGE_PROBLEM":
        codes = [ALT_PAGE_PROBLEM.get(p["problem"], "ALT_PRICE_UNVERIFIED")
                 for p in assessment["page_problems"]]
        return [c if c.startswith("ALT_") else prefix + c for c in codes]
    mapped = ALT_STATUS_PROBLEM.get(status)
    if not mapped:
        return [prefix + status]
    return [mapped if mapped.startswith("ALT_") else prefix + mapped]


def _alt_price_check(items, converted, as_of, price_class, requested=None) -> dict:
    """替代品报价的出处是否站得住，以及**核实到哪一层**。

    ⚠️ 这条路径**不再先按输入价 ±5% 筛选证据**。以前那样做会把真正冲突的
    官方报价直接筛掉：官方 899 与官方 1299 并存、用户填 899 时，
    1299 因为"离输入价太远"被排除，冲突就消失了，替代品于是"没问题"。
    现在和目标商品共用 :func:`evidence.assess_quotations`，顺序一致：

    身份 → 来源 → 时效 → 价格语义 → 规格/地区 → 口径分组 → 同级冲突 →
    到手价 → **最后**才与用户填的价比对。

    核验等级（逐层加严，不许混为一谈）：

    | 等级 | 含义 | 谁做的 | 引擎能复核吗 |
    |---|---|---|---|
    | `source` | 来源等级 ≤ 3、有链接、在保鲜期内 | 本函数（查字段） | 能 |
    | `reported` | 只填了 `page_price` 或 `page_price_set`，没有正文 | 取证方自述 | **不能** |
    | `content` | 给了页面正文，正文里这个数确实以**价格**身份出现 | 本函数（重算 + 语义） | 能 |

    `source` 只能证明"这个价有个看得过去的出处"，
    **不能**证明链接打得开、域名是官方、页面写着这个数。
    """
    assessment = ev.assess_quotations(
        items, "alternative", as_of=as_of, price_class=price_class,
        requested=requested, stated_price=converted,
        mismatch_tolerance=PRICE_MISMATCH_TOLERANCE,
    )
    problems = _quotation_problems(assessment, "ALT_")
    if assessment["status"] == "OK" and not assessment["page_level"]:
        problems.append("ALT_PRICE_UNVERIFIED")
    return {"problem": None, "problems": problems,
            "verified": ev.page_level_for(assessment),
            "status": assessment["status"],
            "settlement_price": assessment["settlement_price"],
            "assessment": assessment}


def _assess_alternative(alt_raw, product, profile, items, base, as_of) -> dict:
    """替代方案是否够格支撑一条 SWITCH。

    四件必须同时成立：**结构完整**（名称/价格/来源/周期）、**币种可比**、
    **预算拿得出**、**价格有据**。少一条就只能暂缓——推荐一个买不起或
    有价无据的东西，比不推荐更糟。
    """
    alt = dict(alt_raw or {})
    problems = vd.alternative_problems(alt)

    raw_price = alt.get("price")
    has_price = isinstance(raw_price, (int, float)) and not isinstance(raw_price, bool) \
        and raw_price > 0
    # 没写币种时按商品原币种处理：同一个页面上的报价通常同币种
    orig_currency = str(alt.get("currency") or product.get("currency") or base).upper()
    converted = None
    if has_price:
        converted = ev.convert_amount(raw_price, orig_currency, base, items, as_of)
        if converted is None:
            problems.append("ALT_PRICE_UNCONVERTED")

    alt_kind = calc.classify_category(alt.get("category") or product.get("category"))
    alt_period = alt.get("period")
    period_missing = alt_kind == "subscription" and calc.period_months(alt_period) is None
    if period_missing:
        problems.append("ALT_PERIOD_MISSING")

    budget = None
    verified_level = None
    if isinstance(converted, (int, float)) and converted > 0:
        budget_price, monthly = converted, None
        if alt_kind == "subscription":
            pm = calc.period_months(alt_period)
            if pm:
                budget_price = converted * 12 / pm
                monthly = converted / pm
        ab = calc.budget_status(budget_price, profile, monthly_equiv=monthly)
        budget = {
            "status": ab["status"], "code": ab["code"], "detail": ab["detail"],
            "basis": ab.get("basis"),
        }
        if ab["status"] == "breach":
            problems.append("ALT_OVER_BUDGET")
        elif ab["status"] == "tight":
            problems.append("ALT_NEEDS_TIME")
        elif ab["status"] == "unknown":
            problems.append("ALT_BUDGET_UNKNOWN")
        elif (ab.get("basis") == "monthly"
              and (ab.get("snapshot") or {}).get("surplus_basis") == "estimated"):
            # 替代品"买得起"是拿上界结余算出来的：缺的储蓄/投资只要吃掉一点就翻转
            share = ab.get("share")
            if share is None or share > ESTIMATE_TOLERANCE:
                problems.append("ALT_BUDGET_UNKNOWN")

        price_check = _alt_price_check(items, converted, as_of,
                                       calc.price_class(product.get("category")),
                                       requested=ev.requested_identity(alt))
        problems.extend(price_check.get("problems") or [])
        verified_level = price_check["verified"]

    return {
        "name": alt.get("name"),
        "price": converted,
        "original_price": raw_price if has_price else None,
        "currency": orig_currency,
        "kind": alt_kind,
        "period": alt_period,
        "period_missing": period_missing,
        "source": alt.get("source"),
        "budget": budget,
        "price_verified": verified_level,
        "annual_cost": calc.annual_of(converted, alt_period)
        if (alt_kind == "subscription" and isinstance(converted, (int, float))) else None,
        "problems": problems,
        "qualified": not problems,
        "normalized": {**alt, "price": converted, "original_price": raw_price
                       if has_price else None, "currency": orig_currency},
    }


#: 证据层的阻断码 → 结论层应该用的理由码。
#: 目标价格站不住时，不该输出"买这个/换成这个"——比较的基础本身就不成立。
#: 输入价与证据价的可接受偏差。超过就是两个不同的数字，不是"口径小差异"。
PRICE_MISMATCH_TOLERANCE = 0.05

PRICE_DEFECT_CODES = {
    "EV_STALE": "PRICE_STALE",
    "EV_NO_DATE": "PRICE_STALE",
    "EV_FUTURE_DATE": "PRICE_STALE",
    "EV_NO_SOURCE": "PRICE_UNVERIFIED",
    "EV_BAD_URL": "PRICE_UNVERIFIED",
    "EV_NO_SOURCE_TYPE": "PRICE_UNVERIFIED",
    "EV_PRICE_VALUE_INVALID": "PRICE_UNVERIFIED",
    "EV_CURRENCY_UNCONVERTED": "PRICE_UNVERIFIED",
    "EV_BAD_DATE": "PRICE_STALE",
    "EV_QUOTE_CONTENT_MISMATCH": "PRICE_UNVERIFIED",
    "EV_PAGE_PRICE_INVALID": "PRICE_UNVERIFIED",
    "EV_PAGE_TEXT_HASH_MISMATCH": "PRICE_UNVERIFIED",
    "EV_PAGE_TEXT_NO_QUOTE": "PRICE_UNVERIFIED",
}


def _target_price_defect(ev_result, items, price, target_prices, as_of=None,
                         price_class="durable", product=None) -> dict | None:
    """目标商品的价格证据有没有硬伤。有就返回该用的理由码。

    只看 ``subject=target`` 的价格类证据：替代品的证据缺陷由替代品自己承担，
    不能拿它去否决目标商品的结论。

    与替代品**共用** :func:`evidence.assess_quotations`，顺序固定为
    「身份 → 来源 → 时效 → 价格语义 → 规格/地区 → 口径分组 → 同级冲突 →
    到手价 → 输入价比对」；两边只差"出问题了该怎么说"。

    七类硬伤，任何一类都让"买这个/换成这个"失去前提：

    1. **根本没有报价**——`EV_MISSING_KIND` 不带证据索引，不能靠遍历 findings 发现；
    2. 报价自身不成立（过期、无日期、未来日期、无链接、数值非法）；
    3. 来源只有口述或社区（等级 > 3）；
    4. **规格/地区/型号没说清**，分不出该拿哪个价去比；
    5. **同一身份、同一口径**的报价互相打架——谁都不能被采信；
    6. 页面证据自相矛盾（页面价与引用价不符、正文哈希对不上、
       正文里这个数不是价格）——**只看可信来源的**，低可信证据不参与否决；
    7. 输入价与**到手价口径**的报价相差超过 5%——两个数字里必有一个是错的。

    关于第 5 条与第 7 条：官方标价 1099 与授权商城促销 899 可以同时为真，
    那不是"数据打架"，是口径不同；只有身份与口径都对得上的报价，
    差 5% 以上才算真冲突。比输入价时也只比**到手价口径**的那一个
    （含税到手 > 实际成交 > 促销 > 官方标价 > 不含税）。
    """
    if not target_prices:
        return {
            "code": "PRICE_UNVERIFIED",
            "why": "没有任何目标商品的价格证据：现有的价只是转述，不是核实过的报价",
            "from": "NO_PRICE_EVIDENCE",
        }

    for f in ev_result.get("findings") or []:
        if f.get("severity") != "BLOCK":
            continue
        idx = (f.get("detail") or {}).get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(items)):
            continue
        item = items[idx]
        if str(item.get("subject") or "target").strip().lower() != "target":
            continue
        if item.get("kind") not in ("price", "promo"):
            continue
        # 低可信来源的问题在 check() 里已降为提示，走不到这里；
        # 这里的阻断全部来自可信来源，属于"高可信证据自身有问题"，正常阻断。
        code = PRICE_DEFECT_CODES.get(f.get("code"))
        if code:
            return {"code": code, "why": f["message"], "from": f["code"]}

    assessment = ev.assess_quotations(
        items, "target", as_of=as_of, price_class=price_class,
        requested=ev.requested_identity(product),
        stated_price=price, mismatch_tolerance=PRICE_MISMATCH_TOLERANCE,
    )
    status = assessment["status"]

    if status == "NO_CREDIBLE":
        return {"code": "PRICE_UNVERIFIED",
                "why": "价格只有用户口述或社区来源，不能据此断言价格合理",
                "from": "SOURCE_TIER"}
    if status == "STALE":
        return {"code": "PRICE_STALE",
                "why": "目标商品没有一条在保鲜期内的报价：旧价不能当现价用",
                "from": "NO_FRESH_QUOTE"}
    if status == "IDENTITY_AMBIGUOUS":
        opts = assessment["identity"]
        return {"code": "PRICE_UNVERIFIED",
                "why": ("报价里并列存在多个规格/地区/型号（"
                        + "、".join(opts["variants"] + opts["regions"] + opts["models"])
                        + "），又没说清你要买的是哪一个：不同规格不能放在一起比价，"
                          "也不能默认按最便宜的那个算，先确认身份再判断"),
                "from": "IDENTITY_AMBIGUOUS"}
    if status == "CONFLICT":
        conflict_values = sorted({v for vals in assessment["conflicts"].values()
                                  for v in vals})
        return {"code": "PRICE_UNVERIFIED",
                "why": (f"同一等级、同一口径的来源给出的价 {conflict_values} 相差超过 "
                        f"{int(ev.SAME_TIER_CONFLICT_TOLERANCE * 100)}%：至少有一个是错的，"
                        "不能取平均、取最新或取便宜的那个，回到来源核对到手价"),
                "from": "SOURCE_CONFLICT"}
    if status == "BASIS_AMBIGUOUS":
        return {"code": "PRICE_UNVERIFIED",
                "why": "同一商品并列存在多个到手价口径，分不出该用哪一个去比",
                "from": "PRICE_BASIS_AMBIGUOUS"}
    if status == "PAGE_PROBLEM":
        problem = assessment["page_problems"][0]["problem"]
        why = {
            "PAGE_PRICE_INVALID": "页面价填的不是正数，等于没核对过",
            "QUOTE_CONTENT_MISMATCH": "证据里的价格与页面上看到的对不上",
            "PAGE_TEXT_HASH_MISMATCH": "提供的页面正文与 page_text_sha256 对不上，"
                                       "这一条不能当页面证据",
            "PAGE_TEXT_HASH_UNVERIFIABLE": "声明了正文哈希却没给正文，无从重算",
            "PAGE_TEXT_HASH_REQUIRED": "要求哈希核验但没有提供哈希",
            "PAGE_TEXT_NO_QUOTE": "页面价在提供的页面内容里找不到",
            "PAGE_TEXT_NOT_A_PRICE": "这个数字在正文里只出现在型号/容量/销量这类位置，"
                                     "不是价格",
            "PAGE_TEXT_PRICE_AMBIGUOUS": "正文里这个数字既有价格用法也有非价格用法，"
                                         "分不出哪个才是商品价格",
        }.get(problem, "页面证据不成立")
        return {"code": "PRICE_UNVERIFIED", "why": why, "from": problem}
    if status == "NO_VALUE":
        return {"code": "PRICE_UNVERIFIED",
                "why": "最可信那一档来源里没有可用的报价",
                "from": "NO_CREDIBLE_QUOTE"}
    if status == "MISMATCH":
        return {
            "code": "PRICE_UNVERIFIED",
            "why": (f"你给的 {price} 元与最可信来源的到手价报价 "
                    f"{sorted(set(assessment['settlement']))} 相差超过 "
                    f"{int(PRICE_MISMATCH_TOLERANCE * 100)}%，两个数里必有一个不对，"
                    "先确认哪个是到手价"),
            "from": "PRICE_MISMATCH",
        }
    return None


def _affordability_unverified(fin, level, price, kind=None, period=None):
    """承受力是否只是"看起来够"，而不是确认够。

    四种情况不能支撑"买得起"：
    0. 连价格都没定下来（外币换不出来之类）；
    1. 是订阅但没说计费周期——月付 200 和年付 200 差 12 倍，无法折算真实成本；
    2. 月结余是缺储蓄/投资额时估出来的上界，且这笔消费吃掉了其中一大半；
    3. 收入、支出、存量都不足以判断——**L2 与 L3 都算**，只有 L1 快判豁免。
       300 元的判断和 3000 元的判断一样需要知道"这笔钱会不会挤掉别的"，
       只是前者可以少问几句，不是可以不问。

    返回 ``{"why": ...}`` 表示不成立，返回 None 表示可以按现状下结论。
    """
    snap = fin.get("snapshot") or {}
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
        return {"why": "这笔消费的金额还没定下来，无法判断承受力"}
    if kind == "subscription" and calc.period_months(period) is None:
        return {"why": "这是订阅但没有说明计费周期，无法折算真实年度成本"}
    if level == "L1":
        return None  # 几十块的东西不要求完整财务画像，否则快判通道形同虚设

    if fin["status"] == "ok" and snap.get("surplus_basis") == "estimated":
        share = fin.get("share")
        if share is None or share > ESTIMATE_TOLERANCE:
            return {
                "why": "月结余是在储蓄目标/每月投资未填的情况下估出来的上界",
                "surplus": snap.get("monthly_surplus"),
                "share": share,
                "tolerance": ESTIMATE_TOLERANCE,
            }
        return None

    if fin["status"] == "unknown":
        return {
            "why": "收入、固定支出与可动用存量都不足以判断这笔消费的承受力",
            "missing": snap.get("missing_fields"),
            "unconfirmed": snap.get("unconfirmed_fields"),
        }
    return None


# ---------------------------------------------------------------------------
# 判定核心
# ---------------------------------------------------------------------------
def _evaluate(draft: dict, profile: dict, owned=None) -> dict:
    base = (profile.get("meta") or {}).get("base_currency") or "CNY"
    as_of = draft.get("as_of")
    raw_product = dict(draft.get("product") or {})
    raw_items = list(draft.get("evidence") or [])

    # 1) 币种归一：所有金额统一到 base；汇率必须新，换不出来就阻断
    items, product, currency_findings = ev.to_base(raw_items, base, raw_product, as_of=as_of)

    price = product.get("price")
    prediction = draft.get("prediction") or {}
    period = product.get("period", "once")

    # 2) 分级：以系统计算为准，只允许往上调
    computed = calc.level_of(price, product.get("category"), period, profile)
    given = draft.get("level")
    level = computed["level"]
    level_source = "computed"
    if given in vd.LEVEL_RANK:
        if vd.LEVEL_RANK[given] > vd.LEVEL_RANK[computed["level"]]:
            level, level_source = given, "raised"
        elif vd.LEVEL_RANK[given] < vd.LEVEL_RANK[computed["level"]]:
            level_source = "downgrade_attempt"  # 保持 computed，audit 会报
    draft["level"] = level

    # 3) 外部理由：清掉 enforced 标记，丢掉引擎管辖的码
    #    否则模型可以自己"宣布"预算结论，或把自己的理由标成 enforced 躲开检查
    reasons = []
    for raw in draft.get("reasons") or []:
        item = raw if isinstance(raw, dict) else {"code": str(raw)}
        code = item.get("code")
        if not code or code in ENGINE_CODES:
            continue
        reasons.append({"code": code, "note": item.get("note") or ""})
    own_codes = {r["code"] for r in reasons}

    # 4) 财务与约束：由画像 + 价格强制推导
    #    订阅按"年化总额"做承受力判断，但月现金流只吃每月摊额——
    #    月付 200 的订阅，不能拿 2400 去和月结余比，也不能只拿 200 去看总负担。
    budget_price, monthly_equiv = price, None
    if computed["kind"] == "subscription" and isinstance(price, (int, float)):
        pm = calc.period_months(period)
        if pm:
            budget_price = price * 12 / pm
            monthly_equiv = price / pm
    fin = calc.budget_status(budget_price, profile, monthly_equiv=monthly_equiv)
    snap = fin.get("snapshot") or {}
    enforced_codes = []
    if fin["status"] in ("ok", "ok_stock"):
        enforced_codes.append("AFFORDABLE_WITHIN_BUDGET")
    elif fin["status"] == "tight":
        enforced_codes.append("BUDGET_TIGHT")
    elif fin["status"] == "breach":
        enforced_codes.append("BUDGET_BREACH")

    violations = calc.constraint_violations(profile, product)
    if violations:
        enforced_codes.append("CONSTRAINT_VIOLATION")

    for code in enforced_codes:
        if code in own_codes:
            continue
        reasons.append({
            "code": code,
            "note": f"由本地画像与价格自动判定（{vd.RULE_ZH.get(code, code)}）",
            "enforced": True,
        })

    # 5) 替代方案先评估：不合格的替代品不产出 SWITCH
    alt_raw = draft.get("alternative")
    alt = None
    if isinstance(alt_raw, dict) and str(alt_raw.get("name") or "").strip():
        alt = _assess_alternative(alt_raw, product, profile, items, base, as_of)

    # 6) 结论
    alt_norm = alt["normalized"] if alt else draft.get("alternative")
    alt_problems = alt["problems"] if alt else None
    resolved = vd.resolve(reasons, alt_norm, draft.get("override"), alt_problems=alt_problems)

    # 7) 承受力没被确认时，不能输出"买得起"：先降结论，再记账
    # SWITCH 不需要过这一关：它推荐的**不是**这个买不起/算不清的东西，
    # 替代品自己的承受力已经在 _assess_alternative 里单独验过了。
    unverified = _affordability_unverified(fin, level, price, computed["kind"], period)
    if unverified and resolved["verdict"] == "BUY":
        # 「在预算内」和「不知道在不在预算内」不能同时成立：先撤掉前一条
        reasons = [r for r in reasons if r["code"] != "AFFORDABLE_WITHIN_BUDGET"]
        enforced_codes = [c for c in enforced_codes if c != "AFFORDABLE_WITHIN_BUDGET"]
        reasons.append({
            "code": "BUDGET_UNVERIFIED",
            "note": f"由本地画像与价格自动判定（{unverified['why']}）",
            "enforced": True,
        })
        enforced_codes.append("BUDGET_UNVERIFIED")
        resolved = vd.resolve(reasons, alt_norm, draft.get("override"), alt_problems=alt_problems)

    verdict = resolved["verdict"]

    # 8) 证据校验：L1 不要求价格证据，否则快判通道形同虚设
    price_class = calc.price_class(product.get("category"))
    ev_result = ev.check(
        items,
        as_of=as_of,
        price_class=price_class,
        required_kinds=("price",) if (verdict in ("BUY", "SWITCH") and level != "L1") else (),
        base=base,
    )
    if currency_findings:
        _append_findings(ev_result, currency_findings)

    # 8b) 目标价格自己站不住时，不输出购买类结论。
    #     "价格合理/换这个更便宜"都建立在这个数字上；数字不可信，结论就不该成立。
    price_defect = None
    if verdict in ("BUY", "SWITCH") and level != "L1":
        target_prices = [
            e for e in items
            if isinstance(e, dict) and e.get("kind") in ("price", "promo")
            and str(e.get("subject") or "target").strip().lower() == "target"
            and isinstance(e.get("value"), (int, float))
        ]
        price_defect = _target_price_defect(ev_result, items, price, target_prices,
                                            as_of, price_class, product=product)
        if price_defect:
            reasons = [r for r in reasons if r["code"] != "AFFORDABLE_WITHIN_BUDGET"]
            enforced_codes = [c for c in enforced_codes if c != "AFFORDABLE_WITHIN_BUDGET"]
            if price_defect["code"] not in {r["code"] for r in reasons}:
                reasons.append({
                    "code": price_defect["code"],
                    "note": "由证据校验自动判定（" + price_defect["why"] + "）",
                    "enforced": True,
                })
                enforced_codes.append(price_defect["code"])
            resolved = vd.resolve(reasons, alt_norm, draft.get("override"),
                                  alt_problems=alt_problems)
            verdict = resolved["verdict"]

    needs_price = verdict in ("BUY", "SWITCH")

    # 9) 财务门的发现：结论已经按事实调整过，所以这些是说明而不是错误
    financial = []
    claims_affordable = verdict in ("BUY", "SWITCH")
    if fin["status"] == "breach":
        tail = {"hard_cap": "触及你自设的单笔上限", "capacity": "存量加结余也覆盖不了"}.get(
            fin.get("reason"), "超出承受能力")
        financial.append({
            "code": "BUDGET_BREACH", "severity": "INFO", "gate": "financial",
            "message": f"{fin['detail']}（{tail}），这个价格不能买"
                       + ("；已改判为建议替换方案" if verdict == "SWITCH"
                          else "，结论已被强制为不建议购买"),
            "detail": fin,
        })
    elif fin["status"] == "tight":
        financial.append({
            "code": "BUDGET_TIGHT", "severity": "WARN", "gate": "financial",
            "message": f"{fin['detail']}：需动用存量或跨月"
                       + ("；替代方案不需要跨月，已改判为建议替换方案"
                          if verdict == "SWITCH" else "，结论已被强制为暂缓"),
            "detail": fin,
        })
    elif fin["status"] == "ok_stock" and fin.get("heavy"):
        financial.append({
            "code": "FIN_RESERVE_HEAVY", "severity": "WARN", "gate": "financial",
            "message": fin["detail"] + "，一次用掉超过一半的可动用存量",
            "detail": fin,
        })
    elif fin["status"] == "unknown":
        hint = ""
        if snap.get("unconfirmed_fields"):
            hint = "；你有历史记录但还没确认：" + "、".join(snap["unconfirmed_fields"])
        elif snap.get("missing_fields"):
            hint = "；还缺：" + "、".join(snap["missing_fields"])
        financial.append({
            "code": "FIN_BUDGET_UNKNOWN",
            # BLOCK 只在"结论仍然推荐这个商品、而承受力未确认"时成立。
            # 结论是暂缓或替换方案时，这是说明，不是错误。
            "severity": "BLOCK" if (verdict == "BUY" and unverified) else
                        ("WARN" if claims_affordable else "INFO"),
            "gate": "financial",
            "message": "收入/支出/存量都不足以判断承受力" + hint
                       + ("。结论已按此降为暂缓" if verdict == "DEFER" else ""),
            "detail": fin,
        })

    if snap.get("stale_fields"):
        financial.append({
            "code": "FIN_PROFILE_STALE", "severity": "WARN", "gate": "financial",
            "message": "这些财务数据已过期，未参与本次计算，需要先确认：" + "、".join(snap["stale_fields"]),
            "detail": {"stale": snap["stale_fields"]},
        })

    if snap.get("surplus_estimated"):
        financial.append({
            "code": "FIN_SURPLUS_ESTIMATED",
            "severity": "BLOCK" if (verdict == "BUY" and unverified) else "WARN",
            "gate": "financial",
            "message": "储蓄目标或每月投资额还没填，可支配余额只能算上界"
                      + ("；不能据此断言买得起" if claims_affordable
                         else "。结论已按此降为暂缓" if verdict == "DEFER" else ""),
            "detail": {"surplus": snap.get("monthly_surplus"),
                       "tolerance": ESTIMATE_TOLERANCE},
        })

    # 已经由 FIN_BUDGET_UNKNOWN / FIN_SURPLUS_ESTIMATED 说明清楚的，不再重复一遍
    _explained = any(f["code"] in ("FIN_BUDGET_UNKNOWN", "FIN_SURPLUS_ESTIMATED")
                     for f in financial)
    if unverified and not claims_affordable and not _explained:
        financial.append({
            "code": "BUDGET_UNVERIFIED", "severity": "INFO", "gate": "financial",
            "message": unverified["why"] + "。结论已按此降为暂缓，不是买不起",
            "detail": unverified,
        })

    goal_name = store.fact_fresh(profile, "goal.name")
    goal_monthly = store.fact_fresh(profile, "goal.monthly")
    goals = [{"name": goal_name, "monthly": goal_monthly}] if goal_name and goal_monthly else None

    numbers = calc.build_numbers(
        price,
        prediction.get("uses_per_month"),
        prediction.get("months"),
        profile,
        goals,
        kind=computed["kind"],
        period=period,
    )
    if owned is not None:
        resources = store.resource_overview(owned)
        numbers["resources"] = {
            "similar": store.similar_resources(product, owned),
            "active_count": resources["active_count"],
            "monthly_subscription_total": resources["monthly_subscription_total"],
        }

    numbers["financial"] = {
        "monthly_surplus": snap.get("monthly_surplus"),
        "surplus_estimated": snap.get("surplus_estimated"),
        "surplus_basis": snap.get("surplus_basis"),
        "spendable": snap.get("spendable"),
        "spendable_basis": snap.get("spendable_basis"),
        "spendable_note": snap.get("spendable_note"),
        "hard_cap": snap.get("hard_cap"),
        "stale_fields": snap.get("stale_fields"),
        "unconfirmed_fields": snap.get("unconfirmed_fields"),
    }

    if alt:
        numbers["alternative"] = {k: v for k, v in alt.items() if k != "normalized"}

    # 10) 一致性审计：只看模型自己给的理由，引擎注入的只参与一致性判断
    own_reasons = [r for r in reasons if not r.get("enforced")]
    enforced_only = [r["code"] for r in reasons if r.get("enforced")]
    audit_input = {
        **draft,
        "verdict": verdict,
        "level": level,
        "product": product,
        "evidence": items,
        "reasons": own_reasons,
        "alternative": alt_norm,
        "numbers": numbers,
        "kind": computed["kind"],
    }
    audit_result = vd.audit(
        audit_input,
        ev_result,
        computed_level=computed["level"],
        enforced_codes=enforced_only,
        declared_level=given,
    )

    # 11) 三道门分开报告
    gates = {
        "financial": {
            "ok": not any(f["severity"] == "BLOCK" for f in financial),
            "findings": financial,
            "status": fin["status"],
            "enforced": [c for c in enforced_codes if c in
                         ("BUDGET_BREACH", "BUDGET_TIGHT", "CONSTRAINT_VIOLATION",
                          "BUDGET_UNVERIFIED")],
        },
        "integrity": ev_result,
        "coherence": audit_result,
    }

    blocking = [f for f in financial if f["severity"] == "BLOCK"]
    if needs_price:
        blocking += [f for f in ev_result["findings"] if f["severity"] == "BLOCK"]
    blocking += [f for f in audit_result["findings"] if f["severity"] == "BLOCK"]

    return {
        "level": {**computed, "level": level, "source": level_source},
        "currency": {"base": base, "product": product.get("currency", base)},
        "product": product,
        "numbers": numbers,
        "verdict": resolved,
        "gates": gates,
        "blocking": blocking,
        "ok": not blocking,
        # 下面三项是**判定阶段归一后的结果**，落账必须写这三份，不是原始输入
        "reasons": reasons,
        "evidence": items,
        "alternative": alt_norm,
        "recheck_suggestion": _recheck_suggestion(verdict, unverified, alt),
    }


def _recheck_suggestion(verdict, unverified, alt) -> str | None:
    """暂缓时给模型一个"什么时候再看"的落点，避免它编一个时点。"""
    if verdict != "DEFER":
        return None
    if unverified:
        return "补齐上面缺的财务数据后重新判断"
    problems = (alt or {}).get("problems") or []
    if problems:
        return "找到价格、来源、周期都齐全的替代方案后重新判断"
    return "信息补齐或条件变化后重新判断"


def _human_check(d: dict) -> str:
    v = d["verdict"]
    lines = [
        f"结论：{v['verdict_zh']}",
        f"分级：{d['level']['level']}（{d['level']['reason']}）",
        f"可落账：{'是' if d['ok'] else '否'}",
    ]
    if v.get("note"):
        lines.append(f"说明：{v['note']}")
    if v.get("enforced"):
        lines.append("引擎强制：" + "、".join(v["enforced"]))
    num = d["numbers"]
    if num.get("cost_per_use") is not None:
        lines.append(f"单次成本：{num['cost_per_use']} 元")
    if num.get("cost_per_use_note"):
        lines.append(f"　{num['cost_per_use_note']}")
    if num.get("annual_cost") is not None and num.get("cost_per_use") is not None:
        label = "年化成本" if d["level"].get("kind") == "subscription" else "年均摊"
        lines.append(f"{label}：{num['annual_cost']} 元")
    if num.get("invest_opportunity"):
        lines.append("机会成本：" + num["invest_opportunity"]["text"])
    alt = num.get("alternative")
    if alt:
        price_txt = f"{alt['price']} 元" if alt.get("price") is not None else "价格未知"
        if alt.get("original_price") and alt.get("currency") not in (None, "CNY"):
            price_txt += f"（原 {alt['original_price']} {alt['currency']}）"
        lines.append(f"替代方案：{alt.get('name')}｜{price_txt}")
        if alt.get("problems"):
            lines.append("　不成立原因：" + "；".join(
                vd.ALT_PROBLEM_ZH.get(p, p) for p in alt["problems"]))
    if num.get("missing"):
        lines.append("无法计算：" + "、".join(num["missing"]))
    if d.get("recheck_suggestion"):
        lines.append("复查建议：" + d["recheck_suggestion"])

    blocking = d.get("blocking") or []
    if blocking:
        lines.append("必须处理（否则不能落账）：")
        lines += [f"  [{f.get('gate','?')}/{f['code']}] {f['message']}" for f in blocking]
    rest = [
        f
        for g in ("financial", "integrity", "coherence")
        for f in (d["gates"][g].get("findings") or [])
        if f["severity"] != "BLOCK"
    ]
    if rest:
        lines.append("提示（不影响当前结论）：")
        lines += [f"  [{f.get('gate','?')}/{f['code']}] {f['message']}" for f in rest]
    if not blocking and not rest:
        lines.append("待处理：无")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_init(args) -> int:
    h = store.ensure_home()
    store.save_profile(store.load_profile())
    return _out({"ok": True, "home": str(h)}, args.human, lambda d: f"数据目录已就绪：{d['home']}")


def cmd_doctor(args) -> int:
    priv = store.privacy_check()
    profile = store.load_profile()
    events = store.read_events()
    decisions = store.derive_decisions(events)
    disc = store.discretionary(profile)
    gaps = store.gaps(profile, limit=None, min_prio=1)

    repo = Path(__file__).resolve().parent.parent
    personal = ("profile.json", "decisions.jsonl", "owned.json")
    leaked, sandboxed = [], []
    for path in repo.rglob("*"):
        if not path.is_file() or path.name not in personal or ".git" in path.parts:
            continue
        rel = path.relative_to(repo)
        # 点目录（.sim/.tmp 等）是本地沙盒，已在 .gitignore 里，不算泄漏
        (sandboxed if any(p.startswith(".") for p in rel.parts) else leaked).append(str(rel))
    leak_detail = leaked or (f"沙盒内 {len(sandboxed)} 个，已忽略" if sandboxed else None)
    checks = [
        {"name": "数据目录可写", "ok": Path(priv["home"]).is_dir()},
        {"name": "数据目录不在 Git 仓库内", "ok": not priv["inside_git_repo"]},
        {"name": "仓库内无个人数据文件", "ok": not leaked, "detail": leak_detail},
        {"name": "账本可解析", "ok": store.read_events.last_corrupt_lines == 0},
        {"name": "画像已建（schema v2）", "ok": profile.get("schema_version") == 2},
        {"name": "可算月可支配余额", "ok": isinstance(disc, (int, float)) and disc > 0},
    ]
    blocking_fail = [c for c in checks if not c["ok"] and c["name"] != "可算月可支配余额"]
    return _out(
        {
            "ok": not blocking_fail,
            "home": priv["home"],
            "git_roots": priv["git_roots"],
            "corrupt_lines": store.read_events.last_corrupt_lines,
            "decisions": len(decisions),
            "owned_items": len(store.load_owned()["items"]),
            "discretionary": disc,
            "profile_gaps": [g["key"] for g in gaps],
            "checks": checks,
        },
        args.human,
        lambda d: "\n".join(
            [f"数据目录：{d['home']}"]
            + [("✅ " if c["ok"] else "❌ ") + c["name"] +
               (f"（{c['detail']}）" if c.get("detail") else "") for c in d["checks"]]
            + [f"待补画像：{', '.join(d['profile_gaps']) or '无'}"]
        ),
    )


def cmd_brief(args) -> int:
    profile = store.load_profile()
    text = store.brief(profile) + "\n\n【已有物品】\n" + store.owned_digest()
    if args.human:
        print(text)
        return 0
    return _out(
        {
            "brief": text,
            "discretionary": store.discretionary(profile),
            "currency": (profile.get("meta") or {}).get("base_currency"),
            "gaps": store.gaps(profile, limit=5),
            "constraints": profile.get("constraints"),
        },
        False,
    )


def cmd_profile(args) -> int:
    action = args.action
    if action == "show":
        return _out(store.load_profile(), args.human,
                    lambda d: json.dumps(d, ensure_ascii=False, indent=2))
    if action == "brief":
        return cmd_brief(args)
    if action == "gaps":
        found = store.gaps(store.load_profile(), limit=args.limit, min_prio=args.priority)

        def render(rows):
            if not rows:
                return "画像已完整，无需补充"
            out = []
            for row in rows:
                state = "缺失" if row["reason"] == "missing" else "已 %s 天未确认" % row["age_days"]
                out.append(
                    "[P%d] %s（%s）\n     问：%s\n     为什么：%s"
                    % (row["priority"], row["key"], state, row["question"], row["why"])
                )
            return "\n".join(out)

        return _out(found, args.human, render)
    if action == "set":
        if not args.pairs:
            raise SystemExit("profile set 需要 key=value")
        for pair in args.pairs:
            if "=" not in pair:
                raise SystemExit(f"格式应为 key=value：{pair}")
            key, _, value = pair.partition("=")
            store.set_fact(key.strip(), value)
        p = store.load_profile()
        return _out({"ok": True, "facts": p["facts"], "brief": store.brief(p)},
                    args.human, lambda d: d["brief"])
    if action == "confirm":
        if not args.key:
            raise SystemExit("profile confirm 需要字段名")
        item = store.confirm_fact(args.key)
        return _out(item, args.human, lambda d: f"{args.key} 已确认仍为 {d['value']}")
    if action == "constraints":
        kwargs = {}
        for pair in args.pairs or []:
            key, _, value = pair.partition("=")
            if key in store.DEFAULT_CONSTRAINTS:
                kwargs[key] = value if key == "notes" else value.lower() in ("1", "true", "yes", "是")
        cons = store.set_constraints(**kwargs)
        return _out(cons, args.human, lambda d: json.dumps(d, ensure_ascii=False, indent=2))
    return _out({"error": "未知操作"}, args.human)


def cmd_seed(args) -> int:
    """用一份候选稿初始化画像。默认写入的都是"待确认"，不直接参与计算。"""
    payload = _load_input(args.input)
    entries = payload.get("facts") if isinstance(payload.get("facts"), dict) else payload
    result = store.seed_facts(entries, force=args.force)
    profile = store.load_profile()
    return _out(
        {**result, "brief": store.brief(profile), "gaps": store.gaps(profile, limit=6)},
        args.human,
        lambda d: "已写入：" + ("、".join(d["written"]) or "无")
        + ("\n跳过：" + "、".join(f"{s['key']}({s['reason']})" for s in d["skipped"]) if d["skipped"] else "")
        + "\n\n" + d["brief"],
    )


def cmd_owned(args) -> int:
    if args.action == "add":
        item = {
            "name": args.name,
            "category": args.category or "未分类",
            "condition": args.condition,
            "acquired": args.acquired,
            "price": args.price,
            "notes": args.notes or "",
            "monthly_cost": args.monthly_cost,
            "status": args.status,
            "vendor": args.vendor or "",
        }
        return _out(store.add_owned(item), args.human, lambda d: f"已登记：{d['name']}（{d['condition']}）")
    if args.action == "list":
        items = store.load_owned()["items"]
        return _out(items, args.human, lambda d: store.owned_digest())
    if args.action == "find":
        return _out(store.find_owned(args.query), args.human,
                    lambda d: json.dumps(d, ensure_ascii=False, indent=2))
    if args.action == "set":
        items = store.load_owned()
        for it in items["items"]:
            if it.get("id") == args.id or it.get("name") == args.id:
                if args.condition:
                    if args.condition not in store.CONDITIONS:
                        raise SystemExit(f"condition 必须是 {store.CONDITIONS}")
                    it["condition"] = args.condition
                if args.notes is not None:
                    it["notes"] = args.notes
                store.save_owned(items)
                return _out(it, args.human, lambda d: f"已更新：{d['name']} → {d['condition']}")
        return _out({"error": f"未找到 {args.id}"}, args.human)
    return _out({"error": "未知操作"}, args.human)


def cmd_check(args) -> int:
    draft = _load_input(args.input)
    result = _evaluate(draft, store.load_profile(), store.load_owned()["items"])
    return _out(result, args.human, _human_check)


def _ledger_record(draft: dict, result: dict) -> dict:
    """账本记录只写**判定阶段归一后的**理由与证据。

    直接写 ``draft`` 会把三样东西漏进账本：外部塞进来的 ``enforced`` 标记、
    未换算的原始外币金额、以及引擎判定时已经丢弃的无效理由。
    """
    return {
        "type": "decision",
        "id": draft.get("id") or store.new_id(),
        "level": result["level"]["level"],
        "product": result["product"],
        "currency": result["currency"],
        "verdict": result["verdict"]["verdict"],
        "reasons": result["reasons"],
        "enforced": result["verdict"].get("enforced") or [],
        "alternative": result.get("alternative"),
        "numbers": result["numbers"],
        "evidence": result["evidence"],
        "prediction": draft.get("prediction") or {},
        "recheck_condition": draft.get("recheck_condition"),
        "overridden": bool(result["verdict"].get("overridden")),
        "notes": draft.get("notes", ""),
    }


def cmd_judge(args) -> int:
    """判定并落账：能过就写账本，过不了就只返回问题。"""
    draft = _load_input(args.input)
    profile = store.load_profile()
    result = _evaluate(draft, profile, store.load_owned()["items"])
    if not result["ok"]:
        print(json.dumps(
            {"ok": False, "logged": False, "refused": True, "result": result},
            ensure_ascii=False, indent=2))
        return 2
    record = _ledger_record(draft, result)
    event = store.append_event(record)
    payload = {"ok": True, "logged": True, "id": record["id"], "at": event["at"], "result": result}
    if args.human:
        print(_human_check(result) + f"\n已记账 #{record['id']}")
        return 0
    return _out(payload)


def cmd_log(args) -> int:
    draft = _load_input(args.input)
    result = _evaluate(draft, store.load_profile(), store.load_owned()["items"])
    if not result["ok"] and not args.force:
        print(json.dumps({"ok": False, "refused": True, "result": result}, ensure_ascii=False, indent=2))
        return 2
    record = _ledger_record(draft, result)
    if args.force and not result["ok"]:
        record["forced"] = True
    event = store.append_event(record)
    return _out({"ok": True, "id": record["id"], "verdict": record["verdict"], "at": event["at"]},
                args.human, lambda d: f"已记录 #{d['id']}｜{d['verdict']}")


def cmd_outcome(args) -> int:
    bought = str(args.bought).lower() in ("yes", "y", "true", "1")
    decision = store.derive_decisions().get(args.id)
    if not decision:
        return _out({"ok": False, "error": f"未找到 {args.id}"}, args.human,
                    lambda d: d["error"])
    store.append_event({
        "type": "outcome", "ref": args.id, "bought": bought,
        "user_decision": args.decision or (decision.get("verdict") if bought else None),
    })
    return _out({"ok": True, "ref": args.id, "bought": bought}, args.human,
                lambda d: f"已标记 #{d['ref']}：{'已购买' if d['bought'] else '未购买'}")


def cmd_review(args) -> int:
    payload = _load_input(args.input)
    actual = payload.get("actual") or payload
    store.append_event({"type": "review", "ref": args.id, "actual": actual,
                        "notes": payload.get("notes", "")})
    return _out({"ok": True, "ref": args.id, "actual": actual}, args.human,
                lambda d: f"已记录复盘 #{d['ref']}")


def cmd_due(args) -> int:
    items = store.due_for_review(args.days)
    return _out(items, args.human, lambda d: "\n".join(
        f"- #{i['id']}｜{(i.get('product') or {}).get('name','?')}｜{i['age_days']} 天前" for i in d
    ) or "暂无待复盘")


def cmd_stats(args) -> int:
    return _out(store.stats(), args.human, lambda d: json.dumps(d, ensure_ascii=False, indent=2))


def cmd_export(args) -> int:
    decisions = store.derive_decisions()
    rows = ["# 消费决策记录", "", "| 日期 | 商品 | 金额 | 结论 | 复盘 |", "|---|---|---|---|---|"]
    for rec in sorted(decisions.values(), key=lambda r: r.get("at") or ""):
        p = rec.get("product") or {}
        review = rec["reviews"][-1]["actual"] if rec.get("reviews") else {}
        mark = "—"
        if review:
            mark = f"满意度 {review.get('satisfaction', '?')}，月用 {review.get('uses_per_month', '?')} 次"
        rows.append(
            f"| {str(rec.get('at',''))[:10]} | {p.get('name','?')} | {p.get('price','?')} | "
            f"{store.VERDICT_ZH.get(rec.get('verdict'), rec.get('verdict'))} | {mark} |"
        )
    text = "\n".join(rows) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        return _out({"ok": True, "path": args.out, "rows": len(rows) - 4}, args.human,
                    lambda d: f"已导出 {d['rows']} 条 → {d['path']}")
    print(text)
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--human", action="store_true", help="输出中文摘要而非 JSON")

    p = argparse.ArgumentParser(prog="mbm", description="买不买 AI 本地工具", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", parents=[common]).set_defaults(func=cmd_init)
    sub.add_parser("doctor", parents=[common]).set_defaults(func=cmd_doctor)
    sub.add_parser("brief", parents=[common]).set_defaults(func=cmd_brief)

    pr = sub.add_parser("profile", parents=[common])
    pr.add_argument("action", choices=["show", "brief", "gaps", "set", "confirm", "constraints", "seed"])
    pr.add_argument("pairs", nargs="*", help="key=value，或 constraints 的开关")
    pr.add_argument("--key", help="confirm 用")
    pr.add_argument("--limit", type=int, default=3)
    pr.add_argument("--priority", type=int, default=3)
    pr.set_defaults(func=cmd_profile)

    sd = sub.add_parser("seed", parents=[common], help="用候选稿初始化画像（默认待确认）")
    sd.add_argument("--input", required=True)
    sd.add_argument("--force", action="store_true", help="覆盖已确认的值")
    sd.set_defaults(func=cmd_seed)

    ow = sub.add_parser("owned", parents=[common])
    ow.add_argument("action", choices=["add", "list", "find", "set"])
    ow.add_argument("--name")
    ow.add_argument("--category")
    ow.add_argument("--condition", default="good", choices=list(store.CONDITIONS))
    ow.add_argument("--acquired")
    ow.add_argument("--price", type=float)
    ow.add_argument("--notes")
    ow.add_argument("--monthly-cost", type=float, help="服务类每月成本")
    ow.add_argument("--status", default="active", choices=["active", "paused", "expired"])
    ow.add_argument("--vendor")
    ow.add_argument("--query")
    ow.add_argument("--id")
    ow.set_defaults(func=cmd_owned)

    ck = sub.add_parser("check", parents=[common], help="只校验，不落账")
    ck.add_argument("--input", required=True)
    ck.set_defaults(func=cmd_check)

    jg = sub.add_parser("judge", parents=[common], help="判定并落账")
    jg.add_argument("--input", required=True)
    jg.set_defaults(func=cmd_judge)

    lg = sub.add_parser("log", parents=[common], help="落账（可 --force）")
    lg.add_argument("--input", required=True)
    lg.add_argument("--force", action="store_true")
    lg.set_defaults(func=cmd_log)

    oc = sub.add_parser("outcome", parents=[common])
    oc.add_argument("id")
    oc.add_argument("--bought", default="yes")
    oc.add_argument("--decision", help="用户实际选择的结论，用于与建议对比")
    oc.set_defaults(func=cmd_outcome)

    rv = sub.add_parser("review", parents=[common])
    rv.add_argument("id")
    rv.add_argument("--input", required=True)
    rv.set_defaults(func=cmd_review)

    du = sub.add_parser("due", parents=[common])
    du.add_argument("--days", type=int)
    du.set_defaults(func=cmd_due)

    sub.add_parser("stats", parents=[common]).set_defaults(func=cmd_stats)

    ex = sub.add_parser("export", parents=[common])
    ex.add_argument("--out")
    ex.set_defaults(func=cmd_export)

    return p


def _configure_stdio() -> None:
    """把标准输出统一成 UTF-8。

    Windows 控制台默认是 GBK / cp1252，直接 print 中文会抛
    ``UnicodeEncodeError``，而这个工具的输出全是中文。重定向到文件时同理：
    不显式指定就会按系统代码页写，读回来是乱码。
    失败就退回默认行为——不能因为改了输出编码反而让命令起不来。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
