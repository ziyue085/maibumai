"""成本算术与决策分级。

只做加乘除，不做加权评分。任何缺失输入一律返回 None，
由调用方写明"无法计算"，绝不填默认值假装算过。
"""

from __future__ import annotations

#: 品类 → 大类。用于分级与"该看什么指标"。
CATEGORY_KIND = {
    "durable": (
        "数码", "电子", "手机", "电脑", "家电", "家具", "家居", "乐器", "工具",
        "厨具", "运动器材", "手表", "箱包", "鞋", "服饰", "眼镜", "床", "椅",
    ),
    "consumable": ("食品", "零食", "日耗", "纸品", "洗护", "美妆", "护肤", "清洁", "文具"),
    "subscription": ("会员", "订阅", "服务", "云盘", "宽带", "保险", "课程"),
    "experience": ("演出", "门票", "旅行", "旅游", "餐饮", "活动", "体验", "展览", "运动课"),
}

DEFAULT_THRESHOLD = 500        # 超过即进入重决策；可在 profile 覆盖
DEFAULT_SMALL = 100            # 以下快速通道

#: 价格波动快的品类：报价保鲜期 7 天；其余 14 天。
FAST_PRICE_WORDS = ("数码", "电子", "手机", "电脑", "耳机", "服饰", "鞋", "美妆", "护肤", "游戏")


def price_class(category: str | None) -> str:
    text = str(category or "")
    return "fast" if any(w in text for w in FAST_PRICE_WORDS) else "durable"
BREACH_MULTIPLE = 6            # 超过月可支配预算 6 倍视为承受不起（经验阈值）


def classify_category(category: str | None) -> str:
    text = str(category or "")
    for kind, words in CATEGORY_KIND.items():
        if any(w in text for w in words):
            return kind
    return "other"


def threshold_of(profile: dict) -> int:
    budget = (profile or {}).get("budget") or {}
    explicit = budget.get("single_purchase_threshold")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return int(explicit)
    disc = budget.get("discretionary_monthly")
    if isinstance(disc, (int, float)) and disc > 0:
        return int(max(300, disc * 0.1))
    return DEFAULT_THRESHOLD


def level_of(price, category=None, period="once", profile=None) -> dict:
    """L1 快判 / L2 标准 / L3 重决策。"""
    profile = profile or {}
    behavior = profile.get("behavior") or {}
    small = behavior.get("small_amount_fastpath", DEFAULT_SMALL)
    threshold = threshold_of(profile)
    kind = classify_category(category)

    if not isinstance(price, (int, float)) or price <= 0:
        return {"level": "L2", "kind": kind, "reason": "价格未知，无法走快速通道"}

    if kind == "subscription":
        annual = price * 12 if period == "month" else price
        if annual > threshold:
            level = "L3"
        elif annual <= small:
            level = "L1"
        else:
            level = "L2"
        return {"level": level, "kind": kind, "annual_cost": annual,
                "reason": f"订阅类按年化 {annual} 元评估"}

    if price <= small:
        return {"level": "L1", "kind": kind, "reason": f"{price} 元低于快速通道阈值 {small}"}

    if price > threshold:
        return {"level": "L3", "kind": kind, "reason": f"{price} 元超过单笔门槛 {threshold}"}

    if kind == "durable" and price > threshold * 0.6:
        return {"level": "L3", "kind": kind, "reason": "耐用品且接近单笔门槛"}

    return {"level": "L2", "kind": kind, "reason": "常规决策"}


def cost_per_use(price, uses_per_month, months):
    """单次使用成本。任一输入缺失返回 None——不猜使用频率。"""
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
    """若这笔钱完全从可支配预算里出，需要几个月。"""
    try:
        price, disc = float(price), float(discretionary_monthly)
    except (TypeError, ValueError):
        return None
    if disc <= 0 or price <= 0:
        return None
    return round(price / disc, 1)


def budget_status(price, profile) -> dict:
    """返回 ok / tight / breach / unknown，并给出对应的规则码建议。"""
    budget = (profile or {}).get("budget") or {}
    disc = budget.get("discretionary_monthly")
    cap = budget.get("hard_cap_single")

    if isinstance(cap, (int, float)) and isinstance(price, (int, float)) and price > cap:
        return {"status": "breach", "code": "BUDGET_BREACH", "hard_cap": cap,
                "detail": f"{price} 元超过自设单笔上限 {cap} 元"}

    if not isinstance(disc, (int, float)) or disc <= 0 or not isinstance(price, (int, float)):
        return {"status": "unknown", "code": "INFO_GAP", "detail": "未设置月可支配预算，无法做预算判断"}

    share = budget_share(price, disc)
    months = months_to_afford(price, disc)
    if price > disc * BREACH_MULTIPLE:
        return {"status": "breach", "code": "BUDGET_BREACH", "share": share, "months": months,
                "detail": f"相当于 {months} 个月的全部可支配预算"}
    if price > disc:
        return {"status": "tight", "code": "BUDGET_TIGHT", "share": share, "months": months,
                "detail": f"占月可支配 {share:.0%}，需跨 {months} 个月支出"}
    return {"status": "ok", "code": "AFFORDABLE_WITHIN_BUDGET", "share": share, "months": months,
            "detail": f"占月可支配 {share:.0%}"}


def break_even_uses(price, acceptable_unit_cost):
    """若接受每次 X 元，这笔钱至少要换来多少次使用。"""
    try:
        price, unit = float(price), float(acceptable_unit_cost)
    except (TypeError, ValueError):
        return None
    if unit <= 0:
        return None
    return int(price / unit + 0.999)


def opportunity_cost(price, goals) -> list:
    """把金额翻译成具体牺牲：挤掉哪个目标、推迟多久。"""
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


def build_numbers(price, uses_per_month=None, months=None, profile=None, goals=None,
                  kind="other", period="once") -> dict:
    """把一次决策需要的算术一次算齐；算不出的项留在 missing 里。

    - 体验型消费（演出、旅行、课程）刻意不算单次成本：
      花 680 看一场演出，"每次 680 元"没有信息量，用它会得出荒谬结论。
    - 订阅类按年化成本评估：月付 30 元听起来便宜，一年是 360 元。
    """
    numbers, missing = {}, []
    numbers["price"] = price if isinstance(price, (int, float)) else None
    if numbers["price"] is None:
        missing.append("price")

    if kind == "experience":
        numbers["cost_per_use"] = None
        numbers["cost_per_use_note"] = "体验型消费不适用单次成本，按预算占比与稀缺性评估"
        numbers["annual_cost"] = None
    elif kind == "subscription":
        annual = price * 12 if period == "month" and isinstance(price, (int, float)) else price
        numbers["cost_per_use"] = None
        numbers["annual_cost"] = annual
        numbers["cost_per_use_note"] = f"订阅类按年化成本评估：{annual} 元/年"
    else:
        cpu = cost_per_use(price, uses_per_month, months)
        numbers["cost_per_use"] = cpu
        if cpu is None:
            missing.append("cost_per_use（需要价格 + 月使用次数 + 使用月数）")
        numbers["annual_cost"] = annual_cost(price, (months or 0) / 12) if months else None

    numbers["budget"] = budget_status(price, profile or {})

    if goals:
        numbers["opportunity"] = opportunity_cost(price, goals)

    numbers["missing"] = missing
    return numbers
