"""证据台账校验：来源分级、时效、冲突、越界日期。

这一层存在的唯一目的，是让"编造价格 / 编造评价 / 拿两个月前的价格当现价"
从"可能发生"变成"可被检测并阻断"。

校验只读：不抓网页、不推断缺失值、不修补数据。
"""

from __future__ import annotations

import datetime as _dt
from urllib.parse import urlparse

#: 来源等级，数字越小越可信。价格类主张只接受 1-3 级（4 级须标注为用户提供）。
SOURCE_TIERS = {
    "official": 1,          # 品牌官网 / 官方旗舰店 / 官方公告
    "authorized_retail": 2,  # 授权零售商 / 平台自营
    "third_party": 3,       # 第三方商家 / 比价站
    "user_provided": 4,     # 用户截图或口述
    "ugc": 5,               # 内容社区 / 开箱视频 / 评论
}

#: 各类主张的最长可接受时效（天）。经验取值，可按品类调整。
STALE_DAYS = {
    "price": 14,            # 公开报价默认 14 天
    "price_fast": 7,        # 电子、服饰、美妆等价格波动快的品类
    "promo": 3,             # 促销价 / 优惠券
    "price_history": 7,     # 历史价格曲线
    "spec": 365,
    "review": 180,
    "safety": 30,           # 召回、质量事件
    "other": 180,
}

PRICE_KINDS = ("price", "promo")

SEVERITY = ("BLOCK", "WARN", "INFO")

_PRICE_CONFLICT_RATIO = 0.15  # 两个价格源相差超过 15% 视为冲突


def _finding(code, severity, message, detail=None):
    return {"code": code, "severity": severity, "message": message, "detail": detail or {}}


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
                d = _dt.datetime.fromisoformat(text)
                return d.date()
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


def check(items, as_of=None, price_class="durable", required_kinds=("price",)):
    """校验一批证据记录。

    参数
    ----
    items : list[dict]
        每条至少含 ``kind``、``claim``、``source_type``。
        价格类还须含 ``url`` 与 ``observed_at``（YYYY-MM-DD）。
    as_of : date | str | None
        校验基准日，默认今天。允许传入历史日期以复现旧决策。
    price_class : "durable" | "fast"
        耐用品 30 天，快速变动品类 7 天。
    required_kinds : tuple
        缺失即视为阻断。传空元组表示不要求必须含价格。
    """
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
        blocking = sum(1 for f in findings if f["severity"] == "BLOCK")
        return {
            "ok": blocking == 0,
            "blocking": blocking,
            "warnings": sum(1 for f in findings if f["severity"] == "WARN"),
            "findings": findings,
        }

    seen_kinds = set()
    price_points = []

    for idx, item in enumerate(items):
        kind = str(item.get("kind") or "").strip()
        claim = str(item.get("claim") or "").strip()
        src = str(item.get("source_type") or "").strip()
        url = str(item.get("url") or "").strip()
        observed = item.get("observed_at")
        scope = {"index": idx, "kind": kind, "claim": claim}

        if kind:
            seen_kinds.add(kind)
        if not kind:
            findings.append(_finding("EV_NO_KIND", "WARN", f"第 {idx+1} 条证据缺少 kind", scope))
            kind = "other"
        if not claim:
            findings.append(_finding("EV_NO_CLAIM", "WARN", f"第 {idx+1} 条证据没有具体主张", scope))

        if src not in SOURCE_TIERS:
            findings.append(
                _finding("EV_BAD_SOURCE", "WARN", f"未知来源类型 {src!r}，按最低可信度处理", scope)
            )
            tier = 5
        else:
            tier = SOURCE_TIERS[src]

        needs_source = kind in ("price", "promo", "price_history", "review", "spec", "safety")
        if needs_source and not url:
            # 用户口述价格本来就没有链接，这是可接受的，但必须被标记为"未核实"
            sev = "WARN" if src == "user_provided" else "BLOCK"
            findings.append(
                _finding("EV_NO_SOURCE", sev, f"{kind} 类主张缺少可核验链接", scope)
            )
        if url and not _valid_url(url):
            findings.append(_finding("EV_BAD_URL", "BLOCK", f"链接不可核验：{url}", scope))

        observed_date = _parse_date(observed)
        if needs_source and observed is None:
            findings.append(
                _finding("EV_NO_DATE", "BLOCK", f"{kind} 类主张必须标注观测日期", scope)
            )
        elif observed is not None and observed_date is None:
            findings.append(
                _finding("EV_BAD_DATE", "WARN", f"观测日期无法解析：{observed!r}", scope)
            )
        elif observed_date and observed_date > as_of_date:
            findings.append(
                _finding(
                    "EV_FUTURE_DATE",
                    "BLOCK",
                    f"观测日期 {observed_date} 晚于校验基准日 {as_of_date}，数据不可能成立",
                    scope,
                )
            )

        if kind in PRICE_KINDS and observed_date and observed_date <= as_of_date:
            limit_key = "price_fast" if (kind == "price" and price_class == "fast") else kind
            limit = STALE_DAYS.get(limit_key, STALE_DAYS["price"])
            age = (as_of_date - observed_date).days
            if age > limit:
                findings.append(
                    _finding(
                        "EV_STALE",
                        "BLOCK",
                        f"价格观测距今 {age} 天，超过 {limit} 天上限：不得当作现价使用",
                        {**scope, "age_days": age, "limit_days": limit},
                    )
                )
            value = item.get("value")
            if isinstance(value, (int, float)):
                price_points.append((value, tier, observed_date))

            if src == "ugc":
                findings.append(
                    _finding("EV_UGC_PRICE", "WARN", "价格来自内容社区，只能作线索，须回源核实", scope)
                )
            if src == "user_provided":
                findings.append(
                    _finding(
                        "EV_USER_PRICE",
                        "WARN",
                        "价格为用户提供，可能是促销价或已失效，须与公开来源交叉核对",
                        scope,
                    )
                )

        if kind in ("review", "spec") and observed_date:
            limit = STALE_DAYS.get(kind, 180)
            age = (as_of_date - observed_date).days
            if age > limit:
                findings.append(
                    _finding(
                        "EV_STALE",
                        "WARN",
                        f"{kind} 类信息距今 {age} 天，超过 {limit} 天，可能对应旧款或已改版",
                        {**scope, "age_days": age, "limit_days": limit},
                    )
                )

    for kind in required_kinds:
        if kind not in seen_kinds:
            findings.append(
                _finding("EV_MISSING_KIND", "BLOCK", f"缺少 {kind} 类证据：无法支撑涉及价格的结论")
            )

    if len(price_points) >= 2:
        values = [p[0] for p in price_points if p[0] > 0]
        if len(values) >= 2 and max(values) / min(values) - 1 > _PRICE_CONFLICT_RATIO:
            findings.append(
                _finding(
                    "EV_CONFLICT",
                    "WARN",
                    f"多个来源价差 {(max(values)/min(values)-1)*100:.0f}%，超过 "
                    f"{int(_PRICE_CONFLICT_RATIO*100)}%：不得取平均，须说明差异并采信最高等级来源",
                    {"values": values, "tiers": [p[1] for p in price_points]},
                )
            )

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
        value = item.get("value")
        if not isinstance(value, (int, float)):
            continue
        tier = SOURCE_TIERS.get(str(item.get("source_type") or ""), 5)
        date = _parse_date(item.get("observed_at")) or _dt.date.min
        key = (tier, -date.toordinal())  # 先比可信度，再比新鲜度
        if best is None or key < best[0]:
            best = (key, value, item)
    return None if best is None else {"value": best[1], "item": best[2]}


def summarise(items) -> str:
    """给输出层用的一行摘要，明确标注可信度与时间。"""
    pick = best_price(items)
    if not pick:
        return "价格未核实"
    item = pick["item"]
    src = item.get("source_type")
    date = item.get("observed_at")
    return f"{pick['value']} 元（{src} / {date}）"
