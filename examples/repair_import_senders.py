"""一次性修复：给历史消息补上真实的发送人（原地更新，不删不插）。

## 背景

解密库（wcdb）路径上线**之前**，消息经旧界面遍历（pywechat/UIA）导入：
群成员名反推不出来时发送人写的是占位串「（发送人未识别）」，非文本消息的
内容还是英文占位（"Image" / "Video Download0:29"）。这些行以 ``import:``
开头的 ``source_id`` 留在库里，更新读取只往后追加、不回头修老行。

另外，``wcdb.read_messages`` 早期只对文本消息解发送人前缀 —— 图片/视频
XML 压缩帧里同样带 ``wxid:\\n`` 前缀（已修），但**修复之前入库的**那些行
发送人被归到了会话名，也要一并自愈。

## 做三件事（全部原地 UPDATE，消息 id 一个不动）

1. **内容匹配**：旧 import 行 ↔ 解密库同会话同类型同正文的消息（同文多条
   按时间就近）→ 补发送人和精确时间；
2. **时间匹配**：内容对不上的（多为非文本，旧行正文是英文占位）→ 同类型
   ±120 秒内**唯一**的解密库消息 → 只补发送人；
3. **自愈 wcdb 行**：已入库、但发送人还是会话名的 wcdb 行（读取端修复前
   入的）→ 按稳定 ``source_id`` 对回解密库，补真实发送人。

``parse_runs``（解析审计/计费）、外键、分析水位线引用的都是原 id，
一个都不用动；对不上的行原样保留。

跑法（先停掉正在跑的客户端，别和它抢写锁）::

    .venv/Scripts/python.exe -X utf8 examples/repair_import_senders.py          # 修
    .venv/Scripts/python.exe -X utf8 examples/repair_import_senders.py --check  # 只看能修多少
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.merchant_app import default_data_dir  # noqa: E402
from wechat_ai_assistant.orders import OrderStore, normalize_message_kind  # noqa: E402
from wechat_ai_assistant.utils import UNRESOLVED_SENDER  # noqa: E402
from wechat_ai_assistant.wechat_time import parse_timestamp  # noqa: E402

#: 一次读多少条来配对。旧行最多的群远小于这个数。
READ_LIMIT = 2000
#: 时间匹配的容差（秒）：旧导入的时间精确到分钟，同分钟多条同类型时
#: 只在窗口内**唯一**才认。
TIME_WINDOW = 120.0


def _norm(text: object) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _sent_at(timestamp: int) -> str:
    try:
        return datetime.fromtimestamp(int(timestamp)).astimezone().strftime(
            "%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _sender_of(message: object, group: str) -> str:
    return (getattr(message, "sender_display", "")
            or getattr(message, "sender_username", "")
            or group)


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    from wechat_ai_assistant.WeChatTools import Tools
    from wechat_ai_assistant.wcdb import WcdbReader

    data_dir = Path(default_data_dir())
    folder = Tools.where_wxid_folder()
    if not folder:
        print("微信还没登录，拿不到本机数据库目录。先登录微信再跑。")
        return 1
    reader = WcdbReader(Path(folder) / "db_storage", data_dir / "wcdb_keys.json")
    if not reader.available:
        print(f"解密库不可用：{reader.error or '请先在软件里提取一次密钥'}")
        return 1

    by_name: dict[str, str] = {}
    for item in reader.list_conversations():
        by_name.setdefault(item.display or item.display_hint or item.username,
                           item.username)

    store = OrderStore(data_dir / "orders.db")
    db = store.db
    legacy = db.execute(
        "SELECT merchant_id, group_id, COUNT(*) AS n FROM messages"
        " WHERE source_id LIKE 'import:%' GROUP BY merchant_id, group_id"
        " ORDER BY n DESC").fetchall()
    shells = db.execute(
        "SELECT DISTINCT merchant_id, group_id FROM messages"
        " WHERE source_id LIKE 'wcdb-%' AND sender=group_id").fetchall()
    groups = {(row["merchant_id"], row["group_id"]) for row in legacy}
    groups |= {(row["merchant_id"], row["group_id"]) for row in shells}
    if not groups:
        print("没有要修的行（既无 import 旧行，也无归到会话名的 wcdb 行）。")
        return 0

    def _update(row_id: str, sender: str, sent: str | None, epoch: float | None) -> None:
        if check_only:
            return
        with db:
            if sent is None:
                db.execute("UPDATE messages SET sender=? WHERE id=?",
                           (sender, row_id))
            else:
                db.execute(
                    "UPDATE messages SET sender=?, sent_at=?, sent_at_epoch=?,"
                    " time_precision='exact' WHERE id=?", (sender, sent, epoch, row_id))

    grand = {"content": 0, "time": 0, "heal": 0}
    for tenant, group in sorted(groups):
        wxid = by_name.get(group)
        if not wxid:
            print(f"跳过「{group}」：解密库的会话列表里找不到它（改过名？）")
            continue
        messages = reader.read_conversation_messages(wxid, limit=READ_LIMIT)
        used: set[int] = set()

        # -- 1) 内容匹配 + 2) 时间匹配：只针对 import 旧行 -----------------
        old_rows = db.execute(
            "SELECT id, kind, content, sent_at FROM messages"
            " WHERE merchant_id=? AND group_id=? AND source_id LIKE 'import:%'",
            (tenant, group)).fetchall()
        by_key: dict[tuple[str, str], list[tuple[float, object]]] = {}
        by_time: dict[tuple[str, float], list[object]] = {}
        for message in messages:
            epoch = float(getattr(message, "timestamp", 0) or 0)
            kind = normalize_message_kind(getattr(message, "kind", ""))
            content = _norm(getattr(message, "content", ""))
            if content:
                by_key.setdefault((kind, content), []).append((epoch, message))
            if epoch:
                by_time.setdefault((kind, epoch), []).append(message)

        fixed_content = fixed_time = 0
        for old in old_rows:
            near = None
            parsed = parse_timestamp(str(old["sent_at"] or ""))
            if parsed is not None:
                try:
                    near = float(parsed.timestamp())
                except (OverflowError, OSError, ValueError):
                    near = None
            # 1) 同类型同正文（同文多条按时间就近，认过的不再认）
            match = None
            rows = by_key.get((str(old["kind"]), _norm(old["content"])), [])
            for epoch, message in sorted(rows, key=lambda kv: kv[0]):
                if id(message) in used:
                    continue
                if match is None or (near is not None
                                     and abs(epoch - near) < abs(
                                         getattr(match, "_epoch", 0) - near)):
                    message._epoch = epoch  # type: ignore[attr-defined]
                    match = message
            if match is None and near is not None:
                # 2) 同类型 ±窗口内匹配（旧行正文是英文占位时靠时间）：
                #    窗口里只有一条 → 认它；多条但**发送人一致**（连发图片/
                #    视频就是同一人）→ 也认；几个人挤在同一分钟就跳过，不猜。
                kind = str(old["kind"])
                window = [m for (k, e), ms in by_time.items()
                          if k == kind and abs(e - near) <= TIME_WINDOW for m in ms]
                senders_in_window = {_sender_of(m, group) for m in window}
                if len(senders_in_window) == 1:
                    match = min(window, key=lambda m: abs(
                        float(getattr(m, "timestamp", 0) or 0) - near))
                    match._matched_by = "time"  # type: ignore[attr-defined]
            if match is None:
                continue
            used.add(id(match))
            sender = _sender_of(match, group)
            epoch = float(getattr(match, "timestamp", 0) or 0) or None
            _update(old["id"], sender, _sent_at(getattr(match, "timestamp", 0)), epoch)
            if getattr(match, "_matched_by", "content") == "time":
                fixed_time += 1
            else:
                fixed_content += 1
        grand["content"] += fixed_content
        grand["time"] += fixed_time

        # -- 3) 自愈：发送人还是会话名的 wcdb 行 ---------------------------
        by_source = {str(getattr(m, "source_id", "") or ""): m for m in messages}
        shell_rows = db.execute(
            "SELECT id, source_id FROM messages"
            " WHERE merchant_id=? AND group_id=? AND source_id LIKE 'wcdb-%'"
            " AND sender=?", (tenant, group, group)).fetchall()
        healed = 0
        for row in shell_rows:
            match = by_source.get(str(row["source_id"] or ""))
            if match is None:
                continue
            sender = _sender_of(match, group)
            if sender == group:
                continue
            _update(row["id"], sender, None, None)
            healed += 1
        grand["heal"] += healed

        unresolved = int(db.execute(
            "SELECT COUNT(*) FROM messages WHERE merchant_id=? AND group_id=?"
            " AND sender=?", (tenant, group, UNRESOLVED_SENDER)).fetchone()[0])
        verb = "可修" if check_only else "已修"
        print(f"「{group}」：{verb} 内容匹配 {fixed_content} + 时间匹配 {fixed_time}"
              f"（import 旧行 {len(old_rows)}），自愈 wcdb 行 {healed}；"
              f"剩余未识别 {unresolved}")

    print(f"合计：内容匹配 {grand['content']}，时间匹配 {grand['time']}，"
          f"自愈 {grand['heal']}。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
