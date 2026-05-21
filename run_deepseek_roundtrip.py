#!/usr/bin/env python3
"""
每次运行：自动构造（或可指定）一段 user 文案 → 调用 DeepSeek → 解析返回 JSON 并打印。

说明：这与「新闻 RSS 筛选」是同一套 API；本脚本演示「发文字 → 收文字 → parse JSON」的通用闭环。

用法：
  python3 run_deepseek_roundtrip.py
  python3 run_deepseek_roundtrip.py -u "用一句话介绍你自己" -s "只输出 JSON：{\"reply\": \"...\"}"
  python3 run_deepseek_roundtrip.py --user-file my_prompt.txt
  python3 run_deepseek_roundtrip.py --raw -u "写两句中文，不要 JSON"
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
from datetime import datetime
from pathlib import Path

from deepseek_news_filter import deepseek_chat_json, deepseek_chat_text

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEFAULT_SYSTEM = """你是一个只输出合法 JSON 的 API。不要 Markdown 代码块，不要解释文字。
用户会给出一段「运行上下文」。你必须只输出一个 JSON 对象，且仅包含这些键：
- schema_version: 整数，固定为 1
- ok: 布尔，固定为 true
- message: 一句简短中文，确认你已处理本次请求
- context_echo: 字符串，必须等于用户正文里以「本地时间:」开头的那一整行原文（便于核对管道）"""


def default_user_message() -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return (
        "运行上下文（本脚本每次 run 自动生成）：\n"
        f"本地时间: {now}\n"
        f"Python 版本: {platform.python_version()}\n"
        "请严格按系统说明只输出 JSON。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek：发 system+user → 解析 JSON（或只打印原文）")
    parser.add_argument("-s", "--system", help="system 提示词；默认使用内置 JSON 自检模板")
    parser.add_argument("-u", "--user", help="user 正文；默认每次自动生成带时间戳的上下文")
    parser.add_argument("--system-file", type=Path, help="从文件读取 system 提示词")
    parser.add_argument("--user-file", type=Path, help="从文件读取 user 正文（UTF-8）")
    parser.add_argument("--raw", action="store_true", help="不强制 JSON：只打印模型原始文本")
    parser.add_argument("--model", default=None, help="覆盖默认模型 deepseek-chat")
    args = parser.parse_args()

    if args.system_file:
        system = args.system_file.read_text(encoding="utf-8").strip()
    else:
        system = (args.system or "").strip() or DEFAULT_SYSTEM

    if args.user_file:
        user = args.user_file.read_text(encoding="utf-8").strip()
    else:
        user = (args.user or "").strip() or default_user_message()

    kwargs: dict = {}
    if args.model:
        kwargs["model"] = args.model

    if args.raw:
        text = deepseek_chat_text(system_prompt=system, user_message=user, **kwargs)
        print(text)
        return 0

    try:
        data = deepseek_chat_json(system_prompt=system, user_message=user, **kwargs)
    except ValueError as e:
        # 若模型未按 JSON 输出，可改用 --raw 查看原文
        logging.error("%s", e)
        logging.error("可尝试加 --raw 查看模型原文，或收紧 system 要求「只输出 JSON」。")
        return 1

    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
