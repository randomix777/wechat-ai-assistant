"""发送人来源路由的回归测试：界面链移除后 avatar/members 各走哪。

背景：以前群聊走 ``Messages.dump_senders_via_avatar`` 逐条点头像，
又慢又抢焦点，已删除。现在 avatar 按 localdb（内存）走，
members 只剩"注入的读取器"含义（测试替身用），生产默认 localdb。

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


class AvatarRoutingTests(unittest.TestCase):
    def test_default_is_localdb(self):
        reader = HistoryReader(_reader([]))
        self.assertEqual(reader._sender_source, "localdb")

    def test_avatar_mode_group_falls_back_to_localdb(self):
        """avatar 已移除：群聊按内存路走，不再点头像。"""
        reader = HistoryReader(
            _reader([]),
            group_members=lambda conv: ["张老板"],
            sender_source="avatar",
        )
        sentinel = [{"消息发送人": "x", "来源": "localdb"}]
        with patch.object(HistoryReader, "_read_via_localdb",
                          return_value=list(sentinel)) as local:
            got = reader._read_one(_group(), 50)
        self.assertEqual(local.call_count, 1)
        self.assertEqual(got, sentinel)

    def test_avatar_mode_private_chat_needs_wcdb_key(self):
        """私聊没有内存形态：没钥匙就明说，不再回落界面链。"""
        reader = HistoryReader(_reader([]), sender_source="avatar")
        with self.assertRaises(RuntimeError):
            reader._read_one(_friend(), 50)

    def test_members_mode_unchanged(self):
        rows = [{"消息发送人": "张老板", "消息内容": "两箱",
                 "消息类型": "文本", "消息发送时间": "2026-10-2 12:21"}]
        dump = _reader(rows)
        reader = HistoryReader(
            dump, group_members=lambda conv: ["张老板"], sender_source="members",
        )
        with patch("wechat_ai_assistant.WeChatAuto.Messages.dump_senders_via_avatar") as avatar:
            got = reader._read_one(_group(), 50)
        self.assertEqual(avatar.call_count, 0)
        self.assertEqual(len(dump.calls), 1)
        self.assertEqual(dump.calls[0]["groupMembers"], ["张老板"])
        self.assertEqual(got, rows)

    def test_unknown_sender_source_falls_back_to_localdb(self):
        dump = _reader([])
        reader = HistoryReader(dump, group_members=lambda conv: [], sender_source="whatever")
        self.assertEqual(reader._sender_source, "localdb")
        sentinel = [{"消息发送人": "x", "来源": "localdb"}]
        with patch.object(HistoryReader, "_read_via_localdb",
                          return_value=list(sentinel)) as local:
            got = reader._read_one(_group(), 5)
        self.assertEqual(local.call_count, 1)
        self.assertEqual(got, sentinel)
        self.assertEqual(dump.calls, [], "注入的 members 路不该再跑")


class AvatarCardCopyTests(unittest.TestCase):
    """右键复制兜底：卡片文本树为空时，从剪贴板拿。纯假对象，不碰界面。"""

    class FakeCopyItem:
        def __init__(self, exists=True):
            self._exists = exists
            self.clicked = False

        def exists(self, timeout=None):
            return self._exists

        def click_input(self):
            self.clicked = True

    class FakeProfile:
        def __init__(self, exists=True):
            self.copy_item = AvatarCardCopyTests.FakeCopyItem(exists)
            self.right_clicked = False

        def right_click_input(self):
            self.right_clicked = True

        def child_window(self, **kwargs):
            return self.copy_item

    def _clipboard(self, reads):
        import wechat_ai_assistant.utils as utils_mod

        writes = []
        return (
            patch.object(utils_mod.SystemSettings, "get_copied_text", side_effect=list(reads)),
            patch.object(utils_mod.SystemSettings, "copy_text_to_clipboard",
                         side_effect=lambda text: writes.append(text)),
            writes,
        )

    def test_copy_success_returns_text_and_restores_clipboard(self):
        import wechat_ai_assistant.utils as utils_mod

        profile = self.FakeProfile()
        get_patch, set_patch, writes = self._clipboard(["旧剪贴板", "张老板\n微信号：wxid_1"])
        with get_patch, set_patch:
            got = utils_mod._copy_avatar_card_text(profile)
        self.assertTrue(profile.right_clicked)
        self.assertTrue(profile.copy_item.clicked)
        self.assertIn("张老板", got)
        self.assertEqual(writes[-1], "旧剪贴板")

    def test_no_copy_menu_raises(self):
        import wechat_ai_assistant.utils as utils_mod

        profile = self.FakeProfile(exists=False)
        get_patch, set_patch, writes = self._clipboard(["旧剪贴板"])
        with get_patch, set_patch:
            with self.assertRaises(ValueError):
                utils_mod._copy_avatar_card_text(profile)
        self.assertEqual(writes[-1], "旧剪贴板")

    def test_empty_clipboard_after_copy_raises(self):
        import wechat_ai_assistant.utils as utils_mod

        profile = self.FakeProfile()
        get_patch, set_patch, writes = self._clipboard(["旧剪贴板", "   "])
        with get_patch, set_patch:
            with self.assertRaises(ValueError):
                utils_mod._copy_avatar_card_text(profile)
        self.assertEqual(writes[-1], "旧剪贴板")


if __name__ == "__main__":
    unittest.main()
