"""同一个软件只允许有一个实例在跑 —— 而且要能看出**是哪一个**。

## 出过什么事

客户机器上同时开着两个版本（旧目录没删干净、或者旧窗口没关就又解压了一份新的），
两个窗口长得一模一样，但一个是 1.11.1、一个是 1.11.6，于是"版本对不上""还是报错"
来回反复，谁也说不清看的是哪个。业务上更糟：两个实例盯着同一个微信、写同一份订单库。

所以启动时先看一眼：**除了自己（和自己的子进程）还有没有别的实例**。有就停下来，
把对方的路径写出来 —— 商户只要照着关掉那一个就行。

## 子进程不能算

打包形态下窗口宿主是同一个 exe 再起一个进程（pywebview 的宿主），源码形态是
`python launcher.py` 再拉一个 `python launcher.py`。所以"自己的整棵进程树"都要排除，
否则软件会认为"已经有自己在跑"，永远起不来。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

#: 允许同时开多个实例（排障/联调用）。正常商户不该设。
ALLOW_MULTIPLE_ENV = "WECHAT_AI_ALLOW_MULTIPLE"


def _self_tree() -> set[int]:
    """自己 + 所有祖先 + 所有子孙的 pid：这些都不算"另一个实例"。"""
    pids = {os.getpid()}
    try:
        import psutil

        me = psutil.Process(os.getpid())
        pids |= {p.pid for p in me.parents()}
        pids |= {c.pid for c in me.children(recursive=True)}
    except Exception:
        LOGGER.debug("读不到自己的进程树", exc_info=True)
    return pids


def _is_our_process(proc: Any) -> bool:
    """进程是不是本软件的实例（打包形态看 exe 名，源码形态看启动脚本）。"""
    try:
        name = str(proc.name() or "").lower()
    except Exception:
        return False
    if getattr(sys, "frozen", False):
        return name == Path(sys.executable).name.lower()
    if name not in ("python.exe", "pythonw.exe", "python", "pythonw"):
        return False
    try:
        cmdline = " ".join(proc.cmdline() or [])
    except Exception:
        return False
    return "launcher.py" in cmdline or "wechat_ai_assistant.web_app" in cmdline


def other_instances() -> list[dict[str, Any]]:
    """除自己这棵树之外的实例：``[{pid, exe, started}]``。查不到就当没有。"""
    try:
        import psutil
    except Exception:
        return []
    skip = _self_tree()
    found: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            if proc.info["pid"] in skip:
                continue
            if not _is_our_process(proc):
                continue
            found.append({
                "pid": int(proc.info["pid"]),
                "exe": str(proc.info.get("exe") or ""),
                "started": float(proc.info.get("create_time") or 0.0),
            })
        except Exception:
            continue
    return found


def describe(instances: list[dict[str, Any]]) -> str:
    """给商户看的说明：**把对方的路径写出来**，他才知道该关哪一个。"""
    lines = ["已经有一个「威信AI智能助理」在运行了：", ""]
    seen: set[str] = set()
    for item in instances:
        exe = item.get("exe") or "（路径未知）"
        if exe in seen:
            continue
        seen.add(exe)
        lines.append(f"　· {exe}")
    lines += [
        "",
        "请先把那个窗口关掉（任务管理器里结束「威信AI智能助理」也行），再打开这一份。",
        "两个同时开着会抢同一个微信窗口和同一份订单库，也容易搞不清在用的是哪一版。",
    ]
    return "\n".join(lines)


def multiples_allowed() -> bool:
    return str(os.environ.get(ALLOW_MULTIPLE_ENV) or "").strip() not in ("", "0", "false", "False")


__all__ = ["ALLOW_MULTIPLE_ENV", "describe", "multiples_allowed", "other_instances"]
