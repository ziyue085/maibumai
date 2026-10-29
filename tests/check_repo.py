#!/usr/bin/env python3
"""仓库自检：不依赖运行时环境，CI 与本地都能跑。

检查四件事：
  1. 仓库里没有个人数据文件（画像、账本、沙盒目录）
  2. 所有 JSON 能被解析
  3. 所有 Python 文件能编译
  4. 版本号在各处一致
"""

from __future__ import annotations

import json
import py_compile
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PERSONAL_PATTERNS = re.compile(
    r"(^|/)(profile|owned)\.json$|decisions\.jsonl|^\.sim/|^\.repro|/\.maibumai/"
)

#: 真实个人数据的指纹。拼起来写，避免本文件自己被扫到。
#: 这些字符串一旦出现在仓库里，就说明有真实数据漏进去了。
PRIVATE_SIGNATURES = [
    "500" + "74",          # 净资产快照
    "38" + "735",          # 现金快照
    "10" + "384",          # 投资快照
    "171" + "67",          # 本机用户名/路径
    "山东" + "理工",
    "子" + "曰",
    "Pura " + "70",
    "Pixel " + "6",
    "联想" + "小新",
]


def tracked_files() -> list:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        return [line for line in out.splitlines() if line.strip()]
    except (OSError, subprocess.CalledProcessError):
        # 没有 git 时退化为遍历（跳过点目录）
        return [
            str(p.relative_to(ROOT)).replace("\\", "/")
            for p in ROOT.rglob("*")
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(ROOT).parts)
        ]


def check_personal_data(files) -> list:
    return [f for f in files if PERSONAL_PATTERNS.search(f)]


def check_json(files) -> list:
    bad = []
    for name in files:
        if not name.endswith(".json"):
            continue
        try:
            json.loads((ROOT / name).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name}: {exc}")
    return bad


def check_python(files) -> list:
    bad = []
    for name in files:
        if not name.endswith(".py"):
            continue
        try:
            py_compile.compile(str(ROOT / name), doraise=True)
        except py_compile.PyCompileError as exc:
            bad.append(f"{name}: {exc.msg}")
    return bad


def check_privacy_signatures(files) -> list:
    """扫真实个人数据的指纹，不只看文件名。

    文件名合规不代表内容合规：真实金额、真实设备型号、本机用户名
    都可能被写进文档或测试样例。
    """
    hits = []
    for name in files:
        path = ROOT / name
        if path.suffix.lower() in (".png", ".jpg", ".zip", ".pdf"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for sig in PRIVATE_SIGNATURES:
            if sig in text:
                hits.append(f"{name} 含 {sig!r}")
    return hits


def check_version() -> list:
    problems = []
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    m = re.search(r"^version:\s*(\S+)$", skill, re.M)
    if not m:
        return ["SKILL.md 缺少 version 字段"]
    version = m.group(1)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## v{version}" not in changelog:
        problems.append(f"CHANGELOG.md 没有 v{version} 的条目")
    return problems


def main() -> int:
    files = tracked_files()
    problems = []

    leaked = check_personal_data(files)
    if leaked:
        problems.append("仓库里出现疑似个人数据文件：" + "、".join(leaked))

    signatures = check_privacy_signatures(files)
    if signatures:
        problems.append("仓库内容含真实个人数据指纹：" + "；".join(signatures))

    bad_json = check_json(files)
    if bad_json:
        problems.append("JSON 解析失败：" + "；".join(bad_json))

    bad_py = check_python(files)
    if bad_py:
        problems.append("Python 编译失败：" + "；".join(bad_py))

    problems += check_version()

    if problems:
        for line in problems:
            print(f"[FAIL] {line}")
        return 1
    print(f"[PASS] 仓库自检通过：{len(files)} 个文件，无个人数据，JSON/Python 与版本号一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
