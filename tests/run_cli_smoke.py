#!/usr/bin/env python3
"""端到端冒烟：用临时数据目录跑一遍完整生命周期。

验证的是"能不能真的用起来"，而不是函数返回值：
    init → 设预算 → 登记已有物品 → 校验草稿 → 落账 → 标记已购 → 复盘 → 统计 → 导出
"""

from __future__ import annotations

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


class Ctx:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mbm-smoke-"))
        self.home = self.tmp / "data"
        self.env = dict(os.environ, MAIBUMAI_HOME=str(self.home))
        self.checks = []

    def run(self, *args, expect=0, stdin=None):
        proc = subprocess.run(
            [PY, str(CLI), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            input=stdin,
        )
        if proc.returncode != expect:
            raise AssertionError(
                f"`mbm {' '.join(args)}` 退出码 {proc.returncode}（期望 {expect}）\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
        return proc.stdout

    def json(self, *args, **kw):
        return json.loads(self.run(*args, **kw))

    def check(self, name, cond, detail=""):
        self.checks.append({"name": name, "ok": bool(cond), "detail": "" if cond else detail})

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


DRAFT = {
    "product": {"name": "索尼 WH-1000XM5", "price": 1999, "category": "数码/耳机"},
    "reasons": [
        {"code": "NEED_REAL", "note": "每天地铁通勤 2 小时"},
        {"code": "PRICE_FAIR", "note": "三家自营 1899-2099"},
        {"code": "AFFORDABLE_WITHIN_BUDGET", "note": "占月可支配 67%"},
    ],
    "evidence": [
        {
            "kind": "price",
            "claim": "自营 1999",
            "value": 1999,
            "source_type": "authorized_retail",
            "url": "https://example.com/xm5",
            "observed_at": "2026-10-28",
        }
    ],
    "prediction": {"uses_per_month": 20, "months": 36},
    "summary": "通勤刚需，价格在正常区间。",
}


def main() -> int:
    ctx = Ctx()
    try:
        # 1. 初始化
        out = ctx.json("init")
        ctx.check("init 创建数据目录", Path(out["home"]).is_dir(), out)

        # 2. 画像
        prof = ctx.json("profile", "set", "budget.discretionary_monthly=3000",
                        "budget.single_purchase_threshold=800")
        ctx.check("预算写入", prof["budget"]["discretionary_monthly"] == 3000, prof)

        # 3. 已有物品
        ctx.json("owned", "add", "--name", "旧耳机", "--category", "数码/耳机",
                 "--condition", "broken", "--price", "299")
        found = ctx.json("owned", "find", "--query", "耳机")
        ctx.check("已有物品可检索", len(found) == 1 and found[0]["condition"] == "broken", found)

        # 4. 校验草稿
        draft_path = ctx.tmp / "draft.json"
        draft_path.write_text(json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
        chk = ctx.json("check", "--input", str(draft_path))
        ctx.check("标准耳机决策给出建议购买", chk["verdict"]["verdict"] == "BUY", chk["verdict"])
        ctx.check("草稿通过证据与审计", chk["ok"], chk["audit"]["findings"])
        ctx.check("单次成本可推导", chk["numbers"]["cost_per_use"] == round(1999 / (20 * 36), 2),
                  chk["numbers"])

        # 5. 落账
        logged = ctx.json("log", "--input", str(draft_path))
        did = logged["id"]
        ctx.check("账本写入成功", bool(did), logged)

        # 6. 标记已购
        ctx.json("outcome", did, "--bought", "yes")

        # 7. 复盘：实际几乎没用
        review_path = ctx.tmp / "review.json"
        review_path.write_text(
            json.dumps(
                {"actual": {"uses_per_month": 0.5, "satisfaction": 2,
                            "would_buy_again": False, "idle_reason": "地铁戴久了闷"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        ctx.json("review", did, "--input", str(review_path))

        # 8. 统计
        st = ctx.json("stats")
        ctx.check("统计覆盖 1 条复盘", st["reviewed"] == 1, st)
        ctx.check("识别为闲置", st["idle_rate"] == 1.0, st)
        ctx.check("追踪后悔支出", st["regret_spend"] == 1999, st)
        ctx.check("使用频率高估倍数被量化", st["usage_overestimate_median"] == 40.0, st)

        # 9. 待复盘列表应清空
        due = ctx.json("due", "--days", "0")
        ctx.check("已复盘的不再提醒", due == [], due)

        # 10. 导出
        md = ctx.tmp / "export.md"
        ctx.json("export", "--out", str(md))
        text = md.read_text(encoding="utf-8")
        ctx.check("导出含该笔记录", "WH-1000XM5" in text and "建议购买" in text, text[:200])

        # 11. 隐私自检
        doc = ctx.json("doctor")
        ctx.check("数据目录不在 Git 仓库内", not doc["git_roots"], doc)

        # 12. 阻断写入
        bad = dict(DRAFT)
        bad["reasons"] = [{"code": "IMPULSE_SIGNAL"}, {"code": "INFO_GAP"}]
        bad_path = ctx.tmp / "bad.json"
        bad_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        ctx.run("log", "--input", str(bad_path), expect=2)
        ctx.check("审计不通过时拒绝落账", True)

    finally:
        ctx.cleanup()

    bad = [c for c in ctx.checks if not c["ok"]]
    for c in ctx.checks:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['name']}")
        if not c["ok"]:
            print(f"        {c['detail']}")
    print("-" * 56)
    print(f"端到端 {len(ctx.checks) - len(bad)}/{len(ctx.checks)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
