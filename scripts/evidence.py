"""证据台账校验：来源分级、时效、币种、数值合法性、冲突、日期越界。

这一层存在的唯一目的，是让"编造价格 / 拿旧价当现价 / 拿外币数字当人民币"
从"可能发生"变成"可被检测并阻断"。

校验只读：不抓网页、不推断缺失值、不修补数据。
所有发现都带 ``gate="integrity"``，与"规则一致性"（coherence）分开报告。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import math
import re
from urllib.parse import urlparse

SOURCE_TIERS = {
    "official": 1,
    "authorized_retail": 2,
    "third_party": 3,
    "user_provided": 4,
    "ugc": 5,
}

#: 汇率被拒的原因 → 给人看的话。用户需要知道"为什么换不出来"。
FX_REJECTION_ZH = {
    "FX_NO_PAIR": "没有写清是哪两个币种之间的汇率",
    "FX_SAME_PAIR": "两个币种相同，不需要汇率",
    "FX_BAD_VALUE": "汇率数值不成立（不是正的有限数，或根本不是数字）",
    "FX_BAD_CURRENCY": "币种不在可换算列表里",
    "FX_OUT_OF_RANGE": "汇率数值超出绝对边界，像是单位写错了",
    "FX_IMPLAUSIBLE": "汇率偏离该币种对的合理区间太多",
    "FX_WEAK_SOURCE": "来源是口述或社区，不足以作为换算依据",
    "FX_NO_LINK": "没有可核验的链接",
    "FX_NO_DATE": "没有标注观测日期",
    "FX_FUTURE_DATE": "观测日期在未来",
    "FX_STALE": "已超过 3 天有效期",
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

#: 同一等级来源互相差多少，就说明"至少有一个是错的"。
#: 比跨等级冲突（15%）严：官方价与社区价不同是常态，**两个官方价不同是事故**。
SAME_TIER_CONFLICT_TOLERANCE = 0.05

#: 汇率的绝对边界。作用是拦住"根本不像汇率"的量级（1e-9、1e300）。
#: 它**管不住**"区间内但不合理"——USD/CNY=1 和 =100 都落在 1e-4~1e4 里，
#: 那两个要由下面的锚点区间拦。
FX_MIN_RATE = 1e-4
FX_MAX_RATE = 1e4

#: 各币种对人民币的**数量级锚点**（2026-10 前后）。
#:
#: ⚠️ 这不是实时汇率，也不该被当成实时汇率用。它只有一个用途：判断一条汇率
#: 是不是"离这个币种对该有的量级太远"。USD/CNY=1 与 =100 离锚点 7.1 分别是
#: 7.1 倍和 14 倍，一眼就不合理；真实汇率一天内的波动远不到这个量级。
FX_ANCHORS = {
    "CNY": 1.0,
    "USD": 7.1,
    "EUR": 8.2,
    "JPY": 0.047,
    "HKD": 0.91,
    "GBP": 9.5,
    "KRW": 0.0052,
    "TWD": 0.22,
    "SGD": 5.5,
}
FX_ANCHOR_DATE = "2026-10-01"

#: 允许偏离锚点的倍数。5 倍是照着**支持币种近三十年的波动区间**定的：
#: 这些币种对里最极端的历史漂移（JPY/CNY 0.048→0.083）也只有 1.8 倍。
#: ⚠️ 新增币种（尤其 VND / IDR 这类小面额币种）必须重新校准，否则真实汇率会被误杀。
FX_PLAUSIBLE_FACTOR = 5.0
#: 锚点表过期（距 FX_ANCHOR_DATE 超过一年）后只能做极粗的量级检查：
#: 拿三年前的锚点卡 5 倍，等于拿过期数据当事实。
FX_PLAUSIBLE_FACTOR_RELAXED = 50.0
FX_ANCHOR_MAX_AGE_DAYS = 365

#: 价格口径。同一个商品可以有好几个"价"，它们**不是**互相矛盾的数据，
#: 而是不同口径：官方标价 1099 与授权商城促销 899 可以都是真的。
#: 到手价的优先级从高到低：含税到手 > 实际成交 > 促销 > 官方标价 > 不含税。
PRICE_BASIS_PRIORITY = {
    "tax_inclusive": 0,
    "street": 1,
    "promo": 2,
    "msrp": 3,
    "tax_exclusive": 4,
}
PRICE_BASIS_UNKNOWN = "unknown"


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


def _url_syntax_ok(url: str) -> bool:
    """**只**验证 URL 结构。不代表可访问、不代表是官方域名、更不代表页面写着这个价。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _positive_number(value) -> bool:
    """是不是一个**能进算式的正数**。

    三件事一起判，少一件就会让脏数据混进计算：

    1. 是数字，且不是布尔——``True == 1``，「汇率=True」必须被挡住；
    2. 大于 0；
    3. **有限**——``float('inf') > 0`` 成立，`nan` 会让后续所有比较静默失效，
       两者都能一路走到预算与比价里，所以必须在这里拦掉。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value <= 0:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        # 超出 float 范围的巨型整数（如 10**400）：不算可用数字
        return False


# ---------------------------------------------------------------------------
# 币种
# ---------------------------------------------------------------------------
def fx_expected_rate(src: str, dst: str) -> float | None:
    """这个币种对按锚点表"应该"大约是多少。币种不在锚点表里就返回 None。"""
    a = FX_ANCHORS.get((src or "").upper())
    b = FX_ANCHORS.get((dst or "").upper())
    if not a or not b:
        return None
    return a / b


def fx_anchor_age_days(as_of=None) -> int:
    """锚点表相对校验基准日的年龄（天）。超过一年，锚点只能当极粗的量级参照。"""
    ref = _parse_date(as_of) or _dt.date.today()
    anchor = _parse_date(FX_ANCHOR_DATE)
    return abs((ref - anchor).days) if anchor else 0


def fx_plausibility(item: dict, as_of=None) -> dict:
    """**合理性**：这个汇率离该币种对该有的量级有多远。

    与"合法性"（``_positive_number``）是两件事——USD/CNY=1 和 =100 都是合法的正数，
    但都不是 2026 年该有的汇率。合法性管"能不能进算式"，合理性管"进算式之前
    要不要先怀疑一下"。

    ⚠️ 通过这一关**不等于汇率是真的**。它只说明"没有离谱到量级都不对"；
    差了 30% 的汇率照样能通过，因为本仓库没有独立的实时汇率源去核对。
    """
    src = str(item.get("base") or "").upper()
    dst = str(item.get("quote") or "").upper()
    expected = fx_expected_rate(src, dst)
    value = item.get("value")
    out = {"ok": True, "expected": expected, "anchor_outdated": False, "why": None}
    if expected is None or not _positive_number(value):
        # 锚点表里没有的币种对：不做合理性判断，也不能因此放行——
        # 币种不在支持列表时 fx_rejection 已经在前面拦掉了。
        return out
    old = fx_anchor_age_days(as_of) > FX_ANCHOR_MAX_AGE_DAYS
    factor = FX_PLAUSIBLE_FACTOR_RELAXED if old else FX_PLAUSIBLE_FACTOR
    ratio = float(value) / expected
    out["anchor_outdated"] = old
    out["ratio"] = round(ratio, 4)
    out["factor"] = factor
    if ratio > factor or ratio < 1 / factor:
        out["ok"] = False
        out["why"] = "FX_IMPLAUSIBLE"
    return out


def fx_rate_report(item: dict, base: str, as_of=None) -> dict:
    """一条汇率证据的**四层检查**，分开看，不许合并成一句"汇率已核实"。

    | 层 | 问的问题 | 通过代表什么 |
    |---|---|---|
    | ``validity`` | 是正的有限数吗 | 能进算式 |
    | ``plausibility`` | 离该币种对的量级离谱吗 | 没有离谱到量级都不对 |
    | ``source`` | 来源/链接/日期过关吗 | 出处看得过去 |
    | ``content`` | 页面正文核验过吗 | **永远 False**：汇率也只有链接格式，没有正文 |

    四层全过也只说明"没有理由拒绝"，**不说明这个汇率是真的**。
    真正的真值需要独立的汇率源，本仓库没有，所以 ``content`` 恒为 False。
    """
    rejection = fx_rejection(item, base, as_of)
    plaus = fx_plausibility(item, as_of)
    return {
        "validity": {"ok": _positive_number(item.get("value")),
                     "why": None if _positive_number(item.get("value"))
                     else "FX_BAD_VALUE"},
        "plausibility": {"ok": plaus["ok"], "why": plaus["why"],
                         "expected": plaus["expected"],
                         "ratio": plaus.get("ratio"),
                         "anchor_outdated": plaus["anchor_outdated"]},
        "source": {"ok": rejection not in ("FX_WEAK_SOURCE", "FX_NO_LINK", "FX_NO_DATE",
                                           "FX_FUTURE_DATE", "FX_STALE"),
                   "why": rejection if rejection in ("FX_WEAK_SOURCE", "FX_NO_LINK",
                                                     "FX_NO_DATE", "FX_FUTURE_DATE",
                                                     "FX_STALE") else None},
        # 汇率证据同样只有 url 格式，没有任何页面正文：这一层无法核验。
        "content": {"ok": False, "why": "NO_PAGE_TEXT"},
        "accepted": rejection is None,
        "rejection": rejection,
    }


def fx_rejection(item: dict, base: str, as_of=None) -> str | None:
    """这条汇率能不能用。能用返回 None，否则返回不能用的原因码。

    汇率直接乘进价格，错一位就是几十上百块的偏差，所以它比价格更严：
    **来源不可信或没有链接的汇率，一律不进计算**——宁可换算失败，也不拿
    "朋友说 7.3" 去当今天的汇率。

    四道关，顺序有意义：先把"数字本身成不成立 / 币种认不认 / 量级离不离谱"
    判完，再看出处。因为一个不合格的数字不该靠"来源很官方"蒙混过关。
    """
    src = str(item.get("base") or "").upper()
    dst = str(item.get("quote") or "").upper()
    value = item.get("value")
    if not (src and dst):
        return "FX_NO_PAIR"
    if src not in SUPPORTED_CURRENCIES or dst not in SUPPORTED_CURRENCIES:
        # 先认币种再看数值：不在列表里的币种，数值再漂亮也不该进算式
        return "FX_BAD_CURRENCY"
    if src == dst:
        return "FX_SAME_PAIR"
    if not _positive_number(value):
        return "FX_BAD_VALUE"
    if not (FX_MIN_RATE <= float(value) <= FX_MAX_RATE):
        return "FX_OUT_OF_RANGE"
    if not fx_plausibility(item, as_of)["ok"]:
        # 区间内的不合理值：USD/CNY=1 与 =100 都在这儿被拦
        return "FX_IMPLAUSIBLE"
    tier = SOURCE_TIERS.get(str(item.get("source_type") or ""), 5)
    if tier > 3:
        # 口述、社区、来源缺失：可以做线索，不能做乘数
        return "FX_WEAK_SOURCE"
    if not _url_syntax_ok(str(item.get("url") or "").strip()):
        return "FX_NO_LINK"
    ref = _parse_date(as_of) or _dt.date.today()
    when = _parse_date(item.get("observed_at"))
    if when is None:
        return "FX_NO_DATE"
    if when > ref:
        return "FX_FUTURE_DATE"
    if (ref - when).days > STALE_DAYS["fx"]:
        return "FX_STALE"
    return None


def fx_rates(items, base: str, as_of=None) -> dict:
    """从证据里读出**可用**的汇率。

    汇率是时效性最强的数据，也是唯一直接乘进价格的数字。所以这里要过八关：
    币种在支持列表里、币种对完整、数值为正且有限、在绝对边界内、**偏离锚点不超过
    允许倍数**、来源等级 ≤ 3、有可核验链接、日期在 3 天保鲜期内。
    任一关不过就跳过——换算失败会阻断，比用错汇率好。

    ⚠️ 八关全过**不等于汇率是真的**：这些检查只能证明"没有理由拒绝它"，
    本仓库没有独立的实时汇率源去核对真值，也没有页面正文可供内容核验。

    ⚠️ 这里是换算的**唯一入口**。`convert_amount` / `to_base` 都只认本函数
    返回的汇率，不存在"先算出来、事后拦截"的路径。
    """
    base = (base or "CNY").upper()
    rates = {}
    for item in items or []:
        if item.get("kind") != "fx":
            continue
        if fx_rejection(item, base, as_of) is not None:
            continue
        src = str(item.get("base") or "").upper()
        dst = str(item.get("quote") or "").upper()
        rates[(src, dst)] = float(item["value"])
    return rates


def convert_amount(value, currency, base, items, as_of=None):
    """把金额换算到 base 币种。无法换算返回 None——不猜汇率。"""
    base = (base or "CNY").upper()
    cur = (currency or base).upper()
    if not _positive_number(value):
        return None
    if cur == base:
        return float(value)
    rates = fx_rates(items, base, as_of)
    if (cur, base) in rates:
        return _finite_or_none(float(value) * rates[(cur, base)])
    if (base, cur) in rates and rates[(base, cur)] > 0:
        return _finite_or_none(float(value) / rates[(base, cur)])
    return None


def _finite_or_none(number) -> float | None:
    """换算出非有限数（溢出到 inf、算出 nan）等于没换算出来。"""
    if not math.isfinite(number):
        return None
    return round(number, 2)


def to_base(items, base="CNY", product=None, as_of=None):
    """把所有价格类证据换算到 base 币种，并换算商品价。

    返回 (归一后的 items, 归一后的 product, findings)。
    换算不出来的，value 置 None 并给 BLOCK——宁可判不了，也不能拿错币种、
    或者拿三个月前的汇率去比较今天的价格。
    """
    base = (base or "CNY").upper()
    items = [dict(i) for i in (items or [])]
    findings = []
    rates = fx_rates(items, base, as_of)
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
        converted = convert_amount(value, cur, base, items, as_of)
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
                        f"价格以 {cur} 计价，但没有可用的 {cur}/{base} 汇率证据"
                        f"（汇率须在 3 天内，且不能晚于校验基准日），无法与你的预算比较",
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
        converted = convert_amount(price, pcur, base, items, as_of)
        if converted is None:
            product["original_price"] = price
            product["price"] = None
            product["price_unconverted"] = True
            findings.append(
                _finding(
                    "EV_CURRENCY_UNCONVERTED",
                    "BLOCK",
                    f"商品价 {price} 以 {pcur} 计价，缺少有效汇率证据，无法判断是否在你的预算内",
                    {"price": price, "currency": pcur, "base": base},
                )
            )
        else:
            product["original_price"] = price
            product["price"] = converted
    return items, product, findings


def url_syntax_ok(url) -> bool:
    """公开版本：URL 结构检查。

    ⚠️ **只查格式**。它能区分"有没有给链接"，**不能**证明链接可打开、
    域名属于官方渠道、或页面上确实是这个价格。后两件事只能由取证方（模型）
    去读页面，并把结果写进证据的 ``page_price`` 字段——本函数不参与。
    """
    return _url_syntax_ok(str(url or "").strip())


#: 兼容旧名。名字里的 "valid" 容易被误读成"已核实"，新代码请用 url_syntax_ok。
valid_url = url_syntax_ok


def quote_content_problem(item: dict, tolerance: float = 0.02) -> str | None:
    """``page_price`` 这一格填得对不对。没问题返回 None，否则返回问题码。

    两种问题，必须分开：

    | 码 | 含义 |
    |---|---|
    | ``PAGE_PRICE_INVALID`` | 填了，但填的是 0、负数、布尔、非数字、``nan``/``inf``——**不成立** |
    | ``QUOTE_CONTENT_MISMATCH`` | 填了正数，但与引用价差超过 2%——**自相矛盾** |

    两者都不能拿到 ``page`` 级核验标记。以前只判第二种：``page_price=0``
    或 ``page_price=True``（布尔是 int 的子类）会被当成"没填"直接放过，
    于是"填了个假页面价"反而能换来"页面核对过"的标记。

    ⚠️ 它**不能**证明模型真的读了那个页面，也**不能**证明域名是官方渠道，
    更不能证明这个数字来自页面正文（而不是随手编的）。这三件事本仓库做不到，
    别把这里的检查当成"内容已核实"。
    """
    if "page_price" not in item:
        return None
    page = item.get("page_price")
    if page is None:
        return None  # 显式留空 = 没填，按"没读过页面"处理
    if not _positive_number(page):
        return "PAGE_PRICE_INVALID"
    value = item.get("value")
    if not _positive_number(value):
        return None  # 引用价本身不成立时由别处报，这里不重复
    if abs(float(page) - float(value)) / float(value) > tolerance:
        return "QUOTE_CONTENT_MISMATCH"
    return None


#: 页面价这一格的状态。给调用方一个比"是不是 None"更可读的答案。
def page_price_ok(item: dict, tolerance: float = 0.02) -> bool:
    """这条报价是否配得上 ``reported`` 级标记：填了、是正数、且与引用价一致。

    留空（没有这个键，或显式填 null）只算"没读过页面"，不算核对过。

    ⚠️ 这里连 ``reported`` 都算不上最高级——真正的"页面核对过"要
    :func:`page_quote_check` 给出 ``content`` 级（有正文可复核）。
    """
    if not isinstance(item, dict) or item.get("page_price") is None:
        return False
    return quote_content_problem(item, tolerance) is None


_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in_text(text: str) -> list:
    """从页面正文里读出所有数字。千分位逗号先去掉，避免 1,299 读成 1。"""
    return [float(m.group(0).replace(",", "")) for m in _NUMBER_RE.finditer(text or "")]


def _price_appears(value, candidates, tolerance: float = 0.02) -> bool:
    """报价上下文核验：这个价在候选集合里出现过吗。

    容差取 ``max(value * tolerance, 0.005)``：6.95 必须原样出现，
    但 1299 与 1299.0 不该被当成两个数。
    """
    if not _positive_number(value):
        return False
    value = float(value)
    limit = max(value * tolerance, 0.005)
    return any(abs(float(c) - value) <= limit for c in candidates if _positive_number(c))


def page_quote_check(item: dict, tolerance: float = 0.02) -> dict:
    """这条报价到底核实到哪一层。返回 ``{"level", "problem", "detail"}``。

    | ``level`` | 含义 | 谁做的 | 引擎能不能复核 |
    |---|---|---|---|
    | ``content`` | 给了页面正文（或页面价格集合），哈希自洽，且**正文里确实有这个价** | 引擎 | 能 |
    | ``reported`` | 只填了 ``page_price``，没有正文可对照 | 取证方自述 | 不能 |
    | ``None`` | 没填 ``page_price``，或填的东西不成立 | — | — |

    以前只有一档 ``page``，名字听起来像"引擎核对过页面"，实际只是"取证方报了个
    数字、引擎检查了它和引用价一不一致"。现在拆开：``reported`` 只代表自述，
    只有 ``content`` 才配叫"页面核对过"。

    可提供的页面内容（任选，给得越多核验越强）：

    - ``page_text``：页面上包含该价格的一段正文（或全文）；
    - ``page_text_sha256``：取证方声明的这段正文的 sha256（引擎会重算比对）；
    - ``page_price_set``：页面上出现过的价格集合（不能贴正文时的退路）。

    ⚠️ 哈希只能证明"你给我的正文和你声明的哈希一致（中途没被改）"，
    **不能**证明这段正文来自那个 URL，也不能证明域名是官方渠道。
    """
    out = {"level": None, "problem": None, "detail": {}}
    if not isinstance(item, dict) or item.get("page_price") is None:
        out["detail"]["how"] = "没有填 page_price：连自述都没有"
        return out

    problem = quote_content_problem(item, tolerance)
    if problem:
        out["problem"] = problem
        out["detail"]["how"] = "page_price 这一格本身不成立或与引用价矛盾"
        return out
    out["level"] = "reported"

    text = item.get("page_text")
    has_text = isinstance(text, str) and text.strip() != ""
    price_set = item.get("page_price_set")
    if not has_text and not isinstance(price_set, (list, tuple)):
        out["detail"]["how"] = "只填了 page_price，没有页面正文可对照：这是取证方自述"
        return out

    candidates = []
    if has_text:
        # 1) 哈希自洽性：只证明正文没被改过，不证明正文来自那个页面
        declared = str(item.get("page_text_sha256") or "").strip().lower()
        if declared:
            actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if actual != declared:
                out["problem"] = "PAGE_TEXT_HASH_MISMATCH"
                out["level"] = None
                out["detail"].update({"declared": declared, "actual": actual,
                                      "how": "正文与声明的哈希对不上"})
                return out
            out["detail"]["hash"] = "ok"
        candidates += _numbers_in_text(text)
    if isinstance(price_set, (list, tuple)):
        candidates += list(price_set)

    # 2) 报价上下文：这个价在页面内容里真的出现过吗
    if not _price_appears(item.get("page_price"), candidates, tolerance):
        out["problem"] = "PAGE_TEXT_NO_QUOTE"
        out["level"] = None
        out["detail"].update({"page_price": item.get("page_price"),
                              "candidates": sorted({round(float(c), 4) for c in candidates
                                                    if _positive_number(c)})[:20],
                              "how": "页面内容里找不到这个价"})
        return out

    out["level"] = "content"
    out["detail"]["how"] = ("页面正文片段里出现了这个价"
                            + ("，且哈希自洽" if out["detail"].get("hash") == "ok" else ""))
    return out


def _norm_basis(item: dict) -> str:
    b = str(item.get("price_basis") or "").strip().lower()
    return b if b in PRICE_BASIS_PRIORITY else PRICE_BASIS_UNKNOWN


def price_comparison_key(item: dict) -> tuple:
    """两条报价能不能放在同一把尺上比：口径 + 规格 + 地区，三者都要对得上。

    三者都未标注时视为同一组（"没说就是默认口径"）；只要有一边标了另一边没标，
    就不能比——**不知道**不等于**相同**。
    """
    return (_norm_basis(item),
            str(item.get("variant") or "").strip().lower(),
            str(item.get("region") or "").strip().upper())


def price_basis_groups(items, subject="target", max_tier=3) -> dict:
    """可信报价按（口径 / 规格 / 地区）分组。

    同一个商品可以有好几个"价"：官网建议零售价 1099、官方授权商城促销 899、
    海外版 1299。它们不是互相矛盾的数据，是**不同口径**。把它们塞进同一个
    列表去比大小，得到的"冲突"是假的。

    分组范围是所有**可信**来源（等级 ≤ ``max_tier``），不只看最高等级那一档：
    官网标价（official）与授权商城促销价（authorized_retail）本来就是不同等级，
    只取最高等级会把合法的促销价排除掉，反而把"促销更便宜"误判成"输入价与官方价不符"。

    返回 ``{(basis, variant, region): [(tier, 值), ...]}``；没有可信报价返回空字典。
    """
    groups: dict = {}
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in PRICE_KINDS:
            continue
        if str(item.get("subject") or "target").strip().lower() != subject:
            continue
        if not _positive_number(item.get("value")):
            continue
        tier = SOURCE_TIERS.get(str(item.get("source_type") or ""), 5)
        if tier > max_tier:
            continue
        groups.setdefault(price_comparison_key(item), []).append(
            (tier, float(item["value"])))
    return groups


def _best_tier_values(pairs) -> list:
    """一组报价里**最高等级**那一档的值。

    跨等级差异是正常的（官网 1099 与第三方站 899 不同很正常），
    只有同一等级内部打架才说明至少有一条是错的。
    """
    if not pairs:
        return []
    best = min(tier for tier, _ in pairs)
    return [value for tier, value in pairs if tier == best]


def basis_conflicts(groups: dict, tolerance: float = SAME_TIER_CONFLICT_TOLERANCE) -> dict:
    """各组内部**同等级**报价是否打架。返回 ``{key: 冲突的值列表}``。"""
    out = {}
    for key, pairs in groups.items():
        values = _best_tier_values(pairs)
        if tier_conflict(values, tolerance):
            out[key] = sorted(set(values))
    return out


def settlement_basis_values(items, subject="target", max_tier=3) -> tuple:
    """**到手价**那一组报价。返回 ``(values, ambiguity)``。

    到手价口径优先级：含税到手 > 实际成交 > 促销 > 官方标价 > 不含税。
    官网标价 1099 与促销 899 同时存在时，到手价是 899，不是"两个价打架"。
    选定口径后，组内再取最高等级那一档。

    ``ambiguity``：

    - ``None``——分出来了，用 ``values`` 去比对输入价；
    - ``"NO_CREDIBLE"``——可信报价一条都没有（只有口述/社区）；
    - ``"AMBIGUOUS"``——并列最高优先级不止一组（比如 128GB 与 256GB 两个规格
      都没标口径），**不知道用户要买的是哪一个**，这时只能暂缓。
    """
    groups = price_basis_groups(items, subject, max_tier)
    if not groups:
        return [], "NO_CREDIBLE"
    ranked = sorted(groups.items(),
                    key=lambda kv: PRICE_BASIS_PRIORITY.get(kv[0][0], 5))
    top_priority = PRICE_BASIS_PRIORITY.get(ranked[0][0][0], 5)
    tied = [kv for kv in ranked if PRICE_BASIS_PRIORITY.get(kv[0][0], 5) == top_priority]
    if len(tied) > 1:
        return [], "AMBIGUOUS"
    return _best_tier_values(tied[0][1]), None


def groups_differ(groups: dict, ratio: float = _PRICE_CONFLICT_RATIO) -> bool:
    """不同口径的报价之间差得够不够多，多到值得提醒一句"这俩不是一回事"。

    ``groups`` 是 :func:`price_basis_groups` 的结果，值为 ``[(tier, 值), ...]``。
    """
    flat = [value for pairs in (groups or {}).values() for _, value in pairs
            if _positive_number(value)]
    if len(flat) < 2:
        return False
    return max(flat) / min(flat) - 1 > ratio


def tier_conflict(values, tolerance: float = SAME_TIER_CONFLICT_TOLERANCE) -> bool:
    """同一等级来源给出的价，互相差到"至少有一个是错的"。

    跨等级冲突是常态（官方价与社区价不同很正常，报 WARN 提醒说明差异即可）；
    **同等级冲突是事故**——两个官方来源报出不同的价，谁都不能被采信，
    取平均、取最新、取便宜的那个都是在替用户挑。
    """
    nums = [float(v) for v in values or [] if _positive_number(v)]
    if len(nums) < 2:
        return False
    low = min(nums)
    return (max(nums) - low) / low > tolerance


def credible_price_values(items, subject="target", max_tier=3) -> list:
    """某一主体下**最可信那一档**来源给出的价格集合。

    低可信报价不能用来"凑近"输入价：官方 899 与社区 599 并存时只有 899 算数，
    否则拿 599 一凑，输入价 600 就"对得上"了——**冲突被低可信来源掩盖**。

    - 只认价格类证据、只认正数且有限的值；
    - 取等级最高的那一档（数值最小 = 最可信）；
    - 那一档超过 ``max_tier``（口述/社区）时返回空列表：**没有可信报价**，
      而不是"有一条报价"。
    """
    points = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") not in PRICE_KINDS:
            continue
        if str(item.get("subject") or "target").strip().lower() != subject:
            continue
        value = item.get("value")
        if not _positive_number(value):
            continue
        points.append((SOURCE_TIERS.get(str(item.get("source_type") or ""), 5), float(value)))
    if not points:
        return []
    best = min(tier for tier, _ in points)
    if best > max_tier:
        return []
    return [value for tier, value in points if tier == best]


def item_freshness(kind, observed_at, as_of=None, price_class="durable"):
    """一条证据的时效结论：(是否可用, 距今天数, 保鲜期上限)。

    日期缺失、不可解析、在未来、超过保鲜期，一律算不可用。
    替代品报价要和目标商品用同一把尺，所以抽出来复用。
    """
    ref = _parse_date(as_of) or _dt.date.today()
    when = _parse_date(observed_at)
    if when is None or when > ref:
        return False, None, None
    limit_key = "price_fast" if (kind == "price" and price_class == "fast") else kind
    limit = STALE_DAYS.get(limit_key, STALE_DAYS["price"])
    age = (ref - when).days
    return age <= limit, age, limit


# ---------------------------------------------------------------------------
# 主校验
# ---------------------------------------------------------------------------
def check(items, as_of=None, price_class="durable", required_kinds=("price",), base="CNY"):
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
        if url and not _url_syntax_ok(url):
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
                        {"value": float(value), "tier": tier, "date": observed_date,
                         "subject": subject, "key": price_comparison_key(item)}
                    )

        if kind == "fx":
            why = fx_rejection(item, base, as_of)
            if why:
                findings.append(
                    _finding(
                        "EV_FX_UNTRUSTED", "WARN",
                        "这条汇率不能参与换算（" + FX_REJECTION_ZH.get(why, why) + "）",
                        {**scope, "reason": why},
                    )
                )
            expected = fx_expected_rate(cur_from, cur_to)
            if fx_anchor_age_days(as_of) > FX_ANCHOR_MAX_AGE_DAYS:
                findings.append(
                    _finding(
                        "EV_FX_ANCHOR_OLD", "WARN",
                        f"内置汇率锚点表基准日 {FX_ANCHOR_DATE} 距今已超过 "
                        f"{FX_ANCHOR_MAX_AGE_DAYS} 天：合理性检查已放宽到 "
                        f"{FX_PLAUSIBLE_FACTOR_RELAXED} 倍，只能拦住明显离谱的量级，"
                        "不能用来判断汇率是不是当前值",
                        {**scope, "anchor_date": FX_ANCHOR_DATE},
                    )
                )
            elif why is None and expected:
                findings.append(
                    _finding(
                        "EV_FX_PLAUSIBLE_ONLY", "INFO",
                        f"汇率 {value} 落在 {cur_from}/{cur_to} 的合理量级内"
                        f"（锚点约 {round(expected, 4)}）：只是**没有理由拒绝它**，"
                        "不等于核实过当前汇率——本仓库没有独立汇率源，也没有页面正文可核",
                        {**scope, "expected": round(expected, 4)},
                    )
                )

        page_check = page_quote_check(item)
        if page_check["problem"] == "PAGE_TEXT_HASH_MISMATCH":
            findings.append(
                _finding(
                    "EV_PAGE_TEXT_HASH_MISMATCH", "BLOCK",
                    "提供的页面正文与 page_text_sha256 对不上：这一条无法作为页面证据"
                    "（哈希只能证明正文没被改过，不能证明正文来自那个页面）",
                    {**scope, "detail": page_check["detail"]},
                )
            )
        elif page_check["problem"] == "PAGE_TEXT_NO_QUOTE":
            findings.append(
                _finding(
                    "EV_PAGE_TEXT_NO_QUOTE", "BLOCK",
                    f"页面价 {item.get('page_price')} 在提供的页面内容里找不到："
                    "要么价格抄错了，要么这段正文不是这个报价所在的页面",
                    {**scope, "detail": page_check["detail"]},
                )
            )
        content_problem = quote_content_problem(item)
        if content_problem == "PAGE_PRICE_INVALID":
            findings.append(
                _finding(
                    "EV_PAGE_PRICE_INVALID", "BLOCK",
                    f"页面价 {item.get('page_price')!r} 不是正数：这一格要么别填，"
                    "要么填页面上真实出现的价格",
                    {**scope, "page_price": item.get("page_price")},
                )
            )
        elif content_problem == "QUOTE_CONTENT_MISMATCH":
            findings.append(
                _finding(
                    "EV_QUOTE_CONTENT_MISMATCH", "BLOCK",
                    f"报价 {value} 与页面抓到的价格 {item.get('page_price')} 不一致：这条证据自相矛盾",
                    scope,
                )
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
        # 同等级来源互相打架：跨等级差异只是"说明差异"，同级差异是谁都不成立。
        # 只对 target 判：reference 本来就是"行情区间"（国产同类 1280 和 1100
        # 不同很正常），要求它们一致没有意义。
        #
        # ⚠️ **先按（口径/规格/地区）分组再判冲突**。官方标价 1099 与授权商城
        # 促销 899 同时为真，把它们当"数据打架"是错的；只有同一把尺上的价，
        # 差 5% 以上才说明至少有一个是错的。
        if subject != "target":
            continue
        top_tier = min((p["tier"] for p in points), default=None)
        if top_tier is None or top_tier > 3:
            continue
        groups: dict = {}
        for p in points:
            if p["tier"] > 3 or p["value"] <= 0:
                continue
            groups.setdefault(p["key"], []).append((p["tier"], p["value"]))
        conflicts = basis_conflicts(groups)
        if conflicts:
            basis_known = all(k[0] != PRICE_BASIS_UNKNOWN for k in conflicts)
            all_values = sorted({v for vals in conflicts.values() for v in vals})
            if basis_known:
                why = (f"同一等级来源、同一价格口径给出的价 {all_values} 相差超过 "
                       f"{int(SAME_TIER_CONFLICT_TOLERANCE*100)}%：至少有一个是错的，"
                       "不得取平均、取最新或取便宜的那个，须回到来源核对")
            else:
                why = (f"同一等级来源给出的价 {all_values} 相差超过 "
                       f"{int(SAME_TIER_CONFLICT_TOLERANCE*100)}%，但没有标注价格口径"
                       "（标价/促销/含税/规格/地区）：既可能是真的打架，"
                       "也可能只是口径不同，补齐口径前不下购买结论")
            findings.append(
                _finding(
                    "EV_PRICE_CONFLICT", "WARN", why,
                    {"subject": subject, "tier": top_tier, "values": all_values,
                     "basis_known": basis_known},
                )
            )
            if not basis_known:
                findings.append(
                    _finding(
                        "EV_PRICE_BASIS_UNKNOWN", "WARN",
                        "价格证据没有标注口径（price_basis）、规格（variant）或地区（region）："
                        "无法判断这些报价是不是在同一把尺上，须补口径或回源核对",
                        {"subject": subject, "values": all_values},
                    )
                )
        elif len(groups) > 1 and groups_differ(groups):
            dims = sorted({k[0] for k in groups if k[0] != PRICE_BASIS_UNKNOWN}
                          | {("不同规格" if k[1] else "") for k in groups}
                          | {("不同地区" if k[2] else "") for k in groups})
            findings.append(
                _finding(
                    "EV_PRICE_BASIS_DIFF", "WARN",
                    f"同一商品存在口径不同的报价（{', '.join(d for d in dims if d)}）："
                    f"{ {str(k): sorted(v for _, v in pairs) for k, pairs in groups.items()} }。"
                    "这不是数据错误——促销价、标价、不同规格或不同地区的价可以同时为真，"
                    "但结论只能拿**到手价口径**的那一个去比",
                    {"subject": subject,
                     "groups": {str(k): sorted(v for _, v in pairs)
                                for k, pairs in groups.items()}},
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
