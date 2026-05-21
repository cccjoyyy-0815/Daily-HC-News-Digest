"""
从 RSS/Atom 源拉取条目，映射为 RawNewsItem（供 DeepSeek 筛选使用）。
"""

from __future__ import annotations

import html as html_lib
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests

from deepseek_news_filter import RawNewsItem

logger = logging.getLogger(__name__)

_DEFAULT_UA = "deepseek_news/1.0 (RSS digest bot; +https://github.com/)"

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """粗略去掉 HTML 标签并做实体反转义。"""
    t = html_lib.unescape(_TAG_RE.sub(" ", text or ""))
    return re.sub(r"\s+", " ", t).strip()


def _entry_link(entry: Any) -> str:
    """从 feedparser entry 取正文链接（部分源只把链接放在 links 里）。"""
    link = (entry.get("link") or "").strip()
    if link:
        return link
    for lnk in entry.get("links") or []:
        if not isinstance(lnk, dict):
            continue
        href = (lnk.get("href") or "").strip()
        rel = (lnk.get("rel") or "").lower()
        if href and rel in ("alternate", "", "self"):
            return href
    eid = (entry.get("id") or "").strip()
    if eid.startswith("http://") or eid.startswith("https://"):
        return eid.split("#", 1)[0]
    return ""


def load_feed_urls(feeds_file: Path) -> list[str]:
    """读取 feeds 文件：每行一个 URL，# 开头为注释，空行忽略。"""
    if not feeds_file.is_file():
        raise FileNotFoundError(f"找不到 feeds 文件: {feeds_file}")
    out: list[str] = []
    for line in feeds_file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def fetch_feed(url: str, *, max_items: int = 40) -> list[RawNewsItem]:
    """抓取单个 RSS/Atom，返回最多 max_items 条。"""
    try:
        r = requests.get(
            url,
            timeout=45,
            headers={"User-Agent": os.getenv("RSS_USER_AGENT", _DEFAULT_UA)},
        )
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except requests.RequestException as e:
        logger.error("请求失败 %s: %s", url, e)
        return []

    if getattr(parsed, "bozo", False) and not parsed.entries:
        logger.warning("解析可能有问题: %s — %s", url, getattr(parsed, "bozo_exception", ""))

    site = urlparse(url).netloc or "RSS"
    items: list[RawNewsItem] = []
    for entry in parsed.entries[:max_items]:
        title = (entry.get("title") or "").strip()
        link = _entry_link(entry).strip()
        raw_summary = entry.get("summary") or entry.get("description") or ""
        summary = strip_html(str(raw_summary))[:2000]
        pub_date = (
            entry.get("published")
            or entry.get("updated")
            or entry.get("pubDate")
            or ""
        )
        if not title and not link:
            continue
        items.append(
            {
                "title": title or "(无标题)",
                "summary": summary,
                "url": link,
                "pub_date": str(pub_date).strip(),
                "source": site,
            }
        )
    logger.info("从 %s 取得 %s 条", url, len(items))
    return items


def fetch_all_feeds(
    urls: list[str],
    *,
    max_per_feed: int = 40,
) -> list[RawNewsItem]:
    merged: list[RawNewsItem] = []
    for u in urls:
        try:
            merged.extend(fetch_feed(u, max_items=max_per_feed))
        except Exception as e:
            logger.error("抓取失败 %s: %s", u, e)
    return merged


def dedupe_by_url(items: list[RawNewsItem]) -> list[RawNewsItem]:
    seen: set[str] = set()
    out: list[RawNewsItem] = []
    for it in items:
        key = (it.get("url") or "").strip() or (it.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


__all__ = [
    "load_feed_urls",
    "fetch_feed",
    "fetch_all_feeds",
    "dedupe_by_url",
    "strip_html",
]
