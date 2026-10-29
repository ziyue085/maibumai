"""证据台账校验：来源分级、时效、币种、数值合法性、冲突、日期越界。

这一层存在的唯一目的，是让"编造价格 / 拿旧价当现价 / 拿外币数字当人民币"
从"可能发生"变成"可被检测并阻断"。

校验只读：不抓网页、不推断缺失值、不修补数据。
所有发现都带 ``gate="integrity"``，与"规则一致性"（coherence）分开报告。
"""

from __future__ import annotations

import datetime as _dt
from urllib.parse import urlparse

SOURCE_TIERS = {
    "official": 1,
    "authorized_retail": 2,
    "third_party": 3,
    "user_provided": 4,
    "ugc": 5,
}

STALE_DAYS = {
    "price": 14,
    "price_fast": 7,
    "promo": 3,
    "price_history": 7,
    "spec": 365,
    "review": 180,
    "safety": 30,
    "fx": 3,
    "other": 180,
}

PRICE_KINDS = ("price", "promo")
VALUE_KINDS = ("price", "promo", "price_history", "fx")
SUPPORTED_CURRENCIES = ("CNY", "USD", "EUR", "JPY", "HKD", "GBP", "KRW", "TWD", "SGD")

_PRICE_CONFLICT_RATIO = 0.15


def _finding(code, severity, message, detail=None):
    return {
        "code": code,
        "severity": severity,
        "gate": "integrity",
        "message": message,
        "detail": detail or {},
    }


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        return value
    text = str(value).strip().replace("/", "-")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for fmt in (None, "%Y-%m-%d", "%Y-%m", "%Y%m%d"):
        try:
            if fmt is None:
                return _dt.datetime.fromisoformat(text).date()
            return _dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


# ---------------------------------------------------------------------------
# 币种
# ---------------------------------------------------------------------------
def fx_rates(items, base: str) -> dict:
    """从证据里读出可用汇率。只认 (from, to) 单向与反向倒数。"""
    base = (base or "CNY").upper()
    rates = {}
    for item in items or []:
        if item.get("kind") != "fx":
            continue
        src = str(item.get("base") or "").upper()
        dst = str(item.get("quote") or "").upper()
        value = item.get("value")
        if src and dst and _positive_number(value):
            rates[(src, dst)] = float(value)
    return rates


def convert_amount(value, currency, base, items):
    """把金额换算到 base 币种。无法换算返回 None——不猜汇率。"""
    base = (base or "CNY").upper()
    cur = (currency or base).upper()
    if not _positive_number(value):
        return None
    if cur == base:
        return float(value)
    rates = fx_rates(items, base)
    if (cur, base) in rates:
        return round(float(value) * rates[(cur, base)], 2)
    if (base, cur) in rates and rates[(base, cur)] > 0:
        return round(float(value) / rates[(base, cur)], 2)
    return None


def to_base(items, base="CNY", product=None):
    """把所有价格类证据换算到 base 币种，并换算商品价。

    返回 (归一后的 items, 归一后的 product, findings)。
    换算不出来的，value 置 None 并给 BLOCK——宁可判不了，也不能拿错币种的数字比较。
    """
    base = (base or "CNY").upper()
    items = [dict(i) for i in (items or [])]
    findings = []
    rates = fx_rates(items, base)
    seen_missing = set()

    for idx, item in enumerate(items):
        if item.get("kind") not in PRICE_KINDS + ("price_history",):
            continue
        cur = str(item.get("currency") or base).upper()
        item["currency"] = cur
        value = item.get("value")
        if not _positive_number(value):
            continue  # 交给 check() 报 EV_PRICE_VALUE_INVALID
        if cur == base:
            continue
        converted = convert_amount(value, cur, base, items)
        if converted is None:
            item["original_value"] = value
            item["value"] = None
            item["value_unconverted"] = True
            if cur not in seen_missing:
                seen_missing.add(cur)
                findings.append(
                    _finding(
                        "EV_CURRENCY_UNCONVERTED",
                        "BLOCK",
                        f"价格以 {cur} 计价，但没有可用的 {cur}/{base} 汇率证据，无法与你的预算比较",
                        {"index": idx, "currency": cur, "base": base, "rates": sorted(map(str, rates))},
                    )
                )
        else:
            item["original_value"] = value
            item["value"] = converted

    product = dict(product or {})
    price = product.get("price")
    pcur = str(product.get("currency") or base).upper()
    product["currency"] = pcur
    if _positive_number(price) and pcur != base:
        converted = convert_amount(price, pcur, base, items)
        if converted is None:
            product["original_price"] = price
            product["price"] = None
            product["price_unconverted"] = True
            findings.append(
                _finding(
                    "EV_CURRENCY_UNCONVERTED",
                    "BLOCK",
                    f"商品价 {price} 以 {pcur} 计价，缺少汇率证据，无法判断是否在你的预算内",
                    {"price": price, "currency": pcur, "base": base},
                )
            )
        else:
            product["original_price"] = price
            product["price"] = converted
    return items, product, findings


# ---------------------------------------------------------------------------
# 主校验
# ---------------------------------------------------------------------------
def check(items, as_of=None, price_class="durable", required_kinds=("price",)):
    as_of_date = _parse_date(as_of) or _dt.date.today()
    items = list(items or [])
    findings = []

    if not items:
        findings.append(
            _finding("EV_NONE", "WARN", "没有记录任何证据：可以给出暂缓/否决，但不能断言价格合理")
        )
        for kind in required_kinds:
            findings.append(
                _finding("EV_MISSING_KIND", "BLOCK", f"缺少 {kind} 类证据：无法支撑涉及价格的结论")
            )
        return _pack(findings)

    seen_kinds = set()
    price_points = []

    for idx, item in enumerate(items):
        kind = str(item.get("kind") or "").strip()
        claim = str(item.get("claim") or "").strip()
        src = str(item.get("source_type") or "").strip()
        url = str(item.get("url") or "").strip()
        observed = item.get("observed_at")
        value = item.get("value")
        scope = {"index": idx, "kind": kind, "claim": claim}

        if kind:
            seen_kinds.add(kind)
        if not kind:
            findings.append(_finding("EV_NO_KIND", "WARN", f"第 {idx+1} 条证据缺少 kind", scope))
            kind = "other"
        if not claim:
            findings.append(_finding("EV_NO_CLAIM", "WARN", f"第 {idx+1} 条证据没有具体主张", scope))

        if not src:
            sev = "BLOCK" if kind in PRICE_KINDS else "WARN"
            findings.append(
                _finding("EV_NO_SOURCE_TYPE", sev, f"第 {idx+1} 条证据没有标注来源类型", scope)
            )
            tier = 5
        elif src not in SOURCE_TIERS:
            findings.append(
                _finding("EV_BAD_SOURCE", "WARN", f"未知来源类型 {src!r}，按最低可信度处理", scope)
            )
            tier = 5
        else:
            tier = SOURCE_TIERS[src]

        # 价格类必须有正数金额（币种换不出来的，已有 EV_CURRENCY_UNCONVERTED 说明原因）
        if (kind in PRICE_KINDS or kind == "fx") and not item.get("value_unconverted"):
            if value is None:
                findings.append(
                    _finding("EV_PRICE_VALUE_INVALID", "BLOCK",
                             f"第 {idx+1} 条价格证据没有数值，不能当作报价使用", scope)
                )
            elif not _positive_number(value):
                findings.append(
                    _finding("EV_PRICE_VALUE_INVALID", "BLOCK",
                             f"第 {idx+1} 条价格不是正数（{value!r}），数据不成立", scope)
                )

        if kind == "fx":
            cur_from = str(item.get("base") or "").upper()
            cur_to = str(item.get("quote") or "").upper()
            if cur_from not in SUPPORTED_CURRENCIES or cur_to not in SUPPORTED_CURRENCIES:
                findings.append(
                    _finding("EV_FX_INVALID", "BLOCK",
                             f"汇率证据缺少有效的 base/quote 币种（{cur_from or '?'}/{cur_to or '?'}）",
                             scope)
                )

        needs_source = kind in ("price", "promo", "price_history", "review", "spec", "safety", "fx")
        if needs_source and not url:
            sev = "WARN" if src == "user_provided" else "BLOCK"
            findings.append(_finding("EV_NO_SOURCE", sev, f"{kind} 类主张缺少可核验链接", scope))
        if url and not _valid_url(url):
            findings.append(_finding("EV_BAD_URL", "BLOCK", f"链接不可核验：{url}", scope))

        observed_date = _parse_date(observed)
        if needs_source and observed is None:
            findings.append(_finding("EV_NO_DATE", "BLOCK", f"{kind} 类主张必须标注观测日期", scope))
        elif observed is not None and observed_date is None:
            findings.append(
                _finding("EV_BAD_DATE", "BLOCK", f"观测日期无法解析：{observed!r}，数据不成立", scope)
            )
        elif observed_date and observed_date > as_of_date:
            findings.append(
                _finding("EV_FUTURE_DATE", "BLOCK",
                         f"观测日期 {observed_date} 晚于校验基准日 {as_of_date}，数据不可能成立", scope)
            )

        if kind in PRICE_KINDS or kind == "fx":
            limit_key = "price_fast" if (kind == "price" and price_class == "fast") else kind
            limit = STALE_DAYS.get(limit_key, STALE_DAYS["price"])
            if observed_date and observed_date <= as_of_date:
                age = (as_of_date - observed_date).days
                if age > limit:
                    findings.append(
                        _finding(
                            "EV_STALE",
                            "BLOCK" if kind in PRICE_KINDS else "WARN",
                            f"{'价格' if kind in PRICE_KINDS else '汇率'}观测距今 {age} 天，"
                            f"超过 {limit} 天上限：不得当作现值使用",
                            {**scope, "age_days": age, "limit_days": limit},
                        )
                    )
            if kind in PRICE_KINDS and observed_date and observed_date <= as_of_date:
                if _positive_number(value):
                    subject = str(item.get("subject") or "target").strip().lower()
                    price_points.append(
                        {"value": float(value), "tier": tier, "date": observed_date, "subject": subject}
                    )

        if kind in PRICE_KINDS and observed_date and observed_date <= as_of_date:
            if src == "ugc":
                findings.append(
                    _finding("EV_UGC_PRICE", "WARN", "价格来自内容社区，只能作线索，须回源核实", scope)
                )
            if src == "user_provided":
                findings.append(
                    _finding("EV_USER_PRICE", "WARN",
                             "价格为用户提供，可能是促销价或已失效，须与公开来源交叉核对", scope)
                )

        if kind in ("review", "spec") and observed_date:
            limit = STALE_DAYS.get(kind, 180)
            age = (as_of_date - observed_date).days
            if age > limit:
                findings.append(
                    _finding("EV_STALE", "WARN",
                             f"{kind} 类信息距今 {age} 天，超过 {limit} 天，可能对应旧款或已改版",
                             {**scope, "age_days": age, "limit_days": limit})
                )

    for kind in required_kinds:
        if kind == "price":
            # 促销价、历史价也是价格，不能因为不是 kind="price" 就当成没价格
            if not ({"price", "promo", "price_history"} & seen_kinds):
                findings.append(
                    _finding("EV_MISSING_KIND", "BLOCK", "缺少 price 类证据：无法支撑涉及价格的结论")
                )
        elif kind not in seen_kinds:
            findings.append(
                _finding("EV_MISSING_KIND", "BLOCK", f"缺少 {kind} 类证据：无法支撑涉及价格的结论")
            )

    by_subject: dict = {}
    for point in price_points:
        by_subject.setdefault(point["subject"], []).append(point)
    for subject, points in by_subject.items():
        values = [p["value"] for p in points if p["value"] > 0]
        if len(values) >= 2 and max(values) / min(values) - 1 > _PRICE_CONFLICT_RATIO:
            findings.append(
                _finding(
                    "EV_CONFLICT", "WARN",
                    f"同一商品在多个来源价差 {(max(values)/min(values)-1)*100:.0f}%，超过 "
                    f"{int(_PRICE_CONFLICT_RATIO*100)}%：不得取平均，须说明差异并采信最高等级来源",
                    {"subject": subject, "values": values},
                )
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


def best_price(items):
    """取可信度最高、时间最新的价格。全部不合格则返回 None——不猜。"""
    best = None
    for item in items or []:
        if item.get("kind") not in PRICE_KINDS:
            continue
        if str(item.get("subject") or "target").strip().lower() != "target":
            continue
        value = item.get("value")
        if not _positive_number(value):
            continue
        tier = SOURCE_TIERS.get(str(item.get("source_type") or ""), 5)
        date = _parse_date(item.get("observed_at")) or _dt.date.min
        key = (tier, -date.toordinal())
        if best is None or key < best[0]:
            best = (key, value, item)
    return None if best is None else {"value": best[1], "item": best[2]}


def summarise(items) -> str:
    pick = best_price(items)
    if not pick:
        return "价格未核实"
    item = pick["item"]
    cur = item.get("currency") or "CNY"
    original = item.get("original_value")
    if original is not None and cur:
        return f"{original} {cur}（约 {pick['value']} 元，{item.get('source_type')} / {item.get('observed_at')}）"
    return f"{pick['value']} 元（{item.get('source_type')} / {item.get('observed_at')}）"
