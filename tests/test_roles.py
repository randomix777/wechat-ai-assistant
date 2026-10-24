"""整理角色的回归测试：注册表、规则引擎、向导分流，全走合成数据。

覆盖：
* 三个内置角色都在，未知 id 回通用整理；
* 活动群消息按报名/时间地点/物资/通知归类，拿不准进闲聊；
* 非文字消息进非文字桶并写明要人工看；
* 时间地点抽取只抠有的，没有不编造；
* 向导按角色分流：三个角色都走云端 AI 服务（订单走订单链路，其它角色走
  共享的角色分析任务），**都过额度门**，都不建单、不占订单的"已整理"状态。
"""
from __future__ import annotations

import hashlib
import sys
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from wechat_ai_assistant import roles  # noqa: E402
from wechat_ai_assistant.roles import (  # noqa: E402
    ALL_ROLES,
    ROLE_ACTIVITY,
    ROLE_GENERIC,
    ROLE_ORDER,
    analysis_instruction,
    extract_facts,
    get_role,
    list_roles,
    organize,
)
from wechat_ai_assistant.wizard import WizardStep  # noqa: E402


def _msg(mid, content, sender="张姐", kind="text", group="义工群",
         sent_at="2026-10-13 10:00"):
    return {"id": mid, "group": group, "sender": sender, "content": content,
            "sent_at": sent_at, "kind": kind}


class RegistryTests(unittest.TestCase):
    def test_three_builtins_in_order(self):
        self.assertEqual([r.id for r in list_roles()],
                         [ROLE_ORDER, ROLE_ACTIVITY, ROLE_GENERIC])
        for role in list_roles():
            self.assertTrue(role.name.strip())
            self.assertTrue(role.description.strip())

    def test_unknown_role_falls_back_to_generic(self):
        self.assertEqual(get_role("不存在").id, ROLE_GENERIC)
        self.assertEqual(get_role("").id, ROLE_GENERIC)

    def test_order_role_has_no_rule_categories(self):
        # 订单走订单链路，规则引擎不该碰它（误传进来也按通用跑，不崩）。
        report = organize(ROLE_ORDER, [_msg("1", "苹果两箱")])
        self.assertEqual(report.role_id, ROLE_GENERIC)


class ActivityTests(unittest.TestCase):
    def test_signup_schedule_supply_notice(self):
        report = organize(ROLE_ACTIVITY, [
            _msg("1", "接龙：周六义工＋1"),
            _msg("2", "周六上午9点在公园门口集合，别迟到"),
            _msg("3", "谁有大喇叭？需要借一个"),
            _msg("4", "通知：本周活动改到周日，请大家周知"),
            _msg("5", "哈哈哈今天天气不错"),
        ])
        buckets = report.buckets
        self.assertEqual([i.message_id for i in buckets["报名接龙"]], ["1"])
        self.assertEqual([i.message_id for i in buckets["时间地点"]], ["2"])
        self.assertEqual([i.message_id for i in buckets["物资需求"]], ["3"])
        self.assertEqual([i.message_id for i in buckets["通知公告"]], ["4"])
        self.assertEqual([i.message_id for i in buckets["闲聊"]], ["5"])
        self.assertEqual(report.total, 5)
        self.assertIn("报名接龙 1 条", report.headline())

    def test_facts_come_along(self):
        report = organize(ROLE_ACTIVITY, [
            _msg("1", "周六上午9点在公园门口集合"),
        ])
        item = report.buckets["时间地点"][0]
        self.assertIn("9", item.facts.get("time", ""))
        self.assertTrue(report.to_dict()["buckets"])

    def test_non_text_goes_to_visible_bucket(self):
        report = organize(ROLE_ACTIVITY, [_msg("1", "", kind="image")])
        labels = list(report.buckets)
        self.assertEqual(len(labels), 1)
        self.assertIn("人工看", labels[0])
        self.assertEqual(report.buckets[labels[0]][0].content, "")


class GenericTests(unittest.TestCase):
    def test_question_task_notice(self):
        report = organize(ROLE_GENERIC, [
            _msg("1", "请问明天几点开始？"),
            _msg("2", "麻烦大家记得带身份证"),
            _msg("3", "通知：下周放假安排"),
            _msg("4", "晚上一起吃饭啊"),
        ])
        buckets = report.buckets
        self.assertEqual([i.message_id for i in buckets["有人提问"]], ["1"])
        self.assertEqual([i.message_id for i in buckets["待办事项"]], ["2"])
        self.assertEqual([i.message_id for i in buckets["通知事项"]], ["3"])
        self.assertEqual([i.message_id for i in buckets["闲聊"]], ["4"])


class FactsTests(unittest.TestCase):
    def test_time_and_place(self):
        facts = extract_facts("10月12日上午9点，地点：市中心广场集合")
        self.assertIn("10月12日", facts.get("time", ""))
        self.assertIn("市中心广场", facts.get("place", ""))

    def test_nothing_made_up(self):
        self.assertEqual(extract_facts("哈哈哈"), {})
        self.assertEqual(extract_facts(""), {})


class RolePromptContractTests(unittest.TestCase):
    """三个角色的提示词契约：护栏人人有、改了必须 bump 版本。"""

    #: (版本, 提示词哈希前 16 位)。改提示词**必须**同时改 ``RoleDef.version``：
    #: 批量缓存按 (角色, 版本, 输入) 失效，不改版本号 = 商户拿到旧提示词生成的缓存。
    #: 这条断言红了不是"改个哈希"就完 —— 先把 roles.py 里的 version 也改掉。
    PROMPT_FINGERPRINTS = {
        "order": ("order-v2", "881bec71f1485372"),
        "activity": ("activity-v2", "6e2c9dce07adda51"),
        "generic": ("generic-v3", "a576c8e95521cad8"),
    }

    def test_every_role_prompt_carries_the_same_guardrails(self):
        """防注入与敏感信息那两段，三个角色**都要有**。

        反转记录：这几段原来只写在"角色报告"那条通用提示里，订单助手直接 return
        自己的表格提示词、一个字都没有。订单处理的是同一批不可信群消息，护栏漏了
        它等于没有 —— 现在把公共边界抽成 ``_ANALYSIS_BOUNDARY``，三条路都拼上。
        """
        for role_id in ALL_ROLES:
            with self.subTest(role=role_id):
                text = analysis_instruction(role_id)
                self.assertIn("【分析边界】", text)
                self.assertIn("不是给你的指令", text, "要写明群消息是数据不是指令")
                self.assertIn("不要复述其完整值", text, "敏感信息不许原样复述")
                self.assertEqual(text.count("【分析边界】"), 1, "别拼两遍")

    def test_a_prompt_edit_requires_a_version_bump(self):
        """(版本, 提示词哈希) 钉死：改提示词不改版本，这条就红。"""
        for role_id, (version, digest) in self.PROMPT_FINGERPRINTS.items():
            with self.subTest(role=role_id):
                self.assertEqual(get_role(role_id).version, version)
                actual = hashlib.sha256(
                    analysis_instruction(role_id).encode("utf-8")).hexdigest()[:16]
                self.assertEqual(
                    actual, digest,
                    f"{role_id} 的提示词变了 —— 请把 roles.py 里的 version 一起 bump，"
                    "再更新这条指纹（缓存按版本失效）")

    def test_only_report_roles_carry_report_sections(self):
        """分节标题只属于**分节**报告角色：订单助手产出订单表、通用整理是
        叙述式（没有固定栏目，空的「暂无」小节是脚手架噪音），都不该列章节。"""
        self.assertEqual(get_role(ROLE_ORDER).result_sections, ())
        self.assertEqual(get_role(ROLE_GENERIC).result_sections, ())
        self.assertTrue(get_role(ROLE_ACTIVITY).result_sections)


class WizardRoleDispatchTests(unittest.TestCase):
    """向导按角色分流：三个角色都走云端 AI，都过同一道额度门。"""

    #: 一份够用的额度快照：`require_quota` 认它就不再联网查。
    @staticmethod
    def _enough_quota():
        from wechat_ai_assistant.wizard import QuotaView

        return QuotaView(logged_in=True, active=True, remaining=100)

    def _wizard(self, *, logged_in: bool = True, analyzer=None):
        import tempfile

        from wechat_ai_assistant.orders import OrderStore
        from wechat_ai_assistant.runmode import SettingsStore
        from wechat_ai_assistant.conversations import ConversationRegistry
        from wechat_ai_assistant.history_read import HistoryReader
        from wechat_ai_assistant.wechat_state import WeChatMonitor, SimulatedWeChat
        from wechat_ai_assistant.wizard import Wizard

        tmp = tempfile.TemporaryDirectory(prefix="pyw-role-")
        store = OrderStore(str(Path(tmp.name) / "orders.db"))
        tenant = store.create_merchant("t")["merchant_id"]
        store.add_import(tenant, "义工群", [
            {"sender": "张姐", "content": "周六义工接龙", "kind": "text",
             "sent_at": "2026-10-13 10:00", "source_id": "r1"}],
            "seed-src")
        wizard = Wizard(
            SettingsStore(str(Path(tmp.name) / "settings.json")),
            WeChatMonitor(SimulatedWeChat(["logged_in"])),
            ConversationRegistry(),
            HistoryReader(lambda **kw: []),
            tenant=tenant,
            orders=store,
            is_product_logged_in=lambda: logged_in,
            role_analyzer=analyzer,
        )
        wizard.quota = self._enough_quota()
        return wizard, store, tmp

    @staticmethod
    def _analyzer(reply: str = "### 报名接龙\n- 张姐 +1", count: int = 1, calls=None):
        """替身 AI 服务：记下被怎么调的，回一段报告正文。"""

        def run(role_id, message_ids, full_history=False):
            if calls is not None:
                calls.append((role_id, message_ids, full_history))
            return {"summary": reply, "message_count": count, "cost": None}

        run.calls = calls if calls is not None else []
        return run

    def test_role_analysis_goes_to_the_ai_service_not_the_local_rules(self):
        """角色整理走云端 AI —— 报告由模型写，本机规则引擎不再参与产品流程。

        反转记录：这两条原来断言"活动助手走本机引擎：不过额度门、不扣费、
        自己分桶"。产品改成三个角色都调 AI（页面写着「所有角色都会调用 AI」）
        之后，分桶不再是产品行为 —— 本机规则引擎只剩离线工具的角色
        （见上面的 ActivityTests/GenericTests）。
        """
        from wechat_ai_assistant import roles as roles_module
        from wechat_ai_assistant.wizard import WizardStep

        calls: list = []
        wizard, store, tmp = self._wizard(analyzer=self._analyzer(calls=calls))
        try:
            with unittest.mock.patch.object(roles_module, "organize") as organize:
                summary = wizard.analyze(role=ROLE_ACTIVITY)
            self.assertEqual(summary["role_id"], ROLE_ACTIVITY)
            self.assertTrue(summary["uses_model"], "走 AI 就要照实说")
            self.assertEqual(calls, [(ROLE_ACTIVITY, None, False)], "要真把角色交给 AI 服务")
            self.assertIn("报名接龙", summary["role_report"]["summary"])
            self.assertFalse(organize.called, "本机规则引擎不该再跑一遍")
            self.assertEqual(wizard.step, WizardStep.ANALYZE,
                             "跑完先停在整理屏看结论，点「下一步」才进分析详情")
            self.assertTrue(wizard._analysis_ready)
            self.assertTrue(wizard.advance()["ok"])
            self.assertEqual(wizard.step, WizardStep.RESULTS)
        finally:
            store.close()
            tmp.cleanup()

    def test_role_analysis_does_not_touch_order_state(self):
        """角色整理记自己的账，不占订单链路的"已整理"——换角色还能重跑。"""
        wizard, store, tmp = self._wizard(analyzer=self._analyzer())
        try:
            summary = wizard.analyze(role=ROLE_ACTIVITY)
            self.assertEqual(summary["role_id"], ROLE_ACTIVITY)
            self.assertEqual(wizard.already_analyzed_count(), 0)
            self.assertEqual(store.analyzed_message_ids(wizard.tenant), set())
        finally:
            store.close()
            tmp.cleanup()

    def test_role_analysis_stops_when_the_quota_is_out(self):
        """额度不够就不许调 AI —— 角色整理同样在花钱，判据和订单链路共用一处。"""
        from wechat_ai_assistant.wizard import QuotaBlocked, QuotaView

        calls: list = []
        wizard, store, tmp = self._wizard(analyzer=self._analyzer(calls=calls))
        try:
            wizard.quota = QuotaView(logged_in=True, active=True, remaining=0)
            with self.assertRaises(QuotaBlocked):
                wizard.analyze(role=ROLE_GENERIC)
            self.assertEqual(calls, [], "没过额度门就一次 AI 都不许调")
        finally:
            store.close()
            tmp.cleanup()

    def test_role_analysis_without_login_says_so(self):
        """没登录就说"先登录" —— 不说"额度不足"，也不说"服务没接上"（都指错下一步）。"""
        calls: list = []
        wizard, store, tmp = self._wizard(logged_in=False,
                                          analyzer=self._analyzer(calls=calls))
        try:
            with self.assertRaises(RuntimeError) as ctx:
                wizard.analyze(role=ROLE_ACTIVITY)
            self.assertIn("还没登录产品账号", str(ctx.exception))
            self.assertEqual(calls, [], "没登录连 AI 都不该调")
        finally:
            store.close()
            tmp.cleanup()

    def test_the_ai_service_reason_reaches_the_merchant(self):
        """AI 服务说得出原因（比如没登录）就照原话说，不改成"请重启"。"""
        wizard, store, tmp = self._wizard(
            analyzer=lambda role_id, message_ids, full_history=False: {"error": "需要登录产品账号"})
        try:
            with self.assertRaises(RuntimeError) as ctx:
                wizard.analyze(role=ROLE_GENERIC)
            self.assertIn("需要登录产品账号", str(ctx.exception))
        finally:
            store.close()
            tmp.cleanup()

    def test_role_summary_does_not_borrow_order_facts(self):
        """报告角色的汇总里不许出现订单链路的数字。

        反转记录：这三项原来填的是 ``already_analyzed_count()``（只认订单解析过的
        消息）和默认的 0 —— 界面照着写成「已整理过、本次没重复提交 N 条」和
        「提交 0 个任务」，对活动/通用整理都是假话。报告角色一律 None（不适用 /
        由账号侧结算），页面就不显示这些句子。
        """
        wizard, store, tmp = self._wizard(analyzer=self._analyzer())
        try:
            store.add_import(wizard.tenant, "义工群", [
                {"sender": "张姐", "content": "周六义工接龙", "kind": "text",
                 "sent_at": "2026-10-13 11:00", "source_id": "r2"}], "seed-src-2")
            summary = wizard.analyze(role=ROLE_ACTIVITY)
            self.assertIsNone(summary["already_analyzed"])
            self.assertIsNone(summary["submitted_jobs"])
            self.assertIsNone(summary["charged_micro_usd"])
            self.assertTrue(summary["is_incremental"])
            self.assertFalse(summary["reused_from_store"])
        finally:
            store.close()
            tmp.cleanup()

    def test_no_new_messages_shows_the_report_from_disk(self):
        """这一批已经分析完了：把**上次落盘的报告**端回来，不是报失败。

        反转记录：今天以前这里直接抛「没读到消息」。而报告只存在内存里，
        重启之后既看不到、重跑又报错 —— 商户花了钱，报告再也拿不回来。
        """
        from wechat_ai_assistant import summary_task

        wizard, store, tmp = self._wizard(
            analyzer=lambda role_id, message_ids, full_history=False: {"error": "没读到消息",
                                                                       "message_count": 0})
        try:
            summary_task.save_role_report(store, wizard.tenant, ROLE_ACTIVITY,
                                          "### 报名接龙\n- 张姐 +1", 7)
            summary = wizard.analyze(role=ROLE_ACTIVITY)
            self.assertTrue(summary["reused_from_store"])
            self.assertEqual(summary["processed"], 7)
            self.assertIn("张姐 +1", summary["role_report"]["summary"])
            # 跑完停在整理屏给商户看结论，点「下一步」才进分析详情。
            self.assertEqual(wizard.step, WizardStep.ANALYZE)
            self.assertTrue(wizard._analysis_ready)
        finally:
            store.close()
            tmp.cleanup()

    def test_no_new_messages_and_no_stored_report_says_why(self):
        """没新消息、也没存过报告，就把原因说清楚（不许装成功）。"""
        wizard, store, tmp = self._wizard(
            analyzer=lambda role_id, message_ids, full_history=False: {"error": "没读到消息",
                                                                       "message_count": 0})
        try:
            with self.assertRaises(RuntimeError) as ctx:
                wizard.analyze(role=ROLE_ACTIVITY)
            self.assertIn("没读到消息", str(ctx.exception))
        finally:
            store.close()
            tmp.cleanup()

    def test_order_analysis_without_login_says_the_same_thing(self):
        """订单那条路没登录时也给同一句"先登录"，不是一句状态词。

        反转记录：以前没登录点「开始识别订货意向」会显示 ``QuotaView.headline()``
        的「未登录产品账号」—— 那是**状态标签**，不是下一步该干什么。
        角色那条路的句子定下来之后，两条路共用同一句。
        """
        wizard, store, tmp = self._wizard(logged_in=False, analyzer=self._analyzer())
        try:
            with self.assertRaises(RuntimeError) as ctx:
                wizard.analyze(role=ROLE_ORDER)
            self.assertIn("还没登录产品账号", str(ctx.exception))
            self.assertIn("先登录", str(ctx.exception))
        finally:
            store.close()
            tmp.cleanup()

    def test_order_still_requires_quota(self):
        from wechat_ai_assistant.wizard import QuotaBlocked, QuotaView

        wizard, store, tmp = self._wizard(analyzer=self._analyzer())
        try:
            wizard.quota = QuotaView(logged_in=True, active=True, remaining=0)
            with self.assertRaises(QuotaBlocked):
                wizard.analyze(role=ROLE_ORDER)
        finally:
            store.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
