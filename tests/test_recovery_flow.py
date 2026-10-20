"""全链路恢复：重启、崩溃、线程、可追溯。

这些是"软件自己不能把商户坑了"的场景：断电、关窗口、网关重启之后，
数据还在、不会重复扣费、后台线程不会越积越多。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _name in ("business_agent", "order_agent", "support_triage"):
    _src = _ROOT / "packages" / _name / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from wechat_ai_assistant.application import MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from wechat_ai_assistant.history_read import SimulatedHistory  # noqa: E402
from wechat_ai_assistant.local_parse import ParseProgress  # noqa: E402
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.runmode import RunMode, SettingsStore  # noqa: E402
from wechat_ai_assistant.wechat_state import SimulatedWeChat, WeChatState  # noqa: E402
from wechat_ai_assistant.wizard import WizardStep  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


class StubClient:
    def __init__(self, remaining=100, active=True, error=None):
        self.remaining, self.active, self.error = remaining, active, error

    def account(self):
        if self.error is not None:
            raise self.error
        return {"subscription": {"active": self.active, "remaining_micro_usd": self.remaining,
                                 "plan_code": "标准版", "period_end": "2026-10-25T00:00:00+00:00"}}


class StubParse:
    model_executed = False

    def __init__(self):
        self.submitted: list[str] = []

    def submit(self, message_id, on_progress=None):
        self.submitted.append(message_id)
        return ParseProgress("done", "已处理", message_id=message_id, job_id=f"job_{message_id[-6:]}")

    def settle(self, on_progress=None):
        if not self.submitted:
            return ParseProgress("pending", "没有待处理的解析")
        return ParseProgress("done", "已处理", message_id=self.submitted[-1], order_id="")


class RecoveryHarness(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-recover-"))
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.merchant = self.store.create_merchant("测试商家")
        self.tenant = self.merchant["merchant_id"]
        self.group = "测试群"
        self.customer = self.store.add_customer(self.tenant, "张姐", "张姐", self.group)
        self.store.add_product(self.tenant, "APL", "苹果", "箱", 2600, ["苹果"])
        self.apps: list[MerchantApplication] = []

    def tearDown(self):
        for app in self.apps:
            app.close()
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_app(self, **kwargs) -> MerchantApplication:
        app = MerchantApplication(
            data_dir=self.dir, orders=self.store, tenant=self.tenant,
            probe=kwargs.pop("probe", SimulatedWeChat([WeChatState.LOGGED_IN])),
            conversation_source=SimulatedConversations([(self.group, ConversationKind.GROUP, 128)]),
            # 默认 0 条。需要真的读到内容的测试用
            # history_reader_fn=... 覆盖它 —— 不要把数据写死在这里，
            # 否则测试结果取决于这个默认值而不是被测的行为。
            history_reader_fn=kwargs.pop("history_reader_fn",
                                          SimulatedHistory(default_count=0)),
            # 群成员解析器必须一起注入。默认实现要**开微信界面**去取（实测一次
            # 2 秒上下，在没有微信的环境里直接失败），测试拿假读取器却走真解析器
            # 会被它拖垮时序。默认给空 —— 需要发送人的测试自己覆盖。
            group_members_fn=kwargs.pop("group_members_fn", lambda conv: []),
            simulated=True,
        )
        app.attach(object(), StubClient(), StubParse(), backend_is_model=False)
        # 真实启动顺序：attach 恢复登录态 → 刷会话列表 → ``resume()`` 定落点
        # （``web_server.create_server`` 就是这个顺序）。落点**只认 resume()**：
        # attach 里那次 advance 是给"刚在浏览器里登录成功"的商户少点一下用的，
        # 不该替启动恢复决定停在哪一屏。
        app.refresh_conversations()
        app.wizard.resume()
        self.apps.append(app)
        return app


class SettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-settings-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_settings_survive_a_restart(self):
        from wechat_ai_assistant.runmode import AutoOrderSettings, MerchantSettings

        store = SettingsStore(self.dir / "settings.json")
        settings = MerchantSettings()
        settings.account_id = "wxid_abc"
        settings.history_conversations = ["cv_1", "cv_2"]
        settings.auto = AutoOrderSettings(mode=RunMode.BACKGROUND)
        settings.wizard_complete = True
        store.save(settings)
        restored = SettingsStore(self.dir / "settings.json").load()
        self.assertEqual(restored.account_id, "wxid_abc")
        self.assertEqual(restored.history_conversations, ["cv_1", "cv_2"])
        self.assertEqual(restored.auto.mode, RunMode.BACKGROUND)
        self.assertTrue(restored.wizard_complete)

    def test_a_corrupt_settings_file_does_not_stop_the_software(self):
        path = self.dir / "settings.json"
        path.write_text("{ not json", encoding="utf-8")
        settings = SettingsStore(path).load()
        self.assertEqual(settings.wizard_complete, False, "配置坏了就回到默认，让商户重走向导")

    def test_switching_wechat_account_clears_the_old_binding(self):
        from wechat_ai_assistant.runmode import MerchantSettings

        settings = MerchantSettings()
        settings.account_id = "wxid_old"
        settings.live_conversation = "cv_old"
        settings.history_conversations = ["cv_old"]
        settings.wizard_complete = True
        settings.reset_for_new_account()
        self.assertEqual(settings.account_id, "")
        self.assertEqual(settings.live_conversation, "")
        self.assertEqual(settings.history_conversations, [])
        self.assertFalse(settings.wizard_complete, "换账号后必须重新绑定会话")

    def test_finishing_the_wizard_does_not_change_the_start_step(self):
        """走完过向导也只影响「有没有首页可回」，不改启动落点：永远是登录前置页。

        反转记录：这里以前自带一份 ``_needs_wizard``（照抄 ``Wizard`` 里那个
        按"完成过/换过号"判断要不要重走向导的方法）。那个方法随启动落点一起
        删了 —— 商户要的是每次打开都从头看一眼，所以副本也跟着去掉，
        改成用真实 API 钉住现状。
        """
        from wechat_ai_assistant.conversations import ConversationRegistry
        from wechat_ai_assistant.runmode import MerchantSettings
        from wechat_ai_assistant.wizard import Wizard, WizardStep

        store = SettingsStore(self.dir / "settings.json")
        settings = MerchantSettings()
        settings.account_id = "wxid_a"
        settings.wizard_complete = True
        store.save(settings)

        wizard = Wizard(store, None, ConversationRegistry(), None, tenant="t")
        self.assertEqual(wizard.resume(), WizardStep.LOGIN)
        self.assertTrue(wizard.has_finished(), "首页还是回的去的")


class ThreadHygieneTests(RecoveryHarness):
    def test_background_polling_does_not_multiply_threads(self):
        before = threading.active_count()
        app = self.make_app()
        for _ in range(20):
            app.start_background()
        import time

        time.sleep(0.4)
        self.assertLessEqual(threading.active_count() - before, 3, "重复启动不得叠加轮询线程")
        app.stop()
        time.sleep(0.3)
        self.assertLessEqual(threading.active_count() - before, 2, "停止后不得残留线程")

    def test_stop_is_idempotent(self):
        app = self.make_app()
        app.start_background()
        app.stop()
        app.stop()
        app.stop()

    def test_close_stops_everything(self):
        app = self.make_app()
        app.start_background()
        app.close()
        self.assertFalse(app.ui_queue.alive)


class CrashRecoveryTests(RecoveryHarness):
    def test_a_restart_recovers_orders_and_settings(self):
        app = self.make_app()
        app.choose_live_conversation(app.refresh_conversations()[0])
        app.close()
        self.apps.remove(app)

        restarted = self.make_app()
        self.assertEqual(restarted.auto.mode, RunMode.BACKGROUND,
                         "产品只有后台整理一种模式，重启后也必须是它")
        # 起点回到向导开头（登录前置页）—— 启动落点不按上次走到哪儿裁，也不因为
        # "登录态是恢复出来的"就跳过它；上次选的会话由 resume() 在会话列表刷新
        # 之后装回来（见 ResumeTests）。
        self.assertEqual(restarted.wizard.step, WizardStep.LOGIN)
        # 订单数据仍在
        self.assertEqual(restarted.orders_for_display(), [])

    def test_history_cursor_survives_a_restart(self):
        app = self.make_app(history_reader_fn=SimulatedHistory(default_count=2))
        from wechat_ai_assistant.history_read import HistoryRange

        app.choose_history_conversations(app.refresh_conversations())
        app.wizard.orders = lambda: self.store
        app.wizard.pipeline.orders = self.store
        app.read_history(range_kind=HistoryRange.LAST_DAY, max_messages=5)
        import time

        time.sleep(0.5)
        result = app.wizard.history_result
        self.assertIsNotNone(result)
        self.assertFalse(result.coverage[0].truncated, "2 条数据、限 5 条应视为读完")
        app.close()
        self.apps.remove(app)

        restarted = self.make_app()
        conversation = restarted.refresh_conversations()[0]
        saved = restarted.cursor.get(conversation.identity, self.tenant)
        self.assertIsNotNone(saved, "读取进度必须持久化")
        self.assertEqual(int(saved["exhausted"]), 1)

    def test_every_run_is_traceable_end_to_end(self):
        message_id = self._message("苹果两箱", "t1")
        app = self.make_app()
        app.choose_live_conversation(app.refresh_conversations()[0])
        outcome = app.pipeline.process(message_id)
        trace = outcome.trace.to_dict()
        for key in ("request_id", "message_id", "job_id", "run_id", "status", "model_executed"):
            self.assertIn(key, trace)
        self.assertTrue(trace["request_id"])
        self.assertTrue(trace["job_id"], "云端任务号必须记录")
        self.assertFalse(trace["model_executed"], "演示后端不得自报为真实模型")

    def _message(self, content: str, source_id: str) -> str:
        self.store.add_import(
            self.tenant, self.group,
            [{"sender": "张姐", "content": content, "sent_at": NOW.isoformat(),
              "source_id": source_id, "stable_source_id": True}],
            source=f"import-{source_id}", source_type="test",
        )
        return self.store.db.execute(
            "SELECT id FROM messages WHERE merchant_id=? AND source_id=?", (self.tenant, source_id)
        ).fetchone()["id"]


if __name__ == "__main__":
    unittest.main()
