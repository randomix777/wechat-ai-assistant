"""发送人来源路由的回归测试：界面链与内存 carve 退役后各走哪。

背景：以前群聊走 ``Messages.dump_senders_via_avatar`` 逐条点头像，又慢又抢焦点，
已删除；后来群聊走 ``localdb`` 内存 carve（读微信进程内存里解密好的 SQLite 页），
也退役了 —— 它对每个会话扫描大块地址空间，读取延迟不可预测。现在生产只有
**解密库直读**一条路（要先提取密钥），``members`` 只剩"注入的读取器"含义
（测试替身用）。``avatar``/``localdb`` 两个旧名仍认，但一律按解密库走。

本文件只测路由（纯内存，不建微信窗口、不联网）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant.conversations import Conversation, ConversationKind  # noqa: E402
from wechat_ai_assistant.history_read import HistoryReader  # noqa: E402


def _group(name="测试群"):
    return Conversation(name=name, kind=ConversationKind.GROUP, member_count=10)


def _friend(name="张老板"):
    return Conversation(name=name, kind=ConversationKind.FRIEND)


def _reader(items):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return items

    fake.calls = calls
    return fake


AVATAR_RECORDS = [
    {"消息发送人": "苹果哥(张老板)", "昵称": "张老板", "备注": "苹果哥",
     "微信号": "wxid_1", "消息内容": "两箱苹果", "消息类型": "文本"},
]


class _AvailableWcdb:
    """一把"钥匙已经提到"的解密库替身：只用来让路由判定走解密库那条支。"""

    available = True

    def message_shards(self):
        return []

    def prepare(self, extra=()):
        return {}

    def cleanup(self, ready=None):
        pass


class AvatarRoutingTests(unittest.TestCase):
    def test_default_is_wcdb(self):
        reader = HistoryReader(_reader([]))
        self.assertEqual(reader._sender_source, "wcdb")

    def test_avatar_mode_is_retired_and_goes_to_the_decrypted_db(self):
        """avatar 已退役：群聊按解密库走，不再点头像、也不再扫进程内存。"""
        reader = HistoryReader(
            _reader([]),
            group_members=lambda conv: ["张老板"],
            sender_source="avatar",
            wcdb_reader_fn=_AvailableWcdb,
        )
        self.assertEqual(reader._sender_source, "wcdb", "退役旧名按解密库走")
        sentinel = [{"消息发送人": "x", "来源": "wcdb"}]
        with patch.object(HistoryReader, "_read_via_wcdb",
                          return_value=list(sentinel)) as wcdb:
            got = reader._read_one(_group(), 50)
        self.assertEqual(wcdb.call_count, 1)
        self.assertEqual(got, sentinel)
        self.assertFalse(hasattr(HistoryReader, "_read_via_localdb"),
                         "内存 carve 那条路必须真的删掉，不是改了名字")

    def test_avatar_mode_private_chat_needs_wcdb_key(self):
        """私聊没有别的形态：没钥匙就明说，不再回落界面链。"""
        reader = HistoryReader(_reader([]), sender_source="avatar")
        with self.assertRaises(RuntimeError) as ctx:
            reader._read_one(_friend(), 50)
        self.assertIn("重新提取密钥", str(ctx.exception), "要给可执行的下一步")

    def test_members_mode_unchanged(self):
        rows = [{"消息发送人": "张老板", "消息内容": "两箱",
                 "消息类型": "文本", "消息发送时间": "2026-10-2 12:21"}]
        dump = _reader(rows)
        reader = HistoryReader(
            dump, group_members=lambda conv: ["张老板"], sender_source="members",
        )
        got = reader._read_one(_group(), 50)
        self.assertEqual(len(dump.calls), 1)
        self.assertEqual(dump.calls[0]["groupMembers"], ["张老板"])
        self.assertEqual(got, rows)

    def test_unknown_sender_source_goes_to_the_decrypted_db(self):
        dump = _reader([])
        reader = HistoryReader(dump, group_members=lambda conv: [], sender_source="whatever",
                               wcdb_reader_fn=_AvailableWcdb)
        self.assertEqual(reader._sender_source, "wcdb", "认不出来的值按生产默认走")
        sentinel = [{"消息发送人": "x", "来源": "wcdb"}]
        with patch.object(HistoryReader, "_read_via_wcdb",
                          return_value=list(sentinel)) as wcdb:
            got = reader._read_one(_group(), 5)
        self.assertEqual(wcdb.call_count, 1)
        self.assertEqual(got, sentinel)
        self.assertEqual(dump.calls, [], "注入的 members 路不该再跑")

