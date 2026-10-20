"""localdb 纯函数单测：合成字节，不碰内存不碰文件不联网。"""
from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant import localdb  # noqa: E402
from wechat_ai_assistant.localdb import (  # noqa: E402
    find_record_before,
    parse_record,
    read_varint,
)


def _record_bytes():
    """int(1791828020) + text('hi') + text('6255144087@chatroom', 19 字节)"""
    group = b"6255144087@chatroom"
    assert len(group) == 19
    body = (1791828020).to_bytes(4, "big") + b"hi" + group
    header = bytes([4, 4, 17, 13 + 2 * len(group)])
    assert header[0] == len(header)
    return header + body


class VarintTests(unittest.TestCase):
    def test_single_byte(self):
        self.assertEqual(read_varint(b"\x7fREST", 0), (127, 1))

    def test_two_bytes(self):
        self.assertEqual(read_varint(bytes([0x81, 0x00]), 0), (128, 2))

    def test_truncated_is_none(self):
        self.assertEqual(read_varint(b"\x81", 0), (None, 0))
        self.assertEqual(read_varint(b"", 0), (None, 0))


class ParseRecordTests(unittest.TestCase):
    def test_fields_come_back_typed(self):
        fields = parse_record(_record_bytes(), 0)
        self.assertEqual(fields, [
            ("int", 1791828020),
            ("text", "hi"),
            ("text", "6255144087@chatroom"),
        ])

    def test_garbage_is_none(self):
        self.assertIsNone(parse_record(b"\x00\x01\x02", 0))
        self.assertIsNone(parse_record(b"\xff" * 200, 0))

    def test_bad_utf8_is_none(self):
        header = bytes([3, 17, 17])
        self.assertIsNone(parse_record(header + b"\xff\xfeAB", 0))


class FindRecordBeforeTests(unittest.TestCase):
    def test_anchor_claims_its_record(self):
        record = _record_bytes()
        chunk = b"\x00" * 100 + record + b"\x00" * 100
        anchor_pos = 100 + 4 + 4 + 2 + 5
        found = find_record_before(chunk, anchor_pos)
        self.assertIsNotNone(found)
        start, fields = found
        self.assertEqual(start, 100)
        self.assertTrue(any(value == "6255144087@chatroom" for _, value in fields))

    def test_outside_span_is_not_claimed(self):
        record = _record_bytes()
        chunk = record + b"\x00" * 100
        self.assertIsNone(find_record_before(chunk, len(record) + 50))

    def test_header_bytes_do_not_claim(self):
        record = _record_bytes()
        self.assertIsNone(find_record_before(record, 1))


class MessageFromFieldsTests(unittest.TestCase):
    def test_sender_content_timestamp(self):
        message = localdb._message_from_fields("6255144087@chatroom", [
            ("text", "6255144087@chatroom"),
            ("text", "wxid_abc"),
            ("text", "两箱苹果"),
            ("int", 1791828020),
        ])
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.sender_wxid, "wxid_abc")
        self.assertEqual(message.content, "两箱苹果")
        self.assertEqual(message.timestamp, 1791828020)

    def test_millis_are_normalized_to_seconds(self):
        message = localdb._message_from_fields("g@chatroom", [
            ("text", "两箱"), ("int", 1791828020000),
        ])
        assert message is not None
        self.assertEqual(message.timestamp, 1791828020)

    def test_no_text_is_no_message(self):
        self.assertIsNone(localdb._message_from_fields("g", [("int", 1)]))

    def test_comma_digit_debris_is_dropped(self):
        fields = [("text", "6255144087@chatroom"), ("text", ",0,0,0,,0,1,0,0,0,0,0"),
                  ("int", 1791828020)]
        self.assertIsNone(localdb._message_from_fields("6255144087@chatroom", fields))

    def test_english_with_attribution_is_kept(self):
        fields = [("text", "wxid_abc"), ("text", "OK got it"), ("int", 1791828020)]
        message = localdb._message_from_fields("g@chatroom", fields)
        assert message is not None
        self.assertEqual(message.content, "OK got it")

    def test_search_snippet_rows_are_dropped(self):
        self.assertIsNone(localdb._message_from_fields("g@chatroom", [
            ("text", "91897306807,水果群"), ("int", 1791828020)]))
        self.assertIsNone(localdb._message_from_fields("g@chatroom", [
            ("text", '果群,{"record_tab_type":0}'), ("int", 1791828020)]))
        self.assertIsNone(localdb._message_from_fields("g@chatroom", [
            ("text", "enan 乐楠]]></nickname></member>"), ("int", 1791828020)]))

    def test_xml_sender_is_cleared_not_kept(self):
        message = localdb._message_from_fields("g@chatroom", [
            ("text", "TA[Lenan 乐楠]]></nickname></member>"),
            ("text", "@Erica 订单来了"), ("int", 1791828020)])
        assert message is not None
        self.assertNotIn("</", message.sender_wxid)
        self.assertIn("订单来了", message.content)

    def test_text_timestamp_is_recognized(self):
        fields = [("text", "wxid_abc"), ("text", "两箱苹果"),
                  ("text", "2026-10-12 15:20")]
        message = localdb._message_from_fields("g@chatroom", fields)
        assert message is not None
        self.assertTrue(1759000000 <= message.timestamp <= 1793000000)

    def test_short_text_timestamp_is_recognized(self):
        fields = [("text", "wxid_abc"), ("text", "12:30 到付"),
                  ("text", "10-12 15:20")]
        message = localdb._message_from_fields("g@chatroom", fields)
        assert message is not None
        self.assertTrue(message.timestamp > 0)

    def test_system_notice_is_typed(self):
        fields = [("text", "6255144087@chatroom"),
                  ("text", "Daniel invited you and A-xxx to the group"),
                  ("int", 1791828020)]
        message = localdb._message_from_fields("6255144087@chatroom", fields)
        assert message is not None
        self.assertEqual(message.kind, "系统通知")

    def test_recall_is_typed(self):
        fields = [("text", "张姐撤回了一条消息"), ("int", 1791828020)]
        message = localdb._message_from_fields("g@chatroom", fields)
        assert message is not None
        self.assertEqual(message.kind, "系统通知")

    def test_normal_text_stays_text(self):
        fields = [("text", "wxid_abc"), ("text", "两箱苹果"), ("int", 1791828020)]
        message = localdb._message_from_fields("g@chatroom", fields)
        assert message is not None
        self.assertEqual(message.kind, "文本")

    def test_is_system_notice(self):
        self.assertTrue(localdb.is_system_notice("A invited you and B to the group"))
        self.assertTrue(localdb.is_system_notice("李总修改群名为水果2群"))
        self.assertFalse(localdb.is_system_notice("两箱苹果"))
        self.assertFalse(localdb.is_system_notice(""))


def _contact_record_bytes():
    """wxid + 昵称 + 群名备注，用于测双向解析。"""
    wxid = b"6255144087@chatroom"
    nick = "张老板".encode("utf-8")
    group = "水果群".encode("utf-8")
    body = wxid + nick + group
    header = bytes([4, 13 + 2 * len(wxid), 13 + 2 * len(nick), 13 + 2 * len(group)])
    assert header[0] == len(header)
    return header + body


class ResolveTests(unittest.TestCase):
    def _chunks(self, *blobs):
        return [(0, blob) for blob in blobs]

    def _patched(self, chunks):
        from unittest.mock import patch
        return (
            patch.object(localdb, "_weixin_pids", return_value=[123]),
            patch.object(localdb, "_open_process", return_value=object()),
            patch.object(localdb, "_iter_regions", return_value=self._chunks(*chunks)),
        )

    def test_resolve_group_wxid(self):
        chunk = b"\x00" * 64 + _contact_record_bytes() + b"\x00" * 64
        p1, p2, p3 = self._patched([chunk])
        with p1, p2, p3:
            self.assertEqual(localdb.resolve_group_wxid("水果群"), "6255144087@chatroom")

    def test_resolve_group_wxid_missing_falls_back(self):
        p1, p2, p3 = self._patched([b"\x00" * 128])
        with p1, p2, p3:
            self.assertIsNone(localdb.resolve_group_wxid("不存在的群"))

    def test_resolve_contact_name_prefers_cjk(self):
        chunk = b"\x00" * 64 + _contact_record_bytes() + b"\x00" * 64
        p1, p2, p3 = self._patched([chunk])
        with p1, p2, p3:
            name = localdb.resolve_contact_name("6255144087@chatroom")
        self.assertIn(name, ("张老板", "水果群"))


class VoteNameTests(unittest.TestCase):

    def test_majority_wins_over_long_content(self):
        votes = ["水果群", "明天团购苹果汁汽水大瓶装", "水果群",
                 "后天还有西瓜吗", "水果群"]
        self.assertEqual(localdb._vote_name(votes), "水果群")

    def test_tie_goes_to_shorter(self):
        self.assertEqual(localdb._vote_name(["张老板", "水果群"]), "张老板")

    def test_overlong_strings_are_not_names(self):
        self.assertEqual(localdb._vote_name(["这" * 50]), "")
        self.assertEqual(localdb._vote_name([]), "")
        self.assertEqual(localdb._vote_name(["", "  "]), "")

    def test_history_reader_localdb_routing(self):
        from unittest.mock import patch

        from wechat_ai_assistant.conversations import Conversation, ConversationKind
        from wechat_ai_assistant.history_read import HistoryReader

        dump_calls = []

        def dump(**kwargs):
            dump_calls.append(kwargs)
            return []

        group = Conversation(name="水果群", kind=ConversationKind.GROUP)
        reader = HistoryReader(dump, group_members=lambda conv: [], sender_source="localdb")
        with patch.object(localdb, "resolve_group_wxid", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                reader._read_one(group, 10)
        self.assertIn("重新提取密钥", str(ctx.exception), "解不出 wxid 要给可执行的下一步")
        self.assertEqual(dump_calls, [], "界面链已移除，不许再回落开窗读取")

    def test_localdb_source_is_retired_and_reads_the_decrypted_db(self):
        """显式传 localdb 也不再扫进程内存 —— 内存 carve 已退役，按解密库走。

        反转记录：这条原来断言"localdb 来源把内存里捡到的记录适配成消息行"
        （发送人解备注名、来源标 localdb、稳定 id 由群+内容+时间算）。
        内存 carve 要对每个会话扫描微信进程的大块地址空间、读取延迟不可预测，
        现在只留解密库一条路；旧名字还认，但不再碰进程内存。
        """
        from unittest.mock import patch

        from wechat_ai_assistant.conversations import Conversation, ConversationKind
        from wechat_ai_assistant.history_read import HistoryReader

        group = Conversation(name="水果群", kind=ConversationKind.GROUP)
        reader = HistoryReader(lambda **kw: [], sender_source="localdb")
        self.assertEqual(reader._sender_source, "wcdb", "退役旧名按解密库走")
        message = localdb.LocalMessage(
            group_wxid="6255144087@chatroom", sender_wxid="wxid_abc",
            content="两箱苹果", timestamp=1791828020)
        with patch.object(localdb, "resolve_group_wxid", return_value="6255144087@chatroom"), \
             patch.object(localdb, "read_conversation", return_value=[message]) as read, \
             patch.object(localdb, "resolve_contact_name", return_value="张老板"):
            with self.assertRaises(RuntimeError) as ctx:
                reader._read_one(group, 10)
        self.assertIn("重新提取密钥", str(ctx.exception), "解不出就要给可执行的下一步")
        self.assertEqual(read.call_count, 0, "不许再扫进程内存")

    def test_read_passes_sender_source_per_call(self):
        from unittest.mock import patch

        from wechat_ai_assistant.conversations import Conversation, ConversationKind
        from wechat_ai_assistant.history_read import HistoryReader, TimeWindow

        group = Conversation(name="水果群", kind=ConversationKind.GROUP)
        reader = HistoryReader(lambda **kw: [], sender_source="members")
        seen = {}
        original = HistoryReader._read_one

        def spy(self, conversation, max_messages, on_progress=None, sender_source=""):
            seen["source"] = sender_source or self._sender_source
            return []

        with patch.object(HistoryReader, "_read_one", spy):
            import datetime
            window = TimeWindow(start_delta=datetime.timedelta(days=1), label="t")
            reader.read([group], window, merchant_id="m", max_messages=5,
                        mode="recent", sender_source="localdb")
        self.assertEqual(seen.get("source"), "localdb")

    def test_stable_source_id_contract_moved_to_the_decrypted_db(self):
        """"同一条重读、发送人变了 id 也不变"这条契约跟着内存路一起搬走。

        反转记录：这里原来用 localdb 的合成记录钉它（``source_id`` 由
        群+内容+时间算）。内存路退役后，id 由**解密库**按（服务端 id，或
        本地 id + 时间）算，发送人只影响发送人一列 —— 新的断言长在
        ``test_wcdb.py::test_stable_source_id_survives_a_sender_rename``，
        那是现在唯一真有的那条路。
        """
        from wechat_ai_assistant import wcdb

        self.assertTrue(callable(getattr(wcdb, "read_messages", None)),
                        "解密库读取还在，契约就有地方可钉")


class SenderUpgradeTests(unittest.TestCase):
    """localdb 重读：后认出来的名字合到同一条上，只升不降。"""

    def _store(self):
        from wechat_ai_assistant.orders import OrderStore

        store = OrderStore(":memory:")
        tenant = store.create_merchant("测试店")["merchant_id"]
        return store, tenant

    def _import(self, store, tenant, sender):
        return store.add_import(
            tenant, "水果群",
            [{"sender": sender, "content": "两箱苹果", "kind": "text",
              "sent_at": "2026-10-12 15:20",
              "source_id": "localdb-abc123", "stable_source_id": True}],
            "test-src")

    def test_reread_does_not_duplicate(self):
        store, tenant = self._store()
        try:
            self._import(store, tenant, "wxid_abc")
            result = self._import(store, tenant, "wxid_abc")
            self.assertEqual(result["inserted"], 0)
            rows = store.db.execute(
                "SELECT COUNT(*) n FROM messages WHERE merchant_id=?", (tenant,)).fetchone()
            self.assertEqual(rows["n"], 1)
        finally:
            store.close()

    def test_resolved_name_upgrades_wxid(self):
        store, tenant = self._store()
        try:
            self._import(store, tenant, "wxid_abc")
            self._import(store, tenant, "张老板")
            row = store.db.execute(
                "SELECT sender FROM messages WHERE merchant_id=?", (tenant,)).fetchone()
            self.assertEqual(row["sender"], "张老板")
        finally:
            store.close()

    def test_new_time_fills_missing_time(self):
        from wechat_ai_assistant.orders import OrderStore

        store = OrderStore(":memory:")
        try:
            tenant = store.create_merchant("时间店")["merchant_id"]
            store.add_import(
                tenant, "水果群",
                [{"sender": "张老板", "content": "两箱苹果", "kind": "text",
                  "sent_at": "", "source_id": "localdb-t1", "stable_source_id": True}],
                "s1")
            store.add_import(
                tenant, "水果群",
                [{"sender": "张老板", "content": "两箱苹果", "kind": "text",
                  "sent_at": "2026-10-12 15:20",
                  "source_id": "localdb-t1", "stable_source_id": True}],
                "s2")
            row = store.db.execute(
                "SELECT sent_at, sent_at_epoch FROM messages WHERE merchant_id=?",
                (tenant,)).fetchone()
            self.assertEqual(row["sent_at"], "2026-10-12 15:20")
            self.assertIsNotNone(row["sent_at_epoch"])
        finally:
            store.close()

    def test_existing_time_is_not_overwritten(self):
        from wechat_ai_assistant.orders import OrderStore

        store = OrderStore(":memory:")
        try:
            tenant = store.create_merchant("时间店")["merchant_id"]
            store.add_import(
                tenant, "水果群",
                [{"sender": "张老板", "content": "两箱苹果", "kind": "text",
                  "sent_at": "2026-10-12 15:20",
                  "source_id": "localdb-t1", "stable_source_id": True}],
                "s1")
            store.add_import(
                tenant, "水果群",
                [{"sender": "张老板", "content": "两箱苹果", "kind": "text",
                  "sent_at": "2026-10-11 10:00",
                  "source_id": "localdb-t1", "stable_source_id": True}],
                "s2")
            row = store.db.execute(
                "SELECT sent_at FROM messages WHERE merchant_id=?", (tenant,)).fetchone()
            self.assertEqual(row["sent_at"], "2026-10-12 15:20")
        finally:
            store.close()

    def test_upgrade_never_downgrades(self):
        from wechat_ai_assistant.orders import _sender_needs_upgrade

        self.assertFalse(_sender_needs_upgrade("张老板", "wxid_abc"))
        self.assertFalse(_sender_needs_upgrade("张老板", "张老板"))
        self.assertFalse(_sender_needs_upgrade("", ""))
        self.assertTrue(_sender_needs_upgrade("", "张老板"))
        self.assertTrue(_sender_needs_upgrade("wxid_abc", "张老板"))
        self.assertTrue(_sender_needs_upgrade("（发送人未识别）", "张老板"))
        self.assertFalse(_sender_needs_upgrade("wxid_abc", "TA[x]]></member>"))


class GroupListTests(unittest.TestCase):
    """内存群列表：wxid + 备注名一次扫出来，UIA 死了也能列会话。"""

    def test_list_maps_names(self):
        chunk = b"\x00" * 64 + _contact_record_bytes() + b"\x00" * 64
        with unittest.mock.patch.object(
                localdb, "_weixin_pids", return_value=[123]), \
             unittest.mock.patch.object(
                localdb, "_open_process", return_value=object()), \
             unittest.mock.patch.object(
                localdb, "_iter_regions", return_value=[(0, chunk)]):
            got = localdb.list_group_conversations()
        self.assertEqual(got, [("张老板", "6255144087@chatroom")])

    def test_memory_first_ui_fallback(self):
        from unittest.mock import patch

        from wechat_ai_assistant.conversations import ConversationKind, RealConversationSource

        source = RealConversationSource(ui_lock=None)
        with patch.object(localdb, "list_group_conversations",
                          return_value=[("水果群", "g@chatroom")]):
            with patch("wechat_ai_assistant.WeChatAuto.Contacts.get_recent_groups",
                       side_effect=AssertionError("内存命中时不该碰界面")):
                groups = source.list_conversations(ConversationKind.GROUP)
        self.assertEqual([(c.name, c.wxid) for c in groups], [("水果群", "g@chatroom")])

    def test_memory_miss_does_not_touch_the_wechat_window(self):
        """内存捡不到就如实返回空：界面链已移除，绝不回落开窗翻通讯录。"""
        from unittest.mock import patch

        from wechat_ai_assistant.conversations import ConversationKind, RealConversationSource

        source = RealConversationSource(ui_lock=None)
        with patch.object(localdb, "list_group_conversations", return_value=[]):
            with patch("wechat_ai_assistant.WeChatAuto.Contacts.get_recent_groups",
                       side_effect=AssertionError("界面链已移除，不许碰微信窗口")):
                groups = source.list_conversations(ConversationKind.GROUP)
        self.assertEqual(list(groups), [], "捡不到就是捡不到，不能编")


class GroupTitleTests(unittest.TestCase):
    """面板标题里的群名：内存解不出的群靠它补齐。"""

    def test_chinese_title(self):
        # 带引号的（"水果群"的）和不带引号的（水果群的）都认。
        self.assertEqual(localdb.extract_group_name_from_title("水果群的"), "水果群")
        self.assertEqual(
            localdb.extract_group_name_from_title('\"水果群\"的'), "水果群")

    def test_english_title(self):
        self.assertEqual(
            localdb.extract_group_name_from_title('Chat history of "Fruit Group"'),
            "Fruit Group")

    def test_garbage_returns_empty(self):
        for text in ("", "   ", "微信", None, "与xxx的聊天"):
            with self.subTest(text=text):
                self.assertEqual(localdb.extract_group_name_from_title(text), "")

    def test_reader_no_longer_takes_an_alias_recorder(self):
        """抓面板标题解群名的旧挂钩已随界面链移除，不许再加回 HistoryReader。"""
        import inspect

        from wechat_ai_assistant.history_read import HistoryReader

        self.assertNotIn("alias_recorder",
                         inspect.signature(HistoryReader.__init__).parameters)


if __name__ == "__main__":
    unittest.main()
