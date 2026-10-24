"""Web 向导的端到端：走 HTTP 接口从登录一路到整理，不碰 Tk。

## 这测的是什么

Tk 界面和 Web 界面共用同一个 ``MerchantApplication`` 和同一个 ``Wizard``，所以
"流程对不对"应该在**接口这一层**验完，而不是靠点像素。这里就是这么做的：

    /api/state（步骤条 + 门禁）
      → /api/wizard/advance（判据在向导里）
      → /api/conversations、/api/conversations/choose
      → /api/history/read、/api/wizard/advance
      → /api/analyze

顺序、编号、门禁都从接口读，**不写死第几步** —— 以后加一步，这里只有
"从哪走到哪"的断言会变，而不是"第 N 步"的字符串。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant import web_dev  # noqa: E402
from wechat_ai_assistant.application import AppEvents, MerchantApplication  # noqa: E402
from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from wechat_ai_assistant.merchant_app import ensure_local_store  # noqa: E402
from wechat_ai_assistant.history_read import SimulatedHistory  # noqa: E402
from wechat_ai_assistant.wechat_state import SimulatedWeChat, WeChatState  # noqa: E402
from wechat_ai_assistant.wizard import WizardStep  # noqa: E402
from types import SimpleNamespace  # noqa: E402


class _Session:
    merchant_name = "测试商家"
    merchant_id = "m-1"


class WebWizardFlowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="pyw-webfow-")
        # 走真实入口建库：套餐行是 add_import 的前置（没有它会 PermissionError）。
        self.store, self.tenant = ensure_local_store(Path(self.dir.name) / "orders.db")
        self.app = MerchantApplication(
            data_dir=Path(self.dir.name),
            orders=self.store,
            tenant=self.tenant,
            probe=SimulatedWeChat([WeChatState.LOGGED_IN]),
            conversation_source=SimulatedConversations(
                [("鲜果批发群", ConversationKind.GROUP, 128)]),
            history_reader_fn=SimulatedHistory(default_count=2),
            events=AppEvents(),
            simulated=True,
            sender_source="members",
        )
        self.server = web_dev.create_server(self.app, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://%s:%d" % self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        try:
            self.app.close()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass
        self.dir.cleanup()

    # ---- 小工具 ----

    def state(self) -> dict:
        with urllib.request.urlopen(self.base + "/api/state", timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---- 用例 ----

    def test_the_wizard_page_renders_from_the_state(self):
        status, body = self._wizard_page()
        self.assertEqual(status, 200)
        self.assertIn("威信AI智能助理", body)

    def _wizard_page(self):
        with urllib.request.urlopen(self.base + "/wizard", timeout=15) as r:
            return r.status, r.read().decode("utf-8")

    def test_going_home_returns_to_the_first_step(self):
        """「重新开始」= 回到第一步（检查微信），不再有仪表盘式首页。"""
        self.app.wizard._go(WizardStep.RESULTS)
        self.post("/api/wizard/home")
        data = self.state()
        self.assertEqual(data["step"], WizardStep.WECHAT)
        self.assertNotIn("home", data, "不再有「首页数据」这一页")
        self.assertEqual(data["steps"][0]["state"], "current",
                         "第一步重新变成当前步")

    def test_gate_is_closed_before_login_and_the_login_page_is_not_a_step(self):
        """没登录就停在登录页；登录页是**前置门槛**，不占编号、不在步骤条里。

        步骤条第一颗点是「检查微信」（编号从它开始）—— 登录页没有编号，
        也不该出现在这条线上。
        """
        data = self.state()
        self.assertEqual(data["step"], WizardStep.LOGIN, "没登录就该停在登录页")
        self.assertFalse(data["product_gate_open"])
        self.assertEqual(data["steps"][0]["id"], WizardStep.WECHAT)
        self.assertEqual(data["steps"][0]["number"], 0, "编号从「检查微信」开始")
        self.assertNotIn(WizardStep.LOGIN, [row["id"] for row in data["steps"]],
                         "登录页不该出现在步骤条上")

        outcome = self.post("/api/wizard/advance")
        self.assertFalse(outcome["ok"], "没登录不许往前走")
        self.assertTrue(outcome["reason"], "要说清差什么")
        self.assertEqual(self.state()["step"], WizardStep.LOGIN)

    def test_logout_brings_the_merchant_back_to_the_login_page(self):
        """退出登录回登录页 —— 不能停在流程里（真实报障）。"""
        self.app.session = _Session()
        self.assertTrue(self.post("/api/wizard/advance")["ok"])
        self.assertEqual(self.state()["step"], WizardStep.WECHAT)

        self.assertTrue(self.post("/api/logout")["ok"])
        data = self.state()
        self.assertEqual(data["step"], WizardStep.LOGIN, "退出后要回登录页")
        self.assertFalse(data["product_gate_open"], "退出后门禁必须关上")
        self.assertFalse(data["account"]["logged_in"])

    def test_the_report_comes_back_from_disk_after_a_restart(self):
        """报告落盘的意义：重启后「查看 AI 整理报告」还拿得到。

        反转记录：以前报告只存在 ``WebState.role_report`` 里，重启就没了；而同一个
        角色再跑又会因为水位线到头而报"没读到消息" —— 报告彻底拿不回来（钱却花了）。
        """
        from wechat_ai_assistant import summary_task

        summary_task.save_role_report(self.app.orders, self.app.tenant, "activity",
                                      "### 报名接龙\n- 张姐 +1", 12)
        # 模拟"刚重启"：内存状态里什么都没有
        self.server.web_state.set(role_report=None, analysis=None)
        payload = self.get("/api/analysis/report?role=activity")
        self.assertIsNotNone(payload.get("report"), payload)
        self.assertIn("张姐 +1", payload["report"]["summary"])
        self.assertEqual(payload["report"]["role_name"], "活动助手")
        self.assertTrue(payload["report"]["stored"])

    def test_the_per_conversation_reports_use_one_key_on_both_paths(self):
        """刚跑完（内存）和重启后（落盘）要给出**同一个键**。

        修之前：内存那份叫 ``group_reports``（向导汇总的字段名），落盘那份叫
        ``groups``。页面只认一个键，于是同一个按钮"刚跑完能按会话展开、
        重启后展不开"，而且不报错 —— 最难受的那种不一致。
        """
        from wechat_ai_assistant import summary_task

        groups = [{"group_label": "鲜果群", "report": "鲜果群的报告", "message_count": 4},
                  {"group_label": "张老板", "report": "张老板的报告", "message_count": 5}]
        # ① 刚跑完：内存里的形状（向导汇总用的是 group_reports）
        self.server.web_state.set(
            role_report={"role_id": "activity", "role_name": "活动助手", "total": 9,
                         "summary": "合并全文", "sections": [], "group_reports": groups,
                         "partial_error": ""},
            analysis={"role_id": "activity"})
        fresh = self.get("/api/analysis/report?role=activity")["report"]
        self.assertEqual([item["group_label"] for item in fresh["groups"]],
                         ["鲜果群", "张老板"], "内存那份也要给 groups")

        # ② 重启后：落盘那份
        summary_task.save_role_report(self.app.orders, self.app.tenant, "activity",
                                      "合并全文", 9, groups=groups)
        self.server.web_state.set(role_report=None, analysis=None)
        stored = self.get("/api/analysis/report?role=activity")["report"]
        self.assertEqual([item["group_label"] for item in stored["groups"]],
                         ["鲜果群", "张老板"], "落盘那份是同一个键")

    def test_a_restart_restores_the_last_report_into_the_state(self):
        """新起的服务要把最近那份报告装回状态：首页那句和按钮靠它。"""
        from wechat_ai_assistant import summary_task, web_dev

        summary_task.save_role_report(self.app.orders, self.app.tenant, "generic",
                                      "### 待办事项\n- 带身份证", 5)
        server = web_dev.create_server(self.app, port=0)
        self.addCleanup(server.server_close)
        state = server.web_state.snapshot()
        self.assertTrue(state["analysis"]["restored"], "恢复出来的要说「上次」，不是「本次」")
        self.assertEqual(state["analysis"]["role_id"], "generic")
        self.assertEqual(state["analysis"]["processed"], 5)
        # 报告正文不进每秒轮询的 /api/state（那是按需拉的），这里直接看状态对象 ——
        # 它就是 /api/analysis/report 读的那份。
        self.assertEqual(server.web_state.role_report["summary"],
                         "### 待办事项\n- 带身份证")

    def test_the_whole_flow_walks_forward_on_the_interface(self):
        """登录 → 微信 → 会话 → 读取（含核对）→ 整理。判据全在接口这一层。"""
        self.app.session = _Session()
        self.assertTrue(self.state()["product_gate_open"])

        # 登录页 → 检查微信（这一步由向导判：没登录不放行）。
        self.assertTrue(self.post("/api/wizard/advance")["ok"])
        self.assertEqual(self.state()["step"], WizardStep.WECHAT)

        # 微信已就绪：检测一次就会自动越过检查微信 —— 不用再点「下一步」。
        self.post("/api/wechat/check")
        self.assertEqual(self.state()["step"], WizardStep.CONVERSATIONS)

        rows = self.get("/api/conversations")["conversations"]
        self.assertTrue(rows, "模拟会话应该列出来")
        self.post("/api/conversations/choose", {"names": [rows[0]["name"]]})
        self.assertTrue(self.post("/api/wizard/advance")["ok"])
        # 范围确认并进选择会话：定完范围，下一屏就是读取。
        self.assertEqual(self.state()["step"], WizardStep.READ)
        chosen = self.app.wizard.conversations.history_selected
        self.assertEqual(len(chosen), 1, "勾选的会话要真的选上，不能丢")
        # 服务端给的是会话**键**（name），和勾选框的 value 一致。
        # 反转记录：以前给的是消歧标识（"名字（128人）"）—— 界面拿它比 name 永远
        # 对不上；而且两个没名字的群会撞成同一个串，还原时会一起勾上。
        self.assertEqual(self.state()["selected"], [item.name for item in chosen])
        # 界面还要拿得到显示名（未命名的群是占位，不是原始 chatroom ID）。
        self.assertEqual(rows[0].get("label"), chosen[0].label)

        self.post("/api/history/read", {"mode": "all"})
        for _ in range(100):
            data = self.state()
            if data["history"] and not data["busy"]:
                break
            threading.Event().wait(0.1)
        data = self.state()
        self.assertIsNotNone(data["history"], "读取结果要出现在状态里")
        self.assertTrue(data["history"]["headline"], "要有一句给商户看的结果说明")

        # 核对与读取同屏：读完点一次「下一步」就到智能整理，不再多一屏。
        self.assertTrue(self.post("/api/wizard/advance")["ok"])
        self.assertEqual(self.state()["step"], WizardStep.ANALYZE)

    def test_the_results_screen_restart_returns_to_the_first_step(self):
        """结果屏的「重新开始」结束向导：记下走完了，回到第一步。"""
        self.app.session = _Session()
        self.app.wizard.step = WizardStep.RESULTS
        self.assertTrue(self.post("/api/wizard/home")["ok"])
        data = self.state()
        self.assertEqual(data["step"], WizardStep.WECHAT)
        self.assertEqual(data["steps"][0]["state"], "current")

    def test_can_go_home_only_after_finishing(self):
        """「重新开始」只对走完过的商户出现；点它回第一步。"""
        self.app.session = _Session()
        self.assertFalse(self.state()["can_go_home"], "还没走完就不该有「重新开始」")

        self.app.wizard.finish()
        self.assertTrue(self.state()["can_go_home"])
        self.assertTrue(self.post("/api/wizard/home")["ok"])
        self.assertEqual(self.state()["step"], WizardStep.WECHAT)

    def test_reading_without_a_selection_is_refused_with_the_same_sentence(self):
        """一个会话都没勾就点「读取」：不许开读，说法和「下一步」那句一致。"""
        from wechat_ai_assistant.wizard import NO_CONVERSATION_SELECTED

        self.app.session = _Session()
        self.app.wizard.step = WizardStep.CONVERSATIONS
        self.app.wizard.conversations.set_history([])
        outcome = self.post("/api/history/read", {"mode": "all"})
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], NO_CONVERSATION_SELECTED)

    def test_probing_wechat_for_the_home_view_does_not_move_the_wizard(self):
        """读一次状态（含首页为了显示而探的那次微信检测）**不许改流程**。

        出过什么事：登录页进屏时后台刷一次额度 →``check_quota`` 顺手
        ``refresh_home`` → 首页探一次微信 → 状态从「尚未检测」变成「已登录」
        → 触发监视器回调 → ``wizard.check_wechat`` 从登录步自动前进。
        结果：商户刚打开软件、什么都没点，就被从登录页甩到「选择会话」。
        """
        self.app.session = _Session()
        self.app.wizard.step = WizardStep.LOGIN
        self.app.check_quota()  # 登录页进屏时走的就是这条路
        self.assertEqual(self.state()["step"], WizardStep.LOGIN,
                         "只读的探测把商户从登录页推走了")
        self.assertTrue(self.app.monitor.latest.ready, "前提：这次探测真的跑过并拿到了状态")

    def test_the_background_wechat_monitor_still_advances_the_wizard(self):
        """显式检测（点「重新检测」、等扫码）照旧推进 —— 别把只读探测的规矩
        当成"什么都不许动"。"""
        self.app.session = _Session()
        self.app.wizard.step = WizardStep.WECHAT
        self.app.check_wechat()
        self.assertEqual(self.state()["step"], WizardStep.CONVERSATIONS)

    def test_a_configured_account_still_starts_on_the_login_step(self):
        """每次启动都停在登录页 —— 已经登录、以前走完过也一样。

        落点两次都按登录状态裁过（已登录跳过登录页、走完过直接进首页），
        于是"启动停在哪"取决于账号是什么时候恢复的。已登录时那一屏写着
        「已经登录」+「下一步」，不会白挡一道。
        """
        self.app.session = _Session()
        self.app.wizard.finish()
        self.assertEqual(self.app.wizard.resume(), WizardStep.LOGIN)
        self.assertEqual(self.state()["step"], WizardStep.LOGIN)
        self.assertTrue(self.state()["product_gate_open"], "登录着，门禁是开的，点下一步就走")

    def test_every_role_says_it_uses_the_model(self):
        """三个角色都调用 AI —— 界面必须写明。

        反转记录：这条原来断言"活动助手/通用整理是本机规则，不调模型、不扣额度"。
        产品改成三个角色都走云端 AI 之后，这个字段反过来必须为真：商户选任何
        一个角色花的都是账号的用量，界面照实说（页面按它渲染「调用 AI」）。

        判据只在服务端 ``_roles_view`` 一处，页面不自己列角色。
        """
        roles = self.state()["roles"]
        by_id = {row["id"]: row for row in roles}
        self.assertEqual(sorted(by_id), ["activity", "generic", "order"])
        for role_id, row in by_id.items():
            with self.subTest(role=role_id):
                self.assertTrue(row["uses_model"], f"{role_id} 也走 AI，不能标成本机规则")
                self.assertTrue(row["action_label"].strip(), "按钮文案由服务端给")

    def test_the_account_view_only_shows_quota_it_really_has(self):
        """没取到额度就不给这些字段。

        默认那份 QuotaView 讲的是「未登录产品账号」，而它旁边就是
        「已登录：…」—— 同一屏自相矛盾，还顺带把 unlimited 说成真。
        """
        self.app.session = _Session()
        account = self.state()["account"]
        self.assertTrue(account["logged_in"])
        self.assertNotIn("quota_headline", account)

    def test_an_adopted_subscription_shows_a_real_percentage(self):
        """登录时抓到的那份额度要能直接显示成进度条（不联网）。"""
        self.app.session = _Session()
        self.app.wizard.adopt_subscription({
            "balance_known": True, "unlimited": False, "active": True,
            "quota_micro_usd": 1_000_000, "used_micro_usd": 250_000,
            "reserved_micro_usd": 0, "remaining_micro_usd": 750_000,
            "plan_code": "标准版", "period_end": "2026-10-25T00:00:00+00:00",
        })
        account = self.state()["account"]
        self.assertEqual(account["used_percent"], 25)
        self.assertFalse(account["unlimited"])
        self.assertIn("75%", account["quota_headline"])

    def test_the_quota_refresh_endpoint_refreshes_in_the_background(self):
        """进「智能整理」那一屏时刷一次真额度 —— 后台跑，不占住请求线程。"""
        self.app.session = _Session()
        started: list[int] = []
        self.app.check_quota = lambda: started.append(1)
        outcome = self.post("/api/account/quota")
        self.assertTrue(outcome["ok"])
        for _ in range(100):
            if started:
                break
            threading.Event().wait(0.05)
        self.assertTrue(started, "要在后台真的去查一次")

    def test_back_walks_backwards_on_the_interface(self):
        self.app.session = _Session()
        self.app.wizard.step = WizardStep.CONVERSATIONS
        self.assertTrue(self.post("/api/wizard/back")["ok"])
        self.assertEqual(self.state()["step"], WizardStep.WECHAT)

    def test_wizard_without_a_next_button_refuses_with_a_reason(self):
        """智能整理那一步要按「开始智能整理」——「下一步」必须说清不是没反应。"""
        self.app.session = _Session()
        self.app.wizard.step = WizardStep.ANALYZE
        outcome = self.post("/api/wizard/advance")
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["reason"])


if __name__ == "__main__":
    unittest.main()

class AnalysisProgressTests(unittest.TestCase):
    """分析进度：日志里留一份，界面上再给一句短的。

    模型出第一个字之前可能等几十秒（实测最长的一个会话等了 48 秒）。
    这段时间界面只有一个空框的话，商户只会以为卡死了 —— 有一句
    「正在分析第 2/3 个会话：某某群」才知道软件在动。
    """

    def test_progress_reaches_the_state_the_page_polls(self):
        import types
        from wechat_ai_assistant import web_server
        from wechat_ai_assistant.application import AppEvents, MerchantApplication

        state = web_server.WebState(app=types.SimpleNamespace())
        events = AppEvents()
        web_server.attach_events(types.SimpleNamespace(events=events), state)
        logged = []
        events.on_log = lambda line: logged.append(line)

        app = types.SimpleNamespace(events=events)
        MerchantApplication._analysis_progress(app, "正在分析第 2/3 个会话：某某群")

        self.assertEqual(state.snapshot()["analysis_progress"], "正在分析第 2/3 个会话：某某群")
        self.assertIn("正在分析第 2/3 个会话：某某群", logged, "日志里也要留一份")


class NoStreamReplayTests(unittest.TestCase):
    """完成态的直播区不许空着。

    商户在整理屏看到的正文就是这次的分析输出；没有流式正文（比如"这次没有
    新消息，显示上次报告"，模型一次都没调）时，服务端要把落盘的报告回放进
    直播区 —— 否则那一屏正文框是空的，商户只会以为整理失败了。
    """

    @staticmethod
    def _run(summary: dict, prefill_stream: str = ""):
        import types
        from wechat_ai_assistant import web_server

        state = web_server.WebState(app=types.SimpleNamespace())
        if prefill_stream:
            state.stream_summary(prefill_stream)
        app = types.SimpleNamespace(analyze=lambda role: summary)
        web_server.WebHandler._do_analyze(
            types.SimpleNamespace(app=app, state=state))
        return state

    def test_no_stream_replays_the_stored_report(self):
        state = self._run({
            "role_id": "generic", "output_kind": "report", "processed": 3,
            "role_report": {"summary": "需要注意：张姐记得带身份证"},
        })
        self.assertEqual(
            state.summary_stream, "需要注意：张姐记得带身份证",
            "没有流式正文时，报告要回放进直播区")

    def test_a_streamed_run_is_not_overwritten(self):
        state = self._run({
            "role_id": "generic", "output_kind": "report", "processed": 3,
            "role_report": {"summary": "完整报告正文"},
        }, prefill_stream="第一批正文……第二批正文……")
        self.assertTrue(
            state.summary_stream.startswith("第一批正文"),
            "流过的正文一个字都不能被回放覆盖")


class ReadingNoticeTests(unittest.TestCase):
    """「正在读数据库」这类过程说明只进小字，不进顶部横幅。

    横幅是"出事了/做完了"级别的位置；把过程说明放上去，等于每次读库都弹一条
    提示，商户会当噪音关掉——最后连真正的错误也不看了。
    """

    def test_it_goes_to_the_reading_line_not_the_banner(self):
        import types
        from wechat_ai_assistant import web_server
        from wechat_ai_assistant.application import AppEvents

        state = web_server.WebState(app=types.SimpleNamespace())
        events = AppEvents()
        web_server.attach_events(types.SimpleNamespace(events=events), state)

        events.on_notice("正在读取本机微信数据库")

        snapshot = state.snapshot()
        self.assertEqual(snapshot["reading_notice"], "正在读取本机微信数据库")
        self.assertEqual(snapshot["notice"], "", "不许占用横幅")
