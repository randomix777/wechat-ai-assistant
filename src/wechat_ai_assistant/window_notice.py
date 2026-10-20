"""首次启动装界面组件时，给商户一个"正在装，别关"的小提示窗。

## 为什么需要

`ensure_webview_runtime` 会在开窗之前下载并安装 WebView2，最多等两分钟。这段时间
屏幕上什么都不发生 —— 商户会以为双击没反应，然后反复双击、或者直接杀进程，安装
就是在这个时候被弄坏的。

## 为什么不用 tkinter

仓库只剩更新闪屏用 tkinter（`launcher._show_update_splash`），再引一处就是在长
第二套界面（`tests/test_launcher_ui.py` 会拦）。这里只用 Win32 的三件套：
RegisterClass / CreateWindowEx / 消息循环，一个 STATIC 控件显示一行字。

## 硬规矩

**提示窗永远不能拦住启动。** 创建失败、线程起不来、系统不给窗口 —— 一律当没这回事
继续装；`close()` 也必须能在任何状态下安全调用（包括根本没显示过）。
"""
from __future__ import annotations

import ctypes
from typing import Any
import logging
import sys
import threading
from ctypes import wintypes

LOGGER = logging.getLogger(__name__)

# ---- Win32 常量 ----
_WS_POPUP = 0x80000000
_WS_BORDER = 0x00800000
_WS_VISIBLE = 0x10000000
_WS_CHILD = 0x40000000
_WS_TABSTOP = 0x00010000
_SS_CENTER = 0x00000001
_SS_NOPREFIX = 0x00000080
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_SETFONT = 0x0030
_SW_SHOW = 5
_SW_SHOWNORMAL = 1
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1
_DEFAULT_GUI_FONT = 17
_COLOR_WINDOW = 5


_USER32_INSTANCE: Any = None


def _user32():  # pragma: no cover - 只在真 Windows 上跑
    """拿到 user32 并**声明每个函数的签名**。

    不声明 argtypes 时，``DefWindowProcW`` 会把 64 位的 ``lparam`` 当成 C int 传，
    直接 ``OverflowError: int too long to convert`` —— 窗口能画出来，但每次消息
    处理都抛异常（第一次写就踩了这两下：结构体、argtypes）。

    注意缓存的是**实例**不是"已配置"标记：``WinDLL()`` 每次返回新对象，argtypes
    记在实例上，缓存标记会导致第二次开窗又变回没签名。
    """
    global _USER32_INSTANCE
    if _USER32_INSTANCE is not None:
        return _USER32_INSTANCE
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    lresult = ctypes.c_ssize_t
    user32.DefWindowProcW.restype = lresult
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.SendMessageW.restype = lresult
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.GetMessageW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    user32.DispatchMessageW.restype = lresult
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.GetSysColorBrush.restype = wintypes.HBRUSH
    user32.GetSysColorBrush.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
    _USER32_INSTANCE = user32
    return user32


#: 这三个结构体 ``ctypes.wintypes`` **没有**，必须自己声明 —— 少一个字段就是
#: RegisterClassW 静默失败、窗口不出现（第一次写就踩在这个上面）。
class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class WaitingNotice:
    """一个"正在安装…"的小窗。``show()`` 不阻塞，``close()`` 关掉它。"""

    #: 窗口尺寸（够放两行中文）。
    SIZE = (420, 120)

    def __init__(self, text: str, title: str = "微信AI智能助理") -> None:
        self.text = text
        self.title = title
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._should_close = threading.Event()

    # ---- 对外 ----

    def show(self) -> "WaitingNotice":
        """起一个线程显示窗口。**失败就不显示**，绝不抛异常。"""
        if sys.platform != "win32":
            return self
        try:
            self._thread = threading.Thread(target=self._run, name="waiting-notice",
                                            daemon=True)
            self._thread.start()
        except Exception:
            LOGGER.debug("提示窗线程起不来", exc_info=True)
            self._thread = None
        return self

    def close(self) -> None:
        """关窗并等线程收尾。没显示过、或已经关掉，调用都安全。

        两条消息都要发：``WM_CLOSE`` 让窗口自己销毁，``WM_QUIT`` 让**消息循环**
        一定退出。只发 WM_CLOSE 时，窗口没了但线程还挂在 ``GetMessageW`` 上
        （实测 ``thread alive = True``），进程收尾就不干净。
        """
        self._should_close.set()
        hwnd = self._hwnd
        if hwnd:
            try:
                _user32().PostMessageW(wintypes.HWND(hwnd), _WM_CLOSE, 0, 0)
            except Exception:
                LOGGER.debug("关闭提示窗失败", exc_info=True)
        thread_id = self._thread_id
        if thread_id:
            try:
                _user32().PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
            except Exception:
                LOGGER.debug("退出提示窗消息循环失败", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)

    def __enter__(self) -> "WaitingNotice":
        return self.show()

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- 窗口线程 ----

    def _run(self) -> None:  # pragma: no cover - 需要真窗口
        try:
            self._create_and_pump()
        except Exception:
            LOGGER.debug("提示窗创建失败（不影响安装）", exc_info=True)

    def _create_and_pump(self) -> None:  # pragma: no cover - 需要真窗口
        user32 = _user32()
        from ctypes import wintypes as w

        wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == _WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == _WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        # 回调必须留引用，否则会被 GC 收走，消息循环直接崩。
        self._wndproc = wndproc_type(wndproc)

        hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        class_name = f"PyWeixinWaitingNotice{id(self)}"
        wc = _WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        wc.hbrBackground = user32.GetSysColorBrush(_COLOR_WINDOW)
        if not user32.RegisterClassW(ctypes.byref(wc)):
            error = ctypes.get_last_error()
            if error not in (0, 1410):  # 1410 = 类已注册（重复调用）
                raise RuntimeError(f"RegisterClassW 失败：{error}")

        width, height = self.SIZE
        screen_w = user32.GetSystemMetrics(_SM_CXSCREEN)
        screen_h = user32.GetSystemMetrics(_SM_CYSCREEN)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)

        hwnd = user32.CreateWindowExW(
            0, class_name, self.title,
            _WS_POPUP | _WS_BORDER | _WS_VISIBLE,
            x, y, width, height, None, None, hinstance, None)
        if not hwnd:
            raise RuntimeError(f"CreateWindowExW 失败：{ctypes.get_last_error()}")

        self._hwnd = hwnd
        # 文字用 STATIC 控件画：不引入字体/绘制代码，一行字够用。
        label = user32.CreateWindowExW(
            0, "STATIC", self.text, _WS_CHILD | _WS_VISIBLE | _SS_CENTER | _SS_NOPREFIX,
            12, 18, width - 24, height - 36, hwnd, None, hinstance, None)
        if label:
            stock = ctypes.windll.gdi32.GetStockObject(_DEFAULT_GUI_FONT)
            user32.SendMessageW(w.HWND(label), _WM_SETFONT, w.WPARAM(stock or 0), 1)

        user32.ShowWindow(hwnd, _SW_SHOWNORMAL)
        user32.UpdateWindow(hwnd)
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        self._ready.set()

        message = _MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))


__all__ = ["WaitingNotice"]
