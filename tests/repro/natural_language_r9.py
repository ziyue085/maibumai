# -*- coding: utf-8 -*-
"""第九轮：三个自然语言场景的真实端到端调用（走 CLI 真实路径）"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MBM = os.path.join(ROOT, "scripts", "mbm.py")
HOME = os.path.join(tempfile.gettempdir(), "mbm_r9_home")
os.makedirs(HOME, exist_ok=True)
ENV = dict(os.environ)
ENV["MAIBUMAI_HOME"] = HOME
ENV["PYTHONUTF8"] = "1"
ENV["PYTHONIOENCODING"] = "utf-8"

SCENARIOS = [
    {
        "id": "场景一",
        "utterance": "我看中一个耳机，官方店 899，我月入 2600、固定开销 1100，通勤每天两小时，旧的降噪坏了，值不值得买？",
        "draft": {
            "product": {"name": "某品牌降噪耳机", "price": 899, "category": "数码/耳机"},
            "reasons": [
                {"code": "NEED_REAL", "note": "每天通勤两小时，旧耳机降噪已坏"},
                {"code": "PRICE_FAIR", "note": "官方店与自营同价"},
                {"code": "LOW_REGRET_RISK", "note": "支持七天无理由退货"},
            ],
            "evidence": [
                {"kind": "price", "claim": "899", "value": 899, "source_type": "official",
                 "url": "https://example.com/headphone", "observed_at": "2026-10-29",
                 "page_text": "某品牌降噪耳机 官方旗舰店 售价 899 元 含税 顺丰包邮",
                 "page_price": 899},
            ],
            "recheck_condition": "核对到手价后重新判断",
        },
    },
    {
        "id": "场景二",
        "utterance": "那个手机 128G 促销 899，256G 标价 1099，我说的是 899，能买吗？",
        "draft": {
            "product": {"name": "某品牌手机", "price": 899, "category": "数码/手机"},
            "reasons": [
                {"code": "NEED_REAL", "note": "旧手机电池不行了"},
                {"code": "PRICE_FAIR", "note": "128G 在促销"},
                {"code": "LOW_REGRET_RISK", "note": "支持七天无理由退货"},
            ],
            "evidence": [
                {"kind": "price", "claim": "899", "value": 899, "source_type": "official",
                 "url": "https://example.com/a", "observed_at": "2026-10-29",
                 "price_basis": "promo", "variant": "128GB"},
                {"kind": "price", "claim": "1099", "value": 1099, "source_type": "official",
                 "url": "https://example.com/b", "observed_at": "2026-10-29",
                 "price_basis": "msrp", "variant": "256GB"},
            ],
            "recheck_condition": "确认要买的容量后重新判断",
        },
    },
    {
        "id": "场景三",
        "utterance": "这个 1299 太贵了，上一代是不是更划算？我打听到上一代 899，可另一家官方店写 1299。",
        "draft": {
            "product": {"name": "某品牌耳机（新款）", "price": 1299, "category": "数码/耳机"},
            "reasons": [
                {"code": "NEED_REAL", "note": "通勤用，旧的坏了"},
                {"code": "SUBSTITUTE_ADEQUATE", "note": "上代同功能够用"},
                {"code": "PRICE_ABOVE_FAIR", "note": "比上代贵太多"},
            ],
            "evidence": [
                {"kind": "price", "claim": "1299", "value": 1299, "source_type": "official",
                 "url": "https://example.com/new", "observed_at": "2026-10-29"},
                {"kind": "price", "claim": "899", "value": 899, "source_type": "official",
                 "url": "https://example.com/old1", "observed_at": "2026-10-29", "subject": "alternative"},
                {"kind": "price", "claim": "1299", "value": 1299, "source_type": "official",
                 "url": "https://example.com/old2", "observed_at": "2026-10-29", "subject": "alternative"},
            ],
            "alternative": {"name": "上代同款", "price": 899, "source": "某官方店 2026-10-29"},
            "recheck_condition": "核对上代到手价后重新判断",
        },
    },
]


def run(args, inp=None):
    p = subprocess.run([sys.executable, MBM] + args, capture_output=True, env=ENV,
                       cwd=ROOT, input=(inp.encode("utf-8") if inp else None))
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def main():
    out = []
    rc, so, se = run(["init"])
    out.append(f"init rc={rc}")
    rc, so, se = run(["profile", "set",
                      "finance.monthly_income=2600", "finance.monthly_fixed_expense=1100",
                      "finance.savings_target_monthly=0", "invest.monthly_contribution=0",
                      "finance.liquid_cash=30000", "finance.emergency_fund=6000",
                      "finance.near_term_obligations=8000"])
    out.append(f"profile rc={rc}")

    for sc in SCENARIOS:
        out.append("=" * 60)
        sid = sc["id"]
        utt = sc["utterance"]
        out.append("[" + sid + "] 用户说：" + utt)
        path = os.path.join(tempfile.gettempdir(), f"mbm_r9_{sc['id']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sc["draft"], f, ensure_ascii=False)
        rc, so, se = run(["judge", "--input", path])
        try:
            d = json.loads(so)
        except Exception:
            out.append(f"  rc={rc} 非 JSON 输出：{so[:500]} {se[:500]}")
            continue
        dec = d.get("decision", {})
        out.append(f"  结论：{dec.get('verdict')}  ok={dec.get('ok')}")
        out.append(f"  强制理由：{dec.get('enforced')}")
        out.append(f"  发现项：{d.get('integrity', {}).get('findings') or d.get('findings')}")
        alt = dec.get("alternative") or {}
        if alt:
            out.append(f"  替代品：{alt.get('name')} 资格={alt.get('qualified')} "
                       f"问题={alt.get('problems')} 核验={alt.get('price_verified')}")
        rc2, so2, _ = run(["judge", "--input", path, "--human"])
        out.append("  --human 摘要：")
        for line in so2.strip().splitlines():
            out.append("    " + line)

    txt = "\n".join(out)
    dst = os.path.join(ROOT, "tests", "repro", "round9-natural-language.txt")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)


if __name__ == "__main__":
    main()
