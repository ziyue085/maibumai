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
        "ttl": 180, "prio": 1, "type": "number",
        "why": "判断承受力的起点",
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
        "q": "应急金现在有多少？（随时能取出来的部分）",
        "ttl": 365, "prio": 2, "type": "number",
        "why": "应急金不足时不该做大额可选消费",
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
}

DEFAULT_CONSTRAINTS = {
    "no_used": False,
    "no_subscription": False,
    "must_be_portable": False,
    "notes": "",
}

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
    for key, value in mapping.items():
        if value is not None:
            _set_fact_raw(p, key, value, note="由 v1 迁移")
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


def _set_fact_raw(profile: dict, key: str, value, note: str = "") -> None:
    profile.setdefault("facts", {})[key] = {
        "value": value,
        "updated_at": today_str(),
        "note": note or "",
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
    """用户确认"还是这个数"，只刷新时间不改变值。"""
    profile = load_profile()
    item = profile.get("facts", {}).get(key)
    if not item:
        raise KeyError(f"{key} 还没有值，不能确认")
    item["updated_at"] = today_str()
    save_profile(profile)
    return item


def fact(profile: dict, key: str, default=None):
    item = (profile.get("facts") or {}).get(key) or {}
    value = item.get("value")
    return default if value is None else value


def fact_age(profile: dict, key: str):
    item = (profile.get("facts") or {}).get(key) or {}
    stamp = item.get("updated_at")
    try:
        then = _dt.date.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return (_dt.date.today() - then).days


def fact_stale(profile: dict, key: str) -> bool:
    spec = FACT_SPECS.get(key) or {}
    ttl = spec.get("ttl")
    age = fact_age(profile, key)
    return bool(ttl and age is not None and age > ttl)


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


def discretionary(profile: dict):
    """每月可自由支配金额。

    优先级：手动指定 > 收入 − 固定支出 − 储蓄目标 − 投资额。
    任一必需输入缺失就返回 None——不猜。
    """
    override = fact(profile, "finance.discretionary_override")
    if isinstance(override, (int, float)):
        return float(override)
    income = fact(profile, "finance.monthly_income")
    expense = fact(profile, "finance.monthly_fixed_expense")
    if not isinstance(income, (int, float)) or not isinstance(expense, (int, float)):
        return None
    save = fact(profile, "finance.savings_target_monthly") or 0
    invest = fact(profile, "invest.monthly_contribution") or 0
    try:
        return max(0.0, float(income) - float(expense) - float(save) - float(invest))
    except (TypeError, ValueError):
        return None


def brief(profile: dict) -> str:
    """给模型读的一页摘要。缺什么就写未知，不填默认值。"""
    def g(key, unit="", dash="未知"):
        v = fact(profile, key)
        return f"{v}{unit}" if v is not None else dash

    disc = discretionary(profile)
    used_ok = fact(profile, "pref.used_ok")
    used_txt = "可以" if used_ok is True else ("不接受" if used_ok is False else "未知")
    lines = [
        "【画像摘要】",
        f"阶段：{g('life.stage')}｜城市：{g('life.city')}｜居住：{g('life.living')}",
        f"月收入 {g('finance.monthly_income', ' 元')}"
        f"｜月固定支出 {g('finance.monthly_fixed_expense', ' 元')}"
        f"｜月储蓄目标 {g('finance.savings_target_monthly', ' 元')}"
        f"｜月投资 {g('invest.monthly_contribution', ' 元')}",
        "→ 月可支配 " + (f"约 {disc:.0f} 元" if disc is not None else "未知（缺收入或固定支出）"),
        f"可投资本金 {g('invest.capital', ' 元')}"
        f"｜应急金 {g('finance.emergency_fund', ' 元')}"
        f"｜单笔硬上限 {g('finance.hard_cap_single', ' 元', '未设')}",
        f"主目标：{g('goal.name')}（每月 {g('goal.monthly', ' 元')}）",
        f"偏好：价格敏感 {g('pref.price_sensitivity')}｜{g('pref.quality_vs_price')}"
        f"｜耐用 {g('pref.durability_weight')}｜便利 {g('pref.convenience_weight')}"
        f"｜二手 {used_txt}",
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

    pending = gaps(profile, limit=5)
    if pending:
        tag = "、".join(
            "%s%s" % (row["key"], "(过期)" if row["reason"] == "stale" else "") for row in pending
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
    }
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


def owned_digest() -> str:
    """给模型读的精简清单。"""
    items = load_owned()["items"]
    if not items:
        return "（尚未登记任何已有物品）"
    lines = []
    for item in sorted(items, key=lambda i: (i.get("category") or "", i.get("name") or "")):
        cond = item.get("condition")
        mark = {
            "good": "良好", "worn": "有磨损可用", "failing": "勉强能用",
            "broken": "已坏", "gone": "已处理",
        }.get(cond, cond)
        sat = item.get("satisfaction")
        extra = f"｜满意度 {sat}/5" if sat else ""
        lines.append(f"- {item.get('name')}｜{item.get('category')}｜{mark}{extra}")
    return "\n".join(lines)


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
