"""商户看得见的那些底线：文案、显示、记账、流程事实。

## 为什么单独一个文件

这些规矩原先散在十几个"读 Tk 界面源码"的测试里（``merchant_ui.py`` 已删）。
它们盯的其实不是 Tk，而是**商户看到什么**：

* 界面上不许出现 token / provider / model 这类技术词；
* 长内容要能看出"被截断了"，而不是被无声切掉；
* 图片/语音这类没文字的消息要有占位，不能看着像坏了；
* "读到的消息来自演示、订单是规则拼的"必须如实说出来；
* 跑过 AI 的消息要记成"整理过"。

索性收成一个文件，**断言契约而不是某个文件的文本**：以后界面再换，只有这个
文件需要跟着看，而不是十几个。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations  # noqa: E402
from wechat_ai_assistant.dashboard import STATUS_LABELS  # noqa: E402
from wechat_ai_assistant.history_read import SimulatedHistory  # noqa: E402
from wechat_ai_assistant.merchant_text import (  # noqa: E402
    CELL_LIMIT,
    FORBIDDEN_TERMS,
    audit_text,
    cell,
    data_origin_note,
    display_content,
)
from wechat_ai_assistant.orders import OrderStore  # noqa: E402
from wechat_ai_assistant.runmode import AutoOrderSettings, RunMode  # noqa: E402
from wechat_ai_assistant.wizard import QuotaView  # noqa: E402


class MerchantWordingTests(unittest.TestCase):
    """会出现在商户眼前的文字，一个技术词都不许有。"""

    def test_audit_text_actually_detects_leaks(self):
        self.assertIn("token", audit_text("这里有 token"))
        self.assertEqual(audit_text("好的，已记单：苹果 2箱"), [])

    def test_the_forbidden_list_covers_the_obvious_leaks(self):
        for term in ("token", "api key", "opencode", "model"):
            self.assertIn(term, FORBIDDEN_TERMS)

    def test_every_state_has_chinese_explanation_without_tech_words(self):
        from wechat_ai_assistant.wechat_state import STATE_COPY, WeChatState

        for state in WeChatState.ALL:
            title, detail, _button = STATE_COPY[state]
            with self.subTest(state=state):
                self.assertTrue(title.strip())
                self.assertTrue(detail.strip())
                self.assertEqual(audit_text(title + detail), [])

    def test_status_labels_and_quota_headlines_are_clean(self):
        strings = list(STATUS_LABELS.values())
        strings += [QuotaView().headline(),
                    QuotaView(remaining=5).headline(),
                    QuotaView(logged_in=True, active=False).headline(),
                    QuotaView(logged_in=True, active=True, remaining=0).headline(),
                    AutoOrderSettings().describe(),
                    ]
        for text in strings:
            with self.subTest(text=text[:24]):
                self.assertEqual(audit_text(text), [], f"商户文案里出现了技术词：{text[:60]}")


class DisplayTests(unittest.TestCase):
    """表格里的一格：能看出被截断，空内容按类型给占位。"""

    def test_short_text_is_untouched(self):
        self.assertEqual(cell("苹果两箱"), "苹果两箱")

    def test_long_text_ends_with_an_ellipsis(self):
        long_text = ("客户说要三箱红富士，另外再带两箱砂糖橘，明天上午送到门口"
                     "，别忘了带一箱鸡蛋，上次漏了")
        self.assertGreater(len(long_text), CELL_LIMIT, "这条测试要真的超过一格的宽度")
        shown = cell(long_text)
        self.assertTrue(shown.endswith("…"), "被切掉却不加省略号，看着就像订单就这么长")
        self.assertEqual(len(shown), CELL_LIMIT + 1)

    def test_newlines_do_not_break_the_row(self):
        self.assertEqual(cell("第一行\n第二行"), "第一行 第二行")

    def test_empty_content_shows_the_kind_label(self):
        self.assertEqual(display_content("text", "苹果两箱"), "苹果两箱")
        self.assertEqual(display_content("image", ""), "［图片］")
        self.assertEqual(display_content("voice", None), "［语音］")
        self.assertEqual(display_content("video", "  "), "［视频］")
        self.assertEqual(display_content("other", ""), "［非文字消息］")
        self.assertEqual(display_content("whatever", ""), "［非文字消息］")


class DataOriginTests(unittest.TestCase):
    """屏幕上的东西从哪来：两件独立的事实，各说各的。

    都不成立时**什么都不显示** —— 正常使用时一直挂着警告，会让真正的警告失去分量。
    """

    class _View:
        def __init__(self, simulated: bool, model_executed: bool):
            self.simulated = simulated
            self.model_executed = model_executed

    def test_clean_when_both_are_real(self):
        self.assertEqual(data_origin_note(self._View(False, True)), "")

    def test_demo_messages_are_disclosed(self):
        self.assertIn("演示", data_origin_note(self._View(True, True)))

    def test_rule_based_orders_are_disclosed(self):
        note = data_origin_note(self._View(False, False))
        self.assertIn("规则", note)
        self.assertNotIn("演示", note, "消息是真的，就不能说成演示数据")

    def test_both_problems_are_both_said(self):
        note = data_origin_note(self._View(True, False))
        self.assertIn("演示", note)
        self.assertIn("规则", note)


class OrganizedBookkeepingTests(unittest.TestCase):
    """跑过 AI 的消息要记成"整理过" —— 漏记会被当成"没整理过"卡住。

    这一步原来在 Tk 窗口里做（界面已删），现在归 ``summary_task.mark_organized``：
    谁跑摘要谁记账。
    """

    def setUp(self):
        from wechat_ai_assistant.summary_task import mark_organized

        self.mark_organized = mark_organized
        self.dir = Path(tempfile.mkdtemp(prefix="pyw-guard-"))
        self.store = OrderStore(str(self.dir / "orders.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.group = "测试群"

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _messages(self, contents):
        self.store.add_import(
            self.tenant, self.group,
            [{"sender": "张姐", "content": c, "kind": "text",
              "sent_at": f"2026-10-14T09:{i:02d}:00-04:00", "source_id": f"s{i}"}
             for i, c in enumerate(contents)], "src", "demo")
        rows = self.store.db.execute(
            "SELECT id, group_id FROM messages WHERE merchant_id=? ORDER BY id",
            (self.tenant,)).fetchall()
        return [{"id": str(r["id"]), "group_id": str(r["group_id"])} for r in rows]

    def test_analyzed_messages_become_organized(self):
        messages = self._messages(["要两箱苹果", "再要一箱"])
        marked = self.mark_organized(self.store, self.tenant, messages,
                                     {"completed_groups": []}, len(messages), "order")
        self.assertEqual(set(marked), {m["id"] for m in messages})
        organized = self.store.organized_message_ids(self.tenant, "order") \
            if hasattr(self.store, "organized_message_ids") else None
        if organized is not None:
            self.assertTrue({m["id"] for m in messages} <= organized)

    def test_nothing_processed_marks_nothing(self):
        messages = self._messages(["要两箱苹果"])
        self.assertEqual(self.mark_organized(self.store, self.tenant, messages,
                                             {"completed_groups": []}, 0, "order"), [])

    def test_only_completed_groups_are_marked(self):
        messages = self._messages(["甲", "乙"])
        messages[0]["group_id"] = "完成群"
        messages[1]["group_id"] = "失败群"
        marked = self.mark_organized(self.store, self.tenant, messages,
                                     {"completed_groups": ["完成群"]}, len(messages), "order")
        self.assertEqual(marked, [messages[0]["id"]])


class FlowFactsTests(unittest.TestCase):
    """流程事实（编号、标题、短名）只由 ``WizardStep`` 给。"""

    def test_login_is_a_front_gate_and_numbering_starts_at_checking_wechat(self):
        """登录页是**前置门槛**：不在流程里、不占编号，编号从「检查微信」开始。"""
        from wechat_ai_assistant.wizard import WizardStep

        self.assertNotIn(WizardStep.LOGIN, WizardStep.ORDER)
        self.assertEqual(WizardStep.ORDER[0], WizardStep.WECHAT)
        self.assertEqual(WizardStep.page_title(WizardStep.WECHAT), "第 0 步　检查微信")
        self.assertEqual(WizardStep.page_title(WizardStep.LOGIN), "登录产品账号",
                         "登录页有标题，但没有编号")
        self.assertTrue(WizardStep.SHORT[WizardStep.CONVERSATIONS])

    def test_page_titles_and_numbers_come_from_one_table(self):
        from wechat_ai_assistant.wizard import WizardStep

        for index, step in enumerate(WizardStep.ORDER):
            with self.subTest(step=step):
                self.assertEqual(WizardStep.number(step), index)
                self.assertEqual(WizardStep.page_title(step),
                                 f"第 {index} 步　{WizardStep.TITLES[step]}")

    def test_the_pages_do_not_hardcode_step_facts(self):
        """页面（Web）不许自己写"第 N 步"或步骤名 —— 从 ``/api/state`` 取。"""
        from wechat_ai_assistant.wizard import WizardStep

        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        self.assertNotIn("第 0 步", page)
        self.assertNotIn("第 1 步", page)
        for step in WizardStep.ORDER:
            with self.subTest(step=step):
                self.assertNotIn(f'"{WizardStep.SHORT[step]}"', page)


class NoSilentFailuresTests(unittest.TestCase):
    """「装死」类：失败必须走到界面上，不能点了没反应。

    以前这些断言长在 Tk 界面测试里（文件已删）。判据没变，只是换了一层：
    失败 → ``AppEvents.on_error`` → ``/api/state.error`` → 页面顶部横幅。
    """

    def _state_with_events(self):
        from types import SimpleNamespace

        from wechat_ai_assistant import web_server
        from wechat_ai_assistant.application import AppEvents

        app = SimpleNamespace(events=AppEvents(), wizard=None)
        state = web_server.WebState(app=app)
        web_server.attach_events(app, state)
        return app, state

    def test_a_failing_background_task_reaches_the_interface(self):
        app, state = self._state_with_events()
        app.events.on_error("读取消息失败，已读到的部分保留着。")
        self.assertIn("读取消息失败", state.snapshot()["error"],
                      "后台任务失败没有传到界面，商户只会觉得点了没反应")

    def test_every_background_task_has_a_message_of_its_own(self):
        """每个后台任务失败都要有专门的说法，不能一律"这一步没能完成"。"""
        from wechat_ai_assistant.application import MerchantApplication

        for name in ("history-read", "quota", "models", "analyze", "login"):
            with self.subTest(task=name):
                self.assertIn(name, MerchantApplication.FAILURE_TEXT)

    def test_the_analyze_page_has_a_place_to_show_failures(self):
        """整理失败要有地方显示 —— 以前"第 4 步没处放"，错误被吞掉。

        页面把 ``state.error`` 画进顶部横幅。**唯一的例外**是一次已经过期的登录
        失败（账号此刻是登录着的，那次回调早就没意义），页面用 ``staleLoginError``
        只放过这一种；别的错误必须照显示。
        """
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        self.assertIn("note(data.error", page, "错误要画到横幅上")
        self.assertIn('startsWith("登录失败：")', page,
                      "只许放过期的登录失败，别的失败一律照显示")

    def test_when_there_is_nowhere_it_still_tells_the_user(self):
        app, state = self._state_with_events()
        app.events.on_error("智能整理失败，已读到的消息保留着，没有产生订单。")
        self.assertTrue(state.snapshot()["error"])


class ReadButNotStoredTests(unittest.TestCase):
    """「读了 35 条却只入库 0 条，商户不知道」——两种 0 必须分开说。"""

    def _view(self, fetched: int, count: int):
        from types import SimpleNamespace

        from wechat_ai_assistant import web_server

        item = SimpleNamespace(
            conversation=SimpleNamespace(name="鲜果批发群", kind="group"),
            message_count=count, truncated=False, unresolved_senders=0,
            fetched_count=fetched, skipped=False, error="",
        )
        result = SimpleNamespace(total_messages=count, mode="all", coverage=[item],
                                 import_ids=[], headline=lambda: "本次读到 0 条")
        return web_server._history_view(result)

    def test_read_but_not_stored_says_so(self):
        row = self._view(fetched=35, count=0)["coverage"][0]
        self.assertEqual(row["fetched"], 35, "读取器拿回多少要传出去")
        self.assertEqual(row["count"], 0, "进了库多少也要传出去")
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        self.assertIn("但本次一条都没进库", page, "页面要把这句说出来")

    def test_the_two_are_not_the_same_message(self):
        """「读取器拿回 0 条」≠「一条都没进库」——不能合成一句话。"""
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        self.assertIn("这不等于群里没消息", page)


class OrderCountTests(unittest.TestCase):
    """「订单超量却不提示」——不许静默截断，且要说清一共多少单。"""

    def test_it_states_how_many_orders_there_are(self):
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        self.assertIn("共 ", page)
        self.assertIn(" 单。", page)

    def test_it_no_longer_silently_drops_orders_past_a_limit(self):
        """``/api/orders`` 不做条数截断：返回多少就是多少。"""
        import json
        import threading
        import urllib.request
        from types import SimpleNamespace

        from wechat_ai_assistant import web_server
        from wechat_ai_assistant.application import AppEvents

        rows = [{"id": f"o{i}", "customer_name": "张姐", "status": "open",
                 "total_cents": 2600, "summary": "苹果两箱", "created_at": ""}
                for i in range(80)]
        app = SimpleNamespace(
            orders_for_display=lambda: rows,
            refresh_conversations=lambda: [],
            session=None,
            tenant="t",
            events=AppEvents(),
            wizard=None,
        )
        server = web_server.create_server(app, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = "http://%s:%d" % server.server_address[:2]
            with urllib.request.urlopen(base + "/api/orders", timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(len(data["orders"]), 80, "超过 60 单就被截断，界面还装作全都在")
        finally:
            server.shutdown()
            server.server_close()

    def test_the_orders_api_says_which_conversation_each_order_came_from(self):
        """``/api/orders`` 要带「来自哪个会话」。

        客户按名字认（同名即同一人），跨群的重名单子只能靠这一列分清。
        """
        import json
        import threading
        import urllib.request
        from types import SimpleNamespace

        from wechat_ai_assistant import web_server
        from wechat_ai_assistant.application import AppEvents

        rows = [{"id": "o1", "customer_name": "张姐", "status": "open", "total_cents": 2600,
                 "summary": "苹果两箱", "created_at": "", "group_id": "鲜果批发群"}]
        app = SimpleNamespace(
            orders_for_display=lambda: rows,
            refresh_conversations=lambda: [],
            session=None,
            tenant="t",
            events=AppEvents(),
            wizard=None,
        )
        server = web_server.create_server(app, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = "http://%s:%d" % server.server_address[:2]
            with urllib.request.urlopen(base + "/api/orders", timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(data["orders"][0]["group"], "鲜果批发群")
        finally:
            server.shutdown()
            server.server_close()

    def test_the_order_table_shows_the_source_column(self):
        """表格里真有这一列，点开某一行也写着来源。"""
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        self.assertIn("<th>来自</th>", page)
        self.assertIn("（未记来源）", page, "没来源的要说清楚，不显示空白")
        self.assertIn("来自：", page, "点开单行也要写来源")

    def test_the_order_view_understands_the_real_order_row(self):
        """``_order_view`` 要认识**真实的** ``OrderRow``，不只是测试用的 dict。

        回归的是一条只有真机上才看得见的空列：``OrderRow`` 的字段叫
        ``order_id`` / ``amount`` / ``updated_at``，而这里原来只按
        ``id`` / ``total_cents`` / ``created_at`` 取 —— 于是产品里
        「订单号」「金额」「时间」三列**恒为空**，而测试全绿，
        因为测试塞的是 dict（形状和产品给的不是一个）。
        """
        from wechat_ai_assistant import web_server
        from wechat_ai_assistant.dashboard import OrderRow

        row = OrderRow(order_id="o1", customer="张姐", status="review",
                       summary="苹果 2 箱", amount=26.0,
                       updated_at="2026-10-18T12:00:00+00:00", group="鲜果批发群")
        view = web_server._order_view(row)
        self.assertEqual(view["id"], "o1", "订单号列不许是空的")
        self.assertEqual(view["total"], 26.0, "金额列不许是空的")
        self.assertEqual(view["created_at"], "2026-10-18T12:00:00+00:00")
        self.assertEqual(view["group"], "鲜果批发群")

    def test_the_order_view_still_understands_plain_dicts(self):
        """另一头也得认：外部调用方/测试塞的是 ``total_cents`` 那种 dict。

        分要换成元 —— 页面上直接印这个数，印 2600 会被读成两千六。
        """
        from wechat_ai_assistant import web_server

        view = web_server._order_view({"id": "o9", "customer_name": "李叔",
                                       "total_cents": 2600, "created_at": "x",
                                       "group_id": "群A"})
        self.assertEqual(view["id"], "o9")
        self.assertEqual(view["customer"], "李叔")
        self.assertEqual(view["total"], 26.0, "total_cents 是分，要除以 100")
        self.assertEqual(view["created_at"], "x")
        self.assertEqual(view["group"], "群A")


class ReacquiredGuardsTests(unittest.TestCase):
    """「装死 / 说假话 / 假装」那几条：界面换成 Web 之后重新钉住。

    ``examples/guard_map.py`` 登记了商户看得见的症状。它们原来的断言长在 Tk 界面
    测试里，随界面一起删了 —— 这个类把**还能成立的**部分按新层重新钉住：

    * 进度不再长期停在"准备中"（读完了就有结果和下一步）；
    * 演示数据要明说；登录后要写明是谁；
    * 按钮有主次；没有发送功能就别摆发送开关；
    * 长内容截断要看得出来；
    * 小窗口下按钮仍在（Web：操作条不在滚动区里，永远贴底）。
    """

    @classmethod
    def setUpClass(cls):
        cls.page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        cls.server = (ROOT / "src" / "wechat_ai_assistant" / "web_server.py").read_text(
            encoding="utf-8")

    def test_after_reading_the_page_does_not_say_ready(self):
        """读完后进度区写「已完成」—— 不要一直挂着"准备中"。"""
        self.assertNotIn("准备中", self.page, "进度区不许长期写「准备中」")
        self.assertIn("data.history", self.page, "读完了要画结果")

    def test_the_next_button_only_appears(self):
        """「下一步」只在真有读取结果时出现。

        反转记录：这条原来要求"没有结果时把「下一步」灰着"（断言
        ``disabled:!h``）。页面改成"读完只留流程导航、没读完只留读取按钮"之后，
        它压根不渲染 —— 比"渲染出来再灰掉"更没有误点的余地。
        """
        body = self.page[self.page.index("  read(data)"):self.page.index("  analyze(data)")]
        self.assertIn("else if (h)", body, "有结果那一支要认得出来")
        with_result = body[body.index("else if (h)"):body.index("} else {")]
        self.assertIn('label:"下一步"', with_result, "有结果时才给「下一步」")
        self.assertNotIn('label:"下一步"', body.replace(with_result, ""),
                         "没结果时不许摆一个不能点的「下一步」")

    def test_a_demo_source_says_so_explicitly(self):
        """模拟数据源时会明说 —— 真实数据反过来也不许被说成演示。"""
        view = type("V", (), {"simulated": True, "model_executed": True})()
        self.assertIn("演示", data_origin_note(view))

    def test_the_header_shows_who_is_logged_in(self):
        """登录后账号条写明是谁 —— 顶栏不能一直写「未登录」。"""
        self.assertIn("已登录：", self.page)
        # 账号状态以应用为准，不是登录那一刻的缓存副本。
        self.assertIn("def _account_now", self.server)
        self.assertIn("_account_view(self.app)", self.server)

    def test_the_quota_bar_is_drawn_from_what_is_left(self):
        """额度条画**剩余**，不画已用。

        报过的问题：新账号"分析额度剩余 100%"，但条是按"已用 0%"画的 —— 一条
        空的进度条。商户看到的是"额度是空的"，以为没额度了。满额就该是满条。
        """
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        body = page[page.index("function quotaHtml"):page.index("function referenceCard")]
        self.assertIn("100 - used", body, "宽度必须由剩余算出")
        self.assertIn('style="width:\' + left + \'%"', body, "条宽用的是剩余")
        self.assertIn('"剩余 " + left + "%', body, "数字也要写剩余，和标题口径一致")

    def test_reading_does_not_claim_to_operate_the_wechat_window(self):
        """读取只解密数据库副本、不碰窗口 —— 明面上不许说"会操作微信窗口"。

        反转记录：这句原来是"读取时会短暂操作微信窗口，请勿同时操作电脑"。
        界面链和内存 carve 都退役之后，读取一次窗口都不点，那句话就成了假话；
        真正要提醒的是"解密比较占机器、这期间别关微信"。
        """
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        self.assertNotIn("读取时会短暂操作微信窗口", page)
        self.assertIn("别关微信", page, "真正的注意事项要留在页面上")

    def test_the_analyze_page_says_how_roles_are_charged(self):
        """换角色会重新分析并计费、一次最多 200 条 —— 页面上要说明白。

        反转记录：以前这里只有一句「已整理过的不再重复提交」，读起来像是不花钱。
        实际是**每个角色各记进度**：同一个角色再跑只发新增，换角色会把同一批消息
        再送一遍，按用量结算。
        """
        body = self.page[self.page.index("  analyze(data)"):self.page.index("  results(data)")]
        self.assertIn("每个角色各自记进度", body)
        self.assertIn("换角色会把同一批消息重新分析一遍", body)
        self.assertIn("一次最多 200 条", body)

    def test_the_role_report_survives_a_restart_in_the_interface(self):
        """报告落盘之后**要有入口**：结果屏的按钮 + 放报告的容器 + 带上角色去取。

        只落盘不给入口 = 商户还是看不到，等于没存。（跑完向导没有首页了：
        结果屏看产物，「重新开始」回第一步。）
        """
        results = self.page[self.page.index("  results(data)"):]
        self.assertIn('action:"role-report"', results, "结果屏要能点开报告")
        self.assertIn('id="roleReport"', results, "点开了得有地方放")
        self.assertIn("/api/analysis/report?role=", self.page, "取报告要指明是哪个角色的")

    def test_a_multi_conversation_report_is_split_per_conversation(self):
        """多选会话时，报告要能**按会话分开看**，不是一坨拼起来的全文。

        反转记录：以前只有一份「【群A】…【群B】…」的拼接全文，商户想只看某个群
        得在长文里翻。现在每个会话一个可折叠块（第一个展开），合并全文收在最下面。
        单会话仍然是一段正文 —— 给一个会话套一层折叠是多余的。
        """
        self.assertIn("function roleGroupBlocks", self.page, "要有按会话渲染的函数")
        self.assertIn("groups.length <= 1", self.page, "只有一个会话时不摆折叠块")
        self.assertIn("合并全文", self.page, "多会话时合并全文仍然拿得到")
        # 键名必须和 /api/analysis/report 一致（内存/落盘都用 groups）。
        self.assertIn("report.groups", self.page)

    def test_the_refresh_button_row_is_not_pinned_to_the_top(self):
        """「刷新列表」那行不吸顶；分类页签和"已选 N 个"才吸顶。

        原来整块（刷新按钮 + 页签 + 已选）一起 sticky，等于从会话列表里长期
        截走一块高度 —— 而刷新是一次性维护动作，不需要一直挂在那儿。
        290 个会话往下翻时，真正需要一直可见的是**分类页签**和**已选几个**。
        """
        css_start = self.page.index(".conv-header {")
        css_block = self.page[css_start:self.page.index(".conv-sticky {")]
        self.assertNotIn("position:sticky", css_block, "刷新按钮那行不该吸顶")
        sticky = self.page[self.page.index(".conv-sticky {"):self.page.index(".conv-toolbar {")]
        self.assertIn("position:sticky", sticky, "页签那行仍然要吸顶")        # 结构上分开：刷新按钮在 conv-header 里，页签在 conv-sticky 里。
        body = self.page[self.page.index("  conversations(data)"):self.page.index("  read(data)")]
        header_at = body.index('class="conv-header"')
        sticky_at = body.index('class="conv-sticky"')
        self.assertLess(header_at, sticky_at)
        self.assertIn("刷新列表", body[header_at:sticky_at], "刷新按钮要在不吸顶那块里")
        self.assertIn("conversationTabsHtml(rows)", body[sticky_at:], "页签要在吸顶那块里")

    def test_the_conversation_list_shows_a_readable_label_not_the_raw_id(self):
        """会话列显示 ``label``；勾选的 value 仍是 ``name``（数据键）。

        解密库读到的未命名群，``name`` 是 ``18815684683@chatroom``；直接显示出来
        商户会以为"没读出来"。原始 ID 只放进 ``title``（悬停可查），不占正文。
        """
        body = self.page[self.page.index("function conversationsHtml"):self.page.index("function render(")]
        self.assertIn("row.label || row.name", body, "显示要用 label")
        self.assertIn("value=", body)
        self.assertIn("esc(row.name)", body, "勾选仍以 name 为键")
        self.assertIn("title=", body, "原始 ID 留作悬停可查")

    def test_maintenance_buttons_are_lighter_than_the_primary(self):
        """维护按钮都比主按钮弱 —— 一屏里只有一个主按钮。"""
        self.assertIn("button.primary", self.page)
        self.assertIn("button:hover:not(:disabled)", self.page)

    def test_no_send_switch_when_nothing_is_sent(self):
        """不会发送时不显示发送开关 —— 产品里根本没有自动发送。"""
        self.assertNotIn("暂停发送", self.page)
        self.assertNotIn("恢复发送", self.page)

    def test_long_text_is_marked_as_truncated(self):
        """过长时带省略号 —— 截断要看得出来。"""
        self.assertTrue(cell("苹" * (CELL_LIMIT + 5)).endswith("…"))

    def test_the_actions_stay_reachable_in_a_small_window(self):
        """首页所有控件都在窗口内 —— Web 的做法：操作条贴底、不跟着内容滚。"""
        self.assertIn("footer { background:var(--card); border-top:1px solid var(--line);", self.page)
        self.assertIn("footer.hidden = !view.actions.length;", self.page)

    def test_the_page_never_shadows_its_own_helpers(self):
        """``const note = $(...)`` 会把 ``note()`` 遮住 —— 页面直接冻在「正在加载…」。

        这是真实出现过的 bug（"新版已就绪"那块加了一个同名局部变量）。
        局部变量叫 ``noteEl``，别再撞名。
        """
        self.assertNotIn("const note =", self.page)
        self.assertIn("const noteEl =", self.page)

    def test_a_render_error_is_shown_not_swallowed(self):
        """渲染出错也要说出来：冻在「正在加载…」上，商户只会觉得软件坏了。"""
        self.assertIn("console.error(err)", self.page)
        self.assertIn("这一屏没能画出来", self.page)

    def test_the_analysis_screen_splits_the_stream_from_the_categories(self):
        """分析中那一屏：左边流式正文，右边这次要盯的类别。

        反转记录：模型出第一个字之前可能等 48 秒（实测最长的一个会话），
        屏幕上只有一个「等待 AI 首段内容…」的空框 —— 商户只会以为卡死了。
        所以左栏要有进度（正在分析第几个会话），右栏要按角色给的类别
        把已经写出来的内容归位。
        """
        self.assertIn("analysis-split", self.page, "分析中要左右两栏")
        self.assertIn("需要关注的类别", self.page)
        self.assertIn("analysis_progress", self.page, "进度要来自服务端，不是页面自己编")
        self.assertIn("模型还在读这一批", self.page, "还没出字时也要有话说")
        self.assertIn("推理过程", self.page, "推理过程要单独显示出来")
        self.assertIn("不是分析结论", self.page, "推理不是结论，这句必须标清楚")
        self.assertIn("reasoning_stream", self.page)
        self.assertIn("splitByHeadings", self.page, "按报告标题切出类别")
        self.assertIn("currentSections", self.page, "类别来自服务端给的 sections")

    def test_when_the_service_is_gone_it_says_what_to_do(self):
        """服务真没了，页面不能永远只喊「正在重试」。

        反转记录：重启客户端时旧窗口留在了停掉的端口上，徽标一秒一次地
        「连接中断，正在重试…」，而商户唯一能做的（关掉窗口重开）没有任何提示。
        短时断连仍先自己重试 —— 重启那几秒不值得吓人。
        """
        body = self.page[self.page.index("function setConnectionStatus"):
                         self.page.index("async function post(")]
        self.assertIn("连接中断，正在重试", body, "短时断连先自己重试")
        self.assertIn("连不上本机服务", body, "久连不上要直说连不上")
        self.assertIn("关掉窗口重开", body, "还要给一条出路")
        self.assertIn("connFailedSince", body, "按「从什么时候开始断」判断久不久")
        self.assertIn("CONN_GIVE_UP_MS", body)
        self.assertIn("不用重新读取", body, "重开之后不用重读，这句要说")


if __name__ == "__main__":
    unittest.main()

class PrivacyStatementTests(unittest.TestCase):
    """数据与隐私说明：合规陈述要**在**，但平时折起来。

    这是"有，但不用真的读"——章节该有的都有，商户点得开；不点就只看到一行小字。
    """

    def test_it_covers_what_a_privacy_notice_must_cover(self):
        from wechat_ai_assistant.merchant_text import PRIVACY_TEXT

        for section in ("谁在处理", "处理目的", "不会发送", "在本机完成的部分",
                        "保存期限", "您的权利", "安全措施", "变更与联系"):
            self.assertIn(section, PRIVACY_TEXT, "隐私说明缺了「%s」" % section)

    def test_it_speaks_merchant_language(self):
        from wechat_ai_assistant.merchant_text import PRIVACY_TEXT, audit_text

        self.assertEqual(audit_text(PRIVACY_TEXT), [],
                         "隐私说明里不该出现实现词（token/session/sqlite/model…）")

    def test_the_page_renders_it_collapsed_at_the_bottom(self):
        from wechat_ai_assistant import web_wizard_page

        page = web_wizard_page.PAGE
        self.assertEqual(page.count("privacyCard()"), 2,
                         "只该有定义 + 首页那一处调用（登录屏不放）")
        self.assertIn("<details", page)
        self.assertIn('<summary class="hint">数据与隐私说明</summary>', page,
                      "折叠块的标题要就这一句，别写成营销话")
