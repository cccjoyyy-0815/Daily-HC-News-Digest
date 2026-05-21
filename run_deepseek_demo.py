#!/usr/bin/env python3
"""
本地快速验证：读取 .env 中的 DEEPSEEK_API_KEY，对示例新闻调用筛选接口。

用法：
  python3 run_deepseek_demo.py
  python3 run_deepseek_demo.py path/to/news.json   # JSON 数组，元素含 title/summary/url 等
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from deepseek_news_filter import RawNewsItem, filter_news_with_deepseek

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

SAMPLE_NEWS: list[RawNewsItem] = [
    {
        "title": "国家药监局发布药品网络销售监督管理办法修订征求意见稿",
        "summary": "聚焦处方药网售、第三方平台责任与数据留存，强调全流程可追溯。",
        "url": "https://example.com/news/1",
        "pub_date": "2026-05-01",
        "source": "示例",
    },
    {
        "title": "某药企宣布 AI 辅助化合物筛选平台上线",
        "summary": "宣称可缩短早期研发周期，已与两家 CRO 达成合作试点。",
        "url": "https://example.com/news/2",
        "pub_date": "2026-05-02",
        "source": "示例",
    },
    {
        "title": "本地足球队周末联赛战报",
        "summary": "与医药数字化无关，用于测试过滤。",
        "url": "https://example.com/sports/1",
        "pub_date": "2026-05-03",
        "source": "示例",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 新闻筛选演示")
    parser.add_argument(
        "json_path",
        nargs="?",
        help="可选：新闻 JSON 文件路径（顶层为数组）",
    )
    args = parser.parse_args()

    if args.json_path:
        path = Path(args.json_path)
        if not path.is_file():
            print(f"文件不存在: {path}", file=sys.stderr)
            return 1
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            print("JSON 顶层必须是数组", file=sys.stderr)
            return 1
        news_list: list[RawNewsItem] = raw  # type: ignore[assignment]
    else:
        news_list = SAMPLE_NEWS

    try:
        result = filter_news_with_deepseek(news_list)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
