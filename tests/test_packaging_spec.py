"""打包规格必须把**动态加载**的模块列进 hiddenimports。

## 出过什么事（线上，躲了很久）

`web_server.page_html()` 是按名字加载页面的：

    PAGE_SOURCES = {"dev": ..., "wizard": ...}
    _PAGE_MODULES = {"dev": "web_page", "wizard": "web_wizard_page"}
    module = import_module(f".{name}", package=__package__)

PyInstaller 只跟踪**静态可见**的 import。这两个页面模块从来没被静态引用过，
于是打包版里根本没有它们 —— 取 `/wizard` 抛 `ModuleNotFoundError`，HTTP 处理器
异常退出、连接直接关掉，浏览器那头就是 `ERR_EMPTY_RESPONSE`（客户报的
"empty response"）。

源码运行一切正常（模块就在磁盘上），所以本地怎么测都测不出来；Tk 老版本是原生
窗口、不走 HTTP，也从来没有这个问题。**这是一个只有"装成 exe"才会出现的故障。**

这里不跑打包，只做静态检查：凡是 `import_module` 里用字符串点名的模块，必须在
spec 的 hiddenimports 里出现。以后再加页面/插件，忘了登记就会在这里红。
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SPEC = ROOT / "deploy" / "merchant.spec"
SOURCE_DIR = ROOT / "src" / "wechat_ai_assistant"


def _hidden_imports() -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    block = text[text.index("HIDDEN = ["):text.index("]", text.index("HIDDEN = ["))]
    return set(re.findall(r'"([^"]+)"', block))


def _dynamic_page_modules() -> set[str]:
    """从 web_server 里读出"按名字加载"的页面模块。"""
    from wechat_ai_assistant import web_server

    return {f"wechat_ai_assistant.{name}" for name in web_server._PAGE_MODULES.values()}


class PackagingSpecTests(unittest.TestCase):
    def test_the_page_modules_are_bundled(self):
        missing = _dynamic_page_modules() - _hidden_imports()
        self.assertEqual(missing, set(),
                         f"页面模块是按名字加载的，必须加进 hiddenimports：{missing}")

    def test_the_page_modules_really_exist(self):
        """名字写错了同样会漏包 —— 反过来核对一遍模块名。"""
        for dotted in _dynamic_page_modules():
            with self.subTest(module=dotted):
                path = SOURCE_DIR / f"{dotted.rsplit('.', 1)[-1]}.py"
                self.assertTrue(path.is_file(), f"{dotted} 对应的文件不存在：{path}")

    def test_every_dynamic_import_in_the_package_is_registered(self):
        """扫源码里 `import_module("wechat_ai_assistant.xxx")` 这类写法。

        只认**带包名的字面量**：相对导入（``.{name}``）由上面的页面清单覆盖。
        """
        hidden = _hidden_imports()
        offenders = []
        for path in SOURCE_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name != "import_module" or not node.args:
                    continue
                first = node.args[0]
                if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                    continue
                module = first.value
                if not module.startswith("wechat_ai_assistant."):
                    continue
                if module not in hidden:
                    offenders.append(f"{path.name}: {module}")
        self.assertEqual(offenders, [],
                         "这些模块是运行时按名字导入的，打包必须显式登记：" + "；".join(offenders))


if __name__ == "__main__":
    unittest.main()
