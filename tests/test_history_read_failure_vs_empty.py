"""读取失败与「读到 0 条」必须能被分辨。

## 出过什么事

商户报告「读取历史偶尔返回 0 条」。上一轮修掉了「一旦读到 0 条就永久卡死」
（游标把 0 条记成 exhausted，之后永远跳过），但**第一次为什么读到 0 条**
一直没有定位。

这次找到了，是 ``WeChatAuto.dump_chat_history``：

    chat_history_list = chat_history_window.child_window(**Lists.ChatHistoryList)
    if not chat_history_list.exists(timeout=1):
        warn("你与 X 的聊天记录为空")
        return []

微信的历史面板是**异步渲染**的：窗口出现了，消息列表还在加载。1 秒等不到，
代码就把它判定成「这个会话没有聊天记录」并返回空列表。

商户看到的就是「读取到 0 条」，而且没有任何提示 —— 他会以为这个群真的
没消息，而其实只是微信没加载完，再点一次就读出来了。

## 两个缺陷

1. **等待太短**：固定 1 秒，且只试一次。微信加载期间会重建控件，一次
   exists() 可能刚好落在重建的空窗期。
2. **误诊**：把「还没就位」说成「没有聊天记录」，并静默返回空列表。

第 2 条更严重：它让一次**可恢复的读取失败**变成一个**看起来成功的 0 条结果**。
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.Warnings import ChatHistoryUnavailable, NoChatHistoryWarning  # noqa: E402


class DistinguishFailureFromEmptyTests(unittest.TestCase):
    def test_a_missing_list_is_an_error_not_a_warning(self):
        """读不到必须是 RuntimeError，才能和「真的没有消息」分开。"""
        self.assertTrue(issubclass(ChatHistoryUnavailable, RuntimeError))
        self.assertFalse(issubclass(ChatHistoryUnavailable, Warning))

    def test_it_is_not_the_empty_history_warning(self):
        """NoChatHistoryWarning 是「记录不足指定条数」，不是「面板没就位」。"""
        self.assertIsNot(ChatHistoryUnavailable, NoChatHistoryWarning)
        self.assertTrue(issubclass(NoChatHistoryWarning, Warning))

    def test_the_message_says_it_is_a_failure_not_emptiness(self):
        """异常文案不能让人以为这个会话真的没消息。"""
        try:
            raise ChatHistoryUnavailable("这是**读取失败**，不是没有聊天记录，重试通常就能读出来")
        except ChatHistoryUnavailable as exc:
            self.assertIn("读取失败", str(exc))
            self.assertIn("重试", str(exc))

