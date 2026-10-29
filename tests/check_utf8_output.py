#!/usr/bin/env python3
"""检查一份重定向输出是不是合法 UTF-8 中文。

Windows 控制台默认 GBK / cp1252，程序如果把中文按系统代码页写出去，
文件读回来就是乱码；如果编码不支持，直接抛 UnicodeEncodeError。
两种都不能接受，所以这条要单独验。

用法：
    python tests/check_utf8_output.py brief.txt
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 必须出现的中文片段：分别覆盖画像、财务、结论三类输出。
EXPECTED = ("财务画像", "月结余", "可自由消费资金")


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("[FAIL] 需要传入一个输出文件路径")
        return 1
    path = Path(argv[0])
    if not path.is_file():
        print(f"[FAIL] 文件不存在：{path}")
        return 1

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"[FAIL] 不是 UTF-8（{exc}）：前 80 字节 {raw[:80]!r}")
        return 1

    if "\ufffd" in text:
        print("[FAIL] 出现替换字符，说明写入时已经丢字")
        return 1

    missing = [w for w in EXPECTED if w not in text]
    if missing:
        print(f"[FAIL] 缺少中文片段 {missing}；实际前 200 字符：{text[:200]!r}")
        return 1

    print(f"[PASS] 重定向输出是 UTF-8 中文，{len(text)} 字符，含 {list(EXPECTED)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
