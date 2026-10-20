"""「下一步 / 上一步」的判据只有一处：``Wizard.advance`` / ``Wizard.back``。

## 为什么必须只有一处

以前判据散在界面的每个按钮里，踩过的坑是：一个地方把按钮放出来了，
另一个地方点下去却弹「还没有读到消息，请先点开始读取」—— 同一件事两套判据，
一处放行、一处拦下，商户只能干瞪着。

现在 Tk 和 Web 都调同一个方法，返回 ``{"ok", "step", "reason"}``：
``reason`` 是**给商户看的中文**，界面直接显示，不许自己再编一套说法。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.conversations import Conversation, ConversationRegistry  # noqa: E402
from wechat_ai_assistant.runmode import MerchantSettings  # noqa: E402
from wechat_ai_assistant.wizard import Wizard, WizardStep  # noqa: E402


class _Store:
    def __init__(self, settings: MerchantSettings | None = None):
        self.settings = settings or MerchantSettings()

    def load(self):
        return self.settings

    def save(self, _settings):
        pass


class _Monitor:
    def __init__(self, ready: bool):
        self.latest = SimpleNamespace(ready=ready)


def _wizard(*, ready: bool = True, logged_in: bool = True,
            selected: bool = False, role_analyzer=None) -> Wizard:
    conversations = ConversationRegistry()
    if selected:
        conversations.refresh([Conversation(name="鲜果批发群", kind="group", member_count=35)])
        conversations.set_history(conversations.available)
    return Wizard(
        _Store(), _Monitor(ready), conversations, None, tenant="t",
        is_product_logged_in=lambda: logged_in,
        role_analyzer=role_analyzer,
    )


class AdvanceTests(unittest.TestCase):
    def test_login_step_does_not_let_you_through_without_login(self):
        wizard = _wizard(logged_in=False)
        wizard.step = WizardStep.LOGIN
        outcome = wizard.advance()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["step"], WizardStep.LOGIN, "不放行就不能动")
        self.assertIn("登录", outcome["reason"], "要说清还差什么，不能只说不行")

        wizard.is_product_logged_in = lambda: True
        self.assertTrue(wizard.advance()["ok"])
        self.assertEqual(wizard.step, WizardStep.WECHAT)

    def test_wechat_step_needs_wechat_ready(self):
        wizard = _wizard(ready=False)
        wizard.step = WizardStep.WECHAT
        outcome = wizard.advance()
        self.assertFalse(outcome["ok"])
        self.assertEqual(wizard.step, WizardStep.WECHAT)
        self.assertIn("微信", outcome["reason"])

        wizard.monitor.latest = SimpleNamespace(ready=True)
        self.assertTrue(wizard.advance()["ok"])
        self.assertEqual(wizard.step, WizardStep.CONVERSATIONS)

    def test_conversation_step_needs_a_selection(self):
        wizard = _wizard(selected=False)
        wizard.step = WizardStep.CONVERSATIONS
        outcome = wizard.advance()
        self.assertFalse(outcome["ok"])
        self.assertIn("勾选", outcome["reason"])

        wizard = _wizard(selected=True)
        wizard.step = WizardStep.CONVERSATIONS
        self.assertTrue(wizard.advance()["ok"])
        # 范围确认并进选择会话：定完范围，下一屏就是读取。
        self.assertEqual(wizard.step, WizardStep.READ)

    def test_read_step_needs_a_result(self):
        wizard = _wizard()
        wizard.step = WizardStep.READ
        outcome = wizard.advance()
        self.assertFalse(outcome["ok"])
        self.assertIn("读取结果", outcome["reason"])

        wizard.history_result = object()
        self.assertTrue(wizard.advance()["ok"])
        # 核对与读取同屏：读完直接去整理，不再经过单独的「核对读取明细」一步。
        self.assertEqual(wizard.step, WizardStep.ANALYZE)

    def test_the_results_step_ends_the_wizard(self):
        """分析详情是最后一屏：走到它的「回到首页」才算走完（finish）。"""
        wizard = _wizard()
        wizard.step = WizardStep.RESULTS
        self.assertFalse(wizard.settings.wizard_complete, "还没回到首页就不算走完")
        self.assertTrue(wizard.advance()["ok"])
        self.assertEqual(wizard.step, WizardStep.DONE)
        self.assertTrue(wizard.settings.wizard_complete)

    def test_analyzing_stops_on_the_results_step(self):
        """整理完停在「分析详情」，不是直接跳首页。

        那一屏是唯一能看到这次整理结果和明细的地方（角色整理的产出只有它），
        以前 analyze 里直接 finish()，于是那一屏永远到不了。
        """
        from wechat_ai_assistant.wizard import QuotaView

        wizard = _wizard(
            selected=True,
            role_analyzer=lambda role_id, message_ids: {
                "summary": "### 待办事项\n- 张姐：记得带身份证", "message_count": 1},
        )
        # 角色整理也过额度门，给一份够用的快照就不用联网查。
        wizard.quota = QuotaView(logged_in=True, active=True, remaining=100)
        summary = wizard.analyze(role="generic", message_ids=[])
        self.assertEqual(wizard.step, WizardStep.RESULTS)
        self.assertIn("role_report", summary)
        self.assertFalse(wizard.settings.wizard_complete, "还没点「回到首页」就不算走完")

    def test_reread_goes_back_to_the_conversation_step(self):
        """首页的「再读一次」必须真的回到选会话那一屏（范围确认也在那儿）——
        以前页面调 back()，而首页不在步骤里，只会被拒。"""
        wizard = _wizard(selected=True)
        wizard.step = WizardStep.DONE
        self.assertEqual(wizard.reread(), WizardStep.CONVERSATIONS)
        self.assertEqual(wizard.step, WizardStep.CONVERSATIONS)

    def test_steps_without_a_next_button_say_so(self):
        """智能整理要按「开始智能整理」——它没有「下一步」，不能假装有。"""
        for step in (WizardStep.ANALYZE,):
            with self.subTest(step=step):
                wizard = _wizard(selected=True)
                wizard.step = step
                outcome = wizard.advance()
                self.assertFalse(outcome["ok"])
                self.assertEqual(wizard.step, step)
                self.assertTrue(outcome["reason"])

    def test_every_order_step_is_covered(self):
        """新增一步就必须想清楚它的「下一步」—— 不然它是个死胡同。"""
        for step in WizardStep.ORDER:
            with self.subTest(step=step):
                wizard = _wizard(selected=True)
                wizard.history_result = object()
                wizard.step = step
                outcome = wizard.advance()
                self.assertIn("ok", outcome)
                self.assertTrue(outcome["reason"] or outcome["ok"])


class BackTests(unittest.TestCase):
    def test_back_walks_the_order_backwards(self):
        wizard = _wizard()
        wizard.step = WizardStep.CONVERSATIONS
        self.assertTrue(wizard.back()["ok"])
        self.assertEqual(wizard.step, WizardStep.WECHAT)

    def test_the_first_step_has_no_back(self):
        wizard = _wizard()
        wizard.step = WizardStep.LOGIN
        outcome = wizard.back()
        self.assertFalse(outcome["ok"])
        self.assertEqual(wizard.step, WizardStep.LOGIN)

    def test_back_from_home_says_it_cannot(self):
        """首页（done）不在要走的步骤里，没有"上一步"可言。"""
        wizard = _wizard()
        wizard.step = WizardStep.DONE
        self.assertFalse(wizard.back()["ok"])
        self.assertEqual(wizard.step, WizardStep.DONE)


class ResumeTests(unittest.TestCase):
    """启动落在哪一屏：**永远是登录前置页**（它不占流程编号）。

    商户要的是「每次打开都从头看一眼」。落点按登录状态裁过两次：已登录就跳过
    登录页去检查微信，走完过还直接进首页 —— 同一个软件每次打开停的地方都不一样，
    取决于账号是什么时候恢复的。两次都改回来了，别再按账号裁。
    """

    @staticmethod
    def _registry(*names: str) -> ConversationRegistry:
        registry = ConversationRegistry()
        registry.refresh([Conversation(name=name, kind="group", member_count=35)
                          for name in names])
        return registry

    def _wizard(self, conversations: ConversationRegistry,
                settings: MerchantSettings, *, logged_in: bool = True) -> Wizard:
        return Wizard(_Store(settings), _Monitor(True), conversations, None, tenant="t",
                      is_product_logged_in=lambda: logged_in)

    def test_every_start_lands_on_the_login_step(self):
        """登录没登录、走完没走完，起点都是登录前置页。"""
        chosen = self._registry("鲜果批发群").available[0]
        cases = (
            ("第一次用", MerchantSettings(), False),
            ("登录了还没走完", MerchantSettings(), True),
            ("以前走完过", MerchantSettings(wizard_complete=True,
                                          history_conversations=[chosen.identity]), True),
        )
        for label, settings, logged_in in cases:
            with self.subTest(label=label):
                wizard = self._wizard(self._registry("鲜果批发群"), settings,
                                      logged_in=logged_in)
                self.assertEqual(wizard.resume(), WizardStep.LOGIN)

    def test_the_saved_selection_is_loaded_without_moving_the_step(self):
        """绑定要装回来（第 2 步显示上次勾的），但**不改变起点**。

        不装回来的话，「从头看一眼」就变成了「重新挑一遍群」。
        """
        chosen = self._registry("鲜果批发群").available[0]
        wizard = self._wizard(self._registry("鲜果批发群"), MerchantSettings(
            wizard_complete=True, history_conversations=[chosen.identity]))
        self.assertEqual(wizard.resume(), WizardStep.LOGIN)
        self.assertEqual([item.name for item in wizard.conversations.history_selected],
                         ["鲜果批发群"], "上次选的会话要装回来")

    def test_a_saved_conversation_that_is_gone_does_not_fake_a_selection(self):
        """群里没了/换了微信：绑定对不上就老实重选，不许假装还记得。"""
        wizard = self._wizard(self._registry("别的群"), MerchantSettings(
            wizard_complete=True, history_conversations=["cv_已经不存在了"]))
        wizard.resume()
        self.assertEqual(wizard.conversations.history_selected, [])

    def test_a_finished_wizard_still_knows_it_has_a_home(self):
        """起点回到登录页，但「有没有首页可回」照旧 —— 首页的「回到首页」靠它。"""
        wizard = self._wizard(self._registry("鲜果批发群"),
                              MerchantSettings(wizard_complete=True))
        self.assertTrue(wizard.has_finished())


if __name__ == "__main__":
    unittest.main()
