"""成本算术与决策分级。

只做加乘除，不做加权评分。任何缺失输入一律返回 None，
由调用方写明"无法计算"，绝不填默认值假装算过。

这里同时负责**财务硬约束**的计算：可支配余额、单笔硬上限、投资机会成本。
这些数字不是参考值——`budget_status` 的结果会被引擎强制注入结论（见 mbm._evaluate）。
"""

from __future__ import annotations

import store

CATEGORY_KIND = {
    "durable": (
        "数码", "电子", "手机", "电脑", "家电", "家具", "家居", "乐器", "工具",
        "厨具", "运动器材", "手表", "箱包", "鞋", "服饰", "眼镜", "床", "椅",
        "服务器", "主机", "显示器", "键盘", "相机",
    ),
    "consumable": ("食品", "零食", "日耗", "纸品", "洗护", "美妆", "护肤", "清洁", "文具"),
    "subscription": (
        "会员", "订阅", "服务", "云盘", "宽带", "保险", "课程", "API",
        "云服务", "SaaS", "软件",
    ),
    "experience": ("演出", "门票", "旅行", "旅游", "餐饮", "活动", "体验", "展览", "运动课"),
}

DEFAULT_THRESHOLD = 500
DEFAULT_SMALL = 100
BREACH_MULTIPLE = 6
#: 单笔门槛 = max(500, 月可支配 × 25%)。低于 500 元，研究的时间成本超过可能的损失；
#: 超过月可支配四分之一，就是会影响其它安排的决定。
THRESHOLD_RATIO = 0.25
FAST_PRICE_WORDS = ("数码", "电子", "手机", "电脑", "耳机", "服饰", "鞋", "美妆", "护肤", "游戏")

PERIOD_MONTHS = {"month": 1, "quarter": 3, "year": 12}
PERIOD_ZH = {"month": "月", "quarter": "季", "year": "年", "once": "一次性"}


def classify_category(category: str | None) -> str:
    text = str(category or "")
    for kind, words in CATEGORY_KIND.items():
        if any(w in text for w in words):
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
    explicit = store.fact(profile, "finance.single_purchase_threshold")
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
    small = behavior.get("small_amount_fastpath", DEFAULT_SMALL)
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


def budget_status(price, profile) -> dict:
    """ok / tight / breach / unknown。

    breach 与 tight 会被引擎**强制**写入结论，不依赖模型自觉。
    """
    cap = store.fact(profile, "finance.hard_cap_single")
    disc = store.discretionary(profile)

    if not isinstance(price, (int, float)) or price <= 0:
        return {"status": "unknown", "code": "INFO_GAP", "detail": "价格未知，无法做预算判断"}

    if isinstance(cap, (int, float)) and price > cap:
        return {
            "status": "breach", "code": "BUDGET_BREACH", "hard_cap": cap, "reason": "hard_cap",
            "detail": f"{price} 元超过你自设的单笔上限 {cap} 元",
        }

    if not isinstance(disc, (int, float)) or disc <= 0:
        return {"status": "unknown", "code": "INFO_GAP",
                "detail": "未设置收入/固定支出，无法做预算判断"}

    share = budget_share(price, disc)
    months = months_to_afford(price, disc)

    if price > disc * BREACH_MULTIPLE:
        return {"status": "breach", "code": "BUDGET_BREACH", "share": share, "months": months,
                "reason": "multiple",
                "detail": f"相当于 {months} 个月的全部可支配余额"}
    if price > disc:
        return {"status": "tight", "code": "BUDGET_TIGHT", "share": share, "months": months,
                "reason": "over_month",
                "detail": f"占月可支配 {share:.0%}，需跨 {months} 个月支出"}
    return {"status": "ok", "code": "AFFORDABLE_WITHIN_BUDGET", "share": share, "months": months,
            "detail": f"占月可支配 {share:.0%}"}


def emergency_status(price, profile) -> dict:
    """应急金是否够厚。不足时做大额可选消费属于加杠杆，要提示。"""
    fund = store.fact(profile, "finance.emergency_fund")
    expense = store.fact(profile, "finance.monthly_fixed_expense")
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
    """把这笔钱换成"未来少多少钱"。只有用户自己设了收益率才算，绝不替他假设。"""
    rate = store.fact(profile, "invest.expected_annual_return")
    if not isinstance(rate, (int, float)) or rate <= 0:
        return None
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    r = rate / 100 if rate > 1 else rate  # 允许填 8 或 0.08
    future = price * (1 + r) ** years
    return {
        "years": years,
        "rate_pct": round(r * 100, 2),
        "future_value": round(future, 2),
        "forgone": round(future - price, 2),
        "text": f"按你设定的 {round(r*100,2)}% 年化，{price} 元 {years} 年后是 {round(future)} 元",
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
