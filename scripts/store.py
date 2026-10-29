"""maibumai 本地数据层。

设计约束
--------
1. 仅标准库，零第三方依赖（换机器、换宿主都能跑）。
2. 只读写本地文件，不联网、不调用模型。
3. 数据目录默认 ``~/.maibumai``，可用环境变量 ``MAIBUMAI_HOME`` 覆盖。
   默认位置刻意放在仓库之外，避免个人财务与消费记录被误提交。
4. 账本 append-only（JSONL）：单条损坏不致全库不可读，也避免整文件重写。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import statistics
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
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

#: 单品状态。只有 good / worn 才算"还能用"，才可能触发重复购买拦截。
CONDITIONS = ("good", "worn", "failing", "broken", "gone")

DEFAULT_PROFILE = {
    "schema_version": SCHEMA_VERSION,
    "updated_at": None,
    "currency": "CNY",
    "budget": {
        "discretionary_monthly": None,
        "single_purchase_threshold": None,
        "hard_cap_single": None,
        "savings_goal": {"name": None, "target": None, "monthly": None},
    },
    "taste": {"values": [], "avoid": [], "notes": ""},
    "behavior": {
        "impulse_cooldown_hours": 72,
        "small_amount_fastpath": 100,
        "review_days": 60,
    },
}

DEFAULT_OWNED = {"schema_version": SCHEMA_VERSION, "updated_at": None, "items": []}

_HOME_README = """\
这是「买不买 AI」(maibumai) 的本地数据目录。

里面的内容包含你的消费记录、预算与已有物品清单，属于隐私数据。
请不要把这个目录放进任何 Git 仓库，也不要上传到网盘或公开平台。

目录内容：
  profile.json     个人画像（预算、偏好、行为参数）
  owned.json       已有物品档案
  decisions.jsonl  消费决策账本（append-only）

自定义位置：设置环境变量 MAIBUMAI_HOME 指向其他目录。
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
    """数据目录。环境变量优先，否则 ~/.maibumai。"""
    raw = (os.environ.get("MAIBUMAI_HOME") or "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / APP_DIRNAME
    return base.absolute()


def ensure_home() -> Path:
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    readme = h / "README.txt"
    if not readme.exists():
        readme.write_text(_HOME_README, encoding="utf-8")
    for name, default in ((PROFILE_NAME, DEFAULT_PROFILE), (OWNED_NAME, DEFAULT_OWNED)):
        p = h / name
        if not p.exists():
            _write_json(p, default)
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
    """先写临时文件再替换，避免写一半断电导致文件损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# 画像
# --------------------------------------------------------------------------
def _deep_merge(base: dict, override: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_profile() -> dict:
    ensure_home()
    raw = _read_json(home() / PROFILE_NAME, DEFAULT_PROFILE)
    return _deep_merge(DEFAULT_PROFILE, raw if isinstance(raw, dict) else {})


def save_profile(profile: dict) -> None:
    profile = dict(profile)
    profile["schema_version"] = SCHEMA_VERSION
    profile["updated_at"] = now_iso()
    _write_json(home() / PROFILE_NAME, profile)


def _coerce(raw: str):
    text = raw.strip()
    if text.lower() in ("null", "none", "~", ""):
        return None
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if text[:1] in "[{":
        return json.loads(text)
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def set_profile_value(dotted: str, raw: str) -> dict:
    """``budget.discretionary_monthly=3000`` 这类点路径赋值。"""
    profile = load_profile()
    node = profile
    parts = dotted.split(".")
    for key in parts[:-1]:
        if key not in node or not isinstance(node[key], dict):
            node[key] = {}
        node = node[key]
    node[parts[-1]] = _coerce(raw)
    save_profile(profile)
    return profile


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
    # 同名同类别视为更新，不重复堆积
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
        # 粗略词元匹配：中文按 2 字滑窗，英文按空格
        tokens = [t for t in q.replace("，", " ").replace(",", " ").split() if t]
        if tokens and all(t in hay for t in tokens):
            hits.append(item)
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
    path = home() / LEDGER_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def read_events() -> list:
    ensure_home()
    path = home() / LEDGER_NAME
    events, bad = [], 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    if bad:
        # 不静默吞掉损坏：把计数挂在函数属性上供 doctor 读取
        read_events.last_corrupt_lines = bad
    else:
        read_events.last_corrupt_lines = 0
    return events


read_events.last_corrupt_lines = 0


def derive_decisions(events: list | None = None) -> dict:
    """把事件流折叠成 {id: 决策当前状态}。"""
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
                "bought": None,
                "reviews": [],
                "notes": [],
            }
        elif etype == "outcome" and ev.get("ref") in out:
            out[ev["ref"]]["bought"] = bool(ev.get("bought"))
        elif etype == "review" and ev.get("ref") in out:
            out[ev["ref"]]["reviews"].append(
                {"at": ev.get("at"), "actual": ev.get("actual", {}), "notes": ev.get("notes", "")}
            )
        elif etype == "note" and ev.get("ref") in out:
            out[ev["ref"]]["notes"].append({"at": ev.get("at"), "text": ev.get("text", "")})
    return out


def due_for_review(days: int | None = None, decisions: dict | None = None) -> list:
    """已购、已过复盘周期、还没有任何复盘记录的决策。"""
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
    """只用用户自己的记录做聚合，不引入任何外部基准。"""
    decisions = derive_decisions() if decisions is None else decisions
    recs = list(decisions.values())
    by_verdict: dict = {v: 0 for v in VERDICTS}
    spend: dict = {v: 0 for v in VERDICTS}
    for rec in recs:
        v = rec.get("verdict")
        if v in by_verdict:
            by_verdict[v] += 1
            price = _price_of(rec)
            if price is not None:
                spend[v] += price

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
            bias.append(pred / uses)  # 预测值是实际的几倍 = 高估倍数

    regret_spend = sum(_price_of(r) or 0 for r in regret)
    return {
        "decisions": len(recs),
        "by_verdict": by_verdict,
        "spend_by_verdict": spend,
        "reviewed": len(reviewed),
        "review_coverage": round(len(reviewed) / len(recs), 3) if recs else 0.0,
        "idle_count": len(idle),
        "idle_rate": round(len(idle) / len(reviewed), 3) if reviewed else None,
        "idle_by_category": idle_by_cat,
        "regret_count": len(regret),
        "regret_spend": regret_spend,
        "usage_overestimate_median": round(statistics.median(bias), 2) if bias else None,
        "usage_overestimate_sample": len(bias),
    }


def privacy_check() -> dict:
    """数据目录不得落在任何 Git 工作树内。"""
    h = home()
    inside = []
    for candidate in [h, *h.parents]:
        if (candidate / ".git").exists():
            inside.append(str(candidate))
    return {"home": str(h), "inside_git_repo": bool(inside), "git_roots": inside}
