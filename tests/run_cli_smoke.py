#!/usr/bin/env python3
"""端到端冒烟：用临时数据目录跑一遍完整生命周期。

验证"能不能真的用起来"，而不是函数返回值：
    init → 填画像 → 看摘要 → 登记已有物品 → 判定落账 → 标记已购
    → 复盘 → 统计 → 导出 → 隐私自检 → 财务强制 → 币种阻断
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "mbm.py"
PY = sys.executable
TODAY = dt.date.today().isoformat()


class Ctx:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mbm-smoke-"))
        self.home = self.tmp / "data"
        self.env = dict(os.environ, MAIBUMAI_HOME=str(self.home))
        self.checks = []

    def run(self, *args, expect=0, stdin=None):
        proc = subprocess.run(
            [PY, str(CLI), *args],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, input=stdin,
        )
        if proc.returncode != expect:
            raise AssertionError(
                f"`mbm {' '.join(args)}` 退出码 {proc.returncode}（期望 {expect}）\n"
                f"stdout={proc.stdout[:1500]}\nstderr={proc.stderr[:800]}"
            )
        return proc.stdout

    def json(self, *args, **kw):
        return json.loads(self.run(*args, **kw))

    def check(self, name, cond, detail=""):
        self.checks.append({"name": name, "ok": bool(cond), "detail": "" if cond else str(detail)[:500]})

    def write(self, name, obj):
        p = self.tmp / name
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


GOOD = {
    "product": {"name": "索尼 WH-1000XM5", "price": 899, "category": "数码/耳机"},
    "reasons": [
        # 故意带上一个外部伪造的 enforced 标记：判定阶段必须清掉，账本里也不许留
        {"code": "NEED_REAL", "note": "每天地铁通勤 2 小时，旧耳机降噪失效三个月",
         "enforced": True},
        {"code": "PRICE_FAIR", "note": "三家自营 869-929，无溢价"},
        {"code": "DURABILITY_GAIN", "note": "预期使用三年以上"},
    ],
    "evidence": [
        {"kind": "price", "claim": "自营 899", "value": 899,
         "source_type": "authorized_retail", "url": "https://example.com/xm5",
         "observed_at": TODAY}
    ],
    "prediction": {"uses_per_month": 20, "months": 36},
    "summary": "通勤刚需，价格在正常区间。",
}

EXPENSIVE = {
    "product": {"name": "专业相机", "price": 18000, "category": "数码/相机"},
    "reasons": [
        {"code": "NEED_REAL", "note": "想学摄影，已经借朋友的机子拍了一个月"},
        {"code": "PRICE_FAIR", "note": "三家官方渠道都是这个价"},
        {"code": "LOW_REGRET_RISK", "note": "二手残值约七成，随时能出手"},
    ],
    "evidence": [
        {"kind": "price", "claim": "18000", "value": 18000, "source_type": "official",
         "url": "https://example.com/cam", "observed_at": TODAY}
    ],
    "prediction": {"uses_per_month": 4, "months": 60},
}

NO_PRICE = {
    "product": {"name": "某品牌耳机", "price": 900, "category": "数码/耳机"},
    "reasons": [
        {"code": "NEED_REAL", "note": "旧耳机降噪失效已三个月"},
        {"code": "PRICE_FAIR", "note": "店里看到就是这个价"},
        {"code": "LOW_REGRET_RISK", "note": "支持七天无理由退货"},
    ],
    # 故意一条价格证据都不给：只有转述，没有核实的报价
    "evidence": [],
    "recheck_condition": "查到公开报价后重新判断",
}

FOREIGN = {
    "product": {"name": "某耳机", "price": 899, "currency": "USD", "category": "数码/耳机"},
    "reasons": [
        {"code": "NEED_REAL", "note": "旧耳机降噪失效已三个月"},
        {"code": "PRICE_FAIR", "note": "官网就是这个价"},
        {"code": "LOW_REGRET_RISK", "note": "支持七天无理由退货"},
    ],
    "evidence": [
        {"kind": "price", "claim": "899 USD", "value": 899, "currency": "USD",
         "source_type": "official", "url": "https://example.com/usd", "observed_at": TODAY}
    ],
}


def main() -> int:
    ctx = Ctx()
    try:
        out = ctx.json("init")
        ctx.check("init 创建数据目录", Path(out["home"]).is_dir(), out)

        ctx.json("profile", "set",
                 "finance.monthly_income=2500",
                 "finance.monthly_fixed_expense=1200",
                 "finance.savings_target_monthly=0",
                 "invest.monthly_contribution=300")
        brief = ctx.json("brief")
        ctx.check("月可支配按收入减支出减储蓄减投资推算", brief["discretionary"] == 1000, brief["discretionary"])
        ctx.check("存量未填时标注为未知", "⑤ 可自由消费资金：未知" in brief["brief"], brief["brief"][:500])
        ctx.check("摘要按存量/现金流六层展开",
                  "净资产" in brief["brief"] and "月结余" in brief["brief"],
                  brief["brief"][:400])

        gaps = ctx.json("profile", "gaps", "--limit", "3")
        ctx.check("能列出待补画像字段", len(gaps) == 3 and "question" in gaps[0], gaps)

        ctx.json("owned", "add", "--name", "旧耳机", "--category", "数码/耳机",
                 "--condition", "failing", "--price", "299")
        found = ctx.json("owned", "find", "--query", "耳机")
        ctx.check("已有物品可检索", len(found) == 1 and found[0]["condition"] == "failing", found)

        good_path = ctx.write("good.json", GOOD)
        chk = ctx.json("check", "--input", good_path)
        ctx.check("合格草稿给出建议购买", chk["verdict"]["verdict"] == "BUY", chk["verdict"])
        ctx.check("合格草稿可落账", chk["ok"], chk["blocking"])
        ctx.check("单次成本可推导", chk["numbers"]["cost_per_use"] == round(899 / (20 * 36), 2),
                  chk["numbers"])

        logged = ctx.json("judge", "--input", good_path)
        did = logged["id"]
        ctx.check("judge 一步完成判定与落账", logged["logged"] is True, logged)

        # 关键回归：存量未知 ≠ 存量为零。18000 元只能先说「不知道」，不能判买不起
        exp_path = ctx.write("exp.json", EXPENSIVE)
        exp_unknown = ctx.json("check", "--input", exp_path)
        ctx.check("存量未知时 18000 元降为暂缓，而不是买不起",
                  exp_unknown["verdict"]["verdict"] == "DEFER", exp_unknown["verdict"])
        ctx.check("降级理由是「承受力无法确认」",
                  "BUDGET_UNVERIFIED" in (exp_unknown["verdict"]["enforced"] or []),
                  exp_unknown["verdict"]["enforced"])

        # 存量确认为零之后，才谈得上越过安全底线
        ctx.json("profile", "set", "finance.liquid_cash=0",
                 "finance.emergency_fund=0", "finance.near_term_obligations=0")
        exp = ctx.json("judge", "--input", exp_path)
        ctx.check("存量确认不够时强制为不建议购买",
                  exp["result"]["verdict"]["verdict"] == "DONT_BUY", exp["result"]["verdict"])
        ctx.check("强制理由来自财务门",
                  "BUDGET_BREACH" in (exp["result"]["gates"]["financial"]["enforced"] or []),
                  exp["result"]["gates"]["financial"])

        # 关键回归：外币无汇率时不能用外币数字硬比预算
        usd_path = ctx.write("usd.json", FOREIGN)
        raw = ctx.run("judge", "--input", usd_path, expect=2)
        usd = json.loads(raw)
        ctx.check("外币缺汇率时不给购买结论",
                  usd["result"]["verdict"]["verdict"] == "DEFER", usd["result"]["verdict"])
        ctx.check("换不出本币的价格不进入预算计算",
                  usd["result"]["product"]["price"] is None, usd["result"]["product"])
        ctx.check("外币缺汇率时账本里也没有原始金额",
                  any(f["code"] == "EV_CURRENCY_UNCONVERTED"
                      for f in usd["result"]["gates"]["integrity"]["findings"]),
                  usd["result"]["gates"]["integrity"])

        # 关键回归：一条报价都没有时，结论是暂缓并可落账（不是「买了别记账」）
        np_path = ctx.write("no-price.json", NO_PRICE)
        np_res = ctx.json("judge", "--input", np_path)
        ctx.check("没有报价时结论为暂缓",
                  np_res["result"]["verdict"]["verdict"] == "DEFER",
                  np_res["result"]["verdict"])
        ctx.check("暂缓理由来自价格缺失（PRICE_UNVERIFIED）",
                  "PRICE_UNVERIFIED" in (np_res["result"]["verdict"]["enforced"] or []),
                  np_res["result"]["verdict"]["enforced"])
        ctx.check("没有报价时仍可落账（ok=True）", np_res["result"]["ok"] is True,
                  np_res["result"]["blocking"])
        ctx.check("落账记录里没有把「在预算内」写上",
                  "AFFORDABLE_WITHIN_BUDGET" not in (np_res["result"]["verdict"]["enforced"] or []),
                  np_res["result"]["verdict"]["enforced"])

        ctx.json("outcome", did, "--bought", "yes")
        review_path = ctx.write("review.json", {
            "actual": {"uses_per_month": 0.5, "satisfaction": 2,
                       "would_buy_again": False, "idle_reason": "夏天戴久了闷"}
        })
        ctx.json("review", did, "--input", review_path)

        st = ctx.json("stats")
        ctx.check("统计覆盖 3 笔决策", st["decisions"] == 3, st)
        ctx.check("暂缓也计入统计（不是只统计买过的）",
                  st["by_verdict"].get("DEFER") == 1, st["by_verdict"])
        ctx.check("统计覆盖 1 条复盘", st["reviewed"] == 1, st)
        ctx.check("识别为闲置", st["idle_rate"] == 1.0, st)
        ctx.check("追踪后悔支出", st["regret_spend"] == 899, st)
        ctx.check("使用频率高估倍数被量化", st["usage_overestimate_median"] == 40.0, st)

        due = ctx.json("due", "--days", "0")
        ctx.check("已复盘的不再提醒", due == [], due)

        md = ctx.tmp / "export.md"
        ctx.json("export", "--out", str(md))
        text = md.read_text(encoding="utf-8")
        ctx.check("导出含两笔记录", "WH-1000XM5" in text and "专业相机" in text, text[:300])

        # 存量层：月结余只有 1000，但可动用存量足以覆盖 8000
        ctx.json("profile", "set",
                 "finance.liquid_cash=30000",
                 "finance.emergency_fund=6000",
                 "finance.near_term_obligations=8000")
        big = {
            "product": {"name": "二手相机 + 镜头", "price": 8000, "category": "数码/相机"},
            "reasons": [
                {"code": "NEED_REAL", "note": "已经借机拍了一个月，每周都出去拍"},
                {"code": "PRICE_FAIR", "note": "同成色二手行情 7500-8500"},
                {"code": "LOW_REGRET_RISK", "note": "二手残值稳定，随时能转手"},
            ],
            "evidence": [{"kind": "price", "claim": "8000", "value": 8000,
                          "source_type": "third_party", "url": "https://example.com/big",
                          "observed_at": TODAY}],
            "prediction": {"uses_per_month": 3, "months": 36},
        }
        big_path = ctx.write("big.json", big)
        big_res = ctx.json("judge", "--input", big_path)
        ctx.check("可动用存量覆盖时不被月收入否决",
                  big_res["result"]["verdict"]["verdict"] == "BUY", big_res["result"]["verdict"])
        ctx.check("可动用存量按现金减应急金减近期支出",
                  big_res["result"]["numbers"]["financial"]["spendable"] == 16000,
                  big_res["result"]["numbers"]["financial"])

        doc = ctx.json("doctor")
        ctx.check("数据目录不在 Git 仓库内", not doc["git_roots"], doc)
        ctx.check("仓库内无个人数据文件", doc["checks"][2]["ok"], doc["checks"][2])

        # 账本里必须写判定阶段归一后的理由：外部伪造的 enforced 不许入库
        ledger = (ctx.home / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        rec = json.loads(ledger[0])
        forged = [r["code"] for r in rec["reasons"]
                  if r.get("enforced") and r["code"] not in (rec["enforced"] or [])]
        ctx.check("外部伪造的 enforced 没有进账本", not forged, rec["reasons"])
        ctx.check("引擎判定的预算项仍标记为 enforced",
                  "AFFORDABLE_WITHIN_BUDGET" in (rec["enforced"] or []), rec["enforced"])
        ctx.check("外部自宣的预算结论被丢弃",
                  all(r["code"] != "BUDGET_BREACH" for r in rec["reasons"]), rec["reasons"])

        # 暂缓也能落账：结论是"先补信息"，不是"不许记录"
        ctx.json("profile", "set", "finance.liquid_cash=0",
                 "finance.emergency_fund=0", "finance.near_term_obligations=0")
        defer = {
            "product": {"name": "人体工学椅", "price": 1400, "category": "家具/椅子"},
            "reasons": [
                {"code": "NEED_REAL", "note": "每天坐八小时，腰疼两个月"},
                {"code": "PRICE_FAIR", "note": "两家自营同价"},
                {"code": "DURABILITY_GAIN", "note": "预期使用五年以上"},
            ],
            "evidence": [{"kind": "price", "claim": "1400", "value": 1400,
                          "source_type": "official", "url": "https://example.com/chair",
                          "observed_at": TODAY}],
            "recheck_condition": "确认每月储蓄目标与投资额后重新判断",
        }
        defer_path = ctx.write("defer.json", defer)
        deferred = ctx.json("judge", "--input", defer_path)
        ctx.check("需要跨月支出时降为暂缓，而不是只拦账本",
                  deferred["result"]["verdict"]["verdict"] == "DEFER", deferred["result"]["verdict"])
        ctx.check("暂缓带复查条件即可落账", deferred["logged"] is True, deferred)
        no_recheck = dict(defer)
        no_recheck.pop("recheck_condition")
        ctx.run("judge", "--input", ctx.write("defer2.json", no_recheck), expect=2)
        ctx.check("暂缓没写复查条件时拒绝落账", True)

        bad = dict(GOOD)
        bad["reasons"] = [{"code": "IMPULSE_SIGNAL", "note": "看到打折才想买"}]
        bad_path = ctx.write("bad.json", bad)
        ctx.run("judge", "--input", bad_path, expect=2)
        ctx.check("审计不通过时拒绝落账", True)

        ctx.json("profile", "confirm", "--key", "finance.monthly_income")
        ctx.check("可以确认旧信息仍然有效", True)

        # 命令行也能走到引擎：脏汇率必须在换算之前被挡掉，
        # 而不是换出一个荒谬数字再靠预算门"碰巧"拦住。
        dirty_fx = {
            "product": {"name": "某海外耳机", "price": 100, "currency": "USD",
                        "category": "数码/耳机"},
            "reasons": [
                {"code": "NEED_REAL", "note": "旧耳机降噪失效已三个月"},
                {"code": "PRICE_FAIR", "note": "官网就是这个价"},
                {"code": "LOW_REGRET_RISK", "note": "支持七天无理由退货"},
            ],
            "evidence": [
                {"kind": "price", "claim": "100 USD", "value": 100, "currency": "USD",
                 "source_type": "official", "url": "https://example.com/usd",
                 "observed_at": TODAY},
                {"kind": "fx", "claim": "USD/CNY 100000", "value": 100000,
                 "base": "USD", "quote": "CNY", "source_type": "official",
                 "url": "https://example.com/fx", "observed_at": TODAY},
            ],
            "recheck_condition": "查到数量级合理的可核验汇率后重新判断",
        }
        fx_res = ctx.json("judge", "--input", ctx.write("dirty-fx.json", dirty_fx))
        fx_codes = {f["code"] for f in
                    fx_res["result"]["gates"]["integrity"]["findings"]}
        ctx.check("极端汇率在命令行路径同样不进计算",
                  fx_res["result"]["verdict"]["verdict"] == "DEFER"
                  and "EV_FX_UNTRUSTED" in fx_codes
                  and "EV_CURRENCY_UNCONVERTED" in fx_codes,
                  {"verdict": fx_res["result"]["verdict"]["verdict"], "codes": sorted(fx_codes)})
        ctx.check("极端汇率没有被乘进价格",
                  fx_res["result"]["product"].get("price") is None,
                  fx_res["result"]["product"])

    finally:
        ctx.cleanup()

    bad_checks = [c for c in ctx.checks if not c["ok"]]
    for c in ctx.checks:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['name']}")
        if not c["ok"]:
            print(f"        {c['detail']}")
    print("-" * 56)
    print(f"端到端 {len(ctx.checks) - len(bad_checks)}/{len(ctx.checks)} 通过")
    return 0 if not bad_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
