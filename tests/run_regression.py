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


def _res(suite, cid, name, ok, detail="", error=False):
    """``error=True`` 表示这条用例是**抛异常**而不是断言失败。

    变异测试要区分两者：抛异常说明变异把代码改崩了，不等于断言抓到了错误行为。
    """
    return {"suite": suite, "id": cid, "name": name, "ok": ok,
            "detail": detail, "error": error}


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
        # 夹具里写 "TODAY" 的日期替换成当天，避免测试随真实时间腐坏
        raw_args = json.loads(json.dumps(c.get("args") or []).replace('"TODAY"', '"%s"' % _today()))
        raw_kwargs = json.loads(json.dumps(c.get("kwargs") or {}).replace('"TODAY"', '"%s"' % _today()))
        try:
            got = fn(*raw_args, **raw_kwargs)
        except Exception as exc:  # noqa: BLE001
            out.append(_res("calc", c["id"], label, False,
                            f"{c['fn']} 抛异常：{exc!r}", error=True))
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


def _today():
    import datetime as _dt
    return _dt.date.today().isoformat()


def _fixture_profile(spec: dict) -> dict:
    """用扁平 facts 构造一份 v2 画像，不动磁盘。

    fact 值可以是：
      - 纯值：视为"今天刚确认"
      - ``{"value": x, "days_ago": n}``：n 天前填的（测过期）
      - ``{"value": x, "confirmed": false}``：历史快照 / 别人给的数字（测未确认）
      - ``{"value": x, "no_date": true}``：有值但没日期（测手写画像绕过时效）
      - ``{"value": x, "days_ahead": n}``：n 天后的日期（测未来日期）
    """
    import datetime as _dt

    p = json.loads(json.dumps(store.DEFAULT_PROFILE_V2))
    today = _dt.date.today()
    facts = {}
    for k, v in (spec.get("facts") or {}).items():
        if isinstance(v, dict) and ("days_ago" in v or "days_ahead" in v
                                    or "confirmed" in v or v.get("no_date")):
            item = {"value": v.get("value")}
            if not v.get("no_date"):
                days = int(v.get("days_ago") or 0) - int(v.get("days_ahead") or 0)
                item["updated_at"] = (today - _dt.timedelta(days=days)).isoformat()
            if "confirmed" in v:
                item["confirmed"] = v["confirmed"]
            item["note"] = "夹具"
            facts[k] = item
        else:
            facts[k] = {"value": v, "updated_at": today.isoformat(), "note": "夹具"}
    p["facts"] = facts
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
        owned = c.get("owned")
        try:
            result = mbm._evaluate(draft, profile, owned)
        except Exception as exc:  # noqa: BLE001
            out.append(_res("pipeline", c["id"], c["name"], False,
                            f"抛异常：{exc!r}", error=True))
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
        want_enforced = c.get("expect_enforced")
        if want_enforced is not None:
            got_enforced = sorted(result["verdict"].get("enforced") or [])
            if got_enforced != sorted(want_enforced):
                problems.append(f"enforced={got_enforced} 期望 {sorted(want_enforced)}")
        want_level = c.get("expect_level")
        if want_level and result["level"]["level"] != want_level:
            problems.append(f"等级 {result['level']['level']} 期望 {want_level}")
        want_warn = c.get("expect_warn")
        if want_warn is not None:
            got_warn = set()
            for gate in result["gates"].values():
                got_warn |= {f["code"] for f in gate.get("findings") or [] if f["severity"] == "WARN"}
            missing_warn = set(want_warn) - got_warn
            if missing_warn:
                problems.append(f"缺警告 {sorted(missing_warn)}；实际 {sorted(got_warn)}")
        # 任意严重度、任意门的检查码：结论不对但门里报了原因，也是要断言的东西
        want_find = c.get("expect_findings")
        if want_find is not None:
            got_all = set()
            for gate in result["gates"].values():
                got_all |= {f["code"] for f in gate.get("findings") or []}
            missing_find = set(want_find) - got_all
            if missing_find:
                problems.append(f"缺检查码 {sorted(missing_find)}；实际 {sorted(got_all)}")
        # 替代方案不成立的具体原因
        want_alt = c.get("expect_alt_problems")
        if want_alt is not None:
            got_alt = sorted((result["numbers"].get("alternative") or {}).get("problems") or [])
            if got_alt != sorted(want_alt):
                problems.append(f"替代品问题 {got_alt} 期望 {sorted(want_alt)}")
        want_verified = c.get("expect_alt_verified")
        if "expect_alt_verified" in c:
            # 显式写 null 也是断言：替代品**不该**拿到任何核验等级
            got_verified = (result["numbers"].get("alternative") or {}).get("price_verified")
            if got_verified != c.get("expect_alt_verified"):
                problems.append(f"替代品核实等级 {got_verified!r} 期望 {want_verified!r}")
        want_alt_price = c.get("expect_alt_price")
        if want_alt_price is not None:
            got_price = (result["numbers"].get("alternative") or {}).get("price")
            if got_price != want_alt_price:
                problems.append(f"替代品价格 {got_price} 期望 {want_alt_price}")
        res = (result["numbers"] or {}).get("resources") or {}
        similar_n = len(res.get("similar") or [])
        if c.get("expect_similar_min") is not None and similar_n < c["expect_similar_min"]:
            problems.append(f"同类资源 {similar_n} 个，期望至少 {c['expect_similar_min']}")
        if c.get("expect_similar_max") is not None and similar_n > c["expect_similar_max"]:
            problems.append(f"同类资源 {similar_n} 个，期望至多 {c['expect_similar_max']}")
        for key in c.get("expect_numbers_absent") or []:
            if result["numbers"].get(key) is not None:
                problems.append(f"numbers.{key} 不该出现，实际 {result['numbers'].get(key)}")
        for key, want in (c.get("expect_gate") or {}).items():
            got = result["gates"][key]["ok"]
            if got != want:
                problems.append(f"gate.{key}.ok={got} 期望 {want}")
        out.append(_res("pipeline", c["id"], c["name"], not problems, "；".join(problems)))
    return out


def run_fxguard():
    """汇率进入计算之前的八道关。

    ``nan`` / ``inf`` / ``10**400`` 这些值写不进 JSON（不是合法的 JSON 数字），
    所以在 Python 里直接构造——正好也覆盖"调用方传进来一个脏 dict"这条真实路径：
    模型从网页上解析出 ``Infinity`` 是完全可能发生的。

    要守的只有一条：**不许它们在换算之前溜过去**。先算出来、再拦下来是没用的——
    一个 1e7 的价格已经进了预算、等级和结论。

    八道关分两类，别混为一谈：

    - **合法性**（能不能进算式）：正的有限数、币种认得、在绝对边界内；
    - **合理性**（离不离谱）：与该币种对的锚点量级相差不超过允许倍数。

    USD/CNY=1 是合法的正数，但不合理。只查合法性会把它放过去。
    """
    out = []

    def check(cid, name, cond, detail=""):
        out.append(_res("fxguard", cid, name, bool(cond), "" if cond else str(detail)))

    def fx(value, base="USD", quote="CNY"):
        return {"kind": "fx", "claim": f"{base}/{quote}", "value": value,
                "base": base, "quote": quote, "source_type": "official",
                "url": "https://example.com/fx", "observed_at": "2026-10-28"}

    def rej(item):
        return evidence.fx_rejection(item, "CNY", FIXTURE_TODAY)

    inf, nan = float("inf"), float("nan")

    check("F1", "NaN 汇率被拒", rej(fx(nan)) == "FX_BAD_VALUE", rej(fx(nan)))
    check("F2", "Infinity 汇率被拒", rej(fx(inf)) == "FX_BAD_VALUE", rej(fx(inf)))
    check("F3", "-Infinity 汇率被拒", rej(fx(-inf)) == "FX_BAD_VALUE", rej(fx(-inf)))
    check("F4", "极端汇率被拒（1e9 与 1e-9）",
          rej(fx(1e9)) == "FX_OUT_OF_RANGE" and rej(fx(1e-9)) == "FX_OUT_OF_RANGE",
          f"{rej(fx(1e9))} / {rej(fx(1e-9))}")
    check("F5", "布尔汇率被拒（True 会被当成 1）",
          rej(fx(True)) == "FX_BAD_VALUE", rej(fx(True)))
    check("F6", "非法币种被拒（BTC/CNY）",
          rej(fx(2000, "BTC", "CNY")) == "FX_BAD_CURRENCY", rej(fx(2000, "BTC", "CNY")))
    try:
        huge = rej(fx(10 ** 400))
    except Exception as exc:  # noqa: BLE001
        huge = f"抛异常：{exc!r}"
    check("F7", "超大整数汇率被拒且不崩（10**400）", huge == "FX_BAD_VALUE", huge)

    dirty = [fx(nan), fx(inf), fx(-inf), fx(1e9), fx(1e-9), fx(True),
             fx(2000, "BTC", "CNY"), fx(10 ** 400)]
    check("F8", "被拒的汇率一条都不进 fx_rates",
          evidence.fx_rates(dirty, "CNY", FIXTURE_TODAY) == {},
          evidence.fx_rates(dirty, "CNY", FIXTURE_TODAY))
    check("F9", "脏汇率下换算直接失败，不产出数字",
          evidence.convert_amount(100, "USD", "CNY", dirty, FIXTURE_TODAY) is None,
          evidence.convert_amount(100, "USD", "CNY", dirty, FIXTURE_TODAY))
    check("F10", "合法汇率照常换算（正向对照）",
          evidence.convert_amount(100, "USD", "CNY", [fx(7.2)], FIXTURE_TODAY) == 720.0,
          evidence.convert_amount(100, "USD", "CNY", [fx(7.2)], FIXTURE_TODAY))

    bad_price = {"kind": "price", "claim": "页面上写着 Infinity", "value": inf,
                 "source_type": "official", "url": "https://example.com/a",
                 "observed_at": "2026-10-28"}
    res = evidence.check([bad_price], as_of=FIXTURE_TODAY, required_kinds=("price",))
    codes = {f["code"] for f in res["findings"]}
    check("F11", "非有限的商品价同样是数据不成立",
          "EV_PRICE_VALUE_INVALID" in codes and not res["ok"],
          f"{sorted(codes)} ok={res['ok']}")

    # --- F12-F15：数值区间合法 ≠ 汇率合理。
    # 1 和 100 都落在 1e-4~1e4 里，但离 USD/CNY 的量级差了 7 倍和 14 倍。
    check("F12", "USD/CNY=1 被拒（区间内但不合理）",
          rej(fx(1)) == "FX_IMPLAUSIBLE", rej(fx(1)))
    check("F13", "USD/CNY=100 被拒（区间内但不合理）",
          rej(fx(100)) == "FX_IMPLAUSIBLE", rej(fx(100)))
    check("F14", "区间内且合理的汇率放行（正向对照）",
          rej(fx(7.2)) is None and rej(fx(6.5)) is None and rej(fx(8.5)) is None,
          f"{rej(fx(7.2))} / {rej(fx(6.5))} / {rej(fx(8.5))}")
    report = evidence.fx_rate_report(fx(7.2), "CNY", FIXTURE_TODAY)
    check("F15", "四层检查分开报：合法性/合理性/来源可过，页面内容核验恒为否",
          report["validity"]["ok"] and report["plausibility"]["ok"]
          and report["source"]["ok"] and report["content"]["ok"] is False
          and report["accepted"] is True,
          report)
    deep = evidence.fx_rate_report(fx(1), "CNY", FIXTURE_TODAY)
    check("F16", "被拒的汇率在报告里写明是合理性这一层不过",
          deep["validity"]["ok"] and deep["plausibility"]["ok"] is False
          and deep["plausibility"]["why"] == "FX_IMPLAUSIBLE"
          and deep["rejection"] == "FX_IMPLAUSIBLE",
          deep)
    return out


def run_pageguard():
    """``page_price`` 这一格：什么才算"页面核对过"。

    0、负数、布尔、``nan``、``inf`` 都不是价格。旧逻辑只对"正数且不一致"报错，
    非正数一律当成"没填"——于是**填一个假页面价反而能换来 page 级标记**，
    比老实留空还占便宜。这个漏洞的方向是反的。
    """
    out = []

    def check(cid, name, cond, detail=""):
        out.append(_res("pageguard", cid, name, bool(cond), "" if cond else str(detail)))

    def item(page, value=899):
        e = {"kind": "price", "claim": "官网 899", "value": value,
             "source_type": "official", "url": "https://example.com/a",
             "observed_at": "2026-10-28"}
        if page is not ...:
            e["page_price"] = page
        return e

    inf, nan = float("inf"), float("nan")
    invalid = [("0", 0), ("负数", -100), ("布尔", True), ("NaN", nan), ("Infinity", inf)]
    for i, (label, value) in enumerate(invalid, start=1):
        got = evidence.quote_content_problem(item(value))
        check(f"G{i}", f"页面价为{label}：判不成立，且不算 page 级",
              got == "PAGE_PRICE_INVALID" and not evidence.page_price_ok(item(value)),
              got)
    check("G6", "页面价与引用价一致：判成立（正向对照）",
          evidence.quote_content_problem(item(899)) is None
          and evidence.page_price_ok(item(899)),
          evidence.quote_content_problem(item(899)))
    check("G7", "页面价与引用价差 3%：判自相矛盾",
          evidence.quote_content_problem(item(925)) == "QUOTE_CONTENT_MISMATCH"
          and not evidence.page_price_ok(item(925)),
          evidence.quote_content_problem(item(925)))
    check("G8", "没填页面价：不算 page 级，也不报错",
          evidence.quote_content_problem(item(...)) is None
          and not evidence.page_price_ok(item(...)),
          evidence.quote_content_problem(item(...)))
    check("G9", "显式填 null 等同没填",
          evidence.quote_content_problem(item(None)) is None
          and not evidence.page_price_ok(item(None)),
          evidence.quote_content_problem(item(None)))

    # --- G10-G14：page_price 只能证明"取证方自述"，不能证明"页面核对过"。
    # 要拿到 content 级，必须给出可复核的页面内容，且引擎自己能在里面找到这个价。
    import hashlib as _hl

    def sha(text):
        return _hl.sha256(text.encode("utf-8")).hexdigest()

    good_text = "本店售价 899 元，全国联保，支持七天无理由退货。"
    other_text = "本店售价 1299 元，全国联保，支持七天无理由退货。"

    check("G10", "只填 page_price、没有页面内容 → reported 级（取证方自述）",
          evidence.page_quote_check(item(899))["level"] == "reported"
          and evidence.page_quote_check(item(899))["problem"] is None,
          evidence.page_quote_check(item(899)))

    g11 = item(899)
    g11["page_text"] = good_text
    g11["page_text_sha256"] = sha(good_text)
    check("G11", "正文里确有这个价且哈希自洽 → content 级（引擎可复核）",
          evidence.page_quote_check(g11)["level"] == "content"
          and evidence.page_quote_check(g11)["problem"] is None,
          evidence.page_quote_check(g11))

    g12 = item(899)
    g12["page_text"] = other_text
    g12["page_text_sha256"] = sha(other_text)
    check("G12", "正文里没有这个价 → 报价上下文核验失败，拿不到任何等级",
          evidence.page_quote_check(g12)["problem"] == "PAGE_TEXT_NO_QUOTE"
          and evidence.page_quote_check(g12)["level"] is None,
          evidence.page_quote_check(g12))

    g13 = item(899)
    g13["page_text"] = good_text
    g13["page_text_sha256"] = sha(good_text + "！")
    check("G13", "正文与声明哈希不符 → 这条页面证据不能用",
          evidence.page_quote_check(g13)["problem"] == "PAGE_TEXT_HASH_MISMATCH"
          and evidence.page_quote_check(g13)["level"] is None,
          evidence.page_quote_check(g13))

    # 不贴正文时的退路：给页面价格集合，同样要能被核对
    g14 = item(899)
    g14["page_price_set"] = [1299.0, 1599.0]
    check("G14", "只给页面价格集合时，集合里没有这个价照样被抓出来",
          evidence.page_quote_check(g14)["problem"] == "PAGE_TEXT_NO_QUOTE"
          and evidence.page_quote_check(g14)["level"] is None,
          evidence.page_quote_check(g14))
    g15 = item(899)
    g15["page_price_set"] = [899.0, 1299.0]
    check("G15", "页面价格集合里有这个价 → content 级（正向对照）",
          evidence.page_quote_check(g15)["level"] == "content",
          evidence.page_quote_check(g15))
    return out


def run_ledger():
    """落账层：账本里写的是**归一后**的理由与证据，不是原始输入。

    直接写 draft 会把外部伪造的 ``enforced``、未换算的外币金额、
    以及判定阶段已丢弃的无效理由一起带进账本。
    """
    import mbm  # 延迟导入

    out = []
    for c in _load("ledger_cases.json"):
        profile = _fixture_profile(c.get("profile") or {})
        draft = copy.deepcopy(c["draft"])
        draft.setdefault("as_of", FIXTURE_TODAY)
        problems = []
        try:
            result = mbm._evaluate(draft, profile, c.get("owned"))
            record = mbm._ledger_record(draft, result)
        except Exception as exc:  # noqa: BLE001
            out.append(_res("ledger", c["id"], c["name"], False,
                            f"抛异常：{exc!r}", error=True))
            continue

        want_ok = c.get("expect_ok")
        if want_ok is not None and result["ok"] != want_ok:
            problems.append(f"ok={result['ok']} 期望 {want_ok}")
        want_verdict = c.get("expect_verdict")
        if want_verdict and record["verdict"] != want_verdict:
            problems.append(f"结论 {record['verdict']} 期望 {want_verdict}")

        got_codes = [r.get("code") for r in record["reasons"]]
        if c.get("expect_reason_codes") is not None and got_codes != c["expect_reason_codes"]:
            problems.append(f"理由 {got_codes} 期望 {c['expect_reason_codes']}")
        if c.get("expect_enforced") is not None \
                and sorted(record["enforced"]) != sorted(c["expect_enforced"]):
            problems.append(f"enforced {record['enforced']} 期望 {c['expect_enforced']}")

        engine_set = set(record["enforced"])
        forged = [
            r["code"] for r in record["reasons"]
            if r.get("enforced") and r["code"] not in engine_set
        ]
        if forged:
            problems.append(f"这些理由带着伪造 enforced 进了账本：{forged}")

        if c.get("expect_evidence_values") is not None:
            got_vals = [e.get("value") for e in record["evidence"]]
            if got_vals != c["expect_evidence_values"]:
                problems.append(f"证据金额 {got_vals} 期望 {c['expect_evidence_values']}")
        if c.get("expect_product_price") is not None:
            got_price = (record["product"] or {}).get("price")
            if got_price != c["expect_product_price"]:
                problems.append(f"商品价 {got_price} 期望 {c['expect_product_price']}")
        if c.get("expect_record_currency") and \
                (record.get("currency") or {}).get("base") != c["expect_record_currency"]:
            problems.append(f"账本币种 {(record.get('currency') or {}).get('base')}")
        out.append(_res("ledger", c["id"], c["name"], not problems, "；".join(problems)))
    return out


def run_profile():
    """画像时间边界：未来日期、缺日期、seed、v1 迁移。

    这一层必须碰磁盘（seed / 迁移就是文件读写），所以用临时数据目录，
    跑完删掉，绝不碰用户真实的 ``~/.maibumai``。
    """
    import datetime as _dt
    import os
    import shutil
    import tempfile

    out = []
    today = _dt.date.today()
    home = Path(tempfile.mkdtemp(prefix="mbm-profile-"))
    old_env = os.environ.get("MAIBUMAI_HOME")

    def check(cid, name, cond, detail=""):
        out.append(_res("profile", cid, name, bool(cond), "" if cond else detail))

    try:
        os.environ["MAIBUMAI_HOME"] = str(home)
        store.ensure_home()
        store.save_profile(store.load_profile())

        def put(key, value, **meta):
            prof = store.load_profile()
            store._set_fact_raw(prof, key, value, **meta)
            store.save_profile(prof)
            return store.load_profile()

        # --- 未来日期
        prof = put("finance.monthly_income", 2600,
                   updated_at=(today + _dt.timedelta(days=400)).isoformat())
        check("T01", "未来日期不算有效事实",
              store.fact_stale(prof, "finance.monthly_income")
              and store.fact_fresh(prof, "finance.monthly_income") is None,
              store.fact_age(prof, "finance.monthly_income"))

        # --- 只比今天晚几天：abs() 之类的"未来也当过去"会在这里露馅
        prof = put("finance.hard_cap_single", 3000,
                   updated_at=(today + _dt.timedelta(days=5)).isoformat())
        check("T09", "只晚几天也算未来日期（不能取绝对值混过去）",
              store.fact_stale(prof, "finance.hard_cap_single")
              and store.fact_fresh(prof, "finance.hard_cap_single") is None,
              store.fact_age(prof, "finance.hard_cap_single"))

        # --- 缺日期
        prof = put("finance.monthly_fixed_expense", 1100, updated_at="")
        check("T02", "没有日期按不可信处理",
              store.fact_stale(prof, "finance.monthly_fixed_expense"),
              prof["facts"]["finance.monthly_fixed_expense"])

        # --- 完整时间戳
        prof = put("finance.savings_target_monthly", 300,
                   updated_at=today.isoformat() + "T10:00:00+08:00")
        check("T03", "完整 ISO 时间戳能解析",
              store.fact_age(prof, "finance.savings_target_monthly") == 0
              and not store.fact_stale(prof, "finance.savings_target_monthly"),
              prof["facts"]["finance.savings_target_monthly"])

        # --- set_fact 用今天
        store.set_fact("invest.monthly_contribution", 400)
        prof = store.load_profile()
        check("T04", "用户刚填的值记为今天且已确认",
              prof["facts"]["invest.monthly_contribution"]["updated_at"] == today.isoformat()
              and prof["facts"]["invest.monthly_contribution"]["confirmed"] is True,
              prof["facts"]["invest.monthly_contribution"])

        # --- seed 没有 observed_at
        profile = store.load_profile()
        profile["facts"] = {}
        store.save_profile(profile)
        store.seed_facts({"finance.liquid_cash": {"value": 32000, "source": "口头"}})
        prof = store.load_profile()
        item = prof["facts"]["finance.liquid_cash"]
        check("T05", "seed 缺 observed_at 不补今天",
              item.get("updated_at") == "" and store.fact_stale(prof, "finance.liquid_cash"),
              item)

        # --- seed 带 observed_at
        profile = store.load_profile()
        profile["facts"] = {}
        store.save_profile(profile)
        store.seed_facts({"finance.liquid_cash": {"value": 32000,
                                                  "observed_at": "2026-08-31",
                                                  "source": "资产快照"}})
        prof = store.load_profile()
        check("T06", "seed 的 observed_at 原样保留",
              prof["facts"]["finance.liquid_cash"]["updated_at"] == "2026-08-31",
              prof["facts"]["finance.liquid_cash"])

        # --- v1 迁移沿用旧时间
        v1 = {"schema_version": 1, "updated_at": "2025-06-01T10:00:00+08:00",
              "budget": {"discretionary_monthly": 2000, "hard_cap_single": 3000},
              "currency": "CNY"}
        (home / "profile.json").write_text(
            json.dumps(v1, ensure_ascii=False), encoding="utf-8")
        prof = store.load_profile()
        item = prof["facts"]["finance.discretionary_override"]
        check("T07", "v1 迁移不把旧数据刷新成今天",
              item["updated_at"] == "2025-06-01T10:00:00+08:00"
              and store.fact_stale(prof, "finance.discretionary_override"),
              item)

        # --- v1 的整体 updated_at 就是今天：文件被保存过 ≠ 每个数字今天确认过
        v1_today = {"schema_version": 1, "updated_at": today.isoformat(),
                    "budget": {"discretionary_monthly": 4000, "hard_cap_single": 20000},
                    "currency": "CNY"}
        (home / "profile.json").write_text(
            json.dumps(v1_today, ensure_ascii=False), encoding="utf-8")
        prof = store.load_profile()
        item = prof["facts"]["finance.discretionary_override"]
        check("T10", "迁移不凭文件时间戳确认字段",
              item["confirmed"] is False
              and store.fact_fresh(prof, "finance.discretionary_override") is None
              and store.financial_snapshot(prof)["monthly_surplus"] is None,
              {"item": item, "surplus": store.financial_snapshot(prof)["monthly_surplus"]})

        # --- v1 无 updated_at
        v1_no_date = {"schema_version": 1,
                      "budget": {"discretionary_monthly": 2000}, "currency": "CNY"}
        (home / "profile.json").write_text(
            json.dumps(v1_no_date, ensure_ascii=False), encoding="utf-8")
        prof = store.load_profile()
        item = prof["facts"]["finance.discretionary_override"]
        check("T08", "v1 没有时间戳时标记为日期不明",
              item["updated_at"] == ""
              and store.fact_stale(prof, "finance.discretionary_override"),
              item)
    finally:
        if old_env is None:
            os.environ.pop("MAIBUMAI_HOME", None)
        else:
            os.environ["MAIBUMAI_HOME"] = old_env
        shutil.rmtree(home, ignore_errors=True)
    return out



def run_realquote():
    """真实网页报价的离线回放。

    ``tests/fixtures/real-quote-record.json`` 由 ``tests/real_quote_probe.py``
    抓取生成，记下 URL、抓取时间、HTTP 状态、正文 sha256 和页面上的价格集合。
    这里**不联网**：CI 用这份快照验证「报价 vs 页面价」这道检查确实生效。

    它证明不了的事情写在记录自己的 `_howto` 里：不证明域名权威、
    不证明现在还是这个价、也不证明取证方真的读了页面。
    """
    import datetime as _dt

    import mbm  # noqa: E402  延迟导入，避免影响其他套件的 import 顺序

    out = []
    record_path = CASES.parent / "fixtures" / "real-quote-record.json"

    def check(cid, name, cond, detail=""):
        out.append(_res("realquote", cid, name, bool(cond), "" if cond else detail))

    if not record_path.exists():
        out.append(_res("realquote", "Q0", "存在真实报价记录", False,
                        f"缺少 {record_path}，先跑 tests/real_quote_probe.py"))
        return out

    record = json.loads(record_path.read_text(encoding="utf-8"))
    claim = record.get("claim_price")
    url = record.get("url")
    fetched = record.get("fetched_at")
    prices = record.get("page_prices") or []

    # --- Q0 记录自身自洽：不要让人手改出一个"看起来像抓过"的文件
    digest = str(record.get("body_sha256") or "")
    check("Q0", "记录自洽（状态/指纹/页面上确有该价格）",
          record.get("http_status") == 200
          and record.get("matched") is True
          and isinstance(claim, (int, float))
          and len(digest) == 64
          and all(ch in "0123456789abcdef" for ch in digest)
          and any(abs(float(p["amount"]) - float(claim)) < 0.005
                  and p.get("currency") == record.get("currency") for p in prices),
          {"status": record.get("http_status"), "matched": record.get("matched"),
           "claim": claim, "prices": prices[:5]})

    try:
        _dt.date.fromisoformat(str(fetched))
        date_ok = True
    except ValueError:
        date_ok = False
    check("Q0b", "抓取日期可解析", date_ok, fetched)

    # --- Q0c 记录里必须有"可复核的页面内容"，否则后面关于 content 级的断言无从谈起
    snippet = record.get("page_text")
    snippet_digest = str(record.get("page_text_sha256") or "")
    snippet_ok = (isinstance(snippet, str) and str(claim) in snippet
                  and len(snippet_digest) == 64
                  and all(ch in "0123456789abcdef" for ch in snippet_digest))
    check("Q0c", "记录带页面正文片段与片段哈希（可复核内容）", snippet_ok,
          {"has_snippet": bool(snippet), "sha_len": len(snippet_digest)})

    base_facts = {
        "finance.monthly_income": 2000, "finance.monthly_fixed_expense": 1500,
        "finance.savings_target_monthly": 0, "invest.monthly_contribution": 0,
        "finance.discretionary_override": 500,
    }

    def draft(alt_value, page_price=None, with_page=True, with_text=False,
              tamper_text=False):
        alt_ev = {"kind": "price", "claim": f"官网 {alt_value}",
                  "value": alt_value, "currency": record.get("currency"),
                  "subject": "alternative", "source_type": "official",
                  "url": url, "observed_at": fetched}
        if with_page and page_price is not None:
            alt_ev["page_price"] = page_price
        if with_text and snippet:
            # tamper_text 只改文字不改哈希：哈希自洽性检查必须抓到
            alt_ev["page_text"] = (snippet + "（本行是事后加的）") if tamper_text else snippet
            alt_ev["page_text_sha256"] = snippet_digest
        elif with_text:
            alt_ev["page_price_set"] = record.get("page_price_set") or []
        return {
            "product": {"name": "某云备份服务 1TB", "price": 8.95, "period": "month",
                        "currency": record.get("currency"), "category": "订阅/云存储"},
            "reasons": [
                {"code": "NEED_REAL", "note": "笔记本照片备份，已经满了两次"},
                {"code": "SUBSTITUTE_ADEQUATE", "note": "同容量有更便宜的对象存储"},
                {"code": "PRICE_ABOVE_FAIR", "note": "按量计费单价明显更高"},
            ],
            "alternative": {"name": "对象存储 B2", "price": alt_value, "period": "month",
                            "currency": record.get("currency"), "source": "官方定价页"},
            "evidence": [
                {"kind": "price", "claim": "8.95", "value": 8.95,
                 "currency": record.get("currency"), "source_type": "official",
                 "url": url, "observed_at": fetched},
                alt_ev,
            ],
            "as_of": fetched,
            # 暂缓必须带复查条件；SWITCH 用不到，但一起写不影响
            "recheck_condition": "打开官方定价页核对当前价后重新判断",
        }

    profile = _fixture_profile({"base_currency": record.get("currency") or "USD",
                                "facts": base_facts})

    # --- Q1 报价与页面价一致，且给了可复核的正文 → content 级（引擎自己能复核）
    res = mbm._evaluate(draft(claim, page_price=claim, with_text=True), profile)
    alt = res["numbers"].get("alternative") or {}
    check("Q1", "给了页面正文且正文里确有这个价 → content 级，可给替换建议",
          res["verdict"]["verdict"] == "SWITCH" and res["ok"] is True
          and alt.get("price_verified") == "content" and not alt.get("problems"),
          {"verdict": res["verdict"]["verdict"], "ok": res["ok"],
           "verified": alt.get("price_verified"), "problems": alt.get("problems")})

    # --- Q1b 只填 page_price、不给正文 → 降为 reported 级（取证方自述）
    res1b = mbm._evaluate(draft(claim, page_price=claim, with_text=False), profile)
    alt1b = res1b["numbers"].get("alternative") or {}
    check("Q1b", "只填页面价不给正文 → reported 级，不许说成引擎核对过",
          res1b["verdict"]["verdict"] == "SWITCH"
          and alt1b.get("price_verified") == "reported",
          {"verdict": res1b["verdict"]["verdict"], "verified": alt1b.get("price_verified")})

    # --- Q2 有人把引用价写成页面价的七成：证据自相矛盾 → 不给 SWITCH
    fake = round(float(claim) * 0.7, 2)
    res2 = mbm._evaluate(draft(fake, page_price=claim), profile)
    alt2 = res2["numbers"].get("alternative") or {}
    check("Q2", "引用价与页面价不符时不给替换建议",
          res2["verdict"]["verdict"] == "DEFER" and res2["ok"] is True
          and "ALT_PRICE_CONTENT_MISMATCH" in (alt2.get("problems") or []),
          {"verdict": res2["verdict"]["verdict"], "ok": res2["ok"],
           "problems": alt2.get("problems")})

    # --- Q3 只有链接、没有页面价：可以给，但只能标到 source 级，不许冒充已核对
    res3 = mbm._evaluate(draft(claim, with_page=False), profile)
    alt3 = res3["numbers"].get("alternative") or {}
    check("Q3", "没填页面价时只标 source 级，不谎称页面核对过",
          res3["verdict"]["verdict"] == "SWITCH"
          and alt3.get("price_verified") == "source",
          {"verdict": res3["verdict"]["verdict"],
           "verified": alt3.get("price_verified")})

    # --- Q4/Q5 把这套检查的**证明边界**钉住，别让它被当成"内容已核实"

    # Q4 不给正文时，page_price 是不是**真的**来自那个页面，仍然查不出来。
    # 这里故意用一个页面上没有的数字：只填 page_price 照样能拿到 reported 级。
    # 这是已知限制，不是已修好的功能——要堵住它只能靠取证方交出可复核的内容。
    ghost = round(float(claim) * 3 + 0.01, 2)
    on_page = any(abs(float(p["amount"]) - ghost) < 0.005
                  and p.get("currency") == record.get("currency") for p in prices)
    res4 = mbm._evaluate(draft(ghost, page_price=ghost, with_text=False), profile)
    alt4 = res4["numbers"].get("alternative") or {}
    check("Q4", "已知限制：不给页面正文时，页面价无法自证来自页面",
          (not on_page)
          and res4["verdict"]["verdict"] == "SWITCH"
          and alt4.get("price_verified") == "reported",
          {"ghost": ghost, "on_page": on_page,
           "verdict": res4["verdict"]["verdict"], "verified": alt4.get("price_verified")})

    # Q5 但**给了正文**就能查出来：这个价在正文里根本不存在。
    res5g = mbm._evaluate(draft(ghost, page_price=ghost, with_text=True), profile)
    alt5g = res5g["numbers"].get("alternative") or {}
    check("Q5", "给了页面正文后，正文里没有的价格会被抓出来（报价上下文核验）",
          res5g["verdict"]["verdict"] == "DEFER"
          and "ALT_PAGE_TEXT_NO_QUOTE" in (alt5g.get("problems") or [])
          and alt5g.get("price_verified") is None,
          {"ghost": ghost, "verdict": res5g["verdict"]["verdict"],
           "problems": alt5g.get("problems"), "verified": alt5g.get("price_verified")})

    # Q6 正文与声明的哈希对不上：这条"页面证据"本身不可信
    res6 = mbm._evaluate(draft(claim, page_price=claim, with_text=True,
                               tamper_text=True), profile)
    alt6 = res6["numbers"].get("alternative") or {}
    check("Q6", "正文被改动后与声明哈希不符 → 不给替换建议",
          res6["verdict"]["verdict"] == "DEFER"
          and "ALT_PAGE_TEXT_HASH_MISMATCH" in (alt6.get("problems") or []),
          {"verdict": res6["verdict"]["verdict"], "problems": alt6.get("problems")})

    # Q7 已知限制固化：哈希只证明"给的正文和声明的哈希一致"，
    # 证明不了正文来自那个 URL。取证方贴一段自己写的、哈希自洽的文字，
    # 引擎照样会给 content 级——这一点本仓库堵不住，只能靠取证方诚实。
    forged = (f"本店特价 {claim} 元，仅此一天。" if not snippet
              else f"本店特价 {claim}，仅此一天。")
    import hashlib as _hashlib
    res7 = mbm._evaluate(draft(claim, page_price=claim, with_text=False), profile)
    alt7ev = dict(((res7["evidence"] or [{}])[-1]) or {})
    forged_item = {k: v for k, v in alt7ev.items()}
    forged_item["page_price"] = claim
    forged_item["page_text"] = forged
    forged_item["page_text_sha256"] = _hashlib.sha256(
        forged.encode("utf-8")).hexdigest()
    import evidence as _ev
    forged_check = _ev.page_quote_check(forged_item)
    check("Q7", "已知限制：自造一段哈希自洽的正文也能拿到 content 级",
          forged_check["level"] == "content" and forged_check["problem"] is None,
          {"check": forged_check})

    # Q8 抓得到的：引用价与"页面上确实有、但不是这一个"的价格不符时会被抓出来。
    others = sorted({round(float(p["amount"]), 2) for p in prices
                     if p.get("currency") == record.get("currency")
                     and float(p["amount"]) > 0
                     and abs(float(p["amount"]) - float(claim)) > 0.005})
    if not others:
        check("Q8", "页面上有其他价格可用于对照", False, f"记录里只有 {prices}")
    else:
        other = others[-1]
        res8 = mbm._evaluate(draft(claim, page_price=other), profile)
        alt8 = res8["numbers"].get("alternative") or {}
        check("Q8", "引用价与页面上真实存在的另一个价格不符时被拦下",
              res8["verdict"]["verdict"] == "DEFER"
              and "ALT_PRICE_CONTENT_MISMATCH" in (alt8.get("problems") or []),
              {"other": other, "verdict": res8["verdict"]["verdict"],
               "problems": alt8.get("problems")})
    return out


SUITES = {
    "verdict": run_verdict,
    "audit": run_audit,
    "evidence": run_evidence,
    "calc": run_calc,
    "pipeline": run_pipeline,
    "ledger": run_ledger,
    "profile": run_profile,
    "fxguard": run_fxguard,
    "pageguard": run_pageguard,
    "realquote": run_realquote,
}


def run_all(suites=None):
    results = []
    for name, fn in SUITES.items():
        if suites and name not in suites:
            continue
        try:
            results.extend(fn())
        except Exception as exc:  # noqa: BLE001
            # 整套崩了也要留痕，否则变异测试会把崩溃误当成"用例抓到了"
            results.append(_res(name, "-", "套件异常", False,
                                f"套件抛异常：{exc!r}", error=True))
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
    if any(r.get("error") for r in results):
        print(f"（其中 {sum(1 for r in results if r.get('error'))} 条是异常，不是断言失败）")
    print("-" * 56)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
