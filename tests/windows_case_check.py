#!/usr/bin/env python3
"""Windows 实际使用环境的关键路径检查。

覆盖三件在 Linux 上看不出来的事：
1. 中文输出在非 UTF-8 控制台代码页下不崩（stdout 重定向到文件也是 UTF-8）；
2. 数据目录路径带中文（Windows 用户名常常是中文）时读写正常；
3. 判定、落账、账本归一化在 Windows 路径下行为与 Linux 一致。

两平台都能跑，只是 Windows 才有意义；Linux 上也作为一次额外冒烟。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "mbm.py"

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append({"name": name, "ok": bool(cond), "detail": "" if cond else str(detail)[:600]})


def run(env, *args, expect=0, encoding="utf-8"):
    proc = subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, env=env,
    )
    out = proc.stdout.decode(encoding, errors="replace")
    err = proc.stderr.decode(encoding, errors="replace")
    if proc.returncode != expect:
        raise AssertionError(
            f"`mbm {' '.join(args)}` 退出码 {proc.returncode}（期望 {expect}）\n"
            f"stdout={out[:1200]}\nstderr={err[:800]}"
        )
    return out


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="mbm-win-"))
    # 中文目录名：Windows 用户目录经常是中文，路径处理必须扛得住
    home = base / "中文 数据目录"
    env = dict(os.environ, MAIBUMAI_HOME=str(home))
    env.pop("PYTHONIOENCODING", None)   # 不给外部帮忙，测的是程序自己的输出处理
    env.pop("PYTHONUTF8", None)

    try:
        run(env, "init")
        check("中文路径下能建数据目录", (home / "profile.json").is_file(), home)

        run(env, "profile", "set",
            "finance.monthly_income=2600", "finance.monthly_fixed_expense=1100",
            "finance.savings_target_monthly=0", "invest.monthly_contribution=0")
        brief = run(env, "brief", "--human")
        check("brief 中文可读", "财务画像" in brief and "月结余" in brief, brief[:400])
        check("brief 不带替换符", "\ufffd" not in brief, brief[:400])

        draft = {
            "product": {"name": "某品牌耳机", "price": 899, "category": "数码/耳机"},
            "reasons": [
                {"code": "NEED_REAL", "note": "通勤两小时，旧耳机降噪坏了",
                 "enforced": True},
                {"code": "PRICE_FAIR", "note": "三家自营 869-929"},
                {"code": "DURABILITY_GAIN", "note": "预期使用三年以上"},
            ],
            "evidence": [{"kind": "price", "claim": "自营 899", "value": 899,
                          "source_type": "authorized_retail",
                          "url": "https://example.com/x", "observed_at": "2026-10-29"}],
            "prediction": {"uses_per_month": 20, "months": 36},
        }
        draft_path = base / "草稿.json"
        draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

        out = run(env, "check", "--input", str(draft_path), "--human")
        check("中文文件名能读", "结论：建议购买" in out, out[:400])
        check("可落账提示可见", "可落账：是" in out, out[:400])

        judged = run(env, "judge", "--input", str(draft_path))
        payload = json.loads(judged)
        check("judge 落账成功", payload.get("logged") is True, judged[:400])

        ledger = (home / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        rec = json.loads(ledger[0])
        forged = [r["code"] for r in rec["reasons"]
                  if r.get("enforced") and r["code"] not in (rec["enforced"] or [])]
        check("账本里没有伪造 enforced", not forged, rec["reasons"])
        check("账本理由是判定阶段归一后的", [r["code"] for r in rec["reasons"]][:2]
              == ["NEED_REAL", "PRICE_FAIR"], rec["reasons"])

        # 输出重定向到文件：非 UTF-8 代码页下写出来的必须仍是 UTF-8
        out_file = base / "brief.out"
        with open(out_file, "wb") as fh:
            subprocess.run([sys.executable, str(CLI), "brief", "--human"],
                           stdout=fh, env=env, check=True)
        text = out_file.read_text(encoding="utf-8")
        check("重定向到文件仍是 UTF-8 中文", "财务画像" in text, text[:300])

        doc = json.loads(run(env, "doctor"))
        check("doctor 通过", all(c["ok"] for c in doc["checks"]),
              [c for c in doc["checks"] if not c["ok"]])
        check("数据目录不在 Git 仓库内", not doc["git_roots"], doc["git_roots"])

    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)

    bad = [c for c in CHECKS if not c["ok"]]
    for c in CHECKS:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['name']}")
        if not c["ok"]:
            print(f"        {c['detail']}")
    print("-" * 56)
    print(f"Windows 关键路径 {len(CHECKS) - len(bad)}/{len(CHECKS)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
