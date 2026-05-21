"""
通过第三方搜索 API 获取新闻线索，映射为 RawNewsItem（供 DeepSeek 筛选）。

环境变量：
- NEWS_SEARCH_PROVIDER: tavily | serpapi（可省略：仅 SerpAPI Key 时默认 serpapi；仅 Tavily 时默认 tavily）
- NEWS_SEARCH_MAX_RESULTS: 默认 15（提高召回；SerpAPI 仍按「每次请求」计费）
- NEWS_MAX_AGE_HOURS: 默认 48，只保留该时间窗内有可靠时间戳的条目
- NEWS_DROP_UNDATED: 默认 1；无解析日期时是否丢弃（1=丢弃以保证「均在 48h 内」）
- NEWS_SEARCH_QUERY_SUFFIX: 可选，追加到搜索词后（扩大召回，如监管相关 OR 词）
- 国际专用：`fetch_search_news(..., extra_query_suffix=..., serpapi_hl=..., serpapi_gl=...)` 由 `ingest_intl_from_search_query` 调用，见 `NEWS_INTL_*`（`.env.example`）
- NEWS_QUERY_APPEND_WHEN_2D: 默认 1；SerpAPI 的 q 在尚无 when/after 时追加 `after:YYYY-MM-DD`（按 NEWS_MAX_AGE_HOURS 回推，与 48h 过滤一致）。设为 0 可关闭。
- SERPAPI_SORT_DATE: 默认 0；设为 1 时传 `so=1`（部分账号/查询下 SerpAPI 会 400，遇错请保持 0）。

Tavily:
- TAVILY_API_KEY, TAVILY_TOPIC, TAVILY_SEARCH_DEPTH
- TAVILY_TIME_RANGE: d|w|m|y（默认 d，约最近一天；精确窗仍靠 NEWS_MAX_AGE_HOURS 过滤）

SerpAPI:
- SERPAPI_API_KEY 或 SERPAPI_KEY, SERPAPI_HL, SERPAPI_GL
- fetch_search_news(..., extra_query_suffix=..., serpapi_hl=..., serpapi_gl=...) 可覆盖单次请求的 hl/gl 与词后缀（用于海外独立搜索）
"""

from __future__ import annotations

import logging
import os
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from rss_ingest import dedupe_by_url

from deepseek_news_filter import RawNewsItem

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
SERPAPI_URL = "https://serpapi.com/search.json"


def _resolve_search_provider() -> str:
    """未显式设置 NEWS_SEARCH_PROVIDER 时，按已配置的 Key 推断。"""
    explicit = os.getenv("NEWS_SEARCH_PROVIDER", "").strip().lower()
    if explicit in ("tavily", "serpapi"):
        return explicit
    serp = (os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY") or "").strip()
    tav = os.getenv("TAVILY_API_KEY", "").strip()
    if serp and not tav:
        return "serpapi"
    if tav and not serp:
        return "tavily"
    return "tavily"


def _parse_item_datetime(pub: str) -> datetime | None:
    """解析 pub_date / iso_date 为带时区的 UTC。"""
    s = (pub or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
        if "T" in s:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # 常见 "2024-11-12T07:09:00+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    return None


def filter_items_within_hours(items: list[RawNewsItem], hours: int) -> list[RawNewsItem]:
    """
    只保留发布时间（pub_date 可解析）不早于「现在 − hours」的条目。
    无日期条目由 NEWS_DROP_UNDATED 控制（默认丢弃，以满足「均在窗内」）。
    """
    if hours <= 0:
        return items
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    drop_undated = os.getenv("NEWS_DROP_UNDATED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    kept: list[RawNewsItem] = []
    dropped_old = 0
    dropped_undated = 0
    for it in items:
        dt = _parse_item_datetime(str(it.get("pub_date") or ""))
        if dt is None:
            if drop_undated:
                dropped_undated += 1
                continue
            kept.append(it)
            continue
        if dt >= cutoff:
            kept.append(it)
        else:
            dropped_old += 1
    logger.info(
        "时间窗过滤（最近 %s 小时，UTC）：保留 %s 条，丢弃过期 %s 条、无可靠日期 %s 条",
        hours,
        len(kept),
        dropped_old,
        dropped_undated,
    )
    return kept


def _build_search_query(base: str, *, extra_suffix: str | None = None) -> str:
    q = (base or "").strip()
    if extra_suffix is not None:
        suffix = extra_suffix.strip()
    else:
        suffix = os.getenv("NEWS_SEARCH_QUERY_SUFFIX", "").strip()
    if suffix:
        q = f"{q} {suffix}".strip()
    return q


def _build_serpapi_q(base: str, *, extra_suffix: str | None = None) -> str:
    q = _build_search_query(base, extra_suffix=extra_suffix)
    if os.getenv("NEWS_QUERY_APPEND_WHEN_2D", "1").strip().lower() not in ("0", "false", "no"):
        low = q.lower()
        if "when:" not in low and "after:" not in low and "before:" not in low:
            hours = max(1, int(os.getenv("NEWS_MAX_AGE_HOURS", "48")))
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            day = cutoff.strftime("%Y-%m-%d")
            q = f"{q} after:{day}".strip()
    return q


def _tavily_search(
    query: str,
    api_key: str,
    max_results: int,
    *,
    extra_suffix: str | None = None,
) -> list[RawNewsItem]:
    topic = os.getenv("TAVILY_TOPIC", "news").strip() or "news"
    depth = os.getenv("TAVILY_SEARCH_DEPTH", "basic").strip() or "basic"
    tr = os.getenv("TAVILY_TIME_RANGE", "d").strip().lower() or "d"
    n = max(1, min(max_results, 20))
    payload: dict[str, Any] = {
        "api_key": api_key,
        "query": _build_search_query(query, extra_suffix=extra_suffix),
        "max_results": n,
        "search_depth": depth,
        "topic": topic,
        "include_answer": False,
    }
    if tr in ("d", "w", "m", "y"):
        payload["time_range"] = tr
    r = requests.post(TAVILY_URL, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    out: list[RawNewsItem] = []
    for row in data.get("results") or []:
        if not isinstance(row, dict):
            continue
        title = (row.get("title") or "").strip()
        url = (row.get("url") or "").strip()
        content = (row.get("content") or "").strip()
        pub = (
            str(row.get("published_date") or row.get("published_time") or row.get("date") or "")
            .strip()
        )
        if not title and not url:
            continue
        src = "Tavily"
        if url:
            src = urlparse(url).netloc or "Tavily"
        out.append(
            {
                "title": title or "(无标题)",
                "summary": content[:2000],
                "url": url,
                "pub_date": pub,
                "source": src,
            }
        )
    logger.info("Tavily 返回 %s 条", len(out))
    return out


def _serpapi_source_name(item: dict[str, Any]) -> str:
    src = item.get("source")
    if isinstance(src, dict):
        return str(src.get("name") or "").strip()
    if isinstance(src, str):
        return src.strip()
    return ""


def _serpapi_flatten_news_results(news_results: list[Any]) -> list[dict[str, Any]]:
    """展开 SerpAPI 多种 news_results 结构为带 title/link 的扁平 dict。"""
    flat: list[dict[str, Any]] = []
    for n in news_results:
        if not isinstance(n, dict):
            continue
        if n.get("link") and n.get("title"):
            flat.append(n)
            continue
        hl = n.get("highlight")
        if isinstance(hl, dict) and hl.get("link"):
            merged = {
                "title": (hl.get("title") or "").strip(),
                "link": (hl.get("link") or "").strip(),
                "source": hl.get("source"),
                "date": hl.get("date") or hl.get("iso_date") or "",
                "iso_date": hl.get("iso_date") or "",
                "snippet": hl.get("snippet") if isinstance(hl.get("snippet"), str) else "",
            }
            if merged["title"] or merged["link"]:
                flat.append(merged)
        stories = n.get("stories")
        if isinstance(stories, list):
            for s in stories:
                if isinstance(s, dict) and s.get("link"):
                    flat.append(s)
    return flat


def _serpapi_search(
    query: str,
    api_key: str,
    max_results: int,
    *,
    extra_suffix: str | None = None,
    hl: str | None = None,
    gl: str | None = None,
) -> list[RawNewsItem]:
    logger.info(
        "SerpAPI：本次将发起 1 次 google_news 请求（免费档每月搜索次数有限，请以控制台为准）"
    )
    q_final = _build_serpapi_q(query, extra_suffix=extra_suffix)
    params: dict[str, Any] = {
        "engine": "google_news",
        "q": q_final,
        "api_key": api_key,
        "hl": (hl or os.getenv("SERPAPI_HL", "zh-cn") or "zh-cn").strip(),
        "gl": (gl or os.getenv("SERPAPI_GL", "cn") or "cn").strip(),
    }
    if os.getenv("SERPAPI_SORT_DATE", "0").strip().lower() in ("1", "true", "yes"):
        params["so"] = "1"
    logger.debug("SerpAPI q=%r", q_final)
    r = requests.get(SERPAPI_URL, params=params, timeout=60)
    if not r.ok:
        # 响应体里通常不含 api_key；便于排查 SerpAPI 返回的 JSON error
        try:
            err_body = r.json()
        except Exception:
            err_body = r.text[:800]
        raise RuntimeError(f"SerpAPI HTTP {r.status_code}: {err_body}") from None
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"SerpAPI 错误: {data.get('error')}")

    news_results = data.get("news_results") or []
    rows = _serpapi_flatten_news_results(news_results)
    out: list[RawNewsItem] = []
    for row in rows[:max_results]:
        title = (row.get("title") or "").strip()
        url = (row.get("link") or "").strip()
        snippet = (row.get("snippet") or row.get("summary") or "").strip()
        pub = str(row.get("iso_date") or row.get("date") or "").strip()
        src = _serpapi_source_name(row) or "SerpAPI"
        if not title and not url:
            continue
        out.append(
            {
                "title": title or "(无标题)",
                "summary": snippet[:2000],
                "url": url,
                "pub_date": pub,
                "source": src,
            }
        )
    logger.info("SerpAPI Google News 解析 %s 条", len(out))
    return out


def fetch_search_news(
    query: str,
    *,
    max_results: int | None = None,
    extra_query_suffix: str | None = None,
    serpapi_hl: str | None = None,
    serpapi_gl: str | None = None,
) -> list[RawNewsItem]:
    """
    按环境变量选择 Tavily 或 SerpAPI，返回去重后的 RawNewsItem 列表。
    默认只保留 NEWS_MAX_AGE_HOURS（默认 48）内有可靠时间戳的条目。

    extra_query_suffix：若传入则仅此轮追加到 query 后（不读 NEWS_SEARCH_QUERY_SUFFIX）；
    传空字符串表示不追加任何后缀。
    serpapi_hl / serpapi_gl：仅 SerpAPI 生效，覆盖 SERPAPI_HL / SERPAPI_GL。
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("搜索 query 不能为空")

    provider = _resolve_search_provider()
    mr = int(os.getenv("NEWS_SEARCH_MAX_RESULTS", "15")) if max_results is None else max_results
    mr = max(1, min(mr, 30))

    if provider == "tavily":
        key = os.getenv("TAVILY_API_KEY", "").strip()
        if not key:
            raise ValueError("使用 Tavily 请在 .env 中设置 TAVILY_API_KEY")
        raw = _tavily_search(q, key, mr, extra_suffix=extra_query_suffix)
    elif provider == "serpapi":
        key = (os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY") or "").strip()
        if not key:
            raise ValueError("使用 SerpAPI 请在 .env 中设置 SERPAPI_API_KEY（或 SERPAPI_KEY）")
        raw = _serpapi_search(
            q,
            key,
            mr,
            extra_suffix=extra_query_suffix,
            hl=serpapi_hl,
            gl=serpapi_gl,
        )
    else:
        raise ValueError(f'不支持的 NEWS_SEARCH_PROVIDER="{provider}"，请使用 tavily 或 serpapi')

    hours = int(os.getenv("NEWS_MAX_AGE_HOURS", "48"))
    raw = filter_items_within_hours(raw, hours)
    return dedupe_by_url(raw)


__all__ = ["fetch_search_news", "filter_items_within_hours"]
