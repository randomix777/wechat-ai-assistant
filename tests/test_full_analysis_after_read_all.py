"""「读取全部」的语义到分析层才算完。

读取层（``history_read``）只管把消息搬进库；商户点「读取全部」的意图是
**全部分析** —— 只要增量他会点「更新消息」。所以向导把意图挂起一次，
下一次角色整理消费：分析器收到 ``full_history=True``（忽略水位线、不受
一次 200 条的成本上限约束），成功就摘；失败不摘，重试仍然全量；
再下一次整理回到增量 —— 重复全量只会重复扣费。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant.conversations import ConversationRegistry  # noqa: E402
from wechat_ai_assistant.history_read import HistoryReader, ReadMode  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.runmode import SettingsStore  # noqa: E402
from wechat_ai_assistant.wechat_state import SimulatedWeChat, WeChatMonitor  # noqa: E402
from wechat_ai_assistant.wizard import QuotaView, Wizard  # noqa: E402


class FullAnalysisIntentTests(unittest.TestCase):
    """向导把「读取全部」翻译成一次全量整理：挂起、消费、失败不摘。"""

    def _wizard(self, analyzer):
        tmp = tempfile.TemporaryDirectory(prefix="pyw-full-")
        store = OrderStore(str(Path(tmp.name) / "orders.db"))
        tenant = store.create_merchant("t")["merchant_id"]
        wizard = Wizard(
            SettingsStore(str(Path(tmp.name) / "settings.json")),
            WeChatMonitor(SimulatedWeChat(["logged_in"])),
            ConversationRegistry(),
            HistoryReader(lambda **kw: []),
            tenant=tenant,
            orders=store,
            is_product_logged_in=lambda: True,
            role_analyzer=analyzer,
        )
        wizard.quota = QuotaView(logged_in=True, active=True, remaining=100)
        return wizard, store, tmp

    def test_read_all_arms_exactly_one_full_analysis(self):
        calls = []

        def analyzer(role_id, message_ids, full_history=False):
            calls.append(full_history)
            return {"summary": "报告正文", "message_count": 1}

        wizard, store, tmp = self._wizard(analyzer)
        try:
            wizard.read_history(mode=ReadMode.ALL)
            self.assertTrue(wizard.full_analysis_pending,
                            "读取全部之后，全量整理挂起")
            wizard.analyze(role="generic")
            self.assertEqual(calls, [True], "读取全部后的第一次整理是全量")
            self.assertFalse(wizard.full_analysis_pending, "全量意图随成功消费掉")
            wizard.analyze(role="generic")
            self.assertEqual(calls, [True, False], "再整理回到增量：重复全量只是重复扣费")
        finally:
            store.close()
            tmp.cleanup()

    def test_update_read_does_not_arm_anything(self):
        calls = []

        def analyzer(role_id, message_ids, full_history=False):
            calls.append(full_history)
            return {"summary": "报告正文", "message_count": 1}

        wizard, store, tmp = self._wizard(analyzer)
        try:
            wizard.read_history(mode=ReadMode.UPDATE)
            self.assertFalse(wizard.full_analysis_pending)
            wizard.analyze(role="generic")
            self.assertEqual(calls, [False], "「更新消息」本来就是增量")
        finally:
            store.close()
            tmp.cleanup()

    def test_a_failed_full_analysis_stays_armed(self):
        """失败不摘：网络抖一下，不能让全量悄悄降级成增量。"""
        calls = []
        state = {"fail": True}

        def analyzer(role_id, message_ids, full_history=False):
            calls.append(full_history)
            if state["fail"]:
                raise RuntimeError("网络断了")
            return {"summary": "报告正文", "message_count": 1}

        wizard, store, tmp = self._wizard(analyzer)
        try:
            wizard.read_history(mode=ReadMode.ALL)
            with self.assertRaises(RuntimeError):
                wizard.analyze(role="generic")
            self.assertTrue(wizard.full_analysis_pending, "失败不许摘")
            state["fail"] = False
            wizard.analyze(role="generic")
            self.assertEqual(calls, [True, True], "重试仍然全量")
            self.assertFalse(wizard.full_analysis_pending)
        finally:
            store.close()
            tmp.cleanup()

    def test_explicit_message_ids_keep_their_scope(self):
        """调用方点名 message_ids 时尊重点名：全量挂起不给它覆盖掉。"""
        calls = []

        def analyzer(role_id, message_ids, full_history=False):
            calls.append((list(message_ids or ()), full_history))
            return {"summary": "报告正文", "message_count": 1}

        wizard, store, tmp = self._wizard(analyzer)
        try:
            wizard.read_history(mode=ReadMode.ALL)
            wizard.analyze(role="generic", message_ids=["m-1"])
            self.assertEqual(calls, [(["m-1"], False)],
                             "点名消息就只分析点名的；全量挂起留给下一次常规整理")
            self.assertTrue(wizard.full_analysis_pending)
        finally:
            store.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
