"""发送人认不出来时，**必须说出来**。

## 实测发现的三件事

在一个 424 人的微信群里读到 107 条消息：

* 发送人 **100%** 是占位串
* 已绑定到客户的 = **0/107**
* 而那批消息里**红包 0 条** —— 全是「非常香，肉嫩好吃」这类普通聊天和图片

## 一、占位串原来在说谎

它写的是 **「红包或转账(无法获取发送人)」**。

商户看到「红包或转账」会以为这批消息全是红包，于是得出一个**完全错误**的结论，
根本不会想到去查「为什么发送人没认出来」。

**一条错误的说法比没有更糟** —— 现在写「（发送人未识别）」。

## 二、0% 识别被当成成功报出去

「读到 107 条」和「读到 107 条但每条发送人都是占位符」在界面上**曾经一模一样**。
商户以为成功了，转头去查一个不存在的问题。

所以 ``ConversationCoverage`` 必须带 ``unresolved_senders``，两个界面都要显示。

## 三、历史数据里还留着旧串

已经落库的消息带的是旧占位串。判断「认出来没有」时新旧都要认，
否则修完代码，历史数据会被误判成「发送人叫『红包或转账(无法获取发送人)』」。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant.history_read import ConversationCoverage  # noqa: E402
from wechat_ai_assistant.utils import (LEGACY_UNRESOLVED_SENDER, UNRESOLVED_SENDER,  # noqa: E402
                            UNRESOLVED_TIMESTAMP, sender_is_resolved)


class PlaceholderTellsTheTruthTests(unittest.TestCase):
    def test_it_does_not_claim_the_message_was_a_red_packet(self):
        """那批消息里红包 0 条。声称是红包 = 让商户得出错误结论。"""
        self.assertNotIn("\u7ea2\u5305", UNRESOLVED_SENDER)
        self.assertNotIn("\u8f6c\u8d26", UNRESOLVED_SENDER)
        self.assertNotIn("\u7ea2\u5305", UNRESOLVED_TIMESTAMP)

    def test_it_says_what_is_actually_wrong(self):
        self.assertIn("\u53d1\u9001\u4eba", UNRESOLVED_SENDER)
        self.assertIn("\u672a\u8bc6\u522b", UNRESOLVED_SENDER)

    def test_the_old_string_is_kept_only_for_reading_history(self):
        """\u5df2\u843d\u5e93\u7684\u6d88\u606f\u5e26\u7684\u662f\u65e7\u4e32 \u2014\u2014 \u5224\u65ad\u65f6\u5fc5\u987b\u4e5f\u8ba4\u5b83\u3002"""
        self.assertEqual(LEGACY_UNRESOLVED_SENDER, "\u7ea2\u5305\u6216\u8f6c\u8d26(\u65e0\u6cd5\u83b7\u53d6\u53d1\u9001\u4eba)")

    def test_the_source_no_longer_writes_the_old_string(self):
        """\u5199\u5165\u8def\u5f84\u4e0d\u5f97\u518d\u51fa\u73b0\u65e7\u4e32\uff08\u9664\u4e86\u5b9a\u4e49\u5b83\u7684\u5e38\u91cf\uff09\u3002"""
        source = (ROOT / "src/wechat_ai_assistant/utils.py").read_text(encoding="utf-8")
        lines = source.splitlines()
        offenders = [f"L{i}: {l.strip()[:70]}" for i, l in enumerate(lines, 1)
                     if ("\u7ea2\u5305\u6216\u8f6c\u8d26(\u65e0\u6cd5\u83b7\u53d6" in l
                         or "\u7ea2\u5305\u6216\u8f6c\u8d26(\u65e0\u6cd5\u83b7\u53d6" in l)
                     and "LEGACY_UNRESOLVED" not in l and not l.strip().startswith("#")]
        self.assertEqual(offenders, [], "\u8fd8\u6709\u5730\u65b9\u5199\u65e7\u5360\u4f4d\u4e32")


class SenderResolvedTests(unittest.TestCase):
    def test_a_real_name_counts(self):
        self.assertTrue(sender_is_resolved("\u5f20\u59d0"))

    def test_the_placeholder_does_not(self):
        self.assertFalse(sender_is_resolved(UNRESOLVED_SENDER))

    def test_empty_does_not(self):
        self.assertFalse(sender_is_resolved(""))
        self.assertFalse(sender_is_resolved(None))
        self.assertFalse(sender_is_resolved("   "))

    def test_the_old_placeholder_does_not_either(self):
        """\u4fee\u4e86\u5b57\u7b26\uff0c\u5386\u53f2\u6570\u636e\u4e0d\u80fd\u88ab\u8bef\u5224\u6210\u300c\u53d1\u9001\u4eba\u53eb\u8fd9\u4e2a\u540d\u5b57\u300d\u3002"""
        self.assertFalse(sender_is_resolved(LEGACY_UNRESOLVED_SENDER))


class CoverageCarriesTheSignalTests(unittest.TestCase):
    """\u4e0d\u8bb0\u4e0b\u6765\u5c31\u6ca1\u4e86 \u2014\u2014 \u754c\u9762\u4e0a\u770b\u4e0d\u51fa\u5dee\u5f02\u3002"""

    def test_the_field_exists_and_defaults_to_zero(self):
        coverage = ConversationCoverage(conversation=None)
        self.assertEqual(coverage.unresolved_senders, 0)

    def test_the_reader_counts_it(self):
        source = (ROOT / "src/wechat_ai_assistant/history_read.py").read_text(encoding="utf-8")
        self.assertIn("unresolved_senders", source)
        self.assertIn("sender_is_resolved", source,
                      "\u6570\u91cf\u5fc5\u987b\u6309\u300c\u8ba4\u51fa\u6765\u4e86\u5417\u300d\u7b97\uff0c\u4e0d\u80fd\u53ea\u6570\u7a7a\u7684")

    def test_both_interfaces_show_it(self):
        """\u53ea\u5199\u8fdb\u5e93\u4e0d\u7b49\u4e8e\u8bf4\u51fa\u6765 \u2014\u2014 \u5546\u6237\u770b\u7684\u662f\u754c\u9762\u3002"""
        # Web \u7aef\u65ad\u8a00 **\u6570\u636e\u5951\u7ea6**\uff1a\u8bfb\u53d6\u7ed3\u679c\u89c6\u56fe\u5fc5\u987b\u628a\u201c\u591a\u5c11\u6761\u53d1\u9001\u4eba\u6ca1\u8ba4\u51fa\u6765\u201d\u7ed9\u51fa\u6765\uff0c
        # \u9875\u9762\u624d\u6709\u4e1c\u897f\u53ef\u663e\u793a\u3002\u6309\u6587\u4ef6\u540d\u627e\u6e90\u7801\u7684\u5199\u6cd5\u4e00\u91cd\u6784\u5c31\u7ea2 \u2014\u2014 \u8fd9\u91cc\u6539\u6210\u8c03\u51fd\u6570\u3002
        from types import SimpleNamespace

        from wechat_ai_assistant import web_server

        item = SimpleNamespace(
            conversation=SimpleNamespace(name="\u9c9c\u679c\u6279\u53d1\u7fa4", kind="group"),
            message_count=3, truncated=False, unresolved_senders=2,
            fetched_count=3, skipped=False, error="",
        )
        result = SimpleNamespace(total_messages=3, mode="all", coverage=[item],
                                 import_ids=[], headline=lambda: "all")
        view = web_server._history_view(result)
        self.assertEqual(view["coverage"][0]["unresolved"], 2,
                         "Web \u7aef\u70b9\u6ca1\u628a\u8fd9\u4e2a\u503c\u4f20\u51fa\u53bb\uff0c\u9875\u9762\u5c31\u6ca1\u6cd5\u663e\u793a")


if __name__ == "__main__":
    unittest.main()
