"""界面设计系统：颜色、字体、卡片、步骤条。唯一的样式事实来源。

## 为什么单独一层

以前样式散在各页面里写死：标题 17 号、分区 11 号、按钮靠描边区分主次、
`vista` 主题吃不下背景色所以主按钮做不出填充色。改一处风格要翻六个页面，
翻完还不一定齐。

现在页面只说"我要一张卡片/一个主按钮/一段次要文字"，长什么样这里定。
文案一律不动（单测锁着），只换视觉。

## 基座是 clam 不是 vista

`vista` 不吃 background/foreground，主按钮永远做不出品牌填充色。
`clam` 全可样式化，自带扁平观感，配好后更接近大厂后台的干净风格。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Sequence

FONT = "Microsoft YaHei"

#: 墨 / 次要 / 弱化 / 线 / 背景 / 卡片 / 品牌 / 成功 / 警告 / 危险
INK = "#1a1d21"
SECONDARY = "#5b6470"
MUTED = "#8a94a0"
LINE = "#e3e7ec"
APP_BG = "#f2f4f7"
CARD_BG = "#ffffff"
BRAND = "#0e9f6e"
BRAND_DARK = "#0b7d57"
BRAND_TINT = "#e6f5ef"
WARN = "#b7790b"
WARN_TINT = "#fdf3e2"
DANGER = "#c0392b"
DANGER_TINT = "#fdecea"
GOOD = "#0e9f6e"

TITLE_FONT = (FONT, 19, "bold")
BRAND_FONT = (FONT, 15, "bold")
SECTION_FONT = (FONT, 12, "bold")
BODY_FONT = (FONT, 10)
BODY_BOLD = (FONT, 10, "bold")
BIG_BUTTON_FONT = (FONT, 11, "bold")
SMALL_FONT = (FONT, 9)
HINT_FONT = (FONT, 9)


def apply_theme(root: Any) -> ttk.Style:
    """配好整套 ttk 样式。样式名保持不动（单测锁着），只换视觉。"""
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:  # pragma: no cover - platform dependent
        pass
    # Win11 云母标题栏：真正的系统材质，不是仿的。失败（旧系统、缺依赖）
    # 就当没这回事，内容区主题不受影响。
    _apply_win11_chrome(root)
    style.configure(".", background=CARD_BG, foreground=INK, font=BODY_FONT,
                    fieldbackground=CARD_BG, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE)
    root.configure(background=CARD_BG)
    try:
        root.option_add("*TCombobox*Listbox.background", CARD_BG)
    except Exception:
        pass

    # ---- 文字层级
    style.configure("Title.TLabel", font=TITLE_FONT, foreground=INK, background=APP_BG)
    style.configure("Brand.TLabel", font=BRAND_FONT, foreground=INK, background=CARD_BG)
    style.configure("Section.TLabel", font=SECTION_FONT, foreground=INK,
                    background=CARD_BG)
    style.configure("SectionPage.TLabel", font=SECTION_FONT, foreground=INK,
                    background=APP_BG)
    # 页面灰底上的小提示（直接挂在 page-body frame 上的裸 label 用这个，
    # 否则默认白底会在灰底上露出一块白斑）。
    style.configure("PageHint.TLabel", font=HINT_FONT, foreground=MUTED,
                    background=APP_BG)
    # 页面上的粗字小标题（整理角色/分析结果这种），灰底版本。
    style.configure("PageStrong.TLabel", font=(FONT, 10, "bold"), foreground=INK,
                    background=APP_BG)
    # 页面上的警告行（密钥没提/登录过期），灰底版本。
    style.configure("PageWarn.TLabel", font=BODY_FONT, foreground=WARN,
                    background=APP_BG)
    # 页面上的通过数（已选 N 个），灰底版本。
    style.configure("PageOk.TLabel", font=BODY_FONT, foreground=GOOD,
                    background=APP_BG)
    style.configure("Body.TLabel", font=BODY_FONT, foreground=INK, background=CARD_BG)
    style.configure("Secondary.TLabel", font=BODY_FONT, foreground=SECONDARY,
                    background=CARD_BG)
    style.configure("Muted.TLabel", font=HINT_FONT, foreground=MUTED, background=CARD_BG)
    style.configure("MutedPage.TLabel", font=HINT_FONT, foreground=MUTED,
                    background=CARD_BG)
    style.configure("Good.TLabel", font=BODY_BOLD, foreground=GOOD, background=CARD_BG)
    style.configure("Warn.TLabel", font=BODY_FONT, foreground=WARN, background=CARD_BG)
    style.configure("Bad.TLabel", font=BODY_FONT, foreground=DANGER, background=CARD_BG)
    # 卡片里的粗结果行（读到 N 条这种）
    style.configure("Result.TLabel", font=(FONT, 11, "bold"), foreground=INK,
                    background=CARD_BG)

    # ---- 按钮层级：主（品牌填充）/ 次（白底描边）/ 暂停（大字）
    # 没写 style 的普通按钮自动落到"次"这一档 —— 页面里二十多个次要按钮
    # 不用逐个改，只要主按钮显式标 Big 就行，主次永远不会混。
    style.configure(
        "TButton",
        padding=(9, 6),
        font=BODY_FONT,
        background=CARD_BG,
        foreground=SECONDARY,
        bordercolor=LINE,
        lightcolor=CARD_BG,
        darkcolor=CARD_BG,
    )
    style.map(
        "TButton",
        background=[("active", "#eef1f4"), ("disabled", "#eef1f4")],
        foreground=[("disabled", MUTED)],
        bordercolor=[("active", MUTED)],
    )
    for name in ("Big.TButton", "Pause.TButton"):
        style.configure(
            name,
            padding=(16, 10),
            font=BIG_BUTTON_FONT,
            borderwidth=0,
            relief="flat",
            background=BRAND,
            foreground="#ffffff",
            bordercolor=BRAND,
            lightcolor=BRAND,
            darkcolor=BRAND,
        )
        style.map(
            name,
            background=[("active", BRAND_DARK), ("disabled", "#b9c4cd")],
            foreground=[("disabled", "#f2f4f7")],
        )
    # 单测要求主按钮保留 font 与 borderwidth 键 —— 上面已有。
    style.configure(
        "Subtle.TButton",
        padding=(9, 6),
        font=BODY_FONT,
        borderwidth=1,
        relief="solid",
        background=CARD_BG,
        foreground=SECONDARY,
        bordercolor=LINE,
        lightcolor=CARD_BG,
        darkcolor=CARD_BG,
    )
    style.map(
        "Subtle.TButton",
        background=[("active", "#eef1f4")],
        bordercolor=[("active", MUTED)],
    )

    # ---- 容器（全白画布：Label 自带底色，和父容器不一致就会露斑，
    # 全白是最不会出错的做法，结构靠卡片边框和间距表达）
    # 页面容器用浅灰底，卡片（白底 + LINE 描边）自然浮出来。
    # 卡片内部文字样式保持 CARD_BG 不动，只有直接坐在页面上的
    # SectionPage / PageHint 用 APP_BG。
    style.configure("Page.TFrame", background=APP_BG)
    style.configure("Card.TFrame", background=CARD_BG)
    style.configure("CardTitle.TFrame", background=CARD_BG)
    style.configure("TopBar.TFrame", background=CARD_BG)
    style.configure("Stepper.TFrame", background=CARD_BG)
    # 卡片边框：白底细线 + 标题色
    style.configure("TLabelframe", background=CARD_BG, bordercolor=LINE,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", font=SECTION_FONT, foreground=INK,
                    background=CARD_BG)

    # ---- 输入与表格
    style.configure("Treeview", background=CARD_BG, fieldbackground=CARD_BG,
                    foreground=INK, font=BODY_FONT, rowheight=26,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
    style.configure("Treeview.Heading", font=BODY_BOLD, foreground=SECONDARY,
                    background="#eef1f4", relief="flat", padding=(4, 6))
    style.map("Treeview", background=[("selected", BRAND_TINT)],
              foreground=[("selected", INK)])
    style.configure("Horizontal.TProgressbar", background=BRAND,
                    troughcolor="#e3e7ec", bordercolor="#e3e7ec",
                    lightcolor=BRAND, darkcolor=BRAND, thickness=8)
    style.configure("TNotebook", background=APP_BG, bordercolor=LINE)
    style.configure("TNotebook.Tab", padding=(12, 6), font=BODY_BOLD,
                    background="#eef1f4", foreground=SECONDARY)
    style.map("TNotebook.Tab", background=[("selected", CARD_BG)],
              foreground=[("selected", BRAND_DARK)])
    style.configure("TEntry", padding=6)
    style.configure("Vertical.TScrollbar", background="#e3e7ec",
                    troughcolor=APP_BG, bordercolor=APP_BG,
                    lightcolor="#e3e7ec", darkcolor="#e3e7ec",
                    arrowcolor=SECONDARY)
    return style


def card(parent: Any, title: str = "", padding: int = 14) -> Any:
    """一张白底细边框卡片。返回内容 frame，往里 pack 即可。

    边框用外层 1px 线色 frame 实现（clam 下 Frame 不吃 border，用嵌套保证
    任何主题都有一圈干净的线）。
    """
    outer = tk.Frame(parent, background=LINE, borderwidth=0,
                     highlightthickness=0)
    outer.pack(fill="x", pady=(0, 12))
    inner = ttk.Frame(outer, style="Card.TFrame", padding=padding)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    if title:
        ttk.Label(inner, text=title, style="Section.TLabel").pack(
            anchor="w", pady=(0, 8))
    return inner


#: 步骤条：key、短名。顺序即 WizardStep.ORDER。
STEPS: Sequence[tuple[str, str]] = (
    ("wechat", "检查微信"),
    ("conversations", "选择会话"),
    ("history", "确认范围"),
    ("read", "读取消息"),
    ("review", "核对明细"),
    ("analyze", "智能整理"),
    ("results", "AI分析"),
)


def _apply_win11_chrome(root: Any) -> None:
    """Win11 云母标题栏 + 强制浅色标题（内容区是浅色的，深色标题栏打架）。

    两步缺一不可：只要 mica，标题栏会跟系统深色模式走黑；
    只要浅色标题，没有 mica 材质。任何一步失败都静默跳过。
    """
    try:
        import pywinstyles

        pywinstyles.apply_style(root, "mica")
    except Exception:
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        use_dark = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(use_dark), ctypes.sizeof(use_dark))
    except Exception:
        pass


class Stepper:
    """顶栏下的步骤条。圆点 + 短名：已完成绿底白字、当前品牌色描边加粗、
    未到灰字。只管画，状态由调用方按向导步骤推。"""

    def __init__(self, parent: Any):
        self.frame = ttk.Frame(parent, style="Stepper.TFrame")
        self.frame.pack(fill="x")
        self._dots: dict[str, Any] = {}
        self._labels: dict[str, Any] = {}
        self._cells: dict[str, Any] = {}
        self._arrows: list[Any] = []
        self._current: str = STEPS[0][0] if STEPS else ""
        for index, (key, short) in enumerate(STEPS):
            cell = ttk.Frame(self.frame, style="Stepper.TFrame")
            cell.pack(side="left", padx=(0, 4))
            dot = tk.Label(cell, text=str(index + 1), font=(FONT, 9, "bold"),
                           width=3, background="#e3e7ec", foreground=MUTED,
                           borderwidth=0, relief="flat", padx=4, pady=2)
            dot.pack(side="left")
            label = ttk.Label(cell, text=short, style="Muted.TLabel")
            label.pack(side="left", padx=(6, 10))
            self._dots[key] = dot
            self._labels[key] = label
            self._cells[key] = cell
            if index < len(STEPS) - 1:
                arrow = ttk.Label(self.frame, text="›", style="Muted.TLabel")
                arrow.pack(side="left", padx=(0, 4))
                self._arrows.append(arrow)
        # 窄窗（和微信并排时只有 860 宽）7 步一排会溢出被切掉：
        # 只留首/末/当前/前后一步的名字，其余缩成圆点。
        try:
            parent.bind("<Configure>", lambda _e: self._fit(), add="+")
            self.frame.after_idle(self._fit)
        except Exception:
            pass

    def _fit(self) -> None:
        """按实际宽度折叠步骤名。pack_forget 只是不画，点和状态都在，
        窗口拉宽就地恢复，不重建。"""
        try:
            width = int(self.frame.winfo_width() or 0)
        except Exception:
            return
        if width <= 1:
            try:
                self.frame.after(200, self._fit)
            except Exception:
                pass
            return
        order = [key for key, _ in STEPS]
        try:
            position = order.index(self._current)
        except ValueError:
            position = 0
        compact = width < 780
        keep = {order[0], order[-1], order[position]}
        if position > 0:
            keep.add(order[position - 1])
        if position < len(order) - 1:
            keep.add(order[position + 1])
        for key in order:
            label = self._labels[key]
            try:
                shown = bool(label.winfo_ismapped())
            except Exception:
                shown = True
            want = (not compact) or (key in keep)
            if want and not shown:
                try:
                    label.pack(side="left", padx=(6, 10))
                except Exception:
                    pass
            elif not want and shown:
                try:
                    label.pack_forget()
                except Exception:
                    pass
        # 箭头永远留着：它只占一个字符，不断行；藏了它步骤的先后反而看不清。
        # （也不敢 pack_forget 箭头再 pack 回来 —— 会掉到行尾，顺序全乱。）

    def set_step(self, current: str) -> None:
        """按当前步骤刷新三态。未知步骤（如 quota）映射到 analyze。"""
        mapped = {"quota": "analyze", "done": "results"}.get(current, current)
        self._current = mapped
        order = [key for key, _ in STEPS]
        try:
            position = order.index(mapped)
        except ValueError:
            position = 0
        for index, key in enumerate(order):
            dot = self._dots[key]
            label = self._labels[key]
            if index < position:
                dot.configure(text="✓", background=BRAND, foreground="#ffffff")
                label.configure(style="Secondary.TLabel")
            elif index == position:
                dot.configure(text=str(index + 1), background=BRAND,
                              foreground="#ffffff", highlightthickness=0)
                label.configure(style="Body.TLabel")
            else:
                dot.configure(text=str(index + 1), background="#e3e7ec",
                              foreground=MUTED, highlightthickness=0)
                label.configure(style="Muted.TLabel")


#: iconphoto 的 PhotoImage 必须留一份引用，否则被 GC 收走、图标又变回默认羽毛。
_ICON_IMAGES: list[Any] = []


def set_window_icon(window: Any) -> None:
    """给窗口挂上产品图标；找不到图标或系统不认就保持默认，绝不打断启动。

    任务栏和 Alt-Tab 用的是 exe 自己的图标（`deploy/merchant.spec` 里的
    ``icon=``），这里管的是窗口那一份 —— 开发态直接跑源码时也认得出是
    我们的程序，而不是 Python 自带的羽毛。
    """
    try:
        from .paths import asset_path
    except Exception:  # pragma: no cover - 极端裁剪环境
        return
    ico = asset_path("app.ico")
    if ico is not None:
        try:
            window.iconbitmap(default=str(ico))
            return
        except Exception:
            pass
    png = asset_path("app.png")
    if png is not None:
        try:
            image = tk.PhotoImage(file=str(png))
            _ICON_IMAGES.append(image)
            window.iconphoto(True, image)
        except Exception:
            pass


__all__ = [
    "APP_BG", "BODY_BOLD", "BODY_FONT", "BRAND", "BRAND_DARK", "BRAND_TINT",
    "CARD_BG", "DANGER", "DANGER_TINT", "GOOD", "INK", "LINE", "MUTED",
    "SECONDARY", "STEPS", "WARN", "WARN_TINT",
    "Stepper", "apply_theme", "card", "set_window_icon",
]
