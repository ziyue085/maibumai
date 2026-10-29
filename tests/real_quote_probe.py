"""真实网页报价探针：抓一次公开页面，把价格原样记下来。

存在的意义只有一个——把「你以为查到了价」变成「有据可查的一次抓取」。

它会做三件事：

1. 抓页面（urllib，无第三方依赖）；
2. 从 HTML 文本里抽出所有 ``$ 价格``，判断你要引用的价格是否真在页面上；
3. 写一份 record（JSON）：URL、抓取时间、HTTP 状态、正文 sha256、页面上出现的价格集合。

**它不判断来源是否权威，也不判断页面有没有被 A/B 测试成别的价。**
它能证明的只是「这个 URL 在某时刻返回的正文里，确实出现过这个数字」。

用法::

    python tests/real_quote_probe.py --url https://... --expect 6.95 \\
        --out tests/fixtures/real-quote-record.json

抓取失败时退出码为 3，不会静默产出一份空记录。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request

#: 抓取时带上的 UA。有些站点会拒绝默认的 Python-urllib。
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) maibumai-quote-probe/1.0"

#: 页面上"符号 + 数字"的价格。只认美元/人民币/欧元/英镑四种符号。
PRICE_RE = re.compile(r"(?P<sym>US\$|\$|¥|CN¥|€|£)\s?(?P<num>[0-9][0-9,]*(?:\.[0-9]{1,2})?)")

SYMBOL_CCY = {"$": "USD", "US$": "USD", "¥": "CNY", "CN¥": "CNY", "€": "EUR", "£": "GBP"}


def fetch(url: str, timeout: int = 25) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.status, resp.read()


def extract_prices(html: str) -> list:
    """把页面上的价格抽成 ``[{amount, currency}]``，去重后按金额升序。"""
    seen, out = set(), []
    for m in PRICE_RE.finditer(html):
        try:
            amount = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        ccy = SYMBOL_CCY.get(m.group("sym"), "")
        key = (ccy, round(amount, 2))
        if key in seen:
            continue
        seen.add(key)
        out.append({"amount": round(amount, 2), "currency": ccy})
    return sorted(out, key=lambda x: (x["currency"], x["amount"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="抓一次真实报价并记录")
    ap.add_argument("--url", required=True)
    ap.add_argument("--expect", type=float, required=True, help="你打算引用的价格")
    ap.add_argument("--currency", default="USD")
    ap.add_argument("--out", help="记录写到哪个 json；不填则只打印")
    ap.add_argument("--timeout", type=int, default=25)
    args = ap.parse_args(argv)

    try:
        status, body = fetch(args.url, args.timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(json.dumps({"ok": False, "reason": "FETCH_FAILED", "error": str(exc)},
                         ensure_ascii=False, indent=2))
        return 3

    text = body.decode("utf-8", errors="replace")
    prices = extract_prices(text)
    found = [p for p in prices
             if p["currency"] == args.currency and abs(p["amount"] - args.expect) < 0.005]

    record = {
        "_howto": [
            "本文件由 tests/real_quote_probe.py 生成，是一份**抓取留痕**。",
            "它证明：这个 URL 在 fetched_at 返回的正文里出现过 claim_price 这个数字。",
            "它**不**证明该域名是官方渠道，也不证明现在还是这个价。",
            "重新生成：python tests/real_quote_probe.py --url ... --expect ... --out 本文件",
        ],
        "url": args.url,
        "fetched_at": dt.date.today().isoformat(),
        "http_status": status,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "claim_price": args.expect,
        "currency": args.currency,
        "matched": bool(found),
        "page_prices": prices[:60],
        "page_price_count": len(prices),
    }

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    print(json.dumps({k: record[k] for k in
                      ("url", "http_status", "body_bytes", "claim_price",
                       "currency", "matched", "page_price_count")},
                     ensure_ascii=False, indent=2))
    return 0 if record["matched"] else 4


if __name__ == "__main__":
    sys.exit(main())
