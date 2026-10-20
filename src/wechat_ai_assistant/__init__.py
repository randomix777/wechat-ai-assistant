"""WeChat AI Assistant application package.

The package keeps the historical automation API available at the top level
while importing Windows UI dependencies only when those names are requested.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

from .compat import import_legacy_environment
from ._packages import ensure_packages_on_path

import_legacy_environment()

#: 源码树里 ``packages/*`` 是纯源码，不进 venv。**必须在任何子模块之前**挂上
#: ``sys.path``：以前只有 opencode/integration/receipts/service 这几个模块自己
#: 调，于是谁先被导入谁定局 —— 而 ``orders`` 在 import 时就把契约读成了
#: 「降级兜底」那一份空 schema，并且**整个进程**都跟着用那一份。表现就是
#: 全量套件里几十个 "parse result does not match the required schema"
#: （按字母序第一个踩到的模块污染后面所有模块），单跑那个模块却是绿的。
ensure_packages_on_path()

#: 顶层仍然按需导出历史 API，但**只导出读得到的那一套**。
#:
#: 原来这里还有一整排指向 ``.WeChatAuto`` 的名字（``Messages`` / ``Monitor`` /
#: ``AutoReply`` …）。那套模块带的是点"发送"按钮、自动加好友、自动回复 ——
#: 产品一样都不用（``runmode`` 写着只读不写），但它**在源码树里**，只要有人
#: ``from wechat_ai_assistant import Messages`` 一下，PyInstaller 就会把它收进
#: 制品。于是"这个工具不会替我给别人发微信"就从**事实**退回成**承诺**。
#:
#: 所以这里不再导出它们。读消息走本机解密库（``localdb`` / ``wcdb``），导航走
#: ``.WeChatTools``，都不需要那一套。
_EXPORTS = {
    "Tools": ".WeChatTools", "Navigator": ".WeChatTools",
    "SystemSettings": ".WinSettings", "GlobalConfig": ".Config",
    "MessageDirection": ".reliability", "MessageEvent": ".reliability",
    "MessageKind": ".reliability", "MessageLedger": ".reliability",
    "OperationQueueClosed": ".reliability", "OperationQueueFull": ".reliability",
    "UiOperationQueue": ".reliability", "retry_call": ".reliability",
    "AgentConfig": ".agent", "AgentStore": ".agent",
    "LocalAgent": ".agent", "CloudClient": ".cloud", "CloudError": ".cloud",
    "CloudResponse": ".cloud", "CloudRequestHandler": ".cloud_server",
    "CloudStore": ".cloud_server", "create_server": ".cloud_server",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
