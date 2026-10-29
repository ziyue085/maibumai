#!/usr/bin/env python3
"""回归测试运行器。

三层：
  1. verdict  规则码 → 结论（决策矩阵）
  2. audit    输出审计（自洽性 / 具体性 / 可核验性）
  3. evidence 来源与时效
  4. calc     算术与分级

用法：
    python tests/run_regression.py            # 全部
    python tests/run_regression.py -v         # 显示每条
    python tests/run_regression.py --suite verdict
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import calc        # noqa: E402
import evidence    # noqa: E402
import verdict     # noqa: E402

CASES = ROOT / "tests" / "cases"


def _load(name):
    return json.loads((CASES / name).read_text(encoding="utf-8"))["cases"]


def _res(suite, cid, name, ok, detail=""):
    return {"suite": suite, "id": cid, "name": name, "ok": ok, "detail": detail}


# ---------------------------------------------------------------------------
def run_verdict():
    out = []
    for c in _load("verdict_cases.json"):
        reasons = [{"code": x} for x in c.get("reasons", [])]
        got = verdict.resolve(reasons, c.get("alternative"), c.get("override"))
        ok = got["verdict"] == c["expect"]
        out.append(
            _res("verdict", c["id"], c["name"], ok,
                 "" if ok else f"期望 {c['expect']}，实际 {got['verdict']}（{got.get('note')}）")
        )
    return out


def run_audit():
    out = []
    for c in _load("audit_cases.json"):
        d = c["decision"]
        prod = d.get("product") or {}
        needs_price = d.get("verdict") in ("BUY", "SWITCH")
        evr = evidence.check(
            d.get("evidence") or [],
            as_of=d.get("as_of"),
            price_class=calc.price_class(prod.get("category")),
            required_kinds=("price",) if needs_price else (),
        )
        au = verdict.audit(d, evr)
        blocking = {f["code"] for f in au["findings"] if f["severity"] == "BLOCK"}
        missing = [code for code in c.get("expect", []) if code not in blocking]
        ok = (au["ok"] == c.get("expect_ok")) and not missing
        detail = ""
        if not ok:
            detail = f"期望阻断 {c.get('expect')}，实际 {sorted(blocking)}；ok={au['ok']}"
        if ok and c.get("expect_ok") and au["warnings"]:
            ok = False
            detail = f"反向对照出现了 {au['warnings']} 条警告：" + str(
                [f["code"] for f in au["findings"]]
            )
        out.append(_res("audit", c["id"], c["name"], ok, detail))
    return out


def run_evidence():
    out = []
    for c in _load("evidence_cases.json"):
        args = dict(c.get("args") or {})
        res = evidence.check(c.get("items") or [], **args)
        codes = {f["code"] for f in res["findings"]}
        missing = [code for code in c.get("expect", []) if code not in codes]
        ok = (res["ok"] == c.get("expect_ok", res["ok"])) and not missing
        detail = "" if ok else f"期望 {c.get('expect')}，实际 {sorted(codes)}；ok={res['ok']}"
        out.append(_res("evidence", c["id"], c["name"], ok, detail))
    return out


def run_calc():
    out = []
    for c in _load("calc_cases.json"):
        fn = getattr(calc, c["fn"])
        label = c.get("name") or c["fn"]
        try:
            got = fn(*(c.get("args") or []), **(c.get("kwargs") or {}))
        except Exception as exc:  # noqa: BLE001
            out.append(_res("calc", c["id"], label, False, f"{c['fn']} 抛异常：{exc!r}"))
            continue
        bad = []
        for key, want in (c.get("expect") or {}).items():
            if key == "value":
                actual = got
            elif key == "len":
                actual = len(got) if hasattr(got, "__len__") else None
            elif isinstance(got, dict):
                actual = got.get(key)
            else:
                actual = None
            if actual != want:
                bad.append(f"{key}={actual!r} 期望 {want!r}")
        out.append(_res("calc", c["id"], label, not bad, "；".join(bad)))
    return out


SUITES = {
    "verdict": run_verdict,
    "audit": run_audit,
    "evidence": run_evidence,
    "calc": run_calc,
}


def run_all(suites=None):
    results = []
    for name, fn in SUITES.items():
        if suites and name not in suites:
            continue
        results.extend(fn())
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--suite", action="append", choices=list(SUITES))
    args = ap.parse_args(argv)

    results = run_all(args.suite)
    failed = [r for r in results if not r["ok"]]

    for r in results:
        if args.verbose or not r["ok"]:
            print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['suite']:8s} {r['id']:4s} {r['name']}")
            if not r["ok"] and r["detail"]:
                print(f"        {r['detail']}")

    by_suite = {}
    for r in results:
        s = by_suite.setdefault(r["suite"], [0, 0])
        s[0] += 1
        s[1] += 0 if r["ok"] else 1

    print("\n" + "-" * 56)
    for name, (total, bad) in by_suite.items():
        print(f"{name:10s} {total - bad}/{total} 通过")
    print(f"{'总计':10s} {len(results) - len(failed)}/{len(results)} 通过")
    print("-" * 56)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
