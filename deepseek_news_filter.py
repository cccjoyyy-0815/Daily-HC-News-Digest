"""
DeepSeek 新闻流水线「上游」模块（中国医药 AI 数字化主题 + 可选海外高优先级搜索）。

职责概览：
- 从 feeds 文件 **抓取 RSS**（委托 `rss_ingest`）或从 **Tavily / SerpAPI** 按关键词搜索（委托 `search_ingest`）；
- 调用 DeepSeek **筛选、摘要、分层**（国内 high/medium/low；可选第二路 **海外仅高优先级**）；
- 下游（如 `digest_email`）只负责 **排版与投递**。

技术说明：OpenAI Python SDK v1 使用 client.chat.completions.create，
与旧版 openai.ChatCompletion.create 同属 Chat Completions 接口。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, TypedDict

from dotenv import load_dotenv
from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError

# 始终从本文件所在项目根目录加载 .env，避免在其它 cwd 下运行时读不到配置；
# override=True：避免 shell 里误设的「空变量」挡住 .env 中的值。
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

logger = logging.getLogger(__name__)

# DeepSeek OpenAI 兼容接口
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
# 单次请求中用户侧新闻 JSON 的字符上限（保守估计，避免触发 context 或输出截断）
MAX_USER_CHARS_PER_BATCH = 12_000
# 单条新闻在分批时预留的字符（含 JSON 括号等）
MIN_CHARS_PER_ITEM = 200
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SEC = 1.0


class RawNewsItem(TypedDict, total=False):
    """原始新闻条目（字段均可选，但 title/url 建议提供）。"""

    title: str
    summary: str
    url: str
    pub_date: str
    source: str


class FilteredNewsItem(RawNewsItem, total=False):
    """筛选后条目：原字段 + 要点摘要（场景、工具/平台、合作方）；海外分区可含 region。"""

    refined_summary: str
    region: str  # US | Europe | APAC（仅海外高优先级列表使用）


class TieredNewsResult(TypedDict):
    """按重要性分层后的结果。"""

    high: list[FilteredNewsItem]
    medium: list[FilteredNewsItem]
    low: list[FilteredNewsItem]


SYSTEM_PROMPT = """你是中国医药与医疗信息化领域的专业编辑。你的任务是阅读用户给出的 JSON 新闻数组，完成以下要求并只输出合法 JSON（不要 Markdown 代码块、不要解释文字）：

1. 只保留与「中国医药 AI 数字化」强相关或中度相关的新闻：以**制药企业、医疗器械企业**及其上下游（CXO、数字健康服务商等）为核心主体的 AI/数据/软件/自动化/云上平台落地；医院/医保/监管若与药械企业的产品或合作**强绑定**也可保留。弱相关或无关的直接丢弃。

2. 去除重复或高度相似的新闻（标题或摘要实质相同、或同一事件不同来源只保留一条信息更完整者）。

3. 为每条保留的新闻生成 refined_summary：用**中文一段式摘要**，总字数 **80～150 个汉字**（可含必要标点）。**必须尽量写清**（材料不足则据标题与摘要合理概括，并可用「报道未披露」等简短提示缺口）：
   - **应用场景**：业务环节（如研发/临床与注册、生产与质控、供应链、营销与医学、患者服务/数字疗法等）、主要服务对象或落地场景；
   - **具体工具/平台/系统**：若有，写出产品名、SaaS、大模型/算法模块、工业软件、数据平台等；无则说明「未披露具体系统」；
   - **合作方/对手方**：药械企业、医院/科研院所、云厂商或 AI 公司、政府项目牵头方等关键参与方；无则说明「合作方未详」。
   写成连贯叙述，避免空洞形容词堆砌。

4. 按重要性归入 high / medium / low（**high 不必是政策稿**；以「与药企/械企 AI 数字化的相关强度 + 信息是否具体可感」为主）：
   - **high**：与**制药或医疗器械企业**的 **AI/数字化** **强相关**：如企业级平台上线或规模化推广、与云/AI 厂商的**重大战略合作**、**AI 赋能研发/生产/商业化**的标杆案例、**数字疗法或智能医疗器械**关键进展、**头部或高成长药械企业**的实质数字化里程碑等。部委/省级政策若**直接约束或推动药械数字化**仍可放 high，但**high 不以政策为必要条件**。
   - **medium**：中度相关：行业会议与白皮书、区域或单点试点、融资中数字化仅为子话题、**偏泛的产业观察**；有明确药械企业主体但信息深度或新颖度一般者常归 medium。
   - **low**：与药械企业 AI 数字化**弱相关**：泛宏观口号、与药械主业牵连薄的人事/股价短讯、**泛 AI 热点但与药械链路不清**等。
   分层优先级：**具体业务与产品落地、工具与合作链 > 会议与行业观点 > 弱相关边角**。

5. 输出必须是单个 JSON 对象，且顶层键仅为 high、medium、low，值为数组。数组中每个元素必须包含输入中的原有字段（title, summary, url, pub_date, source），并增加 refined_summary 字段。若某层无新闻则输出空数组 []。

6. title、summary、url、pub_date、source 必须与输入中对应条目逐字段一致（不要翻译或改写 title，不要省略字段）；禁止输出空字符串的 url。仅 refined_summary 为允许新增/改写的内容。"""


SYSTEM_PROMPT_INTL_HIGH = """你是全球医药与医疗器械 AI 数字化领域的专业编辑。用户会给出 JSON 新闻数组（多为英文标题/摘要）。你的任务是只输出合法 JSON（不要 Markdown 代码块、不要解释文字），且顶层仅为一个对象，键名固定为 **items**，值为数组。

## 收录范围
- 地理：**美国、欧洲（含英国/欧盟/瑞士等）、亚太地区（日本、韩国、新加坡、澳大利亚、印度等）**；新闻主体或核心市场须落在上述区域之一。
- **排除**：以**中国大陆、香港、澳门**为**唯一或主要**叙事中心的新闻（若全球公告中中国仅为次要市场一笔带过，可保留但 region 按总部/主市场归类）。
- 主题：与**制药企业或医疗器械企业**的 **AI / 数据 / 软件 / 自动化 / 云与网络安全** 等数字化强相关；医院/监管若与药械产品或合作**强绑定**可收录。

## 档位（本任务**只有高优先级一档**）
- **宁少勿滥**：只输出若放在「全球药械 AI 数字化」视角下属于 **high** 档的条目（企业级平台与规模化落地、头部云/AI 与药械的重大合作、AI 赋能研发/生产/商业化的标杆进展、数字疗法或智能器械关键里程碑等）。会议短讯、泛泛行业评论、弱相关边角**一律丢弃**。
- 若候选中没有任何够格条目，输出 `"items": []`。

## 每条目字段
输出数组中每个对象必须包含输入中的原有字段：**title, summary, url, pub_date, source**（与输入逐字段一致，不要改写 title），并增加：
- **refined_summary**：**中文**一段式，**80～150 个汉字**，写清应用场景、具体工具/平台（无则写「未披露具体系统」）、合作方（无则写「合作方未详」）；可夹注必要英文专名。
- **region**：字符串，**仅能**取以下三者之一（按事件**最主要**地理归属判断；跨区时选总部或首要监管/市场所在区）：
  - `US`：美国为主；
  - `Europe`：欧洲为主；
  - `APAC`：亚太为主（含日本、澳新、东南亚、印度等）。

## 去重
标题或 URL 实质重复、或同一事件多来源只保留信息更完整的一条。

## 数量上限
输出条数**不超过 18 条**（若高质量条目更少则如实少输出）。"""


class DailyDigestBundle(TypedDict):
    """日报：国内（含中国语境）分层 + 可选的海外高优先级列表。"""

    domestic: TieredNewsResult
    international_high: list[FilteredNewsItem]


def _norm_title(title: str) -> str:
    """用于匹配模型输出与原始条目（忽略大小写与连续空白）。"""
    t = (title or "").strip().casefold()
    return re.sub(r"\s+", " ", t)


def _build_title_lookup(sources: list[RawNewsItem]) -> dict[str, RawNewsItem]:
    """标题（规范化）→ 首次出现的原始条目。"""
    m: dict[str, RawNewsItem] = {}
    for it in sources:
        key = _norm_title(str(it.get("title", "")))
        if not key:
            continue
        m.setdefault(key, it)
    return m


def _hydrate_tiered_from_sources(tiered: TieredNewsResult, sources: list[RawNewsItem]) -> None:
    """
    模型偶尔会漏掉 url 等字段：按规范化标题从原始列表回填，修复邮件里空链接。
    """
    lookup = _build_title_lookup(sources)
    for tier in ("high", "medium", "low"):
        for it in tiered[tier]:
            key = _norm_title(str(it.get("title", "")))
            src = lookup.get(key)
            if not src:
                continue
            if not str(it.get("url") or "").strip():
                u = (src.get("url") or "").strip()
                if u:
                    it["url"] = u
            if not str(it.get("source") or "").strip() and (src.get("source") or "").strip():
                it["source"] = str(src.get("source") or "").strip()
            if not str(it.get("pub_date") or "").strip() and (src.get("pub_date") or "").strip():
                it["pub_date"] = str(src.get("pub_date") or "").strip()


def _strip_json_fences(text: str) -> str:
    """去掉模型可能返回的 ```json ... ``` 包裹。"""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _estimate_item_chars(item: RawNewsItem) -> int:
    """估算单条新闻序列化后的字符量，用于分批。"""
    try:
        return len(json.dumps(item, ensure_ascii=False))
    except (TypeError, ValueError):
        return MIN_CHARS_PER_ITEM


def _chunk_news_by_budget(
    news_list: list[RawNewsItem],
    max_chars: int = MAX_USER_CHARS_PER_BATCH,
) -> list[list[RawNewsItem]]:
    """按字符预算将新闻列表切成多批，降低 token 超限风险。"""
    batches: list[list[RawNewsItem]] = []
    current: list[RawNewsItem] = []
    used = 0
    for item in news_list:
        need = max(_estimate_item_chars(item), MIN_CHARS_PER_ITEM)
        if current and used + need > max_chars:
            batches.append(current)
            current = []
            used = 0
        current.append(item)
        used += need
    if current:
        batches.append(current)
    return batches


def _empty_result() -> TieredNewsResult:
    return {"high": [], "medium": [], "low": []}


def _merge_tiered(a: TieredNewsResult, b: TieredNewsResult) -> TieredNewsResult:
    """合并两批 API 分层结果。"""
    return {
        "high": [*a["high"], *b["high"]],
        "medium": [*a["medium"], *b["medium"]],
        "low": [*a["low"], *b["low"]],
    }


def _dedupe_by_url(items: list[FilteredNewsItem]) -> list[FilteredNewsItem]:
    """按 url 去重（无 url 时退化为 title）。"""
    seen: set[str] = set()
    out: list[FilteredNewsItem] = []
    for it in items:
        key = (it.get("url") or "").strip() or (it.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _consolidate_cross_batch(
    client: OpenAI,
    model: str,
    merged: TieredNewsResult,
) -> TieredNewsResult:
    """
    多批合并后，用一次较短上下文调用让模型跨批去重并重新分层。
    若合并后条目很少则跳过。
    """
    total = len(merged["high"]) + len(merged["medium"]) + len(merged["low"])
    if total <= 1:
        return merged

    compact = {
        "high": merged["high"],
        "medium": merged["medium"],
        "low": merged["low"],
    }
    user_text = (
        "以下 JSON 是多轮筛选合并后的结果，可能存在跨轮重复或分层不一致。"
        "请重新：去除重复与高度相似项，保留与「中国医药 AI 数字化」相关的条目，"
        "修正 refined_summary（每条 80～150 字，含场景·工具/平台·合作方），并按 high/medium/low 新标准重新分配。"
        "输出格式与字段要求与之前相同，只输出 JSON。\n\n"
        + json.dumps(compact, ensure_ascii=False)
    )
    if len(user_text) > MAX_USER_CHARS_PER_BATCH * 2:
        # 过长时仅做 URL 级去重，避免再次撑爆上下文
        logger.warning("合并后体量过大，跳过跨批 consolidate，仅按 url 去重各层")
        return {
            "high": _dedupe_by_url(merged["high"]),
            "medium": _dedupe_by_url(merged["medium"]),
            "low": _dedupe_by_url(merged["low"]),
        }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    raw = _chat_completion_with_retry(client, model, messages)
    parsed = _parse_tiered_json(raw)
    return parsed


def _parse_tiered_json(raw: str) -> TieredNewsResult:
    """解析并校验模型返回的 JSON 结构。"""
    cleaned = _strip_json_fences(raw)
    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"模型返回非合法 JSON: {cleaned[:500]}...") from e

    if not isinstance(data, dict):
        raise ValueError("顶层 JSON 必须是对象")

    out: TieredNewsResult = _empty_result()
    for key in ("high", "medium", "low"):
        val = data.get(key, [])
        if val is None:
            val = []
        if not isinstance(val, list):
            raise ValueError(f'键 "{key}" 必须是数组')
        for i, item in enumerate(val):
            if not isinstance(item, dict):
                raise ValueError(f'"{key}[{i}]" 必须是对象')
            if "refined_summary" not in item:
                raise ValueError(f'"{key}[{i}]" 缺少 refined_summary')
        out[key] = val  # type: ignore[literal-required]
    return out


_ALLOWED_INTL_REGIONS = frozenset({"US", "Europe", "APAC"})


def _coerce_intl_region(raw: str | None) -> str:
    s = (raw or "").strip()
    if s in _ALLOWED_INTL_REGIONS:
        return s
    low = s.casefold()
    if low in ("usa", "u.s.", "u.s.a.", "america", "united states", "美国"):
        return "US"
    if low in (
        "eu",
        "europe",
        "european",
        "uk",
        "united kingdom",
        "germany",
        "france",
        "欧洲",
        "欧盟",
        "英国",
    ):
        return "Europe"
    if low in ("apac", "asia pacific", "asia-pacific", "japan", "singapore", "australia", "亚太"):
        return "APAC"
    if s:
        logger.warning("无法识别 region=%r，默认 APAC", raw)
    return "APAC"


def _parse_intl_high_json(raw: str) -> list[FilteredNewsItem]:
    """解析海外高优先级-only 模型输出：{\"items\": [...]} 。"""
    cleaned = _strip_json_fences(raw)
    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"模型返回非合法 JSON: {cleaned[:500]}...") from e
    if not isinstance(data, dict):
        raise ValueError("顶层 JSON 必须是对象")
    items = data.get("items", [])
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError('键 "items" 必须是数组')
    out: list[FilteredNewsItem] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f'"items[{i}]" 必须是对象')
        if "refined_summary" not in item:
            raise ValueError(f'"items[{i}]" 缺少 refined_summary')
        item["region"] = _coerce_intl_region(str(item.get("region", "")))
        out.append(item)  # type: ignore[arg-type]
    return out


def _hydrate_intl_list_from_sources(items: list[FilteredNewsItem], sources: list[RawNewsItem]) -> None:
    """按标题回填漏掉的 url / source / pub_date。"""
    lookup = _build_title_lookup(sources)
    for it in items:
        key = _norm_title(str(it.get("title", "")))
        src = lookup.get(key)
        if not src:
            continue
        if not str(it.get("url") or "").strip():
            u = (src.get("url") or "").strip()
            if u:
                it["url"] = u
        if not str(it.get("source") or "").strip() and (src.get("source") or "").strip():
            it["source"] = str(src.get("source") or "").strip()
        if not str(it.get("pub_date") or "").strip() and (src.get("pub_date") or "").strip():
            it["pub_date"] = str(src.get("pub_date") or "").strip()


def _call_intl_single_batch(
    client: OpenAI,
    model: str,
    batch: list[RawNewsItem],
) -> list[FilteredNewsItem]:
    user_payload = json.dumps(batch, ensure_ascii=False)
    user_prompt = (
        "请根据系统说明处理以下新闻 JSON 数组，只输出符合要求的 JSON 对象（顶层键为 items）。\n\n"
        + user_payload
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_INTL_HIGH},
        {"role": "user", "content": user_prompt},
    ]
    raw = _chat_completion_with_retry(client, model, messages)
    return _parse_intl_high_json(raw)


def _consolidate_intl_cross_batch(
    client: OpenAI,
    model: str,
    merged: list[FilteredNewsItem],
) -> list[FilteredNewsItem]:
    """多批海外结果合并后去重、压到高优先级上限。"""
    if len(merged) <= 1:
        return merged
    cap = max(1, min(int(os.getenv("NEWS_INTL_HIGH_CAP", "18")), 30))
    compact = {"items": merged}
    user_text = (
        "以下 JSON 的 items 为「海外药械 AI 数字化」多轮筛选合并结果，可能有重复或档位偏松。"
        f"请去重、只保留**高优先级**条目，修正 refined_summary（80～150 字中文，含场景·工具/平台·合作方）与 region（US|Europe|APAC）；"
        f"**最多 {cap} 条**。输出仍为单个 JSON 对象，仅含键 items。\n\n"
        + json.dumps(compact, ensure_ascii=False)
    )
    if len(user_text) > MAX_USER_CHARS_PER_BATCH * 2:
        logger.warning("海外合并后体量过大，跳过 consolidate，仅按 url 去重")
        return _dedupe_by_url(merged)[:cap]

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_INTL_HIGH},
        {"role": "user", "content": user_text},
    ]
    raw = _chat_completion_with_retry(client, model, messages)
    parsed = _parse_intl_high_json(raw)
    return parsed[:cap]


def filter_intl_high_news_with_deepseek(
    news_list: list[RawNewsItem],
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    max_user_chars_per_batch: int = MAX_USER_CHARS_PER_BATCH,
) -> list[FilteredNewsItem]:
    """
    海外新闻：只保留美国/欧洲/亚太语境下、相当于 high 档的药械 AI 数字化条目。
    返回列表，每项含 refined_summary 与 region（US|Europe|APAC）。
    """
    key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("缺少 DEEPSEEK_API_KEY，请在环境变量或参数 api_key 中提供")
    if not news_list:
        return []

    client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
    batches = _chunk_news_by_budget(news_list, max_chars=max_user_chars_per_batch)
    logger.info("海外新闻共 %s 条，分为 %s 批处理", len(news_list), len(batches))

    merged: list[FilteredNewsItem] = []
    for idx, batch in enumerate(batches, start=1):
        logger.info("海外批次 %s/%s，本批 %s 条", idx, len(batches), len(batch))
        part = _call_intl_single_batch(client, model, batch)
        merged.extend(part)

    merged = _dedupe_by_url(merged)
    if len(batches) > 1:
        merged = _consolidate_intl_cross_batch(client, model, merged)
    cap = max(1, min(int(os.getenv("NEWS_INTL_HIGH_CAP", "18")), 30))
    if len(merged) > cap:
        merged = merged[:cap]

    _hydrate_intl_list_from_sources(merged, news_list)
    return merged


def _chat_completion_with_retry(
    client: OpenAI,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
) -> str:
    """
    调用 Chat Completions（OpenAI SDK v1：client.chat.completions.create），
    带最多 3 次重试与指数退避。
    """
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # 与旧版 openai.ChatCompletion.create 等价的 v1 调用方式
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            choice = resp.choices[0]
            content = choice.message.content or ""
            if not content.strip():
                raise ValueError("模型返回空内容")
            return content
        except (RateLimitError, APIConnectionError, APITimeoutError) as e:
            last_err = e
            logger.warning("第 %s/%s 次调用失败（可重试）: %s", attempt, MAX_RETRIES, e)
        except APIError as e:
            last_err = e
            # 4xx 中部分可重试；token 过长等可能为 400
            status = getattr(e, "status_code", None) or getattr(e, "status", None)
            if status in (429, 500, 502, 503, 504):
                logger.warning("第 %s/%s 次调用失败（APIError 可重试）: %s", attempt, MAX_RETRIES, e)
            else:
                raise
        except Exception as e:
            last_err = e
            logger.warning("第 %s/%s 次调用失败: %s", attempt, MAX_RETRIES, e)

        if attempt < MAX_RETRIES:
            sleep_sec = RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            time.sleep(sleep_sec)

    assert last_err is not None
    raise RuntimeError(f"DeepSeek 调用在 {MAX_RETRIES} 次重试后仍失败") from last_err


def parse_model_json_response(raw: str) -> Any:
    """
    将模型返回的文本解析为 JSON 对象（支持任意合法 JSON 顶层类型）。
    会先去掉 ``` / ```json 代码块围栏。
    """
    cleaned = _strip_json_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"模型返回非合法 JSON（前 500 字）: {cleaned[:500]}...") from e


def deepseek_chat_text(
    *,
    system_prompt: str,
    user_message: str,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
) -> str:
    """
    向 DeepSeek 发送一轮 system + user，返回模型正文（字符串）。
    与新闻筛选无关的通用入口；需自行解析 JSON 时可配合 parse_model_json_response。
    """
    key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("缺少 DEEPSEEK_API_KEY，请在环境变量或参数 api_key 中提供")
    client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    return _chat_completion_with_retry(client, model, messages, temperature=temperature)


def deepseek_chat_json(
    *,
    system_prompt: str,
    user_message: str,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
) -> Any:
    """发送 system + user，并将模型输出按 JSON 解析后返回（dict / list 等）。"""
    raw = deepseek_chat_text(
        system_prompt=system_prompt,
        user_message=user_message,
        api_key=api_key,
        model=model,
        temperature=temperature,
    )
    return parse_model_json_response(raw)


def _call_single_batch(
    client: OpenAI,
    model: str,
    batch: list[RawNewsItem],
) -> TieredNewsResult:
    """对单批新闻发起一次筛选请求。"""
    user_payload = json.dumps(batch, ensure_ascii=False)
    user_prompt = (
        "请根据系统说明处理以下新闻 JSON 数组，只输出符合要求的 JSON 对象。\n\n" + user_payload
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    raw = _chat_completion_with_retry(client, model, messages)
    return _parse_tiered_json(raw)


def filter_news_with_deepseek(
    news_list: list[RawNewsItem],
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    max_user_chars_per_batch: int = MAX_USER_CHARS_PER_BATCH,
) -> TieredNewsResult:
    """
    入口：接收原始新闻列表，返回按 high/medium/low 分层的结果。

    - 自动分批以避免 token 过长。
    - 多批时合并后再请求一次 consolidate（体量过大则仅 URL 去重）。
    - 网络/限流等错误最多重试 3 次。
    """
    key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise ValueError("缺少 DEEPSEEK_API_KEY，请在环境变量或参数 api_key 中提供")

    if not news_list:
        return _empty_result()

    client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)

    batches = _chunk_news_by_budget(news_list, max_chars=max_user_chars_per_batch)
    logger.info("新闻共 %s 条，分为 %s 批处理", len(news_list), len(batches))

    merged = _empty_result()
    for idx, batch in enumerate(batches, start=1):
        logger.info("处理第 %s/%s 批，本批 %s 条", idx, len(batches), len(batch))
        part = _call_single_batch(client, model, batch)
        merged = _merge_tiered(merged, part)

    if len(batches) > 1:
        merged = _consolidate_cross_batch(client, model, merged)
    else:
        # 单批也做一次各层 url 去重，防止模型输出重复
        merged = {
            "high": _dedupe_by_url(merged["high"]),
            "medium": _dedupe_by_url(merged["medium"]),
            "low": _dedupe_by_url(merged["low"]),
        }

    _hydrate_tiered_from_sources(merged, news_list)
    return merged


def ingest_rss_from_feeds_file(
    feeds_file: str | Path,
    *,
    max_per_feed: int | None = None,
    max_total_before_model: int | None = None,
) -> list[RawNewsItem]:
    """
    上游：根据 feeds 列表文件抓取 RSS，去重、按 NEWS_MAX_AGE_HOURS（默认 48）过滤发布时间，再按条数上限截断。
    实际抓取在 `rss_ingest`；时间过滤在 `search_ingest.filter_items_within_hours`。
    """
    from rss_ingest import dedupe_by_url, fetch_all_feeds, load_feed_urls

    path = Path(feeds_file)
    urls = load_feed_urls(path)
    if not urls:
        raise ValueError(
            "feeds 文件中没有任何 URL（去掉 # 注释后每行需有一个 RSS 地址）。"
        )

    m_per = int(os.getenv("MAX_ITEMS_PER_FEED", "40")) if max_per_feed is None else max_per_feed
    m_tot = (
        int(os.getenv("MAX_TOTAL_ITEMS_BEFORE_MODEL", "80"))
        if max_total_before_model is None
        else max_total_before_model
    )

    raw = fetch_all_feeds(urls, max_per_feed=m_per)
    raw = dedupe_by_url(raw)
    from search_ingest import filter_items_within_hours

    hours = int(os.getenv("NEWS_MAX_AGE_HOURS", "48"))
    raw = filter_items_within_hours(raw, hours)
    if len(raw) > m_tot:
        logger.info("条目超过上限 %s，截断至前 %s 条", m_tot, m_tot)
        raw = raw[:m_tot]
    return raw


def ingest_and_filter_from_feeds(
    feeds_file: str | Path,
    *,
    max_per_feed: int | None = None,
    max_total_before_model: int | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    max_user_chars_per_batch: int = MAX_USER_CHARS_PER_BATCH,
) -> TieredNewsResult:
    """
    上游一站式：RSS 抓取 → DeepSeek 筛选分层。
    适合下游只关心「已经分好层的 TieredNewsResult」、自行排版的场景。
    """
    raw = ingest_rss_from_feeds_file(
        feeds_file,
        max_per_feed=max_per_feed,
        max_total_before_model=max_total_before_model,
    )
    if not raw:
        return _empty_result()
    return filter_news_with_deepseek(
        raw,
        api_key=api_key,
        model=model,
        max_user_chars_per_batch=max_user_chars_per_batch,
    )


def ingest_from_search_query(
    query: str,
    *,
    max_results: int | None = None,
) -> list[RawNewsItem]:
    """
    上游：使用 Tavily / SerpAPI（由 NEWS_SEARCH_PROVIDER 决定）按关键词抓取新闻线索。
    """
    from search_ingest import fetch_search_news

    return fetch_search_news(query, max_results=max_results)


def ingest_intl_from_search_query(
    query: str,
    *,
    max_results: int | None = None,
) -> list[RawNewsItem]:
    """
    国际搜索：使用独立 SerpAPI hl/gl（默认 en/us）与 NEWS_INTL_SEARCH_QUERY_SUFFIX；
    不会自动套用国内用的 NEWS_SEARCH_QUERY_SUFFIX（避免把中国词缀拼进海外 query）。
    """
    from search_ingest import fetch_search_news

    q = (query or "").strip()
    if not q:
        raise ValueError("国际搜索 query 不能为空")
    intl_suffix = os.getenv("NEWS_INTL_SEARCH_QUERY_SUFFIX", "").strip()
    mr = max_results
    if mr is None:
        v = os.getenv("NEWS_INTL_SEARCH_MAX_RESULTS", "").strip()
        mr = int(v) if v else int(os.getenv("NEWS_SEARCH_MAX_RESULTS", "15"))
    intl_hl = os.getenv("NEWS_INTL_SERPAPI_HL", "en").strip() or "en"
    intl_gl = os.getenv("NEWS_INTL_SERPAPI_GL", "us").strip() or "us"
    suffix_arg = intl_suffix if intl_suffix else ""
    return fetch_search_news(
        q,
        max_results=mr,
        extra_query_suffix=suffix_arg,
        serpapi_hl=intl_hl,
        serpapi_gl=intl_gl,
    )


def ingest_search_and_filter_from_query(
    query: str,
    *,
    max_results: int | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    max_user_chars_per_batch: int = MAX_USER_CHARS_PER_BATCH,
) -> TieredNewsResult:
    """上游一站式：搜索 API → DeepSeek 筛选分层。"""
    raw = ingest_from_search_query(query, max_results=max_results)
    if not raw:
        return _empty_result()
    return filter_news_with_deepseek(
        raw,
        api_key=api_key,
        model=model,
        max_user_chars_per_batch=max_user_chars_per_batch,
    )


__all__ = [
    "RawNewsItem",
    "FilteredNewsItem",
    "TieredNewsResult",
    "DailyDigestBundle",
    "ingest_rss_from_feeds_file",
    "ingest_and_filter_from_feeds",
    "ingest_from_search_query",
    "ingest_intl_from_search_query",
    "ingest_search_and_filter_from_query",
    "filter_news_with_deepseek",
    "filter_intl_high_news_with_deepseek",
    "deepseek_chat_text",
    "deepseek_chat_json",
    "parse_model_json_response",
    "DEEPSEEK_BASE_URL",
    "DEFAULT_MODEL",
]
