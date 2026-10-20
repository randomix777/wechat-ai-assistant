"""UIA 存活门纯单测：假 Desktop，不碰真微信不联网。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wechat_ai_assistant import wechat_state  # noqa: E402
from wechat_ai_assistant.wechat_state import (  # noqa: E402
    default_sender_source,
    reset_uia_cache,  # noqa: F401 - 探针缓存语义不变，留着给后人用
    uia_automation_available,
)


class _FakeElement:
    def __init__(self, kids=()):
        self._kids = list(kids)
        self.element_info = object()

    def children(self):
        return list(self._kids)


class _FakeWindow(_FakeElement):
    def __init__(self, title, kids=()):
        super().__init__(kids)
        self._title = title

    def window_text(self):
        return self._title


class _FakeDesktop:
    def __init__(self, windows):
        self._windows = windows

    def windows(self):
        return list(self._windows)


def _bushy_window(title="Weixin", leaves=15):
    return _FakeWindow(title, [_FakeElement() for _ in range(leaves)])


class UiaProbeTests(unittest.TestCase):
    def setUp(self):
        reset_uia_cache()
        import pywinauto

        self._real_desktop = pywinauto.Desktop
        self.addCleanup(setattr, pywinauto, "Desktop", self._real_desktop)

    def _patch_desktop(self, windows):
        import pywinauto

        pywinauto.Desktop = lambda backend="uia": _FakeDesktop(windows)

    def test_bushy_tree_is_alive(self):
        self._patch_desktop([_bushy_window(leaves=15)])
        self.assertTrue(uia_automation_available("v1"))

    def test_two_node_black_box_is_dead(self):
        inner = _FakeElement([_FakeElement()])
        self._patch_desktop([_FakeWindow("Weixin", [inner])])
        self.assertFalse(uia_automation_available("v1"))

    def test_no_weixin_window_is_dead(self):
        self._patch_desktop([_bushy_window(title="记事本", leaves=50)])
        self.assertFalse(uia_automation_available("v1"))

    def test_result_is_cached_per_version(self):
        self._patch_desktop([_bushy_window(leaves=15)])
        self.assertTrue(uia_automation_available("v1"))
        self._patch_desktop([])
        self.assertTrue(uia_automation_available("v1"), "同版本必须走缓存")
        self.assertFalse(uia_automation_available("v2"), "换版本重探")

    def test_default_sender_is_wcdb(self):
        # 界面链和内存 carve 都已退役：默认值恒为 wcdb（解密库），不再问探针。
        self.assertEqual(default_sender_source(), "wcdb")


class NoUiaCallsTests(unittest.TestCase):
    """界面操控移除的总锁：生产代码里不许再出现操窗调用。

    用 AST 查（注释和文档字符串不算数）。上游自动化库本身
    （WeChatAuto/WeChatTools/Uielements/utils）是例外 —— 它们是
    放着不用的死代码，只要没人调用就行。测试文件也不管。
    """

    #: 这些名字在生产代码里出现即失败（AST 查，注释不算数）。
    #: 注意 Tools 不在里面：Tools.where_wxid_folder/get_weixin_version
    #: 是被动注册表读取，不动窗口，登录检测还要靠它。
    #: Uielements 也不在里面：它只是界面文案选择器数据（语言检测重建），
    #: 真正碰窗口的方法由下面的属性测试钉死。
    FORBIDDEN_ROOTS = {
        "WeChatAuto", "Contacts", "Messages", "Monitor", "Navigator",
        "live_listen", "LiveListener", "WeChatTargetInspector",
    }
    #: 这些方法一旦被调用（obj.xxx(...)），说明又在碰微信窗口。
    #: 被动注册表读取（where_wxid_folder/get_weixin_version、
    #: restart_weixin、is_weixin_running）不在此列 —— 它们不动窗口。
    FORBIDDEN_ATTRS = {
        "dump_chat_history", "dump_senders_via_avatar",
        "get_recent_groups", "get_friends_info", "get_groupMembers_info",
        "get_groupMembers_detail", "listen_on_chat", "listen_on_newMemberJoin",
        "send_messages_to_friend", "send_messages_to_friends",
        "open_chat_history", "open_dialog_window", "open_seperate_dialog_window",
        "open_chatinfo", "open_friend_profile", "open_contacts",
        "find_main_window", "pull_messages",
    }
    ALLOWLIST = {"WeChatAuto.py", "WeChatTools.py", "Uielements.py", "utils.py"}

    @classmethod
    def setUpClass(cls):
        import ast

        cls.root = Path(__file__).resolve().parent.parent / "src" / "wechat_ai_assistant"
        cls.trees = {}
        for path in sorted(cls.root.glob("*.py")):
            if path.name in cls.ALLOWLIST:
                continue
            cls.trees[path.name] = ast.parse(path.read_text(encoding="utf-8"))

    def test_no_forbidden_imports_or_names(self):
        import ast

        offenders = []
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    mod = (node.module or "").split(".")[-1]
                    hits = {n.name for n in node.names} & self.FORBIDDEN_ROOTS
                    if mod in self.FORBIDDEN_ROOTS or hits:
                        offenders.append(f"{name}: from-import {node.module}")
                    elif mod == "WeChatTools" and any(
                            n.name != "Tools" for n in node.names):
                        offenders.append(f"{name}: from WeChatTools import 非 Tools")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[-1] in self.FORBIDDEN_ROOTS:
                            offenders.append(f"{name}: import {alias.name}")
                elif isinstance(node, ast.Name):
                    if node.id in self.FORBIDDEN_ROOTS:
                        offenders.append(f"{name}: name {node.id}")
        self.assertEqual(offenders, [], "生产代码引用了界面操控模块")

    def test_no_forbidden_window_calls(self):
        import ast

        offenders = []
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in self.FORBIDDEN_ATTRS:
                    offenders.append(f"{name}: .{node.attr}(...)")
        self.assertEqual(offenders, [], "生产代码还在调用操窗方法")


class SendingIsGoneTests(unittest.TestCase):
    """产品里**不许再有任何「往微信里发消息」的实现**。

    ## 为什么删，而且为什么钉死

    这些函数的最后一步都是「点输入框 → 粘贴 → 按 alt+s」。而现在的读取路径是
    解密数据库、不碰窗口；UIA 在新版微信上也不可用 —— 也就是说这条发送路径
    **本来就走不通**。留着它只会让"这个软件会替你回客户"变成一句随时可能被
    重新实现的承诺。

    两条断言：
    1. 这些函数名在源码里完全不存在（不是"没被调用"，而是**没有**）；
    2. 源码里不再出现「按 alt+s」这个发送动作。
    """

    #: 已移除的发送实现（含自动回复、带引用回复、发文件、发语音）。
    REMOVED_APIS = (
        "send_messages_to_friend", "send_messages_to_friends",
        "send_files_to_friend", "send_files_to_friends",
        "send_audios_to_friend", "reply_with_quote",
        "auto_reply_to_friend", "auto_reply_messages",
    )

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent.parent / "src" / "wechat_ai_assistant"

    def test_no_sending_function_exists_anywhere(self):
        import ast

        found = []
        for path in sorted(self.root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in self.REMOVED_APIS:
                        found.append(f"{path.name}:{node.lineno}: {node.name}()")
        self.assertEqual(
            found, [],
            "发送实现又被加回来了（产品只读，不发消息）：\n  " + "\n  ".join(found))

    def test_nothing_presses_send(self):
        """`alt+s` 是微信发送消息的快捷键 —— 源码里（含注释）不该再有。"""
        import re

        pattern = re.compile(r"hotkey\(\s*['\"]alt['\"]\s*,\s*['\"]s['\"]")
        offenders = []
        for path in sorted(self.root.glob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()[:60]}")
        self.assertEqual(
            offenders, [],
            "还有代码在按发送快捷键：\n  " + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
