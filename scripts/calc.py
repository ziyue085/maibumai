"""成本算术与决策分级。

只做加乘除，不做加权评分。任何缺失输入一律返回 None，
由调用方写明"无法计算"，绝不填默认值假装算过。

这里同时负责**财务硬约束**的计算：可支配余额、单笔硬上限、投资机会成本。
这些数字不是参考值——`budget_status` 的结果会被引擎强制注入结论（见 mbm._evaluate）。
"""

from __future__ import annotations

import store

#: 品类关键词 → 大类。**最长匹配优先**，所以"云服务器"不会被"服务器"抢走。
#: 订阅类排在最前，因为误判成耐用品会让年化成本被低估 12 倍。
CATEGORY_RULES = [
    # 订阅与服务
    ("云服务器", "subscription"), ("服务器租用", "subscription"), ("主机租用", "subscription"),
    ("云主机", "subscription"), ("云服务", "subscription"), ("会员", "subscription"),
    ("订阅", "subscription"), ("年费", "subscription"), ("月费", "subscription"),
    ("宽带", "subscription"), ("保险", "subscription"), ("云盘", "subscription"),
    ("SaaS", "subscription"), ("VPS", "subscription"), ("域名", "subscription"),
    ("API", "subscription"), ("软件", "subscription"), ("服务", "subscription"),
    # 体验
    ("演出", "experience"), ("门票", "experience"), ("旅行", "experience"),
    ("旅游", "experience"), ("餐饮", "experience"), ("活动", "experience"),
    ("体验", "experience"), ("展览", "experience"), ("运动课", "experience"),
    ("课程", "experience"),
    # 耐用
    ("服务器", "durable"), ("运动器材", "durable"), ("数码", "durable"), ("电子", "durable"),
    ("手机", "durable"), ("电脑", "durable"), ("家电", "durable"), ("家具", "durable"),
    ("家居", "durable"), ("乐器", "durable"), ("工具", "durable"), ("厨具", "durable"),
    ("手表", "durable"), ("箱包", "durable"), ("眼镜", "durable"), ("椅子", "durable"),
    ("显示器", "durable"), ("键盘", "durable"), ("相机", "durable"), ("主机", "durable"),
    ("耳机", "durable"), ("平板", "durable"), ("服饰", "durable"), ("鞋", "durable"),
    # 消耗
    ("食品", "consumable"), ("零食", "consumable"), ("日耗", "consumable"),
    ("纸品", "consumable"), ("洗护", "consumable"), ("美妆", "consumable"),
    ("护肤", "consumable"), ("清洁", "consumable"), ("文具", "consumable"),
]

#: 长词优先，保证"云服务器"命中订阅而不是"服务器"。
_SORTED_RULES = sorted(CATEGORY_RULES, key=lambda kv: len(kv[0]), reverse=True)

DEFAULT_THRESHOLD = 500
DEFAULT_SMALL = 100            # 以下快速通道
#: **安全底线**倍数：可动用存量 + 这么多个月结余都覆盖不了，才判定"真的买不起"。
#: 这不是消费标准。用 6 倍月结余当红线，对有储蓄但收入不稳定的用户是错的——
#: 他们的问题不是"月收入撑不住"，而是"这笔钱该不该从存量里出"。
BREACH_MULTIPLE = 12
#: 单笔门槛 = max(500, 月可支配 × 25%)。低于 500 元，研究的时间成本超过可能的损失；
#: 超过月可支配四分之一，就是会影响其它安排的决定。
THRESHOLD_RATIO = 0.25
FAST_PRICE_WORDS = ("数码", "电子", "手机", "电脑", "耳机", "服饰", "鞋", "美妆", "护肤", "游戏")

PERIOD_MONTHS = {"month": 1, "quarter": 3, "year": 12}
PERIOD_ZH = {"month": "月", "quarter": "季", "year": "年", "once": "一次性"}


def classify_category(category: str | None) -> str:
    """最长关键词优先。品类判错会直接改变"要不要年化"这个根本问题。"""
    text = str(category or "").lower()
    for word, kind in _SORTED_RULES:
        if word.lower() in text:
            return kind
    return "other"


def price_class(category: str | None) -> str:
    text = str(category or "")
    return "fast" if any(w in text for w in FAST_PRICE_WORDS) else "durable"


def period_months(period: str | None):
    """计费周期 → 月数。一次性或无法识别返回 None。"""
    return PERIOD_MONTHS.get(str(period or "").strip().lower())


def annual_of(price, period):
    """把"每期价格"折成年度总成本。周期不明返回 None——不猜。"""
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    months = period_months(period)
    if months is None:
        return None
    return round(price * 12 / months, 2)


def threshold_of(profile: dict) -> int:
    """单笔门槛。只认**已确认且未过期**的值——一个没确认的门槛
    能把重决策降级成常规决策，这是最容易被绕过的一环。"""
    explicit = store.fact_fresh(profile, "finance.single_purchase_threshold")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return int(explicit)
    disc = store.discretionary(profile)
    if isinstance(disc, (int, float)) and disc > 0:
        return int(max(DEFAULT_THRESHOLD, disc * THRESHOLD_RATIO))
    return DEFAULT_THRESHOLD


def level_of(price, category=None, period="once", profile=None) -> dict:
    """L1 快判 / L2 标准 / L3 重决策。

    订阅类一律先折年化再分级：月付 200 的订阅不是 200 元的消费。
    """
    profile = profile or {}
    behavior = profile.get("behavior") or {}
    # 快判上限**只能收紧，不能放大**：L1 意味着"不值得查证"，
    # 把这个上限抬到一万块就等于关掉整套证据要求。
    # 这个字段不在可写字段里（正常流程改不到），但仍然防一手。
    small = behavior.get("small_amount_fastpath", DEFAULT_SMALL)
    if not isinstance(small, (int, float)) or isinstance(small, bool) or small < 0:
        small = DEFAULT_SMALL
    small = min(float(small), float(DEFAULT_SMALL))
    threshold = threshold_of(profile)
    kind = classify_category(category)

    if not isinstance(price, (int, float)) or price <= 0:
        return {"level": "L2", "kind": kind, "reason": "价格未知，无法走快速通道"}

    if kind == "subscription":
        months = period_months(period)
        if months is None:
            return {
                "level": "L3", "kind": kind, "annual_cost": None,
                "reason": "订阅但计费周期未说明，无法评估真实成本，按重决策处理",
            }
        annual = price * 12 / months
        if annual > threshold:
            level = "L3"
        elif annual <= small:
            level = "L1"
        else:
            level = "L2"
        return {"level": level, "kind": kind, "annual_cost": round(annual, 2),
                "reason": f"订阅按年化 {round(annual, 2)} 元评估"}

    if price <= small:
        return {"level": "L1", "kind": kind, "reason": f"{price} 元低于快速通道阈值 {small}"}

    if price > threshold:
        return {"level": "L3", "kind": kind, "reason": f"{price} 元超过单笔门槛 {threshold}"}

    return {"level": "L2", "kind": kind, "reason": "常规决策"}


def cost_per_use(price, uses_per_month, months):
    try:
        price = float(price)
        uses = float(uses_per_month)
        months = float(months)
    except (TypeError, ValueError):
        return None
    if price <= 0 or uses <= 0 or months <= 0:
        return None
    return round(price / (uses * months), 2)


def annual_cost(price, years):
    try:
        price, years = float(price), float(years)
    except (TypeError, ValueError):
        return None
    if price <= 0 or years <= 0:
        return None
    return round(price / years, 2)


def budget_share(price, discretionary_monthly):
    try:
        price, disc = float(price), float(discretionary_monthly)
    except (TypeError, ValueError):
        return None
    if disc <= 0:
        return None
    return round(price / disc, 3)


def months_to_afford(price, discretionary_monthly):
    try:
        price, disc = float(price), float(discretionary_monthly)
    except (TypeError, ValueError):
        return None
    if disc <= 0 or price <= 0:
        return None
    return round(price / disc, 1)


def budget_status(price, profile, monthly_equiv=None) -> dict:
    """可承受性判断，分两层：每月现金流、一次性存量。

    参数
    ----
    price : 这笔消费的**总额**。订阅请传年化总额，否则年付订阅会被严重低估。
    monthly_equiv : 订阅的每月摊额。给了就先用它去比月现金流——
        月付 200 的订阅，每月只吃掉 200，不该拿 2400 去和月结余比。

    关键原则：
    - **不默认存款都能花**：可动用存量 = 现金 − 应急金 − 近期必要支出，缺一项就是未知。
    - **不只按月收入否决**：月现金流不够时，先看存量拿不拿得出。
    - 只有用户自设的硬上限，才是"一票否决"。
    """
    snap = store.financial_snapshot(profile)
    cap = snap["hard_cap"]
    out = {"snapshot": snap}

    if not isinstance(price, (int, float)) or price <= 0:
        return {**out, "status": "unknown", "code": "INFO_GAP",
                "detail": "价格未知，无法做预算判断"}

    if isinstance(cap, (int, float)) and price > cap:
        return {**out, "status": "breach", "code": "BUDGET_BREACH", "reason": "hard_cap",
                "detail": f"合计 {price:.0f} 元超过你自设的单笔上限 {cap} 元"}

    surplus = snap["monthly_surplus"]
    spend = snap["spendable"]
    flow = monthly_equiv if isinstance(monthly_equiv, (int, float)) else price

    if surplus is None and spend is None:
        return {**out, "status": "unknown", "code": "INFO_GAP",
                "detail": "没有可用的收入、支出或存量数据，无法判断承受力"}

    def _ratio(a, b):
        return round(a / b, 3) if isinstance(b, (int, float)) and b > 0 else None

    if isinstance(surplus, (int, float)) and flow <= max(0.0, surplus):
        return {**out, "status": "ok", "code": "AFFORDABLE_WITHIN_BUDGET",
                "basis": "monthly",
                "share": _ratio(flow, surplus), "months": _ratio(price, surplus),
                "detail": f"每月摊 {flow:.0f} 元，在月可支配 {surplus:.0f} 元之内"}

    if isinstance(spend, (int, float)) and price <= spend:
        ratio = _ratio(price, spend) or 1.0
        return {**out, "status": "ok_stock", "code": "AFFORDABLE_WITHIN_BUDGET",
                "basis": "stock",
                "share": ratio, "months": _ratio(price, surplus) if surplus else None,
                "heavy": ratio > 0.5,
                "detail": f"月现金流不够，但可动用存量 {spend:.0f} 元拿得出（占 {ratio:.0%}）"}

    # 有存款但不知道有多少是自由的：先问，不要替他把存款都算成可花
    if spend is None and isinstance(snap.get("liquid_cash"), (int, float)):
        return {**out, "status": "unknown", "code": "INFO_GAP",
                "detail": f"你有 {snap['liquid_cash']:.0f} 元现金，但没填应急金与近半年必要支出，"
                          "不知道其中多少能真正动用"}

    if isinstance(surplus, (int, float)):
        capacity = (spend or 0) + max(0.0, surplus) * BREACH_MULTIPLE
        if price <= capacity:
            months = round((price - (spend or 0)) / surplus, 1) if surplus > 0 else None
            return {**out, "status": "tight", "code": "BUDGET_TIGHT", "basis": "monthly",
                    "share": _ratio(price, surplus), "months": months, "capacity": round(capacity, 2),
                    "detail": "需要动用存量"
                              + (f"并再攒约 {months} 个月" if months else "")
                              + f"（可动用合计上限约 {capacity:.0f} 元）"}
        return {**out, "status": "breach", "code": "BUDGET_BREACH", "reason": "capacity",
                "capacity": round(capacity, 2), "share": _ratio(price, surplus),
                "detail": f"可动用存量加一年结余合计约 {capacity:.0f} 元，仍不够"}

    # 只有存量、没有现金流数据
    return {**out, "status": "unknown", "code": "INFO_GAP",
            "detail": f"可动用存量 {spend:.0f} 元不够，且没有收入数据，无法判断能否攒出来"}


def emergency_status(price, profile) -> dict:
    """应急金是否够厚。不足时做大额可选消费属于加杠杆，要提示。"""
    fund = store.fact_fresh(profile, "finance.emergency_fund")
    expense = store.fact_fresh(profile, "finance.monthly_fixed_expense")
    if not isinstance(fund, (int, float)) or not isinstance(expense, (int, float)) or expense <= 0:
        return {"known": False}
    months = round(fund / expense, 1)
    return {
        "known": True,
        "months": months,
        "thin": months < 3,
        "detail": f"应急金可覆盖 {months} 个月固定支出",
    }


def investment_opportunity(price, profile, years=10) -> dict | None:
    """把这笔钱换成"假设用于投资会怎样"。

    收益率是用户自己设的**假设**，不是能拿到的收益。
    输出必须同时给"不投资就是本金"，并且明确写"可能亏损"——
    把复利当承诺是最容易做的恐吓，也最容易让整段分析失信。
    """
    rate = store.fact_fresh(profile, "invest.expected_annual_return")
    if not isinstance(rate, (int, float)) or rate <= 0:
        return None
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    r = rate / 100 if rate > 1 else rate
    future = price * (1 + r) ** years
    return {
        "years": years,
        "rate_pct": round(r * 100, 2),
        "future_value": round(future, 2),
        "forgone": round(future - price, 2),
        "hypothetical": True,
        "disclaimer": "这是按你设定的收益率做的假设测算，不是能拿到的收益；实际可能低于本金。",
        "text": (
            f"若把这 {price:.0f} 元按你假设的 {round(r*100,2)}% 年化投 {years} 年，"
            f"测算约 {round(future)} 元（假设，非承诺；也可能亏损）"
        ),
    }


def constraint_violations(profile, product) -> list:
    """用户显式声明的硬约束。只检查结构化的三项，自由文本由模型自行判断。"""
    cons = (profile or {}).get("constraints") or {}
    out = []
    condition = str((product or {}).get("condition") or "").lower()
    kind = classify_category((product or {}).get("category"))
    if cons.get("no_used") and condition in ("used", "refurbished", "二手"):
        out.append({"constraint": "no_used", "detail": "你说过不买二手，这个商品是二手的"})
    if cons.get("no_subscription") and kind == "subscription":
        out.append({"constraint": "no_subscription", "detail": "你说过不想再增加订阅"})
    if cons.get("must_be_portable") and (product or {}).get("portable") is False:
        out.append({"constraint": "must_be_portable", "detail": "你要求可携带，这个不带"})
    return out


def opportunity_cost(price, goals) -> list:
    out = []
    if not isinstance(price, (int, float)):
        return out
    for g in goals or []:
        name = g.get("name")
        monthly = g.get("monthly")
        if not name or not isinstance(monthly, (int, float)) or monthly <= 0:
            continue
        out.append({
            "goal": name,
            "delay_months": round(price / monthly, 1),
            "text": f"等于把「{name}」的进度推后 {round(price / monthly, 1)} 个月",
        })
    return out


def break_even_uses(price, acceptable_unit_cost):
    try:
        price, unit = float(price), float(acceptable_unit_cost)
    except (TypeError, ValueError):
        return None
    if unit <= 0:
        return None
    return int(price / unit + 0.999)


def build_numbers(price, uses_per_month=None, months=None, profile=None, goals=None,
                  kind="other", period="once") -> dict:
    """把一次决策需要的算术一次算齐；算不出的项留在 missing 里。"""
    numbers, missing = {}, []
    numbers["price"] = price if isinstance(price, (int, float)) else None
    if numbers["price"] is None:
        missing.append("price")

    if kind == "experience":
        numbers["cost_per_use"] = None
        numbers["cost_per_use_note"] = "体验型消费不适用单次成本，按预算占比与稀缺性评估"
        numbers["annual_cost"] = None
    elif kind == "subscription":
        annual = annual_of(price, period)
        numbers["cost_per_use"] = None
        numbers["annual_cost"] = annual
        if annual is None:
            numbers["cost_per_use_note"] = "订阅但计费周期未说明，年化成本无法计算"
            missing.append("annual_cost（需要 period: month / quarter / year）")
        else:
            months_n = period_months(period)
            numbers["cost_per_use_note"] = (
                f"订阅按年化成本评估：{annual} 元/年"
                f"（{price} 元/{PERIOD_ZH.get(period, period)}，约 {round(price/months_n, 2)} 元/月）"
            )
    else:
        cpu = cost_per_use(price, uses_per_month, months)
        numbers["cost_per_use"] = cpu
        if cpu is None:
            missing.append("cost_per_use（需要价格 + 月使用次数 + 使用月数）")
        numbers["annual_cost"] = annual_cost(price, (months or 0) / 12) if months else None

    numbers["budget"] = budget_status(price, profile or {})
    numbers["emergency"] = emergency_status(price, profile or {})
    opp = investment_opportunity(price, profile or {})
    if opp:
        numbers["invest_opportunity"] = opp

    if goals:
        numbers["opportunity"] = opportunity_cost(price, goals)

    numbers["missing"] = missing
    return numbers
