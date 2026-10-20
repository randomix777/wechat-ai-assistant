"""导出：把这次读到的消息（含图片/语音…）和 AI 报告打成一个 zip。

## 为什么单独一层

打包本身在 ``export_bundle.py``（纯逻辑，早就有）。这里补的是**挑哪些消息、
能不能导**——这两件事原来写在 Tk 窗口里（已删），而它们跟界面无关：

* 只导**跑过 AI 的**消息：没整理的导出去等于绕开整理；
* 被跳过的条数要能说出来（"还有 3 条没整理，只导了 87 条"），
  不能默默少导然后让商户以为全都在。

Web 那边只负责把字节流出去、把名字给浏览器。
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable

from . import summary_task
from .export_bundle import build_export_bundle
from .merchant_text import display_content

#: 核对/导出最多看多少条。与 Tk 时代一致：200 条足够看清这次读了什么，
#: 再多界面也不好翻。
REVIEW_LIMIT = 200


def review_rows(app: Any, limit: int = REVIEW_LIMIT) -> list[dict[str, Any]]:
    """当前所选会话的最近消息（核对页和导出共用同一份）。"""
    store = getattr(app, "orders", None)
    tenant = getattr(app, "tenant", "")
    wizard = getattr(app, "wizard", None)
    selected = getattr(getattr(wizard, "conversations", None), "history_selected", None) or ()
    groups = [str(getattr(item, "name", "") or "") for item in selected]
    groups = [name for name in groups if name]
    if store is None or not groups:
        return []
    try:
        return list(store.recent_messages(tenant, groups, limit) or [])
    except Exception:
        return []


def exportable(app: Any, rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """挑出**跑过 AI 的**消息，返回 ``(可导出行, 被跳过条数)``。

    判定两个来源都认：逐条记账（``processed_message_ids``）和会话水位线
    （``analyzed_group_ids``）—— 只认前者时，跑过一轮摘要、但没逐条记的会话
    会被整组当成"没整理过"。
    """
    store = getattr(app, "orders", None)
    tenant = getattr(app, "tenant", "")
    try:
        processed = set(store.processed_message_ids(tenant)) if store is not None else set()
    except Exception:
        processed = set()
    try:
        analyzed_groups = summary_task.analyzed_group_ids(app)
    except Exception:
        analyzed_groups = set()

    kept: list[dict[str, Any]] = []
    skipped = 0
    for row in rows or ():
        item = dict(row or {})
        mid = str(item.get("id", "") or "")
        group = str(item.get("group", "") or "")
        if mid not in processed and group not in analyzed_groups:
            skipped += 1
            continue
        # 图片/语音这类没文字的，导出时给占位，而不是一个空格子。
        item["content"] = display_content(item.get("kind", ""), item.get("content", ""))
        kept.append(item)
    return kept, skipped


def filename(rows: list[dict[str, Any]], *, ext: str = "zip") -> str:
    """建议的文件名：**群-日期-条数**，一眼能对上这次导的是什么。"""
    groups: list[str] = []
    for row in rows or ():
        group = str((row or {}).get("group", "") or "").strip()
        if group and group not in groups:
            groups.append(group)
    if len(groups) == 1:
        scope = _part(groups[0], 24) or "会话"
    elif groups:
        scope = f"{_part(groups[0], 16) or '会话'}等{len(groups)}个会话"
    else:
        scope = "会话"
    stamp = time.strftime("%Y%m%d_%H%M")
    return f"微信AI智能助理-{scope}-{len(rows or [])}条-{stamp}.{ext}"


def build(app: Any, *, analysis_report: str = "",
          role_name: str = "", limit: int = REVIEW_LIMIT) -> dict[str, Any]:
    """打一个包。返回 ``{data, info, rows, skipped, filename}``。

    没有可导的消息时 ``data`` 为空、``reason`` 说清为什么（给界面直接用）。
    """
    rows = review_rows(app, limit)
    kept, skipped = exportable(app, rows)
    if not rows:
        return {"data": b"", "info": {}, "rows": 0, "skipped": 0, "filename": "",
                "reason": "还没有读到消息，先读取再导出。"}
    if not kept:
        return {"data": b"", "info": {}, "rows": 0, "skipped": skipped, "filename": "",
                "reason": "这些消息还没整理过，导出去等于绕开整理。"
                          "请先在「智能整理」里跑一遍。"}

    store = getattr(app, "orders", None)
    tenant = getattr(app, "tenant", "")
    media_by_id: dict[str, Any] = {}
    if store is not None:
        try:
            media_by_id = store.messages_media(tenant, [row.get("id") for row in kept])
        except Exception:
            media_by_id = {}

    data, info = build_export_bundle(
        kept, media_by_id,
        analysis_report=str(analysis_report or "").strip(),
        analysis_role=str(role_name or ""),
    )
    return {"data": data, "info": dict(info or {}), "rows": len(kept), "skipped": skipped,
            "filename": filename(kept), "reason": ""}


def _part(text: Any, limit: int) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(text or "")).strip()
    return cleaned[:limit]


__all__ = ["REVIEW_LIMIT", "build", "exportable", "filename", "review_rows"]
