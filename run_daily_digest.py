#!/usr/bin/env python3
"""
每日一次：上游 `deepseek_news_filter` 抓取候选（RSS **或** Tavily/SerpAPI 搜索）
→ DeepSeek 分层 → 下游 `digest_email` 发 HTML 邮件。

可选：另设 `NEWS_INTL_SEARCH_QUERY`（或 `--intl-search-query`）抓取**美国/欧洲/亚太**新闻，
经单独模型流程后，在邮件末尾追加「海外要闻（仅高优先级）」分区。

用法：
  python3 run_daily_digest.py
  python3 run_daily_digest.py --dry-run
  python3 run_daily_digest.py --search-query "中国 医药 人工智能 数字化"
  python3 run_daily_digest.py --intl-search-query "pharma biotech AI digital health FDA EMA"
  python3 run_daily_digest.py --feeds my.txt

搜索模式需在 .env 配置 NEWS_SEARCH_PROVIDER、tavily 或 serpapi 的 API Key（见 .env.example）。
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from deepseek_news_filter import (
    TieredNewsResult,
    filter_intl_high_news_with_deepseek,
    filter_news_with_deepseek,
    ingest_from_search_query,
    ingest_intl_from_search_query,
    ingest_rss_from_feeds_file,
)
from digest_email import send_digest_email, tiered_result_to_html, tiered_result_to_plain

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

    parser = argparse.ArgumentParser(description="每日医药 AI 数字化邮件摘要（RSS 或搜索 API）")
    parser.add_argument(
        "--feeds",
        type=Path,
        default=Path(os.getenv("FEEDS_FILE", "feeds.txt")),
        help="RSS URL 列表文件（未使用 --search-query 时生效）",
    )
    parser.add_argument(
        "--search-query",
        metavar="Q",
        default=None,
        help="若指定则使用 Tavily/SerpAPI 搜索替代 RSS；也可设环境变量 NEWS_SEARCH_QUERY",
    )
    parser.add_argument(
        "--intl-search-query",
        metavar="Q",
        default=None,
        help="可选：海外独立搜索词；也可设环境变量 NEWS_INTL_SEARCH_QUERY（见 .env.example）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="抓取并调用模型，但不发邮件；将 HTML 输出到 stdout",
    )
    parser.add_argument(
        "--skip-model",
        action="store_true",
        help="仅抓取候选，不调用 DeepSeek",
    )
    args = parser.parse_args()

    search_q = (args.search_query or os.getenv("NEWS_SEARCH_QUERY", "") or "").strip()
    intl_q = (args.intl_search_query or os.getenv("NEWS_INTL_SEARCH_QUERY", "") or "").strip()

    max_per = int(os.getenv("MAX_ITEMS_PER_FEED", "40"))
    max_total = int(os.getenv("MAX_TOTAL_ITEMS_BEFORE_MODEL", "80"))

    raw: list = []
    try:
        if search_q:
            logger.info("使用搜索 API，query=%r", search_q)
            raw = ingest_from_search_query(search_q)
        else:
            raw = ingest_rss_from_feeds_file(
                args.feeds,
                max_per_feed=max_per,
                max_total_before_model=max_total,
            )
    except FileNotFoundError as e:
        logger.error("%s", e)
        logger.error("请复制 feeds.example.txt 为 feeds.txt 并填入 RSS 地址，或使用 --search-query。")
        return 1
    except ValueError as e:
        logger.error("%s", e)
        return 1
    except RuntimeError as e:
        logger.error("%s", e)
        return 1

    intl_raw: list = []
    if intl_q:
        try:
            logger.info("国际搜索 API，query=%r", intl_q)
            intl_raw = ingest_intl_from_search_query(intl_q)
            logger.info("国际搜索返回 %s 条候选", len(intl_raw))
        except Exception as e:
            logger.warning("国际搜索失败，已跳过海外分区: %s", e)

    if not raw and not intl_raw:
        logger.warning("未获得任何国内或国际候选条目，发送占位邮件或仅 dry-run 输出提示。")
        today = date.today().isoformat()
        headline = f"医药 AI 数字化日报 · {today}"
        empty: TieredNewsResult = {"high": [], "medium": [], "low": []}
        html_body = tiered_result_to_html(empty, headline=headline, international_high=None)
        text_body = tiered_result_to_plain(empty, headline=headline, international_high=None)
        if args.dry_run:
            print(html_body)
            return 0
        send_digest_email(subject=headline, html_body=html_body, text_body=text_body)
        return 0

    if args.skip_model:
        logger.info("已跳过模型：国内候选 %s 条，国际候选 %s 条。", len(raw), len(intl_raw))
        return 0

    empty_tiered: TieredNewsResult = {"high": [], "medium": [], "low": []}
    if raw:
        logger.info("调用 DeepSeek 筛选国内，共 %s 条…", len(raw))
        tiered = filter_news_with_deepseek(raw)
    else:
        logger.info("国内无候选，跳过国内模型。")
        tiered = empty_tiered

    intl_items: list = []
    if intl_raw:
        logger.info("调用 DeepSeek 筛选海外（仅高优先级），共 %s 条…", len(intl_raw))
        intl_items = filter_intl_high_news_with_deepseek(intl_raw)

    today = date.today().isoformat()
    headline = f"医药 AI 数字化日报 · {today}"
    subject = headline
    html_body = tiered_result_to_html(
        tiered,
        headline=headline,
        international_high=intl_items if intl_items else None,
    )
    text_body = tiered_result_to_plain(
        tiered,
        headline=headline,
        international_high=intl_items if intl_items else None,
    )

    if args.dry_run:
        print(html_body)
        return 0

    send_digest_email(subject=subject, html_body=html_body, text_body=text_body)
    logger.info("完成。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as e:
        logger.error("%s", e)
        raise SystemExit(1)
