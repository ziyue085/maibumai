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
ENGINE_CODES = {"BUDGET_BREACH", "BUDGET_TIGHT", "AFFORDABLE_WITHIN_BUDGET", "CONSTRAINT_VIOLATION"}


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

    # 5) 结论
    resolved = vd.resolve(reasons, draft.get("alternative"), draft.get("override"))
    verdict = resolved["verdict"]
    needs_price = verdict in ("BUY", "SWITCH")

    # 6) 财务门的发现（严重度取决于等级与结论，所以要等结论出来再定）
    financial = []
    if fin["status"] == "breach":
        tail = {"hard_cap": "触及你自设的单笔上限", "capacity": "存量加结余也覆盖不了"}.get(
            fin.get("reason"), "超出承受能力")
        financial.append({
            "code": "BUDGET_BREACH", "severity": "INFO", "gate": "financial",
            "message": f"{fin['detail']}（{tail}），结论已被强制为不建议购买",
            "detail": fin,
        })
    elif fin["status"] == "tight":
        financial.append({
            "code": "BUDGET_TIGHT", "severity": "WARN", "gate": "financial",
            "message": f"{fin['detail']}：需动用存量或跨月，结论已被强制为暂缓",
            "detail": fin,
        })
    elif fin["status"] == "ok_stock" and fin.get("heavy"):
        financial.append({
            "code": "FIN_RESERVE_HEAVY", "severity": "WARN", "gate": "financial",
            "message": fin["detail"] + "，一次用掉超过一半的可动用存量",
            "detail": fin,
        })
    elif fin["status"] == "unknown":
        severe = level == "L3" and isinstance(price, (int, float)) and price > 0
        hint = ""
        if snap.get("unconfirmed_fields"):
            hint = "；你有历史记录但还没确认：" + "、".join(snap["unconfirmed_fields"])
        elif snap.get("missing_fields"):
            hint = "；还缺：" + "、".join(snap["missing_fields"])
        financial.append({
            "code": "FIN_BUDGET_UNKNOWN", "severity": "BLOCK" if severe else "WARN",
            "gate": "financial",
            "message": "收入/支出/存量都不足以判断承受力" + hint + (
                "。这是重决策，先确认再判断" if severe else ""),
            "detail": fin,
        })

    if snap.get("stale_fields"):
        financial.append({
            "code": "FIN_PROFILE_STALE", "severity": "WARN", "gate": "financial",
            "message": "这些财务数据已过期，未参与本次计算，需要先确认：" + "、".join(snap["stale_fields"]),
            "detail": {"stale": snap["stale_fields"]},
        })

    if snap.get("surplus_estimated"):
        severe_est = level == "L3" and verdict == "BUY"
        financial.append({
            "code": "FIN_SURPLUS_ESTIMATED", "severity": "BLOCK" if severe_est else "WARN",
            "gate": "financial",
            "message": "储蓄目标或每月投资额还没填，可支配余额被高估"
                      + ("；大额购买前必须先确认" if severe_est else ""),
            "detail": {"surplus": snap.get("monthly_surplus")},
        })

    # 7) 证据校验：L1 不要求价格证据，否则快判通道形同虚设
    ev_result = ev.check(
        items,
        as_of=as_of,
        price_class=calc.price_class(product.get("category")),
        required_kinds=("price",) if (needs_price and level != "L1") else (),
    )
    if currency_findings:
        _append_findings(ev_result, currency_findings)

    goal_name = store.fact(profile, "goal.name")
    goal_monthly = store.fact(profile, "goal.monthly")
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
        "spendable": snap.get("spendable"),
        "spendable_note": snap.get("spendable_note"),
        "hard_cap": snap.get("hard_cap"),
        "stale_fields": snap.get("stale_fields"),
    }

    # 8) 替代品也要过预算、周期、证据三关，不能"换个更买不起的"
    alt_raw = draft.get("alternative")
    if isinstance(alt_raw, dict) and str(alt_raw.get("name") or "").strip():
        alt_price = alt_raw.get("price")
        alt_kind = calc.classify_category(alt_raw.get("category") or product.get("category"))
        alt_period = alt_raw.get("period")
        alt_budget = None
        if isinstance(alt_price, (int, float)) and alt_price > 0:
            alt_budget_price, alt_monthly = alt_price, None
            if alt_kind == "subscription":
                apm = calc.period_months(alt_period)
                if apm:
                    alt_budget_price = alt_price * 12 / apm
                    alt_monthly = alt_price / apm
            ab = calc.budget_status(alt_budget_price, profile, monthly_equiv=alt_monthly)
            alt_budget = {"status": ab["status"], "code": ab["code"], "detail": ab["detail"]}
        numbers["alternative"] = {
            "price": alt_price if isinstance(alt_price, (int, float)) else None,
            "kind": alt_kind,
            "budget": alt_budget,
            "annual_cost": calc.annual_of(alt_price, alt_period)
            if (alt_kind == "subscription" and isinstance(alt_price, (int, float))) else None,
            "period_missing": alt_kind == "subscription" and calc.period_months(alt_period) is None,
        }

    # 9) 一致性审计：只看模型自己给的理由，引擎注入的只参与一致性判断
    own_reasons = [r for r in reasons if not r.get("enforced")]
    enforced_only = [r["code"] for r in reasons if r.get("enforced")]
    audit_input = {
        **draft,
        "verdict": verdict,
        "level": level,
        "product": product,
        "evidence": items,
        "reasons": own_reasons,
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

    # 10) 三道门分开报告
    gates = {
        "financial": {
            "ok": not any(f["severity"] == "BLOCK" for f in financial),
            "findings": financial,
            "status": fin["status"],
            "enforced": [c for c in enforced_codes if c in
                         ("BUDGET_BREACH", "BUDGET_TIGHT", "CONSTRAINT_VIOLATION")],
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
    }


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
    if num.get("missing"):
        lines.append("无法计算：" + "、".join(num["missing"]))

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
    record = {
        "type": "decision",
        "id": draft.get("id") or store.new_id(),
        "level": result["level"]["level"],
        "product": result["product"],
        "verdict": result["verdict"]["verdict"],
        "reasons": [r for r in draft.get("reasons") or []],
        "enforced": result["verdict"].get("enforced") or [],
        "alternative": draft.get("alternative"),
        "numbers": result["numbers"],
        "evidence": draft.get("evidence") or [],
        "prediction": draft.get("prediction") or {},
        "recheck_condition": draft.get("recheck_condition"),
        "overridden": result["verdict"].get("overridden", False),
        "notes": draft.get("notes", ""),
    }
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
    record = {
        "type": "decision",
        "id": draft.get("id") or store.new_id(),
        "level": result["level"]["level"],
        "product": result["product"],
        "verdict": result["verdict"]["verdict"],
        "reasons": draft.get("reasons") or [],
        "enforced": result["verdict"].get("enforced") or [],
        "alternative": draft.get("alternative"),
        "numbers": result["numbers"],
        "evidence": draft.get("evidence") or [],
        "prediction": draft.get("prediction") or {},
        "recheck_condition": draft.get("recheck_condition"),
        "overridden": bool(result["verdict"].get("overridden")),
        "forced": bool(args.force and not result["ok"]),
        "notes": draft.get("notes", ""),
    }
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


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
