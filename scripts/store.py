"""maibumai 本地数据层（v2）。

设计约束
--------
1. 仅标准库，零第三方依赖。
2. 只读写本地文件，不联网、不调用模型。
3. 数据目录默认 ``~/.maibumai``，可用 ``MAIBUMAI_HOME`` 覆盖；
   默认位置刻意放在仓库之外，避免个人财务与消费记录被误提交。
4. 账本 append-only（JSONL）。
5. **画像按"事实"存**：每个事实带 ``updated_at``，过期就要重新确认，
   不假设旧值仍然有效。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import statistics
import uuid
from pathlib import Path

SCHEMA_VERSION = 2
APP_DIRNAME = ".maibumai"
PROFILE_NAME = "profile.json"
OWNED_NAME = "owned.json"
LEDGER_NAME = "decisions.jsonl"

VERDICTS = ("BUY", "SWITCH", "DEFER", "DONT_BUY")
VERDICT_ZH = {
    "BUY": "建议购买",
    "SWITCH": "建议替换方案",
    "DEFER": "建议暂缓",
    "DONT_BUY": "不建议购买",
}

CONDITIONS = ("good", "worn", "failing", "broken", "gone")

# ---------------------------------------------------------------------------
# 画像事实清单
# ---------------------------------------------------------------------------
#: ttl  = 多少天后需要重新确认（None 表示不随时间失效）
#: prio = 缺失时的追问优先级（1 最高）
#: type = number / bool / text / enum / list
FACT_SPECS = {
    "finance.monthly_income": {
        "q": "每月固定进账大概多少？（生活费 + 兼职 + 奖学金，按月均算）",
        "ttl": 90, "prio": 1, "type": "number",
        "why": "判断承受力的起点；学生收入不稳定，三个月就要重新确认",
    },
    "finance.monthly_fixed_expense": {
        "q": "每月固定支出大概多少？（吃住行话费，不含可选消费）",
        "ttl": 180, "prio": 1, "type": "number",
        "why": "从收入里扣掉才能得到可支配余额",
    },
    "invest.monthly_contribution": {
        "q": "每月投到投资里的钱大概多少？",
        "ttl": 180, "prio": 1, "type": "number",
        "why": "这笔消费最直接的替代用途就是它",
    },
    "finance.savings_target_monthly": {
        "q": "每月打算存下多少？",
        "ttl": 180, "prio": 2, "type": "number",
        "why": "储蓄目标优先于可选消费",
    },
    "finance.hard_cap_single": {
        "q": "有没有一条单笔绝对不越过的金额线？没有就说没有",
        "ttl": 365, "prio": 2, "type": "number",
        "why": "硬约束：超过直接不建议购买",
    },
    "finance.emergency_fund": {
        "q": "应急金现在有多少？（随时能取、但不该拿去买东西的那部分）",
        "ttl": 180, "prio": 2, "type": "number",
        "why": "应急金不算可消费资金，算进去会高估承受力",
    },
    "finance.liquid_cash": {
        "q": "手上随时能动的钱一共多少？（活期 + 货基 + 余额宝，不含投资持仓）",
        "ttl": 90, "prio": 2, "type": "number",
        "why": "一次性大额消费要看存量，不能只按月收入算",
    },
    "finance.near_term_obligations": {
        "q": "未来半年已经定下来的必要支出大概多少？（学费、房租、还款、押金）",
        "ttl": 90, "prio": 3, "type": "number",
        "why": "这笔钱已经名花有主，不能算进可动用存量",
    },
    "finance.spendable_override": {
        "q": "（可选）想直接指定『我最多能拿出多少买东西』吗？留空则自动推算",
        "ttl": 90, "prio": 3, "type": "number",
        "why": "覆盖自动推算的可动用存量",
    },
    "invest.capital": {
        "q": "目前可投资本金大概多少？（不含应急金）",
        "ttl": 180, "prio": 2, "type": "number",
        "why": "看这笔钱占投资本金的比例",
    },
    "goal.name": {
        "q": "现在最主要的一个攒钱目标是什么？",
        "ttl": 365, "prio": 2, "type": "text",
        "why": "把消费翻译成目标延迟",
    },
    "goal.monthly": {
        "q": "每月为这个目标存多少？",
        "ttl": 365, "prio": 2, "type": "number",
        "why": "同上",
    },
    "life.stage": {
        "q": "现在的身份或阶段？（在读 / 工作 / 其他）",
        "ttl": 365, "prio": 2, "type": "text",
        "why": "判断收入稳定性",
    },
    "life.living": {
        "q": "居住环境？（宿舍 / 合租 / 独居；有无厨房、空间大小、能否改造）",
        "ttl": 365, "prio": 2, "type": "text",
        "why": "很多大件消费受空间与安装条件限制",
    },
    "pref.price_sensitivity": {
        "q": "你对价格敏感吗？（高 / 中 / 低）",
        "ttl": 365, "prio": 2, "type": "enum",
        "why": "决定替代方案推荐的激进程度",
    },
    "pref.quality_vs_price": {
        "q": "同等条件下更看重质量还是价格？（质量优先 / 均衡 / 价格优先）",
        "ttl": 365, "prio": 2, "type": "enum",
        "why": "决定往哪个方向换方案",
    },
    "pref.used_ok": {
        "q": "二手可以接受吗？（是 / 否）",
        "ttl": 365, "prio": 2, "type": "bool",
        "why": "二手是常见的高性价比路径",
    },
    "invest.expected_annual_return": {
        "q": "给投资设定的长期年化预期是多少？只用于机会成本演示，可以留空",
        "ttl": 365, "prio": 3, "type": "number",
        "why": "不填就不做复利演示，绝不替你假设收益率",
    },
    "invest.plan": {
        "q": "一句话说下你的投资思路？",
        "ttl": 365, "prio": 3, "type": "text",
        "why": "判断这笔消费是否与计划冲突",
    },
    "goal.target": {
        "q": "这个目标一共要多少钱？",
        "ttl": 365, "prio": 3, "type": "number",
        "why": "同上",
    },
    "life.city": {
        "q": "常住城市？",
        "ttl": 365, "prio": 3, "type": "text",
        "why": "影响物流、售后与区域价格",
    },
    "pref.durability_weight": {
        "q": "耐用性对你重要吗？（高 / 中 / 低）",
        "ttl": 365, "prio": 3, "type": "enum",
        "why": "影响是否建议加钱买更耐用的",
    },
    "pref.convenience_weight": {
        "q": "便利性对你重要吗？（高 / 中 / 低）",
        "ttl": 365, "prio": 3, "type": "enum",
        "why": "影响订阅与服务类判断",
    },
    "interests.list": {
        "q": "长期在投入的兴趣或领域有哪些？几个词就行",
        "ttl": 365, "prio": 3, "type": "list",
        "why": "判断消费是否服务于长期兴趣",
    },
    "finance.single_purchase_threshold": {
        "q": "单笔超过多少你会觉得需要认真想想？",
        "ttl": 365, "prio": 3, "type": "number",
        "why": "分级门槛",
    },
    "finance.discretionary_override": {
        "q": "（可选）想直接指定每月可自由支配金额吗？留空则自动推算",
        "ttl": 180, "prio": 3, "type": "number",
        "why": "覆盖自动推算值",
    },
    "finance.net_worth": {
        "q": "净资产大概多少？（现金 + 投资 + 应收 − 负债）",
        "ttl": 180, "prio": 3, "type": "number",
        "why": "看整体位置；不直接等于可消费金额",
    },
    "finance.receivable": {
        "q": "有多少钱是别人欠你的、但还没到手？",
        "ttl": 90, "prio": 3, "type": "number",
        "why": "应收款不能当现金用",
    },
    "finance.liabilities": {
        "q": "目前有多少负债？（花呗、分期、借款）",
        "ttl": 90, "prio": 2, "type": "number",
        "why": "负债会直接压缩可支配空间",
    },
    "invest.exposure_note": {
        "q": "投资大致分布？一句话就行（例如：纳指、A股、少量加密）",
        "ttl": 365, "prio": 3, "type": "text",
        "why": "多只同向基金其实是同一份风险，不是分散",
    },
}

DEFAULT_CONSTRAINTS = {
    "no_used": False,
    "no_subscription": False,
    "must_be_portable": False,
    "notes": "",
}

#: 参与财务计算的事实。过期的按"未知"处理，不参与计算。
FINANCE_KEYS = (
    "finance.monthly_income",
    "finance.monthly_fixed_expense",
    "finance.savings_target_monthly",
    "invest.monthly_contribution",
    "finance.liquid_cash",
    "finance.emergency_fund",
    "finance.near_term_obligations",
    "finance.hard_cap_single",
)

#: 会**直接改变算式结果**的字段。这些字段过期、未确认或缺日期，
#: 一律按"未知"处理——包括覆盖值、门槛、期望收益率。
#: 覆盖值最危险：一个没确认过的 ``discretionary_override`` 会让系统
#: 用别人的数字（或自己的旧数字）判断今天的承受力。
DECISION_KEYS = FINANCE_KEYS + (
    "finance.discretionary_override",
    "finance.spendable_override",
    "finance.single_purchase_threshold",
    "invest.expected_annual_return",
)

#: 仅用于展示的金额字段（不进计算，但要提示待确认/过期）。
MONEY_KEYS = DECISION_KEYS + (
    "finance.net_worth",
    "finance.receivable",
    "finance.liabilities",
    "invest.capital",
)

DEFAULT_PROFILE_V2 = {
    "schema_version": SCHEMA_VERSION,
    "created_at": None,
    "updated_at": None,
    "meta": {"base_currency": "CNY", "owner_label": ""},
    "facts": {},
    "constraints": dict(DEFAULT_CONSTRAINTS),
    "behavior": {
        "impulse_cooldown_hours": 72,
        "small_amount_fastpath": 100,
        "review_days": 60,
    },
}

DEFAULT_OWNED = {"schema_version": SCHEMA_VERSION, "updated_at": None, "items": []}

_HOME_README = """\
这是「买不买 AI」(maibumai) 的本地数据目录。

里面的内容包含你的消费记录、预算、投资安排与已有物品清单，属于隐私数据。
请不要把这个目录放进任何 Git 仓库，也不要上传到网盘或公开平台。

目录内容：
  profile.json     个人画像（按事实存储，每条带更新日期）
  owned.json       已有物品档案
  decisions.jsonl  消费决策账本（append-only）

日常操作用对话即可，不需要手改文件。
要看当前画像：python scripts/mbm.py profile brief
要补全缺失信息：python scripts/mbm.py profile gaps
自定义位置：设置环境变量 MAIBUMAI_HOME。
自检：python scripts/mbm.py doctor
"""


# --------------------------------------------------------------------------
# 时间与路径
# --------------------------------------------------------------------------
def now_iso() -> str:
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def today_str() -> str:
    return _dt.date.today().isoformat()


def home() -> Path:
    raw = (os.environ.get("MAIBUMAI_HOME") or "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / APP_DIRNAME
    return base.absolute()


def ensure_home() -> Path:
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    readme = h / "README.txt"
    if not readme.exists():
        readme.write_text(_HOME_README, encoding="utf-8")
    if not (h / PROFILE_NAME).exists():
        _write_json(h / PROFILE_NAME, _new_profile())
    if not (h / OWNED_NAME).exists():
        _write_json(h / OWNED_NAME, DEFAULT_OWNED)
    ledger = h / LEDGER_NAME
    if not ledger.exists():
        ledger.touch()
    return h


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return json.loads(json.dumps(default))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"JSON 无法解析：{path} — {exc}") from exc


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 画像
# --------------------------------------------------------------------------
def _new_profile() -> dict:
    p = json.loads(json.dumps(DEFAULT_PROFILE_V2))
    p["created_at"] = now_iso()
    p["updated_at"] = now_iso()
    return p


def _migrate(raw: dict) -> dict:
    """v1 → v2：把 budget.* 搬到 facts，避免用户已有画像丢失。"""
    if raw.get("schema_version") == SCHEMA_VERSION and "facts" in raw:
        return raw
    p = _new_profile()
    budget = raw.get("budget") or {}
    mapping = {
        "finance.discretionary_override": budget.get("discretionary_monthly"),
        "finance.single_purchase_threshold": budget.get("single_purchase_threshold"),
        "finance.hard_cap_single": budget.get("hard_cap_single"),
    }
    goal = budget.get("savings_goal") or {}
    mapping.update({
        "goal.name": goal.get("name"),
        "goal.target": goal.get("target"),
        "goal.monthly": goal.get("monthly"),
    })
    # 旧画像里的数字是什么时候的？只能是它最后一次保存的时间；
    # 连这个都没有，就标成"日期不明"。绝不刷新成今天。
    legacy_stamp = raw.get("updated_at") or ""
    # 文件级的 updated_at 证明的是"这个文件被保存过"，不是"收入还是这个数"。
    # 财务字段一律落成待确认：迁移只搬数据，不替用户签收。
    for key, value in mapping.items():
        if value is not None:
            _set_fact_raw(p, key, value,
                          note="由 v1 迁移，需确认现在是否仍有效",
                          source="v1 画像迁移",
                          confirmed=False,
                          updated_at=legacy_stamp)
    if isinstance(raw.get("behavior"), dict):
        p["behavior"].update(raw["behavior"])
    p["meta"]["base_currency"] = raw.get("currency") or "CNY"
    p["created_at"] = raw.get("updated_at") or now_iso()
    return p


def load_profile() -> dict:
    ensure_home()
    raw = _read_json(home() / PROFILE_NAME, DEFAULT_PROFILE_V2)
    if not isinstance(raw, dict):
        raw = {}
    profile = _migrate(raw)
    profile.setdefault("facts", {})
    profile["constraints"] = {**DEFAULT_CONSTRAINTS, **(profile.get("constraints") or {})}
    profile["behavior"] = {**DEFAULT_PROFILE_V2["behavior"], **(profile.get("behavior") or {})}
    profile["meta"] = {**DEFAULT_PROFILE_V2["meta"], **(profile.get("meta") or {})}
    return profile


def save_profile(profile: dict) -> None:
    p = dict(profile)
    p["schema_version"] = SCHEMA_VERSION
    p["updated_at"] = now_iso()
    _write_json(home() / PROFILE_NAME, p)


def _coerce(raw, kind: str):
    if raw is None:
        return None
    if isinstance(raw, (int, float, bool, list)):
        return raw
    text = str(raw).strip()
    if text.lower() in ("null", "none", "~", "") or text in ("没有", "无", "留空", "空"):
        return None
    if kind == "number":
        cleaned = text.replace("元", "").replace("块", "").replace(",", "").replace(" ", "")
        try:
            num = float(cleaned)
        except ValueError:
            raise ValueError(f"需要数字，收到 {raw!r}")
        return int(num) if num.is_integer() else num
    if kind == "bool":
        return text.lower() in ("是", "y", "yes", "true", "1", "可以", "接受")
    if kind == "list":
        parts = [s.strip() for s in text.replace("，", ",").replace("、", ",").split(",")]
        return [s for s in parts if s]
    return text


def _set_fact_raw(profile: dict, key: str, value, note: str = "",
                  source: str = "", confirmed: bool = True,
                  updated_at: str | None = None) -> None:
    """写入一条事实。

    ``updated_at`` 传 ``None`` 表示"用户刚说的，就是今天"；
    传空串表示"日期不明"——历史快照没有日期时不能拿今天顶上，
    否则一条 2024 年的数字会永远看起来是新鲜的。
    """
    profile.setdefault("facts", {})[key] = {
        "value": value,
        "updated_at": today_str() if updated_at is None else str(updated_at),
        "note": note or "",
        "source": source or "",
        "confirmed": bool(confirmed),
    }


def set_fact(key: str, value, note: str = "") -> dict:
    if key not in FACT_SPECS:
        raise KeyError(f"未知画像字段：{key}。可用字段见 profile gaps")
    profile = load_profile()
    coerced = _coerce(value, FACT_SPECS[key]["type"])
    _set_fact_raw(profile, key, coerced, note)
    save_profile(profile)
    return profile["facts"][key]


def confirm_fact(key: str) -> dict:
    """用户确认"还是这个数"，刷新时间并标记为已确认。"""
    profile = load_profile()
    item = profile.get("facts", {}).get(key)
    if not item:
        raise KeyError(f"{key} 还没有值，不能确认")
    item["updated_at"] = today_str()
    item["confirmed"] = True
    save_profile(profile)
    return item


def seed_facts(entries: dict, force: bool = False) -> dict:
    """写入一批候选事实（历史快照、用户口述）。

    ``entries`` 形如::

        {"finance.liquid_cash": {"value": 32000, "observed_at": "2026-08-31",
                                 "source": "用户资产快照", "confirmed": false}}

    默认 **不覆盖** 已确认的事实；且所有写入的 confirmed 默认为 false——
    历史数字不能当作当前数字用。
    """
    profile = load_profile()
    written, skipped = [], []
    for key, meta in (entries or {}).items():
        if key not in FACT_SPECS:
            skipped.append({"key": key, "reason": "未知字段"})
            continue
        if not isinstance(meta, dict) or "value" not in meta:
            skipped.append({"key": key, "reason": "缺少 value"})
            continue
        existing = (profile.get("facts") or {}).get(key) or {}
        if existing.get("value") is not None and existing.get("confirmed", True) and not force:
            skipped.append({"key": key, "reason": "已有确认值，未覆盖"})
            continue
        value = _coerce(meta.get("value"), FACT_SPECS[key]["type"])
        _set_fact_raw(
            profile, key, value,
            note=meta.get("note", ""),
            source=meta.get("source", ""),
            confirmed=bool(meta.get("confirmed", False)),
            # 没有 observed_at 就是日期不明，不能拿今天顶上
            updated_at=meta.get("observed_at") or "",
        )
        written.append(key)
    save_profile(profile)
    return {"written": written, "skipped": skipped}


def fact(profile: dict, key: str, default=None):
    item = (profile.get("facts") or {}).get(key) or {}
    value = item.get("value")
    return default if value is None else value


def fact_age(profile: dict, key: str):
    """距今天数。日期缺失/不可解析返回 None，未来日期返回负数。

    接受 ``2026-10-29`` 与 ``2026-10-29T10:00:00+08:00`` 两种写法。
    """
    item = (profile.get("facts") or {}).get(key) or {}
    text = str(item.get("updated_at") or "").strip()
    if len(text) >= 10:
        text = text[:10]
    try:
        then = _dt.date.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return (_dt.date.today() - then).days


def fact_confirmed(profile: dict, key: str) -> bool:
    item = (profile.get("facts") or {}).get(key) or {}
    return item.get("confirmed", True) is not False


def fact_stale(profile: dict, key: str) -> bool:
    """过期 = 不可信。

    四种情况都算不可信：
    1. 超过保鲜期；
    2. 决策类字段连 updated_at 都没有（手写画像不能绕过时效检查）；
    3. 标记为未确认（历史快照、别人给的数字、导入的候选稿）；
    4. 值不是数字而字段声明为 number。

    第 3 条对 ``DECISION_KEYS`` 全量生效：覆盖值、单笔门槛、期望收益率
    都会直接改算式结果，未确认就等于没有。
    """
    spec = FACT_SPECS.get(key) or {}
    decisive = key in DECISION_KEYS
    if decisive and not fact_confirmed(profile, key):
        return True
    ttl = spec.get("ttl")
    if not ttl:
        return False
    age = fact_age(profile, key)
    if age is None:
        # 没有日期就是不知道什么时候的数；带保鲜期的字段一律按不可信处理
        return True
    if age < 0:
        # 未来日期不可能是"今天的现状"：要么录入错误，要么时区/格式错乱
        return True
    if age > ttl:
        return True
    if spec.get("type") == "number":
        value = fact(profile, key)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            return True
    return False


def gaps(profile: dict, limit: int | None = None, min_prio: int = 3) -> list:
    """缺失或过期的画像字段，按优先级排序。"""
    out = []
    facts = profile.get("facts") or {}
    for key, spec in FACT_SPECS.items():
        if spec["prio"] > min_prio:
            continue
        item = facts.get(key) or {}
        value = item.get("value")
        age = fact_age(profile, key)
        if value is None:
            reason, score = "missing", 0
        elif not fact_confirmed(profile, key):
            reason, score = "unconfirmed", age or 0
        elif fact_stale(profile, key):
            reason, score = "stale", age or 0
        else:
            continue
        out.append({
            "key": key,
            "question": spec["q"],
            "why": spec["why"],
            "priority": spec["prio"],
            "reason": reason,
            "age_days": age,
            "ttl": spec.get("ttl"),
        })
    out.sort(key=lambda g: (g["priority"], -int(g["age_days"] or 0)))
    return out[:limit] if limit else out


def fact_fresh(profile: dict, key: str):
    """只返回未过期的值。过期 = 不知道，不是"大概是这个数"。"""
    if fact_stale(profile, key):
        return None
    return fact(profile, key)


def financial_snapshot(profile: dict) -> dict:
    """把画像拆成两层：**每月现金流** 与 **一次性存量**。

    这两件事不能混：
    - 月可支配决定"这笔钱会不会挤掉日常"
    - 可动用存量决定"一次性大额拿不拿得出"

    任何一项缺失就留 None，绝不用 0 顶替——把没填的储蓄/投资当成 0，
    会直接高估可支配余额。

    ``surplus_basis`` 说明这个数字有多硬：
    - ``override`` 用户指定，最硬
    - ``income``  完整推算，可用于结论
    - ``estimated`` 缺储蓄或投资额，**只能当上界**，不能用来支撑"买得起"
    """
    income = fact_fresh(profile, "finance.monthly_income")
    expense = fact_fresh(profile, "finance.monthly_fixed_expense")
    save = fact_fresh(profile, "finance.savings_target_monthly")
    invest_month = fact_fresh(profile, "invest.monthly_contribution")
    surplus_override = fact_fresh(profile, "finance.discretionary_override")

    surplus, estimated, surface_basis = None, False, None
    if isinstance(surplus_override, (int, float)):
        surplus, surface_basis = float(surplus_override), "override"
    elif isinstance(income, (int, float)) and isinstance(expense, (int, float)):
        surplus = float(income) - float(expense) - float(save or 0) - float(invest_month or 0)
        # 储蓄目标或投资额没填 → 余额被高估，必须标记
        estimated = (save is None) or (invest_month is None)
        surface_basis = "estimated" if estimated else "income"

    cash = fact_fresh(profile, "finance.liquid_cash")
    emergency = fact_fresh(profile, "finance.emergency_fund")
    near = fact_fresh(profile, "finance.near_term_obligations")
    spend_override = fact_fresh(profile, "finance.spendable_override")

    spendable, spendable_note, spendable_basis = None, "", None
    if isinstance(spend_override, (int, float)):
        spendable, spendable_basis = float(spend_override), "override"
        spendable_note = "按你手动指定的可动用金额"
    elif isinstance(cash, (int, float)) and isinstance(emergency, (int, float)) \
            and isinstance(near, (int, float)):
        spendable, spendable_basis = max(0.0, float(cash) - float(emergency) - float(near)), "derived"
        spendable_note = "活期/现金 − 应急金 − 近期必要支出"
    elif isinstance(cash, (int, float)):
        spendable_note = "只填了现金总额，未扣应急金与近期支出 ⇒ 不把存款当成都能花"
    else:
        spendable_note = "还没填现金与应急金，不知道有多少能真正动用"

    unconfirmed = [
        k for k in MONEY_KEYS
        if fact(profile, k) is not None and not fact_confirmed(profile, k)
    ]
    stale = [
        k for k in MONEY_KEYS
        if fact(profile, k) is not None and fact_confirmed(profile, k) and fact_stale(profile, k)
    ]
    missing = [k for k in FINANCE_KEYS if fact_fresh(profile, k) is None]

    return {
        "monthly_surplus": None if surplus is None else round(surplus, 2),
        "surplus_estimated": estimated,
        "surplus_basis": surface_basis,
        "spendable": None if spendable is None else round(spendable, 2),
        "spendable_basis": spendable_basis,
        "spendable_note": spendable_note,
        "hard_cap": fact_fresh(profile, "finance.hard_cap_single"),
        "emergency_fund": emergency,
        "near_term_obligations": near,
        "liquid_cash": cash,
        "invest_capital": fact_fresh(profile, "invest.capital"),
        "receivable": fact_fresh(profile, "finance.receivable"),
        "liabilities": fact_fresh(profile, "finance.liabilities"),
        "net_worth": fact_fresh(profile, "finance.net_worth"),
        "stale_fields": stale,
        "missing_fields": missing,
        "unconfirmed_fields": unconfirmed,
        "known": surplus is not None or spendable is not None,
    }


def discretionary(profile: dict):
    """每月可支配余额（兼容旧接口）。"""
    return financial_snapshot(profile)["monthly_surplus"]


def brief(profile: dict) -> str:
    """给模型读的一页摘要：严格按"存量 / 现金流"六层展开。

    缺什么写未知，历史值写"待确认"，过期的写"已过期"——
    这三者语义不同，混在一起就会拿旧数字当现数字。
    """
    def view(key, unit=" 元"):
        item = (profile.get("facts") or {}).get(key) or {}
        val = item.get("value")
        if val is None:
            return "未知"
        if not fact_confirmed(profile, key):
            when = item.get("updated_at") or "日期不明"
            return f"{val}{unit}（{when} 记录，待确认）"
        if fact_stale(profile, key):
            stamp = item.get("updated_at")
            if not stamp:
                return f"{val}{unit}（日期不明，不可信）"
            age = fact_age(profile, key)
            if age is not None and age < 0:
                return f"{val}{unit}（日期在未来：{stamp}，不可信）"
            return f"{val}{unit}（已过期，{stamp}）"
        return f"{val}{unit}"

    snap = financial_snapshot(profile)
    used_ok = fact(profile, "pref.used_ok")
    used_txt = "可以" if used_ok is True else ("不接受" if used_ok is False else "未知")

    surplus = snap["monthly_surplus"]
    if surplus is None:
        surplus_txt = "未知"
    elif snap["surplus_estimated"]:
        surplus_txt = f"约 {surplus:.0f} 元（偏高：储蓄目标/月投资未填）"
    else:
        surplus_txt = f"约 {surplus:.0f} 元"

    spend = snap["spendable"]
    spend_txt = f"{spend:.0f} 元" if spend is not None else "未知"

    lines = [
        "【财务画像｜存量与现金流分开看】",
        f"① 净资产：{view('finance.net_worth')}",
        f"   构成：现金 {view('finance.liquid_cash')}"
        f"｜投资 {view('invest.capital')}"
        f"｜应收款 {view('finance.receivable')}"
        f"｜负债 {view('finance.liabilities')}",
        f"② 可动用现金：{view('finance.liquid_cash')}",
        f"③ 应急与必要支出储备：应急金 {view('finance.emergency_fund')}"
        f"｜近半年必要支出 {view('finance.near_term_obligations')}",
        "④ 已安排用途的资金：见 ③ 中的近期必要支出与应急金（这两笔不算可花）",
        f"⑤ 可自由消费资金：{spend_txt}（{snap['spendable_note']}）",
        f"⑥ 每月现金流：收入 {view('finance.monthly_income')}"
        f"｜固定支出 {view('finance.monthly_fixed_expense')}"
        f"｜储蓄目标 {view('finance.savings_target_monthly')}"
        f"｜月投资 {view('invest.monthly_contribution')}",
        f"   → 月结余 {surplus_txt}",
        f"· 单笔硬上限：{view('finance.hard_cap_single') if fact(profile,'finance.hard_cap_single') is not None else '未设'}",
        f"· 投资敞口：{view('invest.exposure_note', '')}"
        f"（多只同向基金是同一份风险，不是分散）",
        f"生活：{view('life.stage','')}｜{view('life.city','')}｜{view('life.living','')}",
        f"主目标：{view('goal.name','')}（每月 {view('goal.monthly')}）",
        f"偏好：价格敏感 {view('pref.price_sensitivity','')}"
        f"｜{view('pref.quality_vs_price','')}"
        f"｜耐用 {view('pref.durability_weight','')}"
        f"｜便利 {view('pref.convenience_weight','')}｜二手 {used_txt}",
    ]
    cons = profile.get("constraints") or {}
    tags = []
    if cons.get("no_used"):
        tags.append("不买二手")
    if cons.get("no_subscription"):
        tags.append("不订阅")
    if cons.get("must_be_portable"):
        tags.append("需便携")
    if cons.get("notes"):
        tags.append(str(cons["notes"]))
    lines.append("硬约束：" + ("；".join(tags) if tags else "无"))
    interests = fact(profile, "interests.list")
    lines.append("长期兴趣：" + ("、".join(interests) if interests else "未知"))

    if snap["unconfirmed_fields"]:
        lines.append("⚠ 只有历史记录、未被确认（不参与计算）：" + "、".join(snap["unconfirmed_fields"]))
    if snap["stale_fields"]:
        lines.append("⚠ 已过期、不参与计算：" + "、".join(snap["stale_fields"]))

    pending = gaps(profile, limit=6)
    if pending:
        tag = "、".join(
            "%s(%s)" % (row["key"], {"missing": "缺", "stale": "过期", "unconfirmed": "待确认"}[row["reason"]])
            for row in pending
        )
        lines.append(f"待补/待确认：{tag}")
    return "\n".join(lines)


def set_constraints(**kwargs) -> dict:
    profile = load_profile()
    cons = profile.get("constraints") or dict(DEFAULT_CONSTRAINTS)
    for key, value in kwargs.items():
        if key in DEFAULT_CONSTRAINTS:
            cons[key] = value
    profile["constraints"] = cons
    save_profile(profile)
    return cons


# --------------------------------------------------------------------------
# 已有物品
# --------------------------------------------------------------------------
def load_owned() -> dict:
    ensure_home()
    raw = _read_json(home() / OWNED_NAME, DEFAULT_OWNED)
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raw = json.loads(json.dumps(DEFAULT_OWNED))
    return raw


def save_owned(data: dict) -> None:
    data = dict(data)
    data["schema_version"] = SCHEMA_VERSION
    data["updated_at"] = now_iso()
    _write_json(home() / OWNED_NAME, data)


def add_owned(item: dict) -> dict:
    if not item.get("name"):
        raise ValueError("已有物品必须提供 name")
    cond = item.get("condition", "good")
    if cond not in CONDITIONS:
        raise ValueError(f"condition 必须是 {CONDITIONS} 之一，收到 {cond!r}")
    data = load_owned()
    record = {
        "id": item.get("id") or f"own-{uuid.uuid4().hex[:6]}",
        "name": item["name"].strip(),
        "category": item.get("category", "未分类"),
        "acquired": item.get("acquired"),
        "price": item.get("price"),
        "condition": cond,
        "satisfaction": item.get("satisfaction"),
        "notes": item.get("notes", ""),
        # 服务类资源：每月成本与状态，用于判断"是否重复订阅""长期负担多重"
        "monthly_cost": item.get("monthly_cost"),
        "status": item.get("status", "active"),
        "vendor": item.get("vendor", ""),
    }
    if record["status"] not in ("active", "paused", "expired"):
        raise ValueError("status 必须是 active / paused / expired")
    for idx, old in enumerate(data["items"]):
        if old.get("name") == record["name"] and old.get("category") == record["category"]:
            record["id"] = old.get("id", record["id"])
            data["items"][idx] = record
            break
    else:
        data["items"].append(record)
    save_owned(data)
    return record


def find_owned(query: str) -> list:
    q = (query or "").strip().lower()
    if not q:
        return []
    hits = []
    for item in load_owned()["items"]:
        hay = f"{item.get('name','')} {item.get('category','')} {item.get('notes','')}".lower()
        if q in hay:
            hits.append(item)
            continue
        tokens = [t for t in q.replace("，", " ").replace(",", " ").split() if t]
        if tokens and all(t in hay for t in tokens):
            hits.append(item)
    return hits


def owned_digest(items=None) -> str:
    """给模型读的精简清单。"""
    items = load_owned()["items"] if items is None else items
    if not items:
        return "（尚未登记任何已有物品）"
    lines = []
    for item in sorted(items, key=lambda i: (i.get("category") or "", i.get("name") or "")):
        cond = item.get("condition")
        mark = {
            "good": "良好", "worn": "有磨损可用", "failing": "勉强能用",
            "broken": "已坏", "gone": "已处理",
        }.get(cond, cond)
        status = item.get("status") or "active"
        status_txt = {"active": "在用", "paused": "暂停", "expired": "已停"}.get(status, status)
        cost = item.get("monthly_cost")
        extra = f"｜${cost}/月" if isinstance(cost, (int, float)) else ""
        sat = item.get("satisfaction")
        if sat:
            extra += f"｜满意度 {sat}/5"
        lines.append(f"- {item.get('name')}｜{item.get('category')}｜{mark}｜{status_txt}{extra}")
    total = resource_overview(items)["monthly_subscription_total"]
    if total:
        lines.append(f"（在用服务合计约 {total} 元/月）")
    return "\n".join(lines)


def resource_overview(items=None) -> dict:
    """已有资源盘点：哪些在用、每月共花多少、有哪些是服务类。"""
    items = load_owned()["items"] if items is None else items
    active = [i for i in items if (i.get("status") or "active") == "active"]
    subs = [i for i in active if isinstance(i.get("monthly_cost"), (int, float))]
    total = round(sum(float(i["monthly_cost"]) for i in subs), 2)
    return {
        "items": items,
        "active_count": len(active),
        "subscriptions": subs,
        "monthly_subscription_total": total,
    }


def _subcategory(category) -> str:
    """取品类最后一段。'数码/耳机' → '耳机'。"""
    parts = [p for p in str(category or "").split("/") if p]
    return parts[-1] if parts else ""


def similar_resources(product: dict, items=None) -> list:
    """找出可能与这笔消费重叠的已有资源。

    判据只有两条：**子品类相同**，或**名称有词元重合**。
    不用顶层品类——买平板时把手机、笔记本都算成"已有同类"，
    只会制造噪声，最后没人看提示。
    """
    name = str((product or {}).get("name") or "")
    subcat = _subcategory((product or {}).get("category"))
    tokens = [t for t in name.replace("（", " ").replace("）", " ").split() if len(t) >= 2]
    hits = []
    for item in (load_owned()["items"] if items is None else items):
        if (item.get("status") or "active") != "active":
            continue
        same_sub = bool(subcat) and subcat == _subcategory(item.get("category"))
        item_name = str(item.get("name") or "")
        name_hit = any(t and t in item_name for t in tokens)
        if same_sub or name_hit:
            hits.append({
                "name": item.get("name"),
                "category": item.get("category"),
                "condition": item.get("condition"),
                "status": item.get("status") or "active",
                "monthly_cost": item.get("monthly_cost"),
            })
    return hits


# --------------------------------------------------------------------------
# 账本
# --------------------------------------------------------------------------
def new_id(when: _dt.datetime | None = None) -> str:
    d = when or _dt.datetime.now()
    return f"{d:%Y%m%d-%H%M}-{uuid.uuid4().hex[:4]}"


def append_event(event: dict) -> dict:
    ensure_home()
    ev = dict(event)
    ev.setdefault("at", now_iso())
    ev.setdefault("schema_version", SCHEMA_VERSION)
    with (home() / LEDGER_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def read_events() -> list:
    ensure_home()
    events, bad = [], 0
    with (home() / LEDGER_NAME).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    read_events.last_corrupt_lines = bad
    return events


read_events.last_corrupt_lines = 0


def derive_decisions(events: list | None = None) -> dict:
    events = read_events() if events is None else events
    out: dict = {}
    for ev in events:
        etype = ev.get("type")
        if etype == "decision":
            did = ev.get("id") or new_id()
            out[did] = {
                "id": did,
                "at": ev.get("at"),
                "level": ev.get("level"),
                "product": ev.get("product", {}),
                "verdict": ev.get("verdict"),
                "reasons": ev.get("reasons", []),
                "alternative": ev.get("alternative"),
                "numbers": ev.get("numbers", {}),
                "evidence": ev.get("evidence", []),
                "prediction": ev.get("prediction", {}),
                "recheck_condition": ev.get("recheck_condition"),
                "enforced": ev.get("enforced", []),
                "overridden": ev.get("overridden", False),
                "bought": None,
                "user_decision": None,
                "reviews": [],
                "notes": [],
            }
        elif etype == "outcome" and ev.get("ref") in out:
            out[ev["ref"]]["bought"] = bool(ev.get("bought"))
            out[ev["ref"]]["user_decision"] = ev.get("user_decision")
        elif etype == "review" and ev.get("ref") in out:
            out[ev["ref"]]["reviews"].append(
                {"at": ev.get("at"), "actual": ev.get("actual", {}), "notes": ev.get("notes", "")}
            )
        elif etype == "note" and ev.get("ref") in out:
            out[ev["ref"]]["notes"].append({"at": ev.get("at"), "text": ev.get("text", "")})
    return out


def due_for_review(days: int | None = None, decisions: dict | None = None) -> list:
    decisions = derive_decisions() if decisions is None else decisions
    limit = days if days is not None else int(load_profile()["behavior"]["review_days"])
    now = _dt.datetime.now().astimezone()
    out = []
    for rec in decisions.values():
        if rec.get("bought") is not True or rec.get("reviews"):
            continue
        try:
            made = _dt.datetime.fromisoformat(rec["at"])
        except (TypeError, ValueError):
            continue
        if made.tzinfo is None:
            made = made.astimezone()
        age = (now - made).days
        if age >= limit:
            out.append({**rec, "age_days": age})
    return sorted(out, key=lambda r: r["at"] or "")


def _price_of(rec: dict):
    p = (rec.get("product") or {}).get("price")
    return p if isinstance(p, (int, float)) else None


def stats(decisions: dict | None = None) -> dict:
    decisions = derive_decisions() if decisions is None else decisions
    recs = list(decisions.values())
    by_verdict: dict = {v: 0 for v in VERDICTS}
    spend: dict = {v: 0 for v in VERDICTS}
    against = 0
    for rec in recs:
        v = rec.get("verdict")
        if v in by_verdict:
            by_verdict[v] += 1
            price = _price_of(rec)
            if price is not None:
                spend[v] += price
        if rec.get("user_decision") and rec.get("user_decision") != v:
            against += 1

    reviewed = [r for r in recs if r.get("reviews")]
    idle, regret, bias = [], [], []
    idle_by_cat: dict = {}
    for rec in reviewed:
        actual = rec["reviews"][-1].get("actual", {})
        uses = actual.get("uses_per_month")
        if isinstance(uses, (int, float)) and uses < 1:
            idle.append(rec)
            cat = (rec.get("product") or {}).get("category") or "未分类"
            idle_by_cat[cat] = idle_by_cat.get(cat, 0) + 1
        if actual.get("would_buy_again") is False:
            regret.append(rec)
        pred = (rec.get("prediction") or {}).get("uses_per_month")
        if isinstance(pred, (int, float)) and pred > 0 and isinstance(uses, (int, float)) and uses > 0:
            bias.append(pred / uses)

    regret_spend = sum(_price_of(r) or 0 for r in regret)
    satisfied = [
        r for r in reviewed
        if (r["reviews"][-1].get("actual", {}).get("satisfaction") or 0) >= 4
    ]
    return {
        "decisions": len(recs),
        "by_verdict": by_verdict,
        "spend_by_verdict": spend,
        "bought_against_advice": against,
        "reviewed": len(reviewed),
        "review_coverage": round(len(reviewed) / len(recs), 3) if recs else 0.0,
        "idle_count": len(idle),
        "idle_rate": round(len(idle) / len(reviewed), 3) if reviewed else None,
        "satisfied_rate": round(len(satisfied) / len(reviewed), 3) if reviewed else None,
        "idle_by_category": idle_by_cat,
        "regret_count": len(regret),
        "regret_spend": regret_spend,
        "usage_overestimate_median": round(statistics.median(bias), 2) if bias else None,
        "usage_overestimate_sample": len(bias),
    }


def privacy_check() -> dict:
    h = home()
    inside = []
    for candidate in [h, *h.parents]:
        if (candidate / ".git").exists():
            inside.append(str(candidate))
    return {"home": str(h), "inside_git_repo": bool(inside), "git_roots": inside}
