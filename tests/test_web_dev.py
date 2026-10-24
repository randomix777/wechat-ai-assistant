"""开发 Web 界面的服务端。

## 三件必须测的事

### 一、只许本机

这个界面能读商户全部消息、订单和密钥配置，**没有任何鉴权**。绑定地址写错成
``0.0.0.0`` 就会被局域网摸到。所以除了绑 ``127.0.0.1``，还要**主动拒绝
非回环来源的请求** —— 只靠绑定地址不够，因为它可能被别的东西改掉。

### 二、选错会话比不选更糟

页面只能传**名字**（JSON 里就这些），而 wizard 要的是 **对象**。
如果按名字取不到对象却「猜一个」，读到的就是**别的群**的聊天记录，
而商户在界面上看不出任何差异。

### 三、阻塞动作不能占着处理线程

``analyze`` 和 ``run_summary`` 内部都是 ``join()``。放在 HTTP 处理线程里
会把整个服务卡死 —— 连 ``/api/state`` 都取不到，页面看起来像整体挂了，
而且看不出是哪个接口的问题。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in ("src", "packages/business_agent/src", "packages/order_agent/src"):
    sys.path.insert(0, str(ROOT / extra))

from wechat_ai_assistant import summary_task, web_dev  # noqa: E402


class _FakeApp:
    """只提供 web_dev 会碰到的那些方法。"""

    def __init__(self, conversations=()):
        self.orders = None
        self.tenant = "m-test"
        self.events = types.SimpleNamespace()
        self._conversations = list(conversations)
        self.chosen: list = []
        self.read_calls = 0

    def refresh_conversations(self):
        return list(self._conversations)

    def choose_history_conversations(self, selected):
        self.chosen = list(selected)

    def read_history(self, **kwargs):
        self.read_calls += 1

    def cancel_history(self):
        pass

    def orders_for_display(self):
        return []

    def check_wechat(self):
        return types.SimpleNamespace(state="ready", detail="ok")

    def start_wechat(self):
        pass


def _serve(app):
    server = web_dev.create_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Client:
    def __init__(self, server):
        self.base = "http://%s:%d" % server.server_address[:2]

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return r.status, r.read()

    def post(self, path, body=None):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def post_expect_error(self, path, body=None):
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
        raise AssertionError("本该失败却成功了")


class PageAndStateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = _FakeApp([types.SimpleNamespace(name="鲜果批发群", kind="group",
                                                   member_count=35)])
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def test_the_page_is_served_and_is_chinese(self):
        status, body = self.client.get("/")
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        # 品牌名跟着桌面端叫「微信AI智能助理」（以前叫接单助手，改名时测试没跟上）。
        self.assertIn("微信AI智能助理", html)
        for term in ("读取历史", "智能整理", "商品与客户名单", "订单", "运行日志"):
            self.assertIn(term, html, f"\u9875\u9762\u91cc\u6ca1\u6709\u300c{term}\u300d")
        # 老路「整理成一段话」收了：商户只有「选角色 → 开始分析」这一条路，
        # 页面上再摆一个入口，就是给第二条说法留门。
        self.assertNotIn("整理成一段话", html)

    def test_state_has_everything_the_page_needs(self):
        status, body = self.client.get("/api/state")
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(status, 200)
        for key in ("step", "steps", "busy", "error", "logs", "conversations", "orders",
                    "summary_stream", "reasoning_stream", "analysis_progress", "analysis",
                    "history", "progress", "full_analysis_pending",
                    "analysis_ready"):
            self.assertIn(key, data, f"页面要用 {key}，但状态里没有")

    def test_the_steps_come_from_the_wizard_not_from_the_page(self):
        """步骤条数据由向导给出：编号、标题、走到哪一步。

        页面若自己写"第 N 步"，加一步就要改页面；这里盯住数据面的契约，
        文案/编号怎么变，页面都不用动。
        """
        from wechat_ai_assistant.wizard import WizardStep

        status, body = self.client.get("/api/state")
        self.assertEqual(status, 200)
        steps = json.loads(body.decode("utf-8"))["steps"]
        self.assertEqual([row["id"] for row in steps], list(WizardStep.ORDER))
        for index, row in enumerate(steps):
            self.assertEqual(row["number"], index)
            self.assertEqual(row["page_title"], WizardStep.page_title(row["id"]))
            self.assertIn(row["state"], ("done", "current", "todo"))
        self.assertEqual(steps[0]["id"], WizardStep.WECHAT, "步骤条从「检查微信」开始")
        self.assertEqual(steps[0]["number"], 0, "登录页是前置门槛，不占编号")

    def test_an_unknown_or_empty_step_marks_everything_todo(self):
        """还没进向导时不许假装走到哪一步了。"""
        from wechat_ai_assistant.web_server import _steps_view

        for current in ("", "没有这一步"):
            with self.subTest(current=current):
                self.assertEqual({row["state"] for row in _steps_view(current)}, {"todo"})

    def test_the_current_step_is_marked_and_the_earlier_ones_done(self):
        from wechat_ai_assistant.web_server import _steps_view
        from wechat_ai_assistant.wizard import WizardStep

        rows = _steps_view(WizardStep.READ)
        states = [row["state"] for row in rows]
        position = WizardStep.number(WizardStep.READ)
        self.assertEqual(states[position], "current")
        self.assertEqual(set(states[:position]), {"done"})
        self.assertEqual(set(states[position + 1:]), {"todo"})
        # 额度页与智能整理同屏：标记在智能整理那一步上。
        self.assertEqual(_steps_view(WizardStep.QUOTA), _steps_view(WizardStep.ANALYZE))

    def test_an_unknown_address_says_so_in_chinese(self):
        code, data = self.client.post_expect_error("/api/nope")
        self.assertEqual(code, 404)
        self.assertIn("没有这个地址", data["error"])

    def test_it_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1",
                         "\u7ed1\u5b9a\u5730\u5740\u5199\u9519\u4f1a\u88ab\u5c40\u57df\u7f51\u6478\u5230")

    def test_a_remote_address_is_rejected(self):
        """"只绑本机"不够 —— 绑定地址可能被改掉，所以要主动拒绝来源。"""
        source = (ROOT / "src/wechat_ai_assistant/web_server.py").read_text(encoding="utf-8")
        self.assertIn("_reject_remote", source)
        self.assertIn("403", source)
        # \u5fc5\u987b\u5728\u6bcf\u4e2a\u5165\u53e3\u90fd\u8c03
        self.assertIn('if self._reject_remote():', source)


    def test_the_page_shows_which_data_directory_it_reads(self):
        """目录错了就表现为「没数据 / 未登录」，不显示出来没人会往哪找。"""
        source = (ROOT / "src/wechat_ai_assistant/web_server.py").read_text(encoding="utf-8")
        self.assertIn("__DATADIR__", source, "\u9875\u9762\u6ca1\u6709\u63a5\u6536\u6570\u636e\u76ee\u5f55")

    def test_the_placeholder_is_always_replaced(self):
        """占位符残留在页面上 = 看到一句“__DATADIR__”。"""
        html = self.client.get("/")[1].decode("utf-8")
        self.assertNotIn("__DATADIR__", html, "占位符没被替换")
        self.assertNotIn("__VERSION__", html, "版本号的占位符没被替换")

    def test_the_page_says_where_the_conversation_list_came_from(self):
        """会话名单的来源必须写在界面上。

        现在只有解密库一条路；没列出来（没钥匙）也要照实说，
        否则商户看到空名单只会以为软件坏了。
        """
        page = (ROOT / "src" / "wechat_ai_assistant" / "web_wizard_page.py").read_text(
            encoding="utf-8")
        self.assertIn("conversation_source", page)
        self.assertIn("解密数据库", page)
        self.assertNotIn("内存扫描", page, "内存 carve 兜底已删除，界面不应再提")
        self.assertIn("/api/conversations?force=1", page,
                      "点刷新要强制重提密钥，不能被 5 分钟节流拦住")

    def test_the_page_title_carries_the_version(self):
        """窗口标题带版本号：两个版本同时开着时，一眼能看出是哪一版。

        Edge 兜底窗口的标题来自页面 <title>，pywebview 的标题由 open_window 传。
        """
        from wechat_ai_assistant import web_window
        from wechat_ai_assistant.version import __version__

        _, body = self.client.get("/wizard")
        html = body.decode("utf-8")
        self.assertIn(f"<title>微信AI智能助理 {__version__}</title>", html)
        self.assertNotIn("__VERSION__", html, "占位符没被替换")
        self.assertIn(__version__, web_window.app_title())

    def test_a_broken_screen_answers_with_a_readable_page(self):
        """取页面时抛异常，也要回一句人话 —— 不能空着。

        客户看到的就是 ``ERR_EMPTY_RESPONSE``（"empty response"）：连接建立了、
        一个字节都没回，既不像出错也没告诉他该做什么。现在至少给出版本号和日志位置。
        """
        from unittest import mock as _mock

        from wechat_ai_assistant import web_server

        import urllib.error

        with _mock.patch.object(web_server, "page_html",
                                side_effect=RuntimeError("模板炸了")):
            try:
                code, body = self.client.get("/wizard")
            except urllib.error.HTTPError as exc:  # 500 会以 HTTPError 抛出
                code, body = exc.code, exc.read()
        self.assertEqual(code, 500)
        text = body.decode("utf-8")
        self.assertIn("界面这一屏没能加载出来", text)
        self.assertIn("发给客服", text)
        self.assertIn("RuntimeError", text, "要把真实原因写出来，客服才知道发什么")

    def test_the_state_carries_the_running_version(self):
        """界面要能显示"这装的是哪一版" —— 客服第一句就问它。

        以前只有更新器知道版本号，界面上一个字都没有；而发版说明里
        "标题栏版本号对得上才算数"那句是 Tk 时代的残留。
        """
        from wechat_ai_assistant.version import __version__

        _, body = self.client.get("/api/state")
        self.assertEqual(json.loads(body.decode("utf-8"))["version"], __version__)
        _, page = self.client.get("/wizard")
        self.assertIn("data.version", page.decode("utf-8"),
                      "页面要把版本号画出来，不然商户报不上来")

    def test_a_product_list_can_be_uploaded_as_xlsx(self):
        """商户给的是 Excel。xlsx 是二进制，必须能在服务端解析成表再导入。

        以前只能贴 CSV 文本，商户还得先另存一遍 —— 而 Excel 另存 CSV 经常把
        编码、前导零、别名里的逗号弄坏。
        """
        import base64
        import io as _io
        import zipfile as _zipfile

        from wechat_ai_assistant import xlsx_read, summary_task
        from wechat_ai_assistant.orders import OrderStore
        from wechat_ai_assistant.web_server import WebHandler

        def build(rows):
            shared: list[str] = []
            index: dict[str, int] = {}

            def sid(text):
                if text not in index:
                    index[text] = len(shared)
                    shared.append(text)
                return index[text]

            body = []
            for r, row in enumerate(rows, start=1):
                cells = []
                for c, value in enumerate(row):
                    col = chr(65 + c)
                    if isinstance(value, (int, float)):
                        cells.append(f'<c r="{col}{r}"><v>{value}</v></c>')
                    else:
                        cells.append(f'<c r="{col}{r}" t="s"><v>{sid(str(value))}</v></c>')
                body.append(f'<row r="{r}">' + "".join(cells) + "</row>")
            ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            sheet = (f'<worksheet xmlns="{ns}"><sheetData>' + "".join(body) + "</sheetData></worksheet>")
            sst = (f'<sst xmlns="{ns}" count="{len(shared)}" uniqueCount="{len(shared)}">'
                   + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
            workbook = (f'<workbook xmlns="{ns}" xmlns:r="{rel}">'
                        '<sheets><sheet name="商品" sheetId="1" r:id="rId1"/></sheets></workbook>')
            rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                    '</Relationships>')
            buffer = _io.BytesIO()
            with _zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("xl/workbook.xml", workbook)
                archive.writestr("xl/_rels/workbook.xml.rels", rels)
                archive.writestr("xl/sharedStrings.xml", sst)
                archive.writestr("xl/worksheets/sheet1.xml", sheet)
            return buffer.getvalue()

        data = build([["编码", "名称", "单位", "价格", "别名"],
                      ["APL", "苹果", "箱", 2600, "苹果"]])
        self.assertTrue(xlsx_read.is_xlsx(data))

        # 1) 服务端把这次上传的内容变成 CSV 文本（就是接口里那一步）
        text = WebHandler._reference_text({
            "filename": "商品.xlsx",
            "data": base64.b64encode(data).decode("ascii"),
        })
        self.assertIn("苹果", text)

        # 2) 这段 CSV 能真的进库
        with tempfile.TemporaryDirectory(prefix="pyw-xlsx-") as folder:
            store = OrderStore(str(pathlib.Path(folder) / "orders.db"))
            tenant = store.create_merchant("表格店")["merchant_id"]
            holder = types.SimpleNamespace(orders=store, tenant=tenant)
            outcome = summary_task.import_reference(holder, "products", text)
            store.close()
        self.assertFalse(outcome.get("error"), outcome)
        self.assertGreaterEqual(int(outcome.get("created") or 0), 1)

    def test_the_template_is_a_real_xlsx_with_the_right_headers(self):
        """商户不懂电脑：给他一个填好表头的文件，而不是让他拼列名。"""
        from wechat_ai_assistant import reference_template, xlsx_read

        for kind, headers in (("products", reference_template.PRODUCT_HEADERS),
                              ("customers", reference_template.CUSTOMER_HEADERS)):
            with self.subTest(kind=kind):
                data = reference_template.build_xlsx(reference_template.template_rows(kind))
                self.assertTrue(xlsx_read.is_xlsx(data), "下载下来的不是有效 xlsx")
                rows = xlsx_read.rows_from_bytes(data)
                self.assertEqual(rows[0], headers, "模板表头必须和导入器认的名字一致")
                self.assertTrue(reference_template.is_example_row(rows[1]),
                                "第二行是示例，导入时要能认出来")

    def test_the_template_carries_a_filling_guide_sheet(self):
        """填表时看的是表，不是软件窗口：说明要放在第二张表里。

        数据必须留在**第一张** —— 读取端只读第一张，说明跑前面就会被当成名单导进去。
        """
        import io as _io
        import zipfile as _zipfile

        from wechat_ai_assistant import reference_template as rt, xlsx_read

        data = rt.build_xlsx(rt.template_rows("products"), notes=rt.template_notes("products"))
        with _zipfile.ZipFile(_io.BytesIO(data)) as archive:
            sheets = [n for n in archive.namelist() if n.startswith("xl/worksheets/")]
        self.assertEqual(sheets, ["xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"])
        # 读取端拿到的仍然只有数据
        rows = xlsx_read.rows_from_bytes(data)
        self.assertEqual(rows[0], rt.PRODUCT_HEADERS)
        # 说明表里写清了价格单位
        guide = " ".join(" ".join(row) for row in rt.template_notes("products"))
        self.assertIn("单位是元", guide)

    def test_the_example_row_is_skipped_on_import(self):
        """商户忘了删示例行也能导 —— 不然他会把「示例：APL」当成真商品。"""
        from wechat_ai_assistant import reference_template as rt

        rows = rt.template_rows("products")
        cleaned = rt.clean_rows(rows)
        self.assertEqual(cleaned, [rt.PRODUCT_HEADERS])
        rows.append(["APL", "苹果", "箱", "2600", "苹果"])
        self.assertEqual(len(rt.clean_rows(rows)), 2, "真实数据要留下")

    def test_the_reported_row_number_matches_the_spreadsheet(self):
        """报错说的行号必须是**商户表格里的行号**。

        模板第 2 行是示例、第 3 行是他填错的那行 —— 如果转换时把示例行删掉，
        错行就会"变成"第 2 行，商户照着去 Excel 找会改错行。
        """
        import base64

        from wechat_ai_assistant import reference_template as rt, summary_task
        from wechat_ai_assistant.orders import OrderStore
        from wechat_ai_assistant.web_server import WebHandler

        rows = list(rt.template_rows("customers"))      # 第 1 行表头、第 2 行示例
        rows.append(["", "鲜果群", "某地址", "", ""])   # 第 3 行：缺名字
        rows.append(["李四", "鲜果群", "", "", ""])     # 第 4 行：正常
        text = WebHandler._reference_text({
            "data": base64.b64encode(rt.build_xlsx(rows)).decode("ascii")})

        with tempfile.TemporaryDirectory(prefix="pyw-row-") as folder:
            store = OrderStore(str(pathlib.Path(folder) / "orders.db"))
            tenant = store.create_merchant("行号店")["merchant_id"]
            outcome = summary_task.import_reference(
                types.SimpleNamespace(orders=store, tenant=tenant), "customers", text)
            store.close()

        self.assertEqual(int(outcome.get("created") or 0), 1, "正常那行要导进来")
        self.assertEqual(int(outcome.get("skipped") or 0), 1)
        problem = outcome["problems"][0]
        self.assertIn("第 3 行", problem, f"行号应是表格里的第 3 行，实际：{problem}")

    def test_a_filled_template_imports(self):
        from wechat_ai_assistant import reference_template as rt, summary_task, xlsx_read
        from wechat_ai_assistant.orders import OrderStore
        from wechat_ai_assistant.web_server import WebHandler

        rows = list(rt.template_rows("products")) + [["APL", "苹果", "箱", "2600", "苹果"]]
        import base64

        text = WebHandler._reference_text({
            "filename": "填好的.xlsx",
            "data": base64.b64encode(rt.build_xlsx(rows)).decode("ascii"),
        })
        with tempfile.TemporaryDirectory(prefix="pyw-tpl-") as folder:
            store = OrderStore(str(pathlib.Path(folder) / "orders.db"))
            tenant = store.create_merchant("模板店")["merchant_id"]
            outcome = summary_task.import_reference(
                types.SimpleNamespace(orders=store, tenant=tenant), "products", text)
            store.close()
        self.assertFalse(outcome.get("error"), outcome)
        self.assertEqual(int(outcome.get("created") or 0), 1, "要么多导、要么没导进来")


class LoginTests(unittest.TestCase):
    """登录是前置条件，不是第一步。

    没登录就去整理，商户会读完一堆消息才发现整理不了。
    """

    def setUp(self):
        self.app = _FakeApp()
        self.app.session = None
        self.logged_in = []
        self.app.login_requested = lambda: self.logged_in.append(1)
        self.detached = []
        self.app.detach = lambda: (self.detached.append(1),
                                   setattr(self.app, "session", None))
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_state_says_whether_we_are_logged_in(self):
        data = json.loads(self.client.get("/api/state")[1].decode("utf-8"))
        self.assertIn("account", data)
        self.assertFalse(data["account"]["logged_in"])

    def test_the_page_has_a_login_entry(self):
        html = self.client.get("/")[1].decode("utf-8")
        self.assertIn("登录产品账号", html)
        self.assertIn("/api/login", html)

    def test_login_endpoint_calls_the_runner(self):
        code, _ = self.client.post("/api/login")
        self.assertEqual(code, 200)
        self.assertEqual(len(self.logged_in), 1)

    def test_logout_does_not_delete_local_data(self):
        """退出只断云端能力，本地读到的一条不能消失。"""
        self.app.session = types.SimpleNamespace(merchant_name="张店", merchant_id="m-1")
        code, _ = self.client.post("/api/logout")
        self.assertEqual(code, 200)
        self.assertEqual(len(self.detached), 1)


class ConversationChoiceTests(unittest.TestCase):
    """\u6309\u540d\u5b57\u53d6\u5bf9\u8c61\u3002\u53d6\u4e0d\u5230\u5c31\u4e22\u5f03\uff0c**\u4e0d\u80fd\u731c**\u3002"""

    def setUp(self):
        self.app = _FakeApp([
            types.SimpleNamespace(name="\u9c9c\u679c\u7fa4", kind="group", member_count=35),
            types.SimpleNamespace(name="\u6c34\u679c\u7fa4", kind="group", member_count=12),
        ])
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_it_passes_conversation_objects_not_strings(self):
        code, data = self.client.post("/api/conversations/choose", {"names": ["\u9c9c\u679c\u7fa4"]})
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual(self.app.chosen[0].name, "\u9c9c\u679c\u7fa4")

    def test_an_unknown_name_is_dropped_not_guessed(self):
        """\u731c\u4e00\u4e2a\u5c31\u662f\u8bfb\u53d6**\u522b\u7684\u7fa4**\uff0c\u800c\u5546\u6237\u770b\u4e0d\u51fa\u5dee\u5f02\u3002"""
        self.client.post("/api/conversations/choose",
                         {"names": ["\u9c9c\u679c\u7fa4", "\u4e0d\u5b58\u5728\u7684\u7fa4"]})
        self.assertEqual([c.name for c in self.app.chosen], ["\u9c9c\u679c\u7fa4"])

    def test_clearing_the_selection_reads_everything(self):
        code, data = self.client.post("/api/conversations/choose", {"names": []})
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(self.app.chosen, [])


class GateSharesOneTruthTests(unittest.TestCase):
    """“能不能整理”只有一份判据，界面不许自己再判一遍。

    老路（「整理成一段话」）撤了之后，接口里不再有那个开关；但判据本身还在
    ``summary_task.has_messages_for``，而且它看的是**库里**的情况，不是本次
    读到几条 —— 更新时读到 0 条，不代表群里没有消息。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        from wechat_ai_assistant.orders import OrderStore

        self.store = OrderStore(str(pathlib.Path(self.dir.name) / "o.db"))
        self.tenant = self.store.create_merchant("测试商家")["merchant_id"]
        self.app = _FakeApp()
        self.app.orders = self.store
        self.app.tenant = self.tenant
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.dir.cleanup()

    def _state(self):
        return json.loads(self.client.get("/api/state")[1].decode("utf-8"))

    def test_the_state_no_longer_carries_the_old_switch(self):
        """老路的开关没了 —— 免得两处说法各留一半。"""
        self.assertNotIn("can_summarize", self._state())

    def test_the_shared_predicate_looks_at_the_store_not_at_this_read(self):
        """按**库里**的情况判断，不按本次读到几条。"""
        result = _result_with_zero_total()
        holder = types.SimpleNamespace(orders=self.store, tenant=self.tenant)
        self.server.web_state.set(history=result)
        self.assertFalse(summary_task.has_messages_for(holder, result))
        self.store.add_import(self.tenant, "鲜果群", [
            {"sender": "张嫂", "content": "要两箱苹果",
             "sent_at": "2026-10-14T09:00:00-04:00", "source_id": "s-0"}], "probe", "demo")
        self.assertTrue(summary_task.has_messages_for(holder, result),
                        "库里明明有消息，却说不能整理")


def _result_with_zero_total():
    """\u672c\u6b21\u8bfb\u5230 0 \u6761\uff08\u66f4\u65b0\u65f6\u5e38\u89c1\uff09\uff0c\u4f46 coverage \u6307\u5411\u7684\u4f1a\u8bdd\u91cc\u6709\u5386\u53f2\u3002"""
    return types.SimpleNamespace(
        total_messages=0,
        import_ids=[],
        coverage=[types.SimpleNamespace(
            conversation=types.SimpleNamespace(name="\u9c9c\u679c\u7fa4"),
            message_count=0, skipped=False, error="", truncated=False)],
    )


class BlockingWorkTests(unittest.TestCase):
    """\u963b\u585e\u52a8\u4f5c\u5fc5\u987b\u79bb\u5f00\u5904\u7406\u7ebf\u7a0b\u3002"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = _FakeApp()
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def test_state_stays_reachable_while_something_blocks(self):
        """\u5361\u4f4f\u7684\u662f\u6574\u4e2a\u670d\u52a1\uff0c\u4e0d\u53ea\u662f\u90a3\u4e00\u4e2a\u63a5\u53e3\u3002"""
        import time

        gate = threading.Event()
        state = self.server.web_state

        def slow() -> None:
            gate.wait(3.0)
            state.finish("\u5b8c\u6210")

        state.start("\u6d4b\u8bd5\u963b\u585e")
        threading.Thread(target=slow, daemon=True).start()
        time.sleep(0.2)
        started = time.time()
        data = json.loads(self.client.get("/api/state")[1].decode("utf-8"))
        self.assertLess(time.time() - started, 2.0, "/api/state \u88ab\u963b\u585e\u7684\u52a8\u4f5c\u5361\u4f4f\u4e86")
        self.assertEqual(data["busy"], "\u6d4b\u8bd5\u963b\u585e")
        gate.set()

    def test_a_second_job_is_refused_while_one_runs(self):
        state = self.server.web_state
        state.start("正在做其他事")
        code, data = self.client.post_expect_error("/api/analyze")
        self.assertEqual(code, 409)
        self.assertIn("正在做其他事", data["error"])

    def test_the_old_summary_path_is_gone(self):
        """老路（「整理成一段话」）的接口没了 —— 商户只有「选角色 → 开始分析」一条路。

        留着接口就等于留着第二条说法：页面上哪天手一抖又摆回这个按钮，
        商户点到的就是另一套行为。
        """
        for path in ("/api/summary", "/api/summary/cancel"):
            code, _ = self.client.post_expect_error(path)
            self.assertEqual(code, 404, path + " 还在，老路没收干净")


class RootPathTests(unittest.TestCase):
    """产品里敲根路径，拿到的是商户版向导页。

    调试页只在开发外壳（``web_dev``）下存在。产品端口上摆着调试页，商户在
    浏览器里敲一下地址就会看到开发按钮 —— 那是给开发看的，不是给商户看的。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = _FakeApp()
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def test_the_root_path_is_the_wizard_page_in_the_product(self):
        from wechat_ai_assistant import web_server

        original = web_server.DEV_RELOAD
        try:
            web_server.DEV_RELOAD = False
            status, body = self.client.get("/")
        finally:
            web_server.DEV_RELOAD = original
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("办理流程", html, "根路径给的是向导页（步骤条在）")
        self.assertNotIn("整理成一段话", html, "老路不在产品端口上")


class MerchantWizardPageTests(unittest.TestCase):
    """商户版向导页（``/wizard``）。

    它是渲染器，不是流程的第二份定义：步骤编号、标题、门禁、微信文案全部来自
    ``/api/state``。所以这里盯的就是"页面里不许有流程事实"。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.app = _FakeApp([types.SimpleNamespace(name="鲜果批发群", kind="group",
                                                   member_count=35)])
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def test_the_page_is_served(self):
        status, body = self.client.get("/wizard")
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("微信AI智能助理", html)
        self.assertIn("/api/state", html, "页面必须靠状态接口拿数据")

    def test_the_page_does_not_carry_step_facts(self):
        """页面里不许出现步骤名/编号 —— 那些由 ``state.steps`` 给。

        写进页面的话，改流程就要同时改 Python 和两套页面（Tk、Web），
        而它们一定会漂。
        """
        from wechat_ai_assistant.wizard import WizardStep

        _, body = self.client.get("/wizard")
        html = body.decode("utf-8")
        self.assertNotIn("第 0 步", html)
        self.assertNotIn("第 1 步", html)
        for step in WizardStep.ORDER:
            with self.subTest(step=step):
                self.assertNotIn(f'"{WizardStep.SHORT[step]}"', html)

    def test_the_page_does_not_re_judge_the_gate(self):
        """门禁只在服务端判（``state.product_gate_open``）。

        页面自己按 ``logged_in`` 再判一次就是两套判据 —— 一处放行、一处拦下，
        商户只能干瞪眼。顶栏拿 ``logged_in`` 画"已登录/未登录"是显示，不算判据。
        """
        _, body = self.client.get("/wizard")
        html = body.decode("utf-8")
        self.assertIn("disabled: !data.product_gate_open", html,
                      "登录页的「下一步」必须按服务端给的门禁灰掉")
        # 顶栏用 logged_in 画"已登录/未登录"是显示；判据不许用它。\n        self.assertNotIn("disabled: !acct.logged_in", html)\n        self.assertNotIn("disabled:!acct.logged_in", html)

    def test_state_carries_the_wechat_status_for_step_one(self):
        class _WithMonitor(_FakeApp):
            def __init__(self):
                super().__init__()
                self.monitor = types.SimpleNamespace(latest=types.SimpleNamespace(
                    state="not_started", detail="没开",
                    ready=False, checked_at=1.0,
                    copy=lambda: ("微信还没有打开", "点按钮打开微信。", "打开微信")))

        server = _serve(_WithMonitor())
        try:
            client = _Client(server)
            status, body = client.get("/api/state")
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(status, 200)
            self.assertEqual(data["wechat"]["title"], "微信还没有打开",
                             "微信文案要来自 WeChatStatus.copy()，页面不自己编")
            self.assertEqual(data["wechat"]["button"], "打开微信")
            self.assertTrue(data["wechat"]["checked"])
        finally:
            server.shutdown()
            server.server_close()


class WizardAdvanceEndpointTests(unittest.TestCase):
    """「下一步」的判据在向导里：接口只把它转出去。"""

    class _Wizard:
        def __init__(self):
            self.calls = []

        def advance(self):
            self.calls.append("advance")
            return {"ok": False, "step": "login", "reason": "还没登录产品账号，先登录才能继续。"}

        def back(self):
            self.calls.append("back")
            return {"ok": True, "step": "login", "reason": ""}

    def setUp(self):
        self.app = _FakeApp()
        self.app.wizard = self._Wizard()
        self.server = _serve(self.app)
        self.client = _Client(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_advance_returns_the_wizards_answer(self):
        status, data = self.client.post("/api/wizard/advance")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("登录", data["reason"], "不放行要把原因给页面显示")
        self.assertEqual(self.app.wizard.calls, ["advance"])

    def test_back_is_wired_too(self):
        status, data = self.client.post("/api/wizard/back")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(self.app.wizard.calls, ["back"])


class DevAndProductSplitTests(unittest.TestCase):
    """开发模块和产品模块的边界。

    ``web_server`` 是产品（接口、状态、页面渲染），``web_dev`` 只是开发时的
    外壳（CLI + 保存即生效）。混在一起的代价很实在：产品包里会带上
    "每次轮询都去读文件时间戳"和一段自动刷新脚本，而它们只在开发时有用。
    """

    def test_the_dev_layer_turns_on_reload_and_the_product_does_not(self):
        from wechat_ai_assistant import web_dev, web_server

        self.assertTrue(web_server.DEV_RELOAD, "web_dev 导入后应该打开开发期行为")
        self.assertTrue(web_server.LIVE_RELOAD, "自动刷新脚本要注进去")

    def test_the_product_page_has_no_live_reload_hook(self):
        from importlib import reload

        from wechat_ai_assistant import web_server

        original = (web_server.DEV_RELOAD, web_server.LIVE_RELOAD)
        try:
            web_server.DEV_RELOAD = False
            web_server.LIVE_RELOAD = ""
            reload(web_server)
            html = web_server.page_html("D:/tmp", "wizard")
        finally:
            web_server.DEV_RELOAD, web_server.LIVE_RELOAD = original
            reload(web_server)
        self.assertNotIn("/api/page-version", html, "产品页面不该带自动刷新")
        self.assertNotIn("__DATADIR__", html, "占位符还是要换成数据目录")

    def test_the_server_is_the_one_that_serves(self):
        """页面渲染和状态都在 web_server：拆出去之后不能还剩两处实现。"""
        source = (ROOT / "src/wechat_ai_assistant/web_server.py").read_text(encoding="utf-8")
        dev = (ROOT / "src/wechat_ai_assistant/web_dev.py").read_text(encoding="utf-8")
        self.assertIn("def create_server", source)
        self.assertNotIn("def create_server", dev, "开发模块只转发，不再实现一遍")
        self.assertIn("class WebState", source)
        self.assertNotIn("class WebState", dev)


class ProductEntryTests(unittest.TestCase):
    """``web_app``：产品入口 —— 起服务 + 装窗口，业务逻辑一份都不重写。"""

    def setUp(self):
        # 诊断日志会把 merchant.log 握着不放（单例 handler），Windows 上删目录
        # 会报"文件被占用" —— 那是运行时的正常行为，不是测试要管的事。
        self.dir = tempfile.TemporaryDirectory(prefix="pyw-webapp-",
                                               ignore_cleanup_errors=True)

    def tearDown(self):
        self.dir.cleanup()

    def test_check_mode_reports_without_opening_a_window(self):
        import contextlib
        import io as _io

        from wechat_ai_assistant import web_app

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = web_app.main(["--check", "--data-dir", self.dir.name])
        out = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("本机界面：http://127.0.0.1:", out, "要说清服务地址")
        self.assertIn(self.dir.name, out, "要说清读的是哪个数据目录")

    def test_it_uses_the_same_assembly_as_the_tk_entry(self):
        """两个入口共用 ``build_runtime`` —— 各写一遍就会一边装了日志一边没装。"""
        source = (ROOT / "src/wechat_ai_assistant/web_app.py").read_text(encoding="utf-8")
        self.assertIn("build_runtime", source)
        app_source = (ROOT / "src/wechat_ai_assistant/merchant_app.py").read_text(encoding="utf-8")
        self.assertIn("def build_runtime", app_source)
        self.assertIn("build_runtime(", app_source.split("def main(")[1],
                      "Tk 入口也要走它，否则两套装配迟早不一样")

    def test_the_window_goes_through_the_host_layer(self):
        source = (ROOT / "src/wechat_ai_assistant/web_app.py").read_text(encoding="utf-8")
        self.assertIn("from .web_window import open_window", source)


if __name__ == "__main__":
    unittest.main()
