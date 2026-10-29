#!/usr/bin/env python3
"""买不买 AI — 命令行入口。

用法示例
--------
    python scripts/mbm.py init
    python scripts/mbm.py doctor
    python scripts/mbm.py profile show
    python scripts/mbm.py profile set budget.discretionary_monthly=3000
    python scripts/mbm.py owned add --name "索尼 WH-1000XM4" --category "数码/耳机" --condition good
    python scripts/mbm.py check --input draft.json
    python scripts/mbm.py log --input draft.json
    python scripts/mbm.py outcome 20261029-1930-7f3a --bought yes
    python scripts/mbm.py review 20261029-1930-7f3a --input review.json
    python scripts/mbm.py due
    python scripts/mbm.py stats
    python scripts/mbm.py export --out decisions.md

所有命令默认输出 JSON，便于宿主解析；``--human`` 输出中文摘要。
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

try:  # Windows 控制台默认 GBK，中文会炸
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass


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
        raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit("输入必须是 JSON 对象")
    return data


# ---------------------------------------------------------------------------
def cmd_init(args) -> int:
    h = store.ensure_home()
    profile = store.load_profile()
    store.save_profile(profile)
    return _out(
        {"ok": True, "home": str(h), "files": sorted(p.name for p in h.iterdir())},
        args.human,
        lambda d: f"数据目录已就绪：{d['home']}\n含：{', '.join(d['files'])}",
    )


def cmd_doctor(args) -> int:
    priv = store.privacy_check()
    profile = store.load_profile()
    events = store.read_events()
    decisions = store.derive_decisions(events)
    disc = (profile.get("budget") or {}).get("discretionary_monthly")
    checks = [
        {"name": "数据目录可写", "ok": Path(priv["home"]).is_dir()},
        {"name": "数据目录不在 Git 仓库内", "ok": not priv["inside_git_repo"]},
        {"name": "账本可解析", "ok": store.read_events.last_corrupt_lines == 0},
        {"name": "已设置月可支配预算", "ok": isinstance(disc, (int, float)) and disc > 0},
    ]
    return _out(
        {
            "ok": all(c["ok"] for c in checks if c["name"] != "已设置月可支配预算"),
            "home": priv["home"],
            "git_roots": priv["git_roots"],
            "corrupt_lines": store.read_events.last_corrupt_lines,
            "decisions": len(decisions),
            "owned_items": len(store.load_owned()["items"]),
            "checks": checks,
        },
        args.human,
        lambda d: "\n".join(
            [f"数据目录：{d['home']}"] + [("✅ " if c["ok"] else "❌ ") + c["name"] for c in d["checks"]]
        ),
    )


def cmd_profile(args) -> int:
    if args.action == "show":
        return _out(store.load_profile(), args.human, lambda d: json.dumps(d, ensure_ascii=False, indent=2))
    if args.action == "set":
        if not args.pairs:
            raise SystemExit("profile set 需要 key=value")
        for pair in args.pairs:
            if "=" not in pair:
                raise SystemExit(f"格式应为 key=value：{pair}")
            key, _, value = pair.partition("=")
            store.set_profile_value(key.strip(), value)
        return _out(store.load_profile(), args.human, lambda d: "已更新\n" + json.dumps(d, ensure_ascii=False, indent=2))
    return _out({"error": "未知操作"}, args.human)


def cmd_owned(args) -> int:
    if args.action == "add":
        item = {
            "name": args.name,
            "category": args.category or "未分类",
            "condition": args.condition,
            "acquired": args.acquired,
            "price": args.price,
            "notes": args.notes or "",
        }
        return _out(store.add_owned(item), args.human, lambda d: f"已登记：{d['name']}（{d['condition']}）")
    if args.action == "list":
        items = store.load_owned()["items"]
        return _out(items, args.human, lambda d: "\n".join(f"- {i['name']}｜{i['category']}｜{i['condition']}" for i in d) or "（空）")
    if args.action == "find":
        return _out(store.find_owned(args.query), args.human, lambda d: json.dumps(d, ensure_ascii=False, indent=2))
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


def _evaluate(draft: dict, profile: dict) -> dict:
    product = draft.get("product") or {}
    price = product.get("price")
    prediction = draft.get("prediction") or {}

    lvl = draft.get("level")
    if not lvl:
        lvl = calc.level_of(
            price,
            product.get("category"),
            product.get("period", "once"),
            profile,
        )
        draft["level"] = lvl["level"]
    else:
        lvl = {"level": lvl, "kind": calc.classify_category(product.get("category")), "reason": "调用方指定"}

    resolved = vd.resolve(draft.get("reasons") or [], draft.get("alternative"), draft.get("override"))

    # 只有"要买/换方案"的结论才强制要求价格证据；不建议购买不需要为了拦截而凑证据
    needs_price = resolved["verdict"] in ("BUY", "SWITCH")
    ev_result = ev.check(
        draft.get("evidence") or [],
        as_of=draft.get("as_of"),
        price_class=calc.price_class(product.get("category")),
        required_kinds=("price",) if needs_price else (),
    )

    goal = (profile.get("budget") or {}).get("savings_goal") or {}
    goals = [goal] if goal.get("name") and goal.get("monthly") else None

    numbers = calc.build_numbers(
        price,
        prediction.get("uses_per_month"),
        prediction.get("months"),
        profile,
        goals,
        kind=lvl["kind"],
        period=product.get("period", "once"),
    )

    audit_input = dict(draft)
    audit_input.setdefault("verdict", resolved["verdict"])
    audit_result = vd.audit(audit_input, ev_result)

    # 只有"要买/换方案"才需要证据层全绿；暂缓或否决时，过期价格本身就是理由，不是缺陷
    asserts_price = resolved["verdict"] in ("BUY", "SWITCH")

    blocking = [f for f in audit_result["findings"] if f["severity"] == "BLOCK"]
    if asserts_price:
        blocking += [f for f in ev_result["findings"] if f["severity"] == "BLOCK"]

    return {
        "level": lvl,
        "numbers": numbers,
        "evidence": ev_result,
        "verdict": resolved,
        "audit": audit_result,
        "blocking": blocking,
        "ok": not blocking,
    }


def cmd_check(args) -> int:
    draft = _load_input(args.input)
    result = _evaluate(draft, store.load_profile())
    return _out(result, args.human, _human_check)


def _human_check(d: dict) -> str:
    v = d["verdict"]
    lines = [
        f"结论：{v['verdict_zh']}",
        f"分级：{d['level']['level']}（{d['level']['reason']}）",
        f"可落账：{'是' if d['ok'] else '否'}",
    ]
    if v.get("note"):
        lines.append(f"说明：{v['note']}")
    num = d["numbers"]
    if num.get("cost_per_use") is not None:
        lines.append(f"单次成本：{num['cost_per_use']} 元")
    if num.get("cost_per_use_note"):
        lines.append(f"　{num['cost_per_use_note']}")
    if num.get("missing"):
        lines.append("无法计算：" + "、".join(num["missing"]))

    blocking = d.get("blocking") or []
    if blocking:
        lines.append("必须处理（否则不能落账）：")
        lines += [f"  [{f['code']}] {f['message']}" for f in blocking]

    blocking_ids = {id(f) for f in blocking}
    rest = [
        f
        for f in d["evidence"]["findings"] + d["audit"]["findings"]
        if id(f) not in blocking_ids
    ]
    if rest:
        lines.append("提示（不影响当前结论）：")
        lines += [f"  [{f['code']}] {f['message']}" for f in rest]
    if not blocking and not rest:
        lines.append("待处理：无")
    return "\n".join(lines)


def cmd_log(args) -> int:
    draft = _load_input(args.input)
    profile = store.load_profile()
    result = _evaluate(draft, profile)
    if not result["ok"] and not args.force:
        print(json.dumps({"ok": False, "refused": True, "result": result}, ensure_ascii=False, indent=2))
        return 2
    record = {
        "type": "decision",
        "id": draft.get("id") or store.new_id(),
        "level": draft["level"],
        "product": draft.get("product") or {},
        "verdict": result["verdict"]["verdict"],
        "reasons": draft.get("reasons") or [],
        "alternative": draft.get("alternative"),
        "numbers": result["numbers"],
        "evidence": draft.get("evidence") or [],
        "prediction": draft.get("prediction") or {},
        "recheck_condition": draft.get("recheck_condition"),
        "notes": draft.get("notes", ""),
        "forced": bool(args.force and not result["ok"]),
    }
    event = store.append_event(record)
    return _out(
        {"ok": True, "id": record["id"], "verdict": record["verdict"], "at": event["at"]},
        args.human,
        lambda d: f"已记录 #{d['id']}｜{d['verdict']}",
    )


def cmd_outcome(args) -> int:
    bought = str(args.bought).lower() in ("yes", "y", "true", "1")
    store.append_event({"type": "outcome", "ref": args.id, "bought": bought})
    return _out({"ok": True, "ref": args.id, "bought": bought}, args.human,
                lambda d: f"已标记 #{d['ref']}：{'已购买' if d['bought'] else '未购买'}")


def cmd_review(args) -> int:
    payload = _load_input(args.input)
    actual = payload.get("actual") or payload
    store.append_event(
        {"type": "review", "ref": args.id, "actual": actual, "notes": payload.get("notes", "")}
    )
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
            f"{vd.VERDICT_ZH.get(rec.get('verdict'), rec.get('verdict'))} | {mark} |"
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

    pr = sub.add_parser("profile", parents=[common])
    pr.add_argument("action", choices=["show", "set"])
    pr.add_argument("pairs", nargs="*", help="key=value")
    pr.set_defaults(func=cmd_profile)

    ow = sub.add_parser("owned", parents=[common])
    ow.add_argument("action", choices=["add", "list", "find", "set"])
    ow.add_argument("--name")
    ow.add_argument("--category")
    ow.add_argument("--condition", default="good", choices=list(store.CONDITIONS))
    ow.add_argument("--acquired")
    ow.add_argument("--price", type=float)
    ow.add_argument("--notes")
    ow.add_argument("--query")
    ow.add_argument("--id")
    ow.set_defaults(func=cmd_owned)

    ck = sub.add_parser("check", parents=[common], help="校验一份决策草稿")
    ck.add_argument("--input", required=True)
    ck.set_defaults(func=cmd_check)

    lg = sub.add_parser("log", parents=[common], help="写入账本")
    lg.add_argument("--input", required=True)
    lg.add_argument("--force", action="store_true", help="忽略审计阻断强写（会标记 forced）")
    lg.set_defaults(func=cmd_log)

    oc = sub.add_parser("outcome", parents=[common])
    oc.add_argument("id")
    oc.add_argument("--bought", default="yes")
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
