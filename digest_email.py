"""
将分层筛选结果渲染为 HTML，并通过 SMTP 发送日报邮件。
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Iterable

from deepseek_news_filter import FilteredNewsItem, TieredNewsResult

logger = logging.getLogger(__name__)

_INTL_REGION_ZH = {"US": "美国", "Europe": "欧洲", "APAC": "亚太"}


def _intl_section_html(title: str, items: Iterable[FilteredNewsItem]) -> str:
    rows: list[str] = []
    for it in items:
        t = html.escape((it.get("title") or "").strip() or "(无标题)")
        summ = html.escape((it.get("refined_summary") or "").strip())
        url = (it.get("url") or "").strip()
        link = f'<a href="{html.escape(url)}">{html.escape(url)[:80]}</a>' if url else ""
        src = html.escape((it.get("source") or "").strip())
        reg = (it.get("region") or "").strip()
        reg_zh = _INTL_REGION_ZH.get(reg, reg)
        meta_parts = [p for p in (reg_zh, it.get("pub_date"), src) if p]
        meta = html.escape(" · ".join(str(x) for x in meta_parts))
        rows.append(
            "<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;vertical-align:top;'>"
            f"<div style='font-weight:600;'>{t}</div>"
            f"<div style='color:#333;font-size:14px;margin-top:8px;line-height:1.55;white-space:pre-wrap;'>{summ}</div>"
            f"<div style='color:#888;font-size:12px;margin-top:4px;'>{meta}</div>"
            f"<div style='font-size:12px;margin-top:4px;'>{link}</div>"
            "</td>"
            "</tr>"
        )
    body = "".join(rows) if rows else "<tr><td style='padding:12px;color:#888;'>（暂无海外高优先级条目）</td></tr>"
    return (
        f"<h2 style='margin:28px 0 8px;font-size:18px;'>{html.escape(title)}</h2>"
        "<table style='width:100%;border-collapse:collapse;font-family:system-ui,sans-serif;'>"
        f"{body}</table>"
    )


def _section_html(title: str, items: Iterable[FilteredNewsItem]) -> str:
    rows: list[str] = []
    for it in items:
        t = html.escape((it.get("title") or "").strip() or "(无标题)")
        summ = html.escape((it.get("refined_summary") or "").strip())
        url = (it.get("url") or "").strip()
        link = f'<a href="{html.escape(url)}">{html.escape(url)[:80]}</a>' if url else ""
        src = html.escape((it.get("source") or "").strip())
        meta_parts = [p for p in (it.get("pub_date"), src) if p]
        meta = html.escape(" · ".join(str(x) for x in meta_parts))
        rows.append(
            "<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;vertical-align:top;'>"
            f"<div style='font-weight:600;'>{t}</div>"
            f"<div style='color:#333;font-size:14px;margin-top:8px;line-height:1.55;white-space:pre-wrap;'>{summ}</div>"
            f"<div style='color:#888;font-size:12px;margin-top:4px;'>{meta}</div>"
            f"<div style='font-size:12px;margin-top:4px;'>{link}</div>"
            "</td>"
            "</tr>"
        )
    body = "".join(rows) if rows else "<tr><td style='padding:12px;color:#888;'>（本层无条目）</td></tr>"
    return (
        f"<h2 style='margin:24px 0 8px;font-size:18px;'>{html.escape(title)}</h2>"
        "<table style='width:100%;border-collapse:collapse;font-family:system-ui,sans-serif;'>"
        f"{body}</table>"
    )


def tiered_result_to_html(
    result: TieredNewsResult,
    *,
    headline: str,
    international_high: list[FilteredNewsItem] | None = None,
) -> str:
    """生成完整 HTML 文档（UTF-8）。international_high 非空时追加「海外要闻」分区。"""
    h = html.escape(headline)
    has_domestic = bool(result["high"] or result["medium"] or result["low"])
    if has_domestic:
        domestic_inner = "".join(
            [
                "<h2 style='margin:24px 0 10px;font-size:18px;color:#333;'>国内（中国）</h2>",
                _section_html("高优先级", result["high"]),
                _section_html("中优先级", result["medium"]),
                _section_html("低优先级", result["low"]),
            ]
        )
    else:
        domestic_inner = (
            "<h2 style='margin:24px 0 10px;font-size:18px;color:#333;'>国内（中国）</h2>"
            "<p style='color:#888;font-size:14px;margin:0 0 16px;'>本轮暂无入选新闻。</p>"
        )
    intl = international_high or []
    intl_inner = (
        _intl_section_html("海外要闻 · 美国 / 欧洲 / 亚太（仅高优先级）", intl) if intl else ""
    )
    inner = domestic_inner + intl_inner
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{h}</title></head>
<body style="margin:0;padding:16px;background:#f6f7f9;">
<div style="max-width:720px;margin:0 auto;background:#fff;padding:20px 24px;border-radius:8px;">
<p style="color:#666;font-size:14px;">本邮件由 deepseek_news 自动生成。国内分区为高中低三层；海外分区仅收录美国/欧洲/亚太范围内、相当于高优先级的药械 AI 数字化要闻。每条「要点摘要」突出应用场景、工具/平台与合作方。</p>
<h1 style="font-size:22px;margin:0 0 16px;">{h}</h1>
{inner}
<p style="color:#aaa;font-size:12px;margin-top:32px;">若链接无法点击，请检查邮箱客户端或复制 URL。</p>
</div>
</body>
</html>"""


def tiered_result_to_plain(
    result: TieredNewsResult,
    *,
    headline: str,
    international_high: list[FilteredNewsItem] | None = None,
) -> str:
    lines = [headline, "", "—— 国内（中国）——", ""]
    if result["high"] or result["medium"] or result["low"]:
        lines += ["—— 高 ——"]
        for it in result["high"]:
            lines.append(f"* {(it.get('title') or '').strip()} — {(it.get('refined_summary') or '').strip()}")
            if it.get("url"):
                lines.append(f"  {it.get('url')}")
        lines += ["", "—— 中 ——"]
        for it in result["medium"]:
            lines.append(f"* {(it.get('title') or '').strip()} — {(it.get('refined_summary') or '').strip()}")
            if it.get("url"):
                lines.append(f"  {it.get('url')}")
        lines += ["", "—— 低 ——"]
        for it in result["low"]:
            lines.append(f"* {(it.get('title') or '').strip()} — {(it.get('refined_summary') or '').strip()}")
            if it.get("url"):
                lines.append(f"  {it.get('url')}")
    else:
        lines.append("（本轮暂无入选）")

    intl = international_high or []
    if intl:
        lines += ["", "—— 海外（美国/欧洲/亚太，仅高优先级）——"]
        for it in intl:
            reg = (it.get("region") or "").strip()
            lab = _INTL_REGION_ZH.get(reg, reg)
            lines.append(
                f"* [{lab}] {(it.get('title') or '').strip()} — {(it.get('refined_summary') or '').strip()}"
            )
            if it.get("url"):
                lines.append(f"  {it.get('url')}")
    return "\n".join(lines) + "\n"


def send_digest_email(
    *,
    subject: str,
    html_body: str,
    text_body: str,
) -> None:
    """
    使用环境变量发送邮件（需在 .env 或环境中配置）：

    - SMTP_HOST, SMTP_PORT（默认 587）
    - SMTP_USER, SMTP_PASSWORD（无密码可留空，部分内网 relay 允许）
    - EMAIL_FROM, EMAIL_TO（多个收件人用英文逗号分隔）
    - SMTP_ENCRYPTION：starttls（默认）| ssl | none
    """
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        raise ValueError(
            "缺少 SMTP_HOST：请确认项目根目录的 .env 已保存到磁盘，且包含一行 "
            "SMTP_HOST=smtp.gmail.com（以及 SMTP_USER、SMTP_PASSWORD、EMAIL_FROM、EMAIL_TO）。"
            "若在编辑器里改过但未保存，按 Cmd+S / Ctrl+S 后再运行。"
        )

    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    mail_from = os.getenv("EMAIL_FROM", "").strip() or user
    mail_to_raw = os.getenv("EMAIL_TO", "").strip()
    if not mail_from:
        raise ValueError("缺少 EMAIL_FROM（或未设置 SMTP_USER 作为发件人）")
    if not mail_to_raw:
        raise ValueError("缺少 EMAIL_TO")

    recipients = [x.strip() for x in mail_to_raw.split(",") if x.strip()]
    enc = os.getenv("SMTP_ENCRYPTION", "starttls").strip().lower()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body, subtype="plain", charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    logger.info("连接 SMTP %s:%s (%s)", host, port, enc)

    if enc == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as smtp:
            smtp.ehlo()
            if enc == "starttls":
                smtp.starttls()
                smtp.ehlo()
            if user or password:
                smtp.login(user, password)
            smtp.send_message(msg)

    logger.info("邮件已发送给: %s", recipients)


__all__ = [
    "tiered_result_to_html",
    "tiered_result_to_plain",
    "send_digest_email",
]
