"""给窗口挂产品图标。

## 两处，两种做法

* **Web 应用窗口**（Edge ``--app`` / pywebview）—— 那是"别人的窗口"，但我们可以
  用 Win32 的 ``WM_SETICON`` + 类图标把**自己的 .ico** 挂上去：标题栏、任务栏、
  Alt-Tab 都会跟着变。不这么做，商户看到的就是 Edge 的图标 —— 那不像我们的软件。
* **更新闪屏**（``launcher._show_update_splash``，Tk）—— 用 Tk 自己的
  ``iconbitmap`` / ``iconphoto``。

找不到图标、系统不认、不是 Windows —— 一律**静默跳过**，图标不能拦住启动。
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

#: iconphoto 的 PhotoImage 必须留一份引用，否则被 GC 收走、图标又变回默认羽毛。
_ICON_IMAGES: list[Any] = []

# ---- Win32 常量（只在 Windows 上用到） ----
_WM_SETICON = 0x0080
_ICON_SMALL = 0
_ICON_BIG = 1
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_GCLP_HICON = -14
_APP_USER_MODEL_ID = "PyWeixin.WeChatAIAssistant"

_user32: Any = None


def _native():
    """user32，按需初始化。非 Windows 返回 None。"""
    global _user32
    if sys.platform != "win32":
        return None
    if _user32 is not None:
        return _user32
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.restype = wintypes.LPARAM
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.SetClassLongPtrW.restype = ctypes.c_void_p
        user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        _user32 = user32
    except Exception:
        LOGGER.debug("user32 初始化失败", exc_info=True)
        _user32 = None
    return _user32


def icon_path() -> Path | None:
    try:
        from .paths import asset_path

        return asset_path("app.ico")
    except Exception:
        return None


def set_app_user_model_id(app_id: str = _APP_USER_MODEL_ID) -> bool:
    """Give Windows a product identity for taskbar grouping instead of Python's."""
    if sys.platform != "win32":  # pragma: no cover - the product is Windows-only
        return False
    try:
        import ctypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        set_app_id = shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        result = set_app_id(app_id)
        if result != 0:
            LOGGER.debug("设置 Windows 应用身份失败：HRESULT=0x%08X", result & 0xFFFFFFFF)
            return False
        return True
    except Exception:
        LOGGER.debug("设置 Windows 应用身份失败", exc_info=True)
        return False


def set_window_user_model_id(hwnd: int, app_id: str = _APP_USER_MODEL_ID) -> bool:
    """Set the AppUserModelID on a window before Explorer creates its taskbar button."""
    if sys.platform != "win32" or not hwnd:  # pragma: no cover - Windows-only API
        return False
    try:
        from win32com.propsys import propsys, pscon

        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(app_id))
        store.Commit()
        return True
    except Exception:
        LOGGER.debug("设置窗口 AppUserModelID 失败：hwnd=%s", hwnd, exc_info=True)
        return False


def set_window_icon(window: Any) -> None:
    """Tk 窗口（更新闪屏）挂图标。"""
    try:  # Tk 只在更新闪屏调用这里；普通客户端启动不导入 Tk。
        import tkinter as tk
    except Exception:  # pragma: no cover
        return
    ico = icon_path()
    if ico is not None:
        try:
            window.iconbitmap(default=str(ico))
            return
        except Exception:
            pass
    try:
        from .paths import asset_path

        png = asset_path("app.png")
    except Exception:
        png = None
    if png is not None:
        try:
            image = tk.PhotoImage(file=str(png))
            _ICON_IMAGES.append(image)
            window.iconphoto(True, image)
        except Exception:
            pass


def apply_native_icon(hwnd: int, ico: Path | None = None) -> bool:
    """给**别人的**顶层窗口挂上我们的图标（Edge --app / pywebview 都行）。

    同时设 ``WM_SETICON`` 和类图标：前者管这个窗口，后者让任务栏和以后同类窗口
    也用我们的 —— 只设其中一个，任务栏那个格子可能还是 Edge 的。
    """
    user32 = _native()
    path = ico or icon_path()
    if user32 is None or not hwnd or path is None:
        return False
    try:
        import ctypes

        big = user32.LoadImageW(None, str(path), _IMAGE_ICON, 32, 32, _LR_LOADFROMFILE)
        small = user32.LoadImageW(None, str(path), _IMAGE_ICON, 16, 16, _LR_LOADFROMFILE)
        if not big and not small:
            return False
        if big:
            user32.SendMessageW(hwnd, _WM_SETICON, _ICON_BIG, big)
            user32.SetClassLongPtrW(hwnd, _GCLP_HICON, big)
        if small:
            user32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, small)
        return True
    except Exception:
        LOGGER.debug("挂窗口图标失败", exc_info=True)
        return False


def find_window_by_pid(pid: int) -> int:
    """找某个进程唯一的顶层可见窗口（Edge ``--app`` 就一个）。找不到返回 0。"""
    user32 = _native()
    if user32 is None or not pid:
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        found: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd: int, _param: int) -> bool:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value != pid or not user32.IsWindowVisible(hwnd):
                return True
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buffer, 256)
            if buffer.value.strip():  # 有标题的才算主窗口
                found.append(int(hwnd))
                return False
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return found[0] if found else 0
    except Exception:
        LOGGER.debug("按进程找窗口失败", exc_info=True)
        return 0


def find_window_by_title(title: str) -> int:
    user32 = _native()
    if user32 is None or not title:
        return 0
    try:
        return int(user32.FindWindowW(None, title) or 0)
    except Exception:
        return 0


def schedule_icon(pid: int = 0, title: str = "", *, timeout: float = 15.0) -> bool:
    """等窗口出现，再挂图标。**阻塞**：调用方放进后台线程。

    窗口是别人起的（Edge 要一两秒才画出窗口），太早找不到；太晚就来不及在
    商户第一眼里出现。所以轮询到 timeout。
    """
    deadline = time.monotonic() + max(0.5, timeout)
    while time.monotonic() < deadline:
        hwnd = find_window_by_pid(pid) if pid else find_window_by_title(title)
        if hwnd and apply_native_icon(hwnd):
            LOGGER.info("已挂上产品图标：hwnd=%s pid=%s", hwnd, pid)
            return True
        time.sleep(0.25)
    LOGGER.debug("没找到窗口/图标没挂上：pid=%s title=%s", pid, title)
    return False


def set_window_icon_async(pid: int = 0, title: str = "", *, timeout: float = 15.0) -> threading.Thread:
    """后台挂图标（不挡窗口）。返回线程，方便测试等待。"""
    thread = threading.Thread(target=schedule_icon, args=(pid, title),
                              kwargs={"timeout": timeout}, name="window-icon", daemon=True)
    thread.start()
    return thread


__all__ = [
    "apply_native_icon", "find_window_by_pid", "find_window_by_title", "icon_path",
    "schedule_icon", "set_app_user_model_id", "set_window_icon", "set_window_icon_async",
    "set_window_user_model_id",
]
