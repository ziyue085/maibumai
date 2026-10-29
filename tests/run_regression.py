#!/usr/bin/env python3
"""回归测试运行器。

五层：
  1. verdict    规则码 → 结论（决策矩阵）
  2. audit      输出审计（一致性门）
  3. evidence   来源、时效、数值合法性（完整性门）
  4. calc       算术与分级
  5. pipeline   跨模块全链路（财务强制、等级、币种、替代品）

断言风格：**精确**。除了 `expect` 里列出的码，不允许出现别的阻断；
确有合理附带项的，必须在 `allow` 里显式声明。这样新增规则不会悄悄溜过去。

用法：
    python tests/run_regression.py
    python tests/run_regression.py -v
    python tests/run_regression.py --suite pipeline
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import calc        # noqa: E402
import evidence    # noqa: E402
import store       # noqa: E402
import verdict     # noqa: E402

CASES = ROOT / "tests" / "cases"

#: 夹具固定基准日，保证测试不随真实日期漂移。
FIXTURE_TODAY = "2026-10-29"


def _load(name):
    return json.loads((CASES / name).read_text(encoding="utf-8"))["cases"]


def _res(suite, cid, name, ok, detail=""):
    return {"suite": suite, "id": cid, "name": name, "ok": ok, "detail": detail}


def _blocking(findings):
    return {f["code"] for f in findings if f["severity"] == "BLOCK"}


def _match(actual: set, expect, allow):
    """expect 必须全部出现；多出来的必须在 allow 里。"""
    expect = set(expect or [])
    allow = set(allow or [])
    missing = expect - actual
    extra = actual - expect - allow
    return not missing and not extra, missing, extra


# ---------------------------------------------------------------------------
def run_verdict():
    out = []
    for c in _load("verdict_cases.json"):
        reasons = [{"code": code, "note": "测试用具体依据，长度足够"} for code in c.get("reasons", [])]
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
            as_of=d.get("as_of") or FIXTURE_TODAY,
            price_class=calc.price_class(prod.get("category")),
            required_kinds=("price",) if needs_price else (),
        )
        au = verdict.audit(d, evr, computed_level=c.get("computed_level"))
        actual = _blocking(au["findings"])
        ok_codes, missing, extra = _match(actual, c.get("expect"), c.get("allow"))
        ok = (au["ok"] == c.get("expect_ok")) and ok_codes
        detail = ""
        if not ok:
            detail = f"期望阻断 {sorted(set(c.get('expect') or []))}，实际 {sorted(actual)}"
            if missing:
                detail += f"；缺 {sorted(missing)}"
            if extra:
                detail += f"；多出 {sorted(extra)}"
            if au["ok"] != c.get("expect_ok"):
                detail += f"；ok={au['ok']} 期望 {c.get('expect_ok')}"
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
        args.setdefault("as_of", FIXTURE_TODAY)
        res = evidence.check(c.get("items") or [], **args)
        actual = {f["code"] for f in res["findings"]}
        ok_codes, missing, extra = _match(actual, c.get("expect"), c.get("allow"))
        ok = (res["ok"] == c.get("expect_ok", res["ok"])) and ok_codes
        detail = "" if ok else f"期望 {c.get('expect')}，实际 {sorted(actual)}；ok={res['ok']}"
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


def _fixture_profile(spec: dict) -> dict:
    """用扁平 facts 构造一份 v2 画像，不动磁盘。"""
    p = json.loads(json.dumps(store.DEFAULT_PROFILE_V2))
    p["facts"] = {
        k: {"value": v, "updated_at": "2026-10-29", "note": "夹具"}
        for k, v in (spec.get("facts") or {}).items()
    }
    p["constraints"] = {**store.DEFAULT_CONSTRAINTS, **(spec.get("constraints") or {})}
    p["meta"]["base_currency"] = spec.get("base_currency", "CNY")
    return p


def run_pipeline():
    """跨模块全链路：财务强制、等级防降、币种、替代品、理由依据。"""
    import mbm  # 延迟导入

    out = []
    for c in _load("pipeline_cases.json"):
        profile = _fixture_profile(c.get("profile") or {})
        draft = copy.deepcopy(c["draft"])
        draft.setdefault("as_of", FIXTURE_TODAY)
        try:
            result = mbm._evaluate(draft, profile)
        except Exception as exc:  # noqa: BLE001
            out.append(_res("pipeline", c["id"], c["name"], False, f"抛异常：{exc!r}"))
            continue

        actual = {f["code"] for f in result["blocking"]}
        ok_codes, missing, extra = _match(actual, c.get("expect_blocking"), c.get("allow"))
        problems = []
        if not ok_codes:
            if missing:
                problems.append(f"缺阻断 {sorted(missing)}")
            if extra:
                problems.append(f"多出阻断 {sorted(extra)}")
        want_verdict = c.get("expect_verdict")
        if want_verdict and result["verdict"]["verdict"] != want_verdict:
            problems.append(f"结论 {result['verdict']['verdict']} 期望 {want_verdict}")
        want_ok = c.get("expect_ok")
        if want_ok is not None and result["ok"] != want_ok:
            problems.append(f"ok={result['ok']} 期望 {want_ok}")
        want_level = c.get("expect_level")
        if want_level and result["level"]["level"] != want_level:
            problems.append(f"等级 {result['level']['level']} 期望 {want_level}")
        for key, want in (c.get("expect_gate") or {}).items():
            got = result["gates"][key]["ok"]
            if got != want:
                problems.append(f"gate.{key}.ok={got} 期望 {want}")
        out.append(_res("pipeline", c["id"], c["name"], not problems, "；".join(problems)))
    return out


SUITES = {
    "verdict": run_verdict,
    "audit": run_audit,
    "evidence": run_evidence,
    "calc": run_calc,
    "pipeline": run_pipeline,
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
