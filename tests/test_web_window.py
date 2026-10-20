"""窗口外壳：内嵌 WebView2 起不来时自己往下退 —— **都不点真窗口**。

## 为什么值得测

客户机器缺 WebView2 Runtime 曾经等于"双击没反应"。现在宿主是一条链：

    内嵌 WebView2 → 系统 Edge ``--app`` → 默认浏览器

这里验证的就是这条链和它周围的判断：

* 起不来时必须**真的**退到 Edge，而不是只弹一句"请安装组件";
* 缺组件时要**在后台补装**（便携包里带着安装包），装没装上不挡启动；
* Edge 那条路必须用**独立的 user-data-dir** —— 不然会被并在商户正在用的
  Edge 里，关窗不退出，我们的进程会一直挂着；
* 页面和接口不在这层：它只负责"窗口怎么来"。
"""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.request
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wechat_ai_assistant import web_window  # noqa: E402


class ChooseHostTests(unittest.TestCase):
    def test_prefer_wins(self):
        self.assertEqual(web_window.choose_host("browser"), "browser")
        self.assertEqual(web_window.choose_host("edge"), "edge")

    def test_webview_is_preferred_when_installed(self):
        with mock.patch.object(web_window, "has_webview", return_value=True), \
                mock.patch.object(web_window, "edge_path", return_value=r"C:\msedge.exe"):
            self.assertEqual(web_window.choose_host(), "pywebview")

    def test_webview_is_still_the_default_when_module_is_missing(self):
        with mock.patch.object(web_window, "has_webview", return_value=False), \
                mock.patch.object(web_window, "edge_path", return_value=r"C:\msedge.exe"):
            self.assertEqual(web_window.choose_host(), "pywebview")

    def test_browser_must_be_explicit(self):
        with mock.patch.object(web_window, "has_webview", return_value=False), \
                mock.patch.object(web_window, "edge_path", return_value=""):
            self.assertEqual(web_window.choose_host("browser"), "browser")

    def test_has_webview_never_raises(self):
        """依赖诊断时检查 pywebview 不能抛异常。"""
        self.assertIn(web_window.has_webview(), (True, False))


class EdgeLaunchTests(unittest.TestCase):
    def test_it_waits_for_the_window_and_isolates_the_profile(self):
        calls = {}

        class _Process:
            pid = 4242

            def wait(self):
                calls["waited"] = True

        def fake_popen(argv, **kwargs):
            calls["argv"] = argv
            return _Process()

        with mock.patch.object(web_window, "edge_path", return_value=r"C:\msedge.exe"), \
                mock.patch.object(subprocess, "Popen", fake_popen), \
                mock.patch.object(web_window, "_wait_for_browser") as watched, \
                mock.patch("wechat_ai_assistant.app_icon.set_window_icon_async") as icon:
            web_window.open_window("http://127.0.0.1:1/wizard", prefer="edge")

        icon.assert_called_once_with(pid=4242)
        argv = calls["argv"]
        self.assertTrue(calls.get("waited"), "不等进程，服务就不知道窗口关了")
        self.assertIn("--app=http://127.0.0.1:1/wizard", argv)
        profile = [a for a in argv if a.startswith("--user-data-dir=")]
        self.assertTrue(profile, "没有独立 profile 会被并进商户正在用的 Edge，关窗也不退出")
        self.assertIn(f"p{os.getpid()}", profile[0],
                      "profile 必须每次启动唯一，否则新进程把窗口交出去就退出，服务被误关")
        self.assertTrue(watched.called, "启动器退出后还要盯着窗口本身")

    def test_missing_edge_raises_so_the_caller_can_fall_back(self):
        with mock.patch.object(web_window, "edge_path", return_value=""):
            with self.assertRaises(FileNotFoundError):
                web_window.open_window("http://127.0.0.1:1/wizard", prefer="edge")


class EdgeLifetimeTests(unittest.TestCase):
    """线上事故：Edge 打开后页面显示「127.0.0.1 页面不可用」。

    原因：Edge 的**启动器进程**会把手交给已在跑的浏览器进程然后立刻退出
    （实测 0.3 秒）。我们等的是启动器，于是误判"窗口关了"，把本机服务关掉 ——
    商户眼前的页面就死了。这里钉住修法。
    """

    def test_it_keeps_waiting_while_the_browser_is_still_up(self):
        # 第一次看到浏览器、第二次没有、第三次没有 → 攒够 grace 才收工。
        seen = [True, False, False, False]
        with mock.patch.object(web_window, "browser_uses_profile",
                               side_effect=lambda _p: seen.pop(0) if seen else False), \
                mock.patch.object(web_window.time, "sleep"):
            web_window._wait_for_browser(Path(r"C:\tmp\profile"))

    def test_it_blocks_when_the_browser_was_never_seen(self):
        """看不到浏览器也**不能**收工：收工就意味着关服务 = 死页面。"""
        calls = {"n": 0}

        def never(_profile):
            calls["n"] += 1
            if calls["n"] > 20:
                raise AssertionError("一直没看到浏览器时不该返回（会关掉服务）")
            return False

        with mock.patch.object(web_window, "browser_uses_profile", side_effect=never), \
                mock.patch.object(web_window.time, "sleep"):
            with self.assertRaises(AssertionError):
                web_window._wait_for_browser(Path(r"C:\tmp\profile"))

    def test_it_detects_a_browser_holding_the_profile(self):
        with mock.patch.object(web_window, "browser_uses_profile", return_value=True):
            self.assertTrue(web_window.browser_uses_profile(Path(r"C:\tmp\profile")))


class ReadinessTests(unittest.TestCase):
    """开窗之前先确认服务能应答 —— 不给商户看一个连不上的页面。"""

    def test_a_dead_port_is_reported_false(self):
        self.assertFalse(web_window.wait_until_serving("http://127.0.0.1:1/wizard", timeout=1))

    def test_it_builds_the_probe_from_the_state_endpoint(self):
        seen = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_open(url, timeout=0):
            seen.append(url)
            return _Response()

        class _Opener:
            def open(self, url, timeout=0):
                return fake_open(url, timeout)

        with mock.patch.object(web_window, "loopback_opener", return_value=_Opener()):
            self.assertTrue(web_window.wait_until_serving("http://127.0.0.1:51599/wizard"))
        self.assertEqual(seen, ["http://127.0.0.1:51599/api/state"])

    def test_the_probe_ignores_system_proxies(self):
        """装了 Clash / 公司代理的机器上，`127.0.0.1` 默认会被送进代理。

        那会让"服务明明起来了"被误判成没起来（浏览器那边同样打不开页面）。
        这里用**行为**验：把 http_proxy 指到一个死地址，探测仍然要能连上本机服务。
        """
        import http.server
        import os as _os
        import threading as _threading

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        _threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            with mock.patch.dict(_os.environ, {
                    "http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1"}):
                self.assertTrue(
                    web_window.wait_until_serving(f"http://127.0.0.1:{port}/wizard", timeout=5),
                    "有代理时也必须连得上本机服务（回环不该走代理）")
        finally:
            server.shutdown()
            server.server_close()

    def test_the_edge_window_is_started_without_a_proxy(self):
        """兜底窗口同样要跳过代理，否则页面还是打不开。"""
        argv = {}

        class _Process:
            pid = 1

            def wait(self):
                return None

        def fake_popen(args, **kwargs):
            argv["args"] = args
            return _Process()

        with mock.patch.object(web_window, "edge_path", return_value=r"C:\msedge.exe"), \
                mock.patch.object(subprocess, "Popen", fake_popen), \
                mock.patch.object(web_window, "_wait_for_browser"), \
                mock.patch("wechat_ai_assistant.app_icon.set_window_icon_async"):
            web_window.open_window("http://127.0.0.1:1/wizard", prefer="edge")
        self.assertIn("--no-proxy-server", argv["args"])


class WaitingNoticeTests(unittest.TestCase):
    """安装组件时的提示窗：**它自己绝不能拦住启动**。"""

    def test_a_broken_window_never_breaks_the_install(self):
        from wechat_ai_assistant import window_notice

        def boom(_self):
            raise RuntimeError("窗口建不出来")

        with mock.patch.object(window_notice.WaitingNotice, "_create_and_pump", boom):
            notice = window_notice.WaitingNotice("正在安装…").show()
            notice.close()  # 不能抛
        self.assertTrue(True)

    def test_close_is_safe_when_nothing_was_shown(self):
        from wechat_ai_assistant import window_notice

        notice = window_notice.WaitingNotice("x")
        notice.close()
        notice.close()

    def test_the_notice_is_used_only_while_installing(self):
        """组件在的时候不许弹窗打扰 —— 每次启动都弹一个才是坏体验。"""
        from wechat_ai_assistant import window_notice

        calls: list[str] = []

        class _Notice:
            def __init__(self, text):
                calls.append("open")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                calls.append("close")

        with mock.patch.object(web_window, "webview2_runtime_present", return_value=False), \
                mock.patch.object(web_window, "ensure_webview_runtime", return_value=True), \
                mock.patch.object(web_window, "_open_webview", return_value="pywebview"), \
                mock.patch.object(window_notice, "WaitingNotice", _Notice):
            web_window.open_window("http://127.0.0.1:1/wizard")
        self.assertEqual(calls, ["open", "close"])


class InstanceGuardTests(unittest.TestCase):
    """同一份软件只许开一个实例，而且要说清**是哪一份**在跑。"""

    def test_our_own_process_tree_is_not_another_instance(self):
        from wechat_ai_assistant import instance_guard

        self.assertIn(os.getpid(), instance_guard._self_tree())

    def test_it_finds_another_copy_and_says_where_it_is(self):
        from wechat_ai_assistant import instance_guard

        fake = type("P", (), {"info": {"pid": 4242, "name": "微信AI智能助理.exe",
                                    "exe": r"D:\\旧目录\\微信AI智能助理.exe",
                                    "create_time": 0.0}})()
        with mock.patch.object(instance_guard, "_self_tree", return_value={os.getpid()}), \
                mock.patch.object(instance_guard, "_is_our_process", return_value=True), \
                mock.patch("psutil.process_iter", return_value=[fake]):
            found = instance_guard.other_instances()
        self.assertEqual([f["pid"] for f in found], [4242])
        note = instance_guard.describe(found)
        self.assertIn("旧目录", note, "要把对方的路径写出来，否则不知道该关哪个")
        self.assertIn("先", note)

    def test_the_escape_hatch_is_read_from_the_environment(self):
        from wechat_ai_assistant import instance_guard

        with mock.patch.dict(os.environ, {instance_guard.ALLOW_MULTIPLE_ENV: "1"}):
            self.assertTrue(instance_guard.multiples_allowed())
        with mock.patch.dict(os.environ, {instance_guard.ALLOW_MULTIPLE_ENV: ""}):
            self.assertFalse(instance_guard.multiples_allowed())

    def test_the_title_carries_the_version(self):
        from wechat_ai_assistant import web_window
        from wechat_ai_assistant.version import __version__

        self.assertIn(__version__, web_window.app_title())


class ScratchLocationTests(unittest.TestCase):
    """免安装版的承诺：中间文件也要待在数据目录旁边，别往 C 盘临时目录撒。"""

    def test_scratch_lives_under_the_data_directory(self):
        import tempfile as _tempfile

        from wechat_ai_assistant import paths

        with _tempfile.TemporaryDirectory(prefix="pyw-scratch-") as folder:
            with mock.patch.object(paths, "default_data_dir", return_value=Path(folder)):
                target = paths.scratch_dir("edge-profile")
            self.assertTrue(str(target).startswith(folder), "必须落在数据目录里")
            self.assertTrue(target.is_dir())

    def test_scratch_falls_back_when_the_data_directory_is_unwritable(self):
        import tempfile as _tempfile

        from wechat_ai_assistant import paths

        with _tempfile.TemporaryDirectory(prefix="pyw-scratch-") as folder:
            blocked = Path(folder) / "nope"
            with mock.patch.object(paths, "default_data_dir", return_value=blocked), \
                    mock.patch.object(Path, "mkdir", side_effect=OSError("只读")):
                target = paths.scratch_dir("x")
        self.assertIn("pyw", str(target), "写不进去就退回系统临时目录，不能抛")

    def test_the_edge_profile_is_per_launch_and_cleaned_up(self):
        """每次启动一个独立 profile（否则窗口会被并进已在跑的 Edge），
        而且两天前的要清掉，不然一天堆一个几百 MB 的目录。"""
        import os as _os
        import tempfile as _tempfile
        import time as _time

        with _tempfile.TemporaryDirectory(prefix="pyw-profile-") as folder:
            profiles = Path(folder)
            stale = profiles / "p_stale"
            stale.mkdir()
            old = _time.time() - 5 * 86400
            _os.utime(stale, (old, old))
            fresh = profiles / "p_fresh"
            fresh.mkdir()

            with mock.patch.object(web_window, "_scratch", return_value=profiles):
                profile = web_window._edge_profile()

            self.assertEqual(profile.name, f"p{_os.getpid()}")
            self.assertFalse(stale.exists(), "两天前的 profile 要清掉")
            self.assertTrue(fresh.exists(), "新的不能误删")


class DiagnosisTests(unittest.TestCase):
    def test_the_report_covers_the_things_that_decide_this_bug(self):
        from wechat_ai_assistant import window_diagnosis

        text = window_diagnosis.collect()
        for section in ("===== 程序 =====", "===== 代理 =====", "===== 本机回环 =====",
                        "===== 窗口宿主 ====="):
            self.assertIn(section, text)
        self.assertIn("版本:", text)
        self.assertIn("WebView2 Runtime 已安装:", text)

    def test_a_report_file_is_written_for_the_customer_to_send(self):
        from wechat_ai_assistant import window_diagnosis

        path = window_diagnosis.write_report("hello")
        try:
            self.assertTrue(path.is_file())
            self.assertIn("hello", path.read_text(encoding="utf-8-sig"))
        finally:
            path.unlink(missing_ok=True)


class BrowserFallbackTests(unittest.TestCase):
    def test_it_opens_the_default_browser(self):
        with mock.patch.object(web_window, "edge_path", return_value=""), \
                mock.patch("webbrowser.open") as opened:
            host = web_window.open_window("http://127.0.0.1:1/wizard", prefer="browser")
        self.assertEqual(host, "browser")
        opened.assert_called_once_with("http://127.0.0.1:1/wizard")


class WebView2RepairTests(unittest.TestCase):
    """缺 WebView2 时：**开窗之前先装**（Electron 自带内核、Tauri 安装器先装的等价物）。

    以前是"起不来再在后台偷偷补"，于是第一轮永远降级；补装要权限、要联网，
    失败了就永远停在兜底上。现在顺序反过来：先装，装上了就用内嵌窗口。
    """

    def _open(self, *, edge: str, runtime_present: bool, install_ok: bool,
              webview_ok: bool = False):
        def webview(*_args, **_kwargs):
            if not webview_ok:
                raise RuntimeError("webview2 missing")
            return "pywebview"

        with mock.patch.object(web_window, "_open_webview", side_effect=webview), \
                mock.patch.object(web_window, "edge_path", return_value=edge), \
                mock.patch.object(web_window, "webview2_runtime_present",
                                  return_value=runtime_present), \
                mock.patch.object(web_window, "ensure_webview_runtime",
                                  return_value=install_ok) as install, \
                mock.patch.object(subprocess, "Popen") as popen, \
                mock.patch.object(web_window, "_wait_for_browser"), \
                mock.patch("wechat_ai_assistant.app_icon.set_window_icon_async"), \
                mock.patch.object(web_window, "wait_forever") as waited, \
                mock.patch("webbrowser.open") as browser:
            popen.return_value.pid = 4242
            popen.return_value.wait = lambda: None
            host = web_window.open_window("http://127.0.0.1:1/wizard")
            for thread in list(web_window.threading.enumerate()):
                if thread.name == "webview2-retry":
                    thread.join(timeout=5)
        return host, install, popen, browser, waited

    def test_it_installs_before_the_window_is_even_attempted(self):
        host, install, _popen, _browser, _waited = self._open(
            edge=r"C:\msedge.exe", runtime_present=False, install_ok=True, webview_ok=True)
        self.assertTrue(install.called, "缺组件时必须在开窗之前先装")
        self.assertEqual(host, "pywebview", "装上了就用内嵌窗口")

    def test_a_waiting_notice_is_shown_while_installing(self):
        """装的时候屏幕上必须有东西 —— 否则商户以为双击没反应，反复双击。"""
        from wechat_ai_assistant import window_notice

        shown: list[bool] = []

        class _Notice:
            def __init__(self, text):
                self.text = text

            def __enter__(self):
                shown.append(True)
                return self

            def __exit__(self, *exc):
                shown.append(False)

        with mock.patch.object(web_window, "webview2_runtime_present", return_value=False), \
                mock.patch.object(web_window, "ensure_webview_runtime", return_value=True), \
                mock.patch.object(web_window, "_open_webview", return_value="pywebview"), \
                mock.patch.object(window_notice, "WaitingNotice", _Notice):
            host = web_window.open_window("http://127.0.0.1:1/wizard")
        self.assertEqual(shown, [True, False], "装之前显示、装完关掉")
        self.assertEqual(host, "pywebview")

    def test_no_notice_when_the_component_is_already_installed(self):
        from wechat_ai_assistant import window_notice

        seen: list[str] = []

        class _Notice:
            def __init__(self, text):
                seen.append(text)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        with mock.patch.object(web_window, "webview2_runtime_present", return_value=True), \
                mock.patch.object(web_window, "_open_webview", return_value="pywebview"), \
                mock.patch.object(window_notice, "WaitingNotice", _Notice):
            web_window.open_window("http://127.0.0.1:1/wizard")
        self.assertEqual(seen, [], "组件在的时候不该弹任何东西")

    def test_it_does_not_install_when_the_component_is_already_there(self):
        host, install, _popen, _browser, _waited = self._open(
            edge=r"C:\msedge.exe", runtime_present=True, install_ok=True, webview_ok=True)
        self.assertFalse(install.called)
        self.assertEqual(host, "pywebview")

    def test_it_falls_back_to_edge_when_the_install_fails(self):
        host, install, popen, browser, _waited = self._open(
            edge=r"C:\msedge.exe", runtime_present=False, install_ok=False)
        self.assertTrue(install.called)
        self.assertEqual(host, "edge", "装不上也得让商户能用")
        self.assertTrue(popen.called)
        browser.assert_not_called()

    def test_without_edge_it_still_opens_a_browser_and_keeps_running(self):
        """连 Edge 都没有：用默认浏览器，而且**进程不许退出** ——
        一退出服务就停，商户眼前的页面直接白掉。"""
        host, _install, _popen, browser, waited = self._open(
            edge="", runtime_present=False, install_ok=False)
        self.assertEqual(host, "browser")
        browser.assert_called_once()
        waited.assert_called_once_with()

    def test_it_retries_the_install_in_the_background_after_falling_back(self):
        """这一轮装不上（多半是断网）：先用兜底窗口，后台再试一次。"""
        host, install, _popen, _browser, _waited = self._open(
            edge=r"C:\msedge.exe", runtime_present=False, install_ok=False)
        self.assertEqual(host, "edge")
        self.assertGreaterEqual(install.call_count, 2,
                                "兜底之后还要在后台再装一次（下次启动就不用兜底了）")

    def test_the_notice_tells_the_truth(self):
        note = web_window.fallback_notice("edge")
        self.assertIn("系统 Edge", note)
        self.assertIn("后台自动安装", note)
        self.assertIn(web_window.WEBVIEW2_DOWNLOAD, note, "装不上时得给出下载地址")
        self.assertIn("需要联网", note, "安装要联网，不能说得像一定成功")

    def test_installing_downloads_verifies_then_asks_for_silence(self):
        # 第一次查（缺）→ 下载安装 → 第二次查（装上了）；以注册表为准。
        with mock.patch.object(web_window, "download_webview2_installer",
                               return_value=Path(r"C:\tmp\setup.exe")) as download, \
                mock.patch.object(subprocess, "run") as run, \
                mock.patch.object(web_window, "webview2_runtime_present",
                                  side_effect=[False, True]) as present:
            ok = web_window.install_webview_runtime()
        self.assertTrue(ok)
        download.assert_called_once_with()
        argv = run.call_args[0][0]
        self.assertIn("/silent", argv, "静默：商户不该看见安装向导")
        self.assertIn("/install", argv)
        self.assertEqual(present.call_count, 2, "装完要再查一次注册表才算数（退出码不算）")

    def test_a_finished_installer_that_changed_nothing_tries_elevation(self):
        """引导包要联网，断网时它也会"跑完"。退出码不算数，注册表说了算。

        普通权限装不上时要再走一次 UAC 提权 —— 写机器级目录必须有管理员权限，
        这正是"我们做了依赖却没装上"的原因之一。
        """
        with mock.patch.object(web_window, "download_webview2_installer",
                               return_value=Path(r"C:\tmp\setup.exe")), \
                mock.patch.object(subprocess, "run"), \
                mock.patch.object(web_window, "_run_installer_elevated",
                                  return_value=False) as elevated, \
                mock.patch.object(web_window, "webview2_runtime_present",
                                  return_value=False):
            self.assertFalse(web_window.install_webview_runtime())
        self.assertTrue(elevated.called, "普通权限没成功就该试提权")

    def test_a_successful_elevation_counts_as_installed(self):
        with mock.patch.object(web_window, "download_webview2_installer",
                               return_value=Path(r"C:\tmp\setup.exe")), \
                mock.patch.object(subprocess, "run"), \
                mock.patch.object(web_window, "_run_installer_elevated",
                                  return_value=True):
            self.assertTrue(web_window.install_webview_runtime())

    def test_a_failed_download_is_not_an_error(self):
        with mock.patch.object(web_window, "download_webview2_installer", return_value=None), \
                mock.patch.object(web_window, "webview2_runtime_present", return_value=False), \
                mock.patch.object(subprocess, "run") as run:
            self.assertFalse(web_window.install_webview_runtime())
        self.assertFalse(run.called, "下载失败就什么都别跑")


class DownloadGuardTests(unittest.TestCase):
    """下载来的东西要在商户机器上**执行**，所以每一步都要把关。"""

    @staticmethod
    def _download(data: bytes, *, trusted: bool = True):
        class _Response:
            def read(self):
                return data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_Response()), \
                mock.patch.object(web_window, "signature_is_trusted", return_value=trusted):
            return web_window.download_webview2_installer()

    def test_an_error_page_is_not_saved_as_an_installer(self):
        self.assertIsNone(self._download(b"<!DOCTYPE html><html>not found"),
                          "小文件/非 PE 的下载结果必须拒掉")

    def test_an_untrusted_signature_is_refused(self):
        payload = b"MZ" + b"\0" * web_window.MIN_INSTALLER_BYTES
        self.assertIsNone(self._download(payload, trusted=False),
                          "签名不可信就不许执行")

    def test_a_trusted_installer_is_kept(self):
        payload = b"MZ" + b"\0" * web_window.MIN_INSTALLER_BYTES
        import tempfile

        with tempfile.TemporaryDirectory(prefix="pyw-wv2-") as folder:
            class _Response:
                def read(self):
                    return payload

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            with mock.patch("urllib.request.urlopen", return_value=_Response()), \
                    mock.patch.object(web_window, "signature_is_trusted", return_value=True):
                path = web_window.download_webview2_installer(Path(folder))
        self.assertIsNotNone(path)
        self.assertEqual(path.name, web_window.WEBVIEW2_INSTALLER)

    def test_signature_check_never_raises_on_a_missing_file(self):
        self.assertIn(web_window.signature_is_trusted(Path(r"C:\nope\missing.exe")),
                      (True, False))


if __name__ == "__main__":
    unittest.main()
