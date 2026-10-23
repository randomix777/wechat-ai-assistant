"""商户界面的本机接口服务：状态、动作、HTTP 路由。**产品形态**。

## 它是什么

``MerchantApplication`` 不依赖任何界面：它只往外发 ``AppEvents``（见
``application.py``）。Tk 界面订阅那套回调，**本模块的 Web 界面订阅同一套** ——
所以"流程对不对"只要在接口这一层验一次，两种界面都吃同一份结论。

## 一条硬规矩：只许本机

这个服务能读商户全部消息、订单和密钥配置，**没有任何鉴权**。绑定地址写错成
``0.0.0.0`` 就会被局域网摸到。所以除了绑 ``127.0.0.1``，还要**主动拒绝非回环
来源的请求** —— 只靠绑定地址不够，因为它可能被别的东西改掉。

## 界面事实只从这边出

页面（``web_page.py`` / ``web_wizard_page.py``）只渲染 ``/api/state``：

* 步骤的编号、标题、短名、走到哪一步 —— ``WizardStep`` 一处给的
* 门禁（能不能进下一步）—— ``Wizard.product_gate_open``
* 微信状态的中文文案 —— ``WeChatStatus.copy()``
* 整理角色、首页汇总 —— 应用本体

改流程、改文案不用动页面；页面里也不许自己判一次（两处判据迟早一处放行、
一处拦下）。

## 开发与交付

产品入口是 ``web_app.py``（起服务 + 装内嵌 WebView2 窗口；排障时可显式选其他宿主）。
开发用 ``web_dev.py``：它在本模块之上加"保存即生效"和 CLI，别的都一样。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from . import summary_task
from .application import MerchantApplication
from .roles import get_role
from .wizard import NO_CONVERSATION_SELECTED, WizardStep

LOGGER = logging.getLogger(__name__)


#: 默认端口。和云端控制台刻意错开，免得两个服务抢端口。
DEFAULT_PORT = 8765

#: 日志面板留多少行。多了浏览器会卡，而调试看最近的几百行足够。
LOG_LINES = 300

#: 页面文件就在旁边（不是静态资源，见各自的模块说明）。
#: ``dev`` 是给开发看的调试页；``wizard`` 是**商户版**向导页（登录前置页 + 正式流程）。
PAGE_SOURCES = {
    "dev": Path(__file__).with_name("web_page.py"),
    "wizard": Path(__file__).with_name("web_wizard_page.py"),
}
_PAGE_MODULES = {"dev": "web_page", "wizard": "web_wizard_page"}


#: 开发期打开：页面文件一变就重载模块、并注入自动刷新脚本（见 ``web_dev``）。
#: 产品里保持 False —— 打包后页面是常量，重载只会白读文件。
DEV_RELOAD = False

#: 自动刷新脚本。开发时由 ``web_dev`` 填进来。
LIVE_RELOAD = ""

_page_mtimes: dict[str, str] = {}


def page_mtime_ns() -> str:
    """两个页面的时间戳拼一起：任何一个变了，浏览器就该刷新。"""
    stamps: list[str] = []
    for which in sorted(PAGE_SOURCES):
        try:
            stamps.append(f"{which}={PAGE_SOURCES[which].stat().st_mtime_ns}")
        except OSError:
            continue
    return "|".join(stamps)


def page_html(data_dir: str, which: str = "dev") -> str:
    """页面 HTML。``which`` 选哪个页面：``dev`` 调试页 / ``wizard`` 商户向导页。

    数据目录由服务端注入 —— 它不能从 ``/api/state`` 拿，那是"服务运行中"的事；
    而目录错了正好表现为"没数据 / 未登录"，不显示出来没人会往哪找。

    开发期（:data:`DEV_RELOAD`）按文件时间戳重载模块，改坏了（语法错误）也
    **不能把页面弄没**：重载失败就继续用旧页面。
    """
    from importlib import import_module, reload

    which = which if which in PAGE_SOURCES else "dev"
    name = _PAGE_MODULES[which]
    module = import_module(f".{name}", package=__package__)
    if DEV_RELOAD:
        try:
            mtime = str(PAGE_SOURCES[which].stat().st_mtime_ns)
        except OSError:
            mtime = ""
        if mtime and mtime != _page_mtimes.get(which):
            try:
                module = reload(module)
                _page_mtimes[which] = mtime
            except Exception:  # pragma: no cover - 编辑中，文件暂时是坏的
                LOGGER.exception("%s.py 重载失败，继续用旧页面", name)
    html = module.PAGE.replace("__DATADIR__", data_dir or "（未知）")
    # 页面标题也带版本号：Edge 兜底窗口的标题来自 <title>，一眼能看出这是哪一版。
    html = html.replace("__VERSION__", _app_version())
    # 合规声明由服务端注入（来源只有 merchant_text 一处），页面自己抄一份就会漂。
    html = html.replace("__PRIVACY__", privacy_json())
    if DEV_RELOAD and LIVE_RELOAD:
        html = html.replace("</body>", LIVE_RELOAD + "\n</body>")
    return html


def privacy_json() -> str:
    """数据与隐私说明，注入成 JS 字面量。

    来源只有一份（``merchant_text.PRIVACY_TEXT``）—— 页面抄一份的话改一处就会出现
    两种说法。这段是**合规陈述**，不是营销文案：平时折叠在首页底部，商户需要时
    点得开、看得见，但不占版面。
    """
    from .merchant_text import PRIVACY_TEXT

    return json.dumps(PRIVACY_TEXT, ensure_ascii=False)


def _app_version() -> str:
    """本程序的版本号。界面要显示它。

    客服第一句一定是"你装的是哪一版"，而以前界面上任何地方都看不到 ——
    发版说明里"标题栏版本号对得上才算数"那句是 Tk 时代的残留（Tk 窗口标题
    带版本号，Web 这套没有）。来源只有一处：``version.py``。
    """
    try:
        from .version import __version__

        return str(__version__ or "")
    except Exception:  # pragma: no cover - 极端裁剪环境
        return ""


class WebState:
    """界面要看的全部状态。**加锁**。

    两类线程都会碰它：HTTP 处理线程（读）和应用线程（回调写入）。
    不是为了严谨，是因为不加锁时 ``/api/state`` 偶尔会读到**写了一半**的
    字典，浏览器里就报 ``undefined`` —— 这种偶发问题最难查。
    """

    def __init__(self, app: Any = None) -> None:
        self._lock = threading.RLock()
        #: 放行判据要拿真实的 orders/tenant。**不能拿占位对象**——
        #: 那样「有没有消息可整理」永远返回 False，页面上就表现为
        #: 「明明读到消息却不让整理」。
        self.app = app
        self.logs: deque[str] = deque(maxlen=LOG_LINES)
        self.error: str = ""
        self.step: str = ""
        self.busy: str = ""
        #: 读取进度（HistoryProgress 的可见部分）
        self.progress: dict[str, Any] | None = None
        #: 上一次读取的结果。有它才能整理、才能判断「能不能下一步」。
        self.history: Any = None
        self.analysis: dict[str, Any] | None = None
        #: 上一次非订单角色的 AI 报告。**不进轮询** —— 全文不该每秒回传；
        #: 页面点「查看 AI 整理报告」时按需取。
        self.role_report: dict[str, Any] | None = None
        #: 本次摘要的流式增量（token/分段到达即追加）。浏览器轮询
        #: ``/api/state`` 就能看到"正在一段段出来"，不用等最后全文。
        #: 只保留最近 ~200KB，多会话长分析不会把内存撑爆。
        self.summary_stream: str = ""
        #: 模型**推理过程**的增量（和报告正文分开存）。
        #: 正文之前那几十秒只有这段字可看；上限比正文小一点，够显示最近的想法。
        self.reasoning_stream: str = ""
        #: 分析进度的一句话（"正在分析第 2/3 个会话：xxx"）。
        #: 模型出第一个字之前可能等几十秒，没有这句商户只会以为卡死了。
        self.analysis_progress: str = ""
        self.conversations: list[dict[str, Any]] = []
        self.conversation_cache_info: str = ""
        self.orders: list[dict[str, Any]] = []
        self.notice: str = ""
        #: 读取相关的一句**小字**（显示在「刷新列表」下面那条状态行，不进横幅）。
        #: 横幅是"出事了/做完了"级别；"正在读数据库"这种过程说明不该占那么大位置。
        self.reading_notice: str = ""
        #: 新版暂存好了的一句话。以前这句只在 Tk 顶栏有 —— 界面换掉之后
        #: 不能顺手把自动更新也丢了（那正是"静默更新不等于一声不吭"的那句）。
        self.update_note: str = ""
        #: 微信状态（标题/说明/按钮/就绪）。第 0 步整屏都靠它画。
        self.wechat: dict[str, Any] = {}
        self._objects: dict[str, Any] = {}
        self.account: dict[str, Any] = {}
        #: 额度刷新是否在进行中。界面进「智能整理」那一屏时触发一次；
        #: 轮询和反复进出都不该叠成一串网络请求。
        self.quota_refreshing = False

    # ---- 额度刷新（进屏触发，不是商户按的按钮） ----

    def begin_quota_refresh(self) -> bool:
        """占住额度刷新。已经在刷就返回 False —— 不叠加。"""
        with self._lock:
            if self.quota_refreshing:
                return False
            self.quota_refreshing = True
            return True

    def end_quota_refresh(self) -> None:
        with self._lock:
            self.quota_refreshing = False

    # ---- 写入（应用线程回调里） ----

    def log(self, line: str) -> None:
        with self._lock:
            self.logs.append(line)

    def stream_summary(self, fragment: str) -> None:
        """追加一段分析流式增量。后台线程调，浏览器轮询取。"""
        if not fragment:
            return
        with self._lock:
            self.summary_stream = (self.summary_stream + fragment)[-200_000:]

    def reset_summary_stream(self) -> None:
        with self._lock:
            self.summary_stream = ""
            self.reasoning_stream = ""

    def stream_reasoning(self, fragment: str) -> None:
        """追加一段推理过程。和正文分两个字段：混在一起，商户会把思考当结论。"""
        if not fragment:
            return
        with self._lock:
            self.reasoning_stream = (self.reasoning_stream + fragment)[-60_000:]

    def fail(self, message: str) -> None:
        with self._lock:
            self.error = message
            self.busy = ""

    def clear_error(self) -> None:
        with self._lock:
            self.error = ""

    def start(self, what: str) -> None:
        with self._lock:
            self.busy = what
            self.error = ""
            self.notice = ""

    def finish(self, notice: str = "") -> None:
        with self._lock:
            self.busy = ""
            if notice:
                self.notice = notice

    def set(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(self, key, value)

    # ---- 读取（HTTP 处理线程） ----

    def set_conversation_objects(self, items: Any) -> None:
        """保留 Conversation 对象本身。页面传回来的只是名字。"""
        with self._lock:
            self._objects = {str(getattr(c, "name", "")): c for c in (items or [])}

    def conversation_objects(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._objects)

    def _step_now(self) -> str:
        """当前步骤。**以向导为准**：事件只是把变化同步过来，丢了事件
        （回调绑晚了、替身没接上）也不该让页面停在旧的一屏。

        没有向导的替身（单测里的假 app）才用缓存。
        """
        wizard = getattr(self.app, "wizard", None)
        live = str(getattr(wizard, "step", "") or "")
        if live:
            return live
        with self._lock:
            return self.step

    def _account_now(self) -> dict[str, Any]:
        """账号状态。**以应用当前状态为准**：登录是 attach 出来的，而事件只在
        登录的那一刻发一次 —— 只认缓存的副本，一旦漏掉那次事件，顶栏就会
        永远写着"未登录"，而流程其实已经能往下走了。

        没有 ``session`` 属性的替身（单测里的假 app）才用缓存。
        """
        if hasattr(self.app, "session"):
            return _account_view(self.app)
        with self._lock:
            return dict(self.account)

    def _gate_open(self) -> bool:
        """放行判据问向导，不问页面。没有向导（单测的假 app）时按已登录处理。"""
        wizard = getattr(self.app, "wizard", None)
        gate = getattr(wizard, "product_gate_open", None)
        if gate is None:
            return bool(self.account.get("logged_in"))
        try:
            return bool(gate())
        except Exception:
            return False

    def _can_go_home(self) -> bool:
        """要不要摆「重新开始」。问向导（走完过没有），没有向导的替身按"没有"处理。"""
        wizard = getattr(self.app, "wizard", None)
        finished = getattr(wizard, "has_finished", None)
        if finished is None:
            return False
        try:
            return bool(finished())
        except Exception:
            return False

    def _selected_view(self) -> list[str]:
        """已勾选要读历史的会话**键**（``name``）。界面拿它把上次的范围显示成已勾选 ——
        商户必须看清"这次要读哪些"，而不是点完才发现读错了群。

        给 ``name`` 而不是显示名：勾选框的 value 就是 ``name``，界面按它比对。
        以前给的是消歧标识（"名字（4人）"），两个没名字的群会撞成同一个串，
        还原勾选时就会一起勾上。
        """
        wizard = getattr(self.app, "wizard", None)
        registry = getattr(wizard, "conversations", None)
        items = getattr(registry, "history_selected", None) or ()
        names: list[str] = []
        for item in items:
            name = str(getattr(item, "name", "") or "")
            if name:
                names.append(name)
        return names

    def _wechat_view(self) -> dict[str, Any]:
        """微信状态。还没收到事件时回落到监视器的当前值 ——
        页面一进来就该看到状态，而不是空白等一次检测。"""
        with self._lock:
            view = dict(self.wechat)
        if view:
            return view
        status = getattr(getattr(self.app, "monitor", None), "latest", None)
        return _wechat_view(status) if status is not None else {}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            history = self.history
            step = self._step_now()
            return {
                "account": self._account_now(),
                # 本机版本号：顶栏要显示，客服排障第一句就问它。
                "version": _app_version(),
                "step": step,
                # 登录前置页和首页不在流程步骤表中，标题也由向导提供。
                "step_title": WizardStep.TITLES.get(step, ""),
                # 步骤条要用的事实（编号、标题、短名、走到哪一步）由向导一处给出，
                # 页面别自己写"第 N 步" —— 加一步或改措辞不用动页面。
                "steps": _steps_view(step),
                # 门禁只问向导，页面不许自己按 logged_in 再判一次（两套判据
                # 迟早一处放行、一处拦下）。
                "product_gate_open": self._gate_open(),
                # 走完过的商户才有「重新开始」可回（第一次用的人用「上一步」就够）。
                "can_go_home": self._can_go_home(),
                "wechat": self._wechat_view(),
                "selected": self._selected_view(),
                # 整理角色：页面画选择器用，不自己列一份。
                "roles": _roles_view(),
                "busy": self.busy,
                # 这一轮整理跑完了没（跑完停在整理屏给商户看，看完自己点下一步）。
                # 判据在向导（Wizard._analysis_ready），页面只渲染不自己判。
                "analysis_ready": bool(getattr(getattr(self.app, "wizard", None),
                                               "analysis_ready", False)),
                "error": self.error,
                "notice": self.notice,
                "reading_notice": self.reading_notice,
                "update_note": self.update_note,
                "progress": dict(self.progress) if self.progress else None,
                "logs": list(self.logs),
                "conversations": list(self.conversations),
                # 这份名单是不是从解密库来的（真名真人数）？解密库不可用时
                # 会退回上次缓存的那份 —— 界面必须如实显示，商户才知道差别。
                "conversation_source": str(getattr(self.app, "conversation_source_label", "") or ""),
                "conversation_cache_info": self.conversation_cache_info,
                "orders": list(self.orders),
                "summary_stream": self.summary_stream,
                "reasoning_stream": self.reasoning_stream,
                "analysis_progress": self.analysis_progress,
                "analysis": dict(self.analysis) if self.analysis else None,
                "history": _history_view(history),
            }


def _wechat_view(status: Any) -> dict[str, Any]:
    """微信状态里**界面要用的那些**：中文标题、说明、按钮、是否就绪。

    文案来自 ``WeChatStatus.copy()``（和 Tk 第 0 步同一份），页面不许自己编
    "微信未登录"这类话 —— 两处说法不一致时，商户不知道该信哪边。
    """
    try:
        title, detail, button = status.copy()
    except Exception:
        title, detail, button = "", "", ""
    return {
        "state": str(getattr(status, "state", "") or ""),
        "title": str(title or ""),
        "detail": str(detail or ""),
        "button": str(button or ""),
        "ready": bool(getattr(status, "ready", False)),
        #: 检测过没有。页面靠它决定"显示正在检测 + 自己触发一次检测"，
        #: 而不是去比对 detail 里那句"尚未检测"（比对文案迟早会漂）。
        "checked": bool(getattr(status, "checked_at", 0.0)),
    }


def _steps_view(current: str) -> list[dict[str, Any]]:
    """步骤条数据：编号、标题、短名，以及每一步是已完成/当前/未到。

    和 Tk 的步骤条同源（都是 ``wizard.WizardStep``）：Web 页面只渲染这份数据。
    以前的事实散在页面与主题两处，改流程要挨个改，还会自相矛盾。
    """
    # 额度页与智能整理同一屏 —— 和 Tk 步骤条同一套映射。
    current = {WizardStep.QUOTA: WizardStep.ANALYZE}.get(current, current)
    try:
        position = WizardStep.number(current)
    except ValueError:
        # 还没进向导（空串）或未知步骤：整条都是"未到"，不假装走到哪了。
        position = -1
    rows: list[dict[str, Any]] = []
    for index, step in enumerate(WizardStep.ORDER):
        if index < position:
            state = "done"
        elif index == position:
            state = "current"
        else:
            state = "todo"
        rows.append({
            "id": step,
            "number": WizardStep.number(step),
            "title": WizardStep.TITLES[step],
            "short": WizardStep.SHORT[step],
            "page_title": WizardStep.page_title(step),
            "state": state,
        })
    return rows


def _account_view(app: Any) -> dict[str, Any]:
    """登录状态 + 套餐概况。**字段名照 Session / QuotaView 的真实定义做，不猜。**

    猜字段的后果是页面上出现一个恒为空的「账号」栏，而日志里明明写着
    「Logged in as」，没人会对得上。
    """
    session = getattr(app, "session", None)
    quota = getattr(getattr(app, "wizard", None), "quota", None)
    view = {
        "logged_in": session is not None,
        "name": str(getattr(session, "merchant_name", "") or "") if session else "",
        "merchant_id": str(getattr(session, "merchant_id", "") or "") if session else "",
    }
    # 额度只在**真的取到过**的时候才给。默认的 QuotaView 讲的是一句
    # 「未登录产品账号」，而它旁边就是「已登录：JYI 账号」—— 同一屏自相矛盾，
    # 还顺带把 unlimited 说成真（默认 total 为 0）。宁可这一栏空着。
    if session is not None and quota is not None and getattr(quota, "logged_in", False):
        try:
            view.update({
                "plan": str(getattr(quota, "plan", "") or ""),
                "period_end": str(getattr(quota, "period_end", "") or ""),
                "used_percent": int(getattr(quota, "used_percent", 0) or 0),
                "unlimited": bool(getattr(quota, "unlimited", False)),
                "quota_headline": str(quota.headline()),
            })
        except Exception:
            LOGGER.debug("quota view skipped", exc_info=True)
    return view


def _roles_view() -> list[dict[str, Any]]:
    """整理角色。页面只画，不许自己列一遍 —— 加一个角色不该动页面。"""
    from .roles import ALL_ROLES, get_role

    rows: list[dict[str, Any]] = []
    for role_id in ALL_ROLES:
        role = get_role(role_id)
        rows.append({
            "id": role.id,
            "name": str(role.name),
            "description": str(getattr(role, "description", "") or ""),
            "uses_model": True,
            "output_kind": str(getattr(role, "output_kind", "report") or "report"),
            "sections": list(getattr(role, "result_sections", ()) or ()),
            "action_label": str(getattr(role, "action_label", "开始 AI 整理") or "开始 AI 整理"),
        })
    return rows


def _history_view(result: Any) -> dict[str, Any] | None:
    """读取结果里**界面真正要用**的那几项。

    不整个序列化：``coverage`` 里有 datetime，``import_ids`` 可能有几百个，
    整个丢给浏览器只会让页面变慢，而且商户看不懂。
    """
    if result is None:
        return None
    rows = []
    for item in getattr(result, "coverage", None) or []:
        conversation = getattr(item, "conversation", None)
        rows.append({
            "name": getattr(conversation, "name", "") if conversation is not None else "",
            # 显示名（未命名的群/联系人在这里是占位，不是原始 ID）。
            "label": str(getattr(conversation, "label", "")
                         or getattr(conversation, "name", "")) if conversation is not None else "",
            "kind": getattr(conversation, "kind", "") if conversation is not None else "",
            "count": int(getattr(item, "message_count", 0) or 0),
            "truncated": bool(getattr(item, "truncated", False)),
            "unresolved": int(getattr(item, "unresolved_senders", 0) or 0),
            "fetched": int(getattr(item, "fetched_count", 0) or 0),
            "skipped": bool(getattr(item, "skipped", False)),
            "error": str(getattr(item, "error", "") or ""),
        })
    try:
        headline = str(result.headline())
    except Exception:
        headline = ""
    return {
        "total": int(getattr(result, "total_messages", 0) or 0),
        "mode": str(getattr(result, "mode", "") or ""),
        # 商户看的那一句（"实际读到 22 条，时间范围 …"）来自 history_read，
        # 页面不自己拼 —— 拼出来的话两处说法会不一致。
        "headline": headline,
        "coverage": rows,
        "imported": len(list(getattr(result, "import_ids", None) or [])),
    }


def _conversation_view(conversation: Any) -> dict[str, Any]:
    return {
        #: **数据键**：消息/订单的 group_id、勾选、绑定都用它，界面不许拿它当显示名。
        "name": str(getattr(conversation, "name", "")),
        #: **显示名**：微信没给名字时是「未命名群聊」这类占位，不是原始 ID。
        "label": str(getattr(conversation, "label", "") or getattr(conversation, "name", "")),
        "kind": str(getattr(conversation, "kind", "")),
        "members": int(getattr(conversation, "member_count", 0) or 0),
        #: 消歧标识（``名字（人数）``）。界面拿它把服务端存着的读取范围
        #: 还原成勾选状态；``/api/state`` 的 ``selected`` 给的就是这个字符串。
        "disambiguation": str(getattr(conversation, "disambiguation", "") or ""),
    }


def _conversation_cache_info(app: Any) -> str:
    """Expose the existing cache timestamp without making UI status logic guess."""
    read_info = getattr(app, "conversation_cache_info", None)
    if not callable(read_info):
        return ""
    try:
        return str(read_info() or "")
    except Exception:
        LOGGER.debug("conversation cache status unavailable", exc_info=True)
        return ""


def _order_view(row: Any) -> dict[str, Any]:
    """一行订单 → 页面要的那几个字段。

    **两个来源的名字不一样**，两边都得认：

    * ``OrderRow``（真实那条路，``orders_for_display`` 给的）用的名字是
      ``order_id`` / ``amount`` / ``updated_at`` / ``group``；
    * 测试与外部调用方常直接塞 dict，用的是 ``id`` / ``total_cents`` /
      ``created_at`` / ``group_id``。

    只认后者的话，页面上「订单号」「金额」「时间」三列在真实数据下**恒为空**，
    而假数据下一切正常 —— 这种"只有真机上才看得见"的空列最难查，
    因为测试用的形状和产品里的从来不是一个（``value`` 的注释里记着一次同类事故）。
    """
    def value(name: str, default: Any = None) -> Any:
        # 允许给兜底值：``value("total_cents", value("total"))`` —— 以前只收一个
        # 参数，调用处的兜底写法直接把 /api/orders 打成 500（订单页永远是空的）。
        if isinstance(row, Mapping):
            return row.get(name, default)
        return getattr(row, name, default)

    total = value("amount")
    if total is None:
        total = value("total")
    if total is None:
        cents = value("total_cents")
        # 分 → 元：页面上直接印这个数，印 2600 会被读成两千六。
        total = (cents / 100) if isinstance(cents, (int, float)) else None
    return {
        "id": str(value("order_id") or value("id") or ""),
        "customer": str(value("customer_name") or value("customer") or ""),
        "status": str(value("status") or ""),
        "total": total,
        "summary": str(value("summary") or ""),
        "created_at": str(value("updated_at") or value("created_at") or ""),
        # 订单从哪个会话来的。客户是按名字认的（「张姐」在几个群里是同一个人），
        # 不写出处的话，跨群的重名单子就成了查不清的账。
        "group": str(value("group") or value("group_id") or ""),
    }



class WebHandler(BaseHTTPRequestHandler):
    app: MerchantApplication
    state: WebState
    #: 这个服务读的是哪个数据目录。**必须告诉使用者**。
    #: 目录不对时商户只会看到「没数据 / 未登录」，看不出原因。
    data_dir: str = ""
    server_version = "wechat-ai-assistant"

    # ---- 基础设施 ----

    def log_message(self, fmt: str, *args: Any) -> None:
        del fmt, args  # 每次请求都打会把控制台刷满，反而看不见内容

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(dict(payload), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, status: int, data: bytes, *, filename: str,
                       content_type: str = "application/zip") -> None:
        """把字节流给浏览器并让它**存成文件**（导出用）。

        文件名同时给 ASCII 兜底和 RFC 5987 的 ``filename*`` —— 中文名字在
        Chrome/Edge 上都靠后者，只给前者会变成一串下划线。
        """
        from urllib.parse import quote

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"export.zip\"; filename*=UTF-8''{quote(filename)}",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        # 上限 8 MB：名单 CSV 可能不小，但不该让一个请求吃光内存。
        raw = self.rfile.read(min(length, 8 * 1024 * 1024))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _reject_remote(self) -> bool:
        """只放回环。详见模块文档：没有鉴权，绝不能被局域网摸到。"""
        host = self.client_address[0] if self.client_address else ""
        if host in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or host.startswith("127."):
            return False
        self._send_json(403, {"error": "这个界面只允许本机访问。"})
        return True

    # ---- 路由 ----

    def _send_failure(self, exc: BaseException) -> None:
        """任何取页/取接口时抛出的异常，都要**回一句人话**，不能空着。

        浏览器把"连上了但一个字节都没回"显示成 ``ERR_EMPTY_RESPONSE``（客户看到的就是
        "empty response"）：既不像软件坏了，也没告诉商户该做什么，客服同样无从下手。
        这里尽量回一个带版本号和日志位置的页面。
        """
        import traceback

        version = _app_version() or "未知"
        log_path = "（未知）"
        try:
            from . import diagnostics

            log_path = str(diagnostics.path_of(Path(self.data_dir or ".")))
        except Exception:
            pass
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        LOGGER.exception("处理请求时出错：%s", self.path)
        html = (
            "<!DOCTYPE html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>微信AI智能助理</title>"
            "<body style=\"font:14px/1.7 'Microsoft YaHei',system-ui;padding:32px;max-width:760px;"
            "margin:0 auto;color:#1b2430\">"
            "<h2 style='color:#b4232c'>界面这一屏没能加载出来</h2>"
            "<p>软件还在运行，只是这一页出错了。请把这个页面截图发给客服，"
            "或者把下面这个文件发给客服：</p>"
            f"<p style='word-break:break-all;background:#f5f7f9;padding:8px 12px;border-radius:6px'>"
            f"{log_path}</p>"
            f"<p style='color:#667486'>版本：{version}<br>错误：{detail}</p>"
            "</body></html>"
        )
        try:
            self._send_html(500, html)
        except Exception:
            LOGGER.debug("连错误页都没发出去", exc_info=True)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self._reject_remote():
            return
        try:
            self._route_get()
        except Exception as exc:
            self._send_failure(exc)

    def _route_get(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            # 调试页只给开发外壳（``web_dev``，``DEV_RELOAD=True``）用。
            # 产品里端口上只该有商户版：调试页摆着「整理成一段话」这类老入口，
            # 商户在浏览器里敲一下地址就能走到另一条路上 —— 那条路已经不收了。
            which = "dev" if DEV_RELOAD else "wizard"
            self._send_html(200, page_html(self.data_dir, which))
        elif path in ("/wizard", "/wizard/"):
            # 商户版向导页（第 0～7 步）。调试页留在 /
            self._send_html(200, page_html(self.data_dir, "wizard"))
        elif path == "/api/page-version":
            # 页面靠它判断 web_page.py 有没有被改过，改了才刷新。
            self._send_json(200, {"version": page_mtime_ns()})
        elif path == "/api/state":
            self._send_json(200, self.state.snapshot())
        elif path == "/api/roles":
            self._send_json(200, {"roles": _roles_view()})
        elif path == "/api/reference/template":
            # 模板下载：商户不懂电脑，别让他自己拼表头 —— 下载 → 填 → 传回来。
            from . import reference_template

            kind = "customers" if "customers" in (self.path.split("?", 1) + [""])[1] else "products"
            data = reference_template.build_xlsx(
                reference_template.template_rows(kind),
                notes=reference_template.template_notes(kind))
            self._send_download(
                200, data, filename=reference_template.template_filename(kind),
                content_type=("application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet"))
        elif path == "/api/export/check":
            # 先问能不能导、会跳过几条 —— 页面照着说，别让商户点了没反应。
            self._send_json(200, self._export_check())
        elif path == "/api/export/bundle":
            self._export_bundle()
        elif path == "/api/analysis/report":
            # 非订单角色的 AI 报告：按需拉，避免全文混进每秒一次的状态轮询。
            # 内存里没有就回**落盘的那份** —— 商户关软件再打开，报告还得能看到
            # （否则上次花的钱换了份看不见的报告）。
            self._send_json(200, self._role_report_payload())
        elif path == "/api/logs":
            # 出问题时商户要有一个"能发给客服"的东西：日志尾巴 + 文件在哪。
            self._send_json(200, self._logs_view())
        elif path == "/api/conversations":
            # ?force=1 = 用户点了「刷新列表」：允许绕过密钥提取的 5 分钟节流。
            self._refresh_conversations(force="force=1" in (self.path.split("?", 1) + [""])[1])
        elif path == "/api/orders":
            rows = [ _order_view(r) for r in self.app.orders_for_display() ]
            self.state.set(orders=rows)
            self._send_json(200, {"orders": rows})
        else:
            self._send_json(404, {"error": f"没有这个地址：{path}"})

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_remote():
            return
        path = self.path.split("?", 1)[0]
        body = self._body()
        try:
            if path == "/api/wizard/advance":
                # 「下一步」的判据在向导里（Wizard.advance）：页面只管把 ok/reason
                # 照画出来，不自己再判一次（两套判据迟早一处放行、一处拦下）。
                outcome = self.app.wizard.advance()
                self.state.log(f"向导推进：{outcome}")
                self._send_json(200, outcome)
            elif path == "/api/wizard/back":
                outcome = self.app.wizard.back()
                self.state.log(f"向导后退：{outcome}")
                self._send_json(200, outcome)
            elif path == "/api/wizard/home":
                # 「重新开始」：到这一步才记「向导走完了」，然后回第一步。
                self.app.wizard.finish()
                self.state.log("向导完成，回到第一步")
                self._send_json(200, {"ok": True, "step": self.app.wizard.step})
            elif path == "/api/wait-for-wechat":
                # 点完「打开微信」后等商户扫码：监视器会持续检测，登录成功即
                # 通过 on_wechat 推进状态。不做的话页面永远停在"等待扫码"。
                watch = getattr(self.app, "watch_wechat", None)
                if callable(watch):
                    watch()
                self._send_json(200, {"ok": True})
            elif path == "/api/wechat/check":
                status = self.app.check_wechat()
                self.state.log(f"检查微信：{getattr(status, 'state', '')} {getattr(status, 'detail', '')}")
                self._send_json(200, {"ok": True, "state": str(getattr(status, "state", ""))})
            elif path == "/api/wechat/restart":
                # 读不到微信界面时的救援：先问一句（会丢未发送的内容），再关掉重开。
                # 重启要等好几秒，**不能占着处理线程** —— 否则页面整段卡死。
                if not self._run_blocking("重启微信", self._do_restart_wechat):
                    return
                self._send_json(200, {"ok": True, "started": True})
            elif path == "/api/wechat/start":
                self.app.start_wechat()
                self._send_json(200, {"ok": True})
            elif path == "/api/conversations/choose":
                names = [str(x) for x in (body.get("names") or [])]
                # 要的是 **Conversation 对象**，不是字符串。给字符串会在
                # wizard 里挂在 ``item.identity`` 上报 AttributeError。
                picked = self._resolve(names)
                self.app.choose_history_conversations(picked)
                self.state.log(f"\u5df2\u9009\u62e9\u4f1a\u8bdd\uff1a{'\u3001'.join(names) or '\uff08\u6e05\u7a7a\uff09'}")
                self._send_json(200, {"ok": True, "count": len(picked)})
            elif path == "/api/history/read":
                # 一个会话都没选就别开读：读空的等于让商户看着进度条等一场空。
                # 判据是向导当前的选择（不在这里重写一遍规则），说法取自
                # ``wizard.NO_CONVERSATION_SELECTED``，与「下一步」那句完全一致。
                wizard = getattr(self.app, "wizard", None)
                selected = getattr(getattr(wizard, "conversations", None),
                                   "history_selected", None)
                if not selected:
                    self._send_json(200, {"ok": False, "reason": NO_CONVERSATION_SELECTED})
                    return
                self.app.read_history(max_messages=int(body.get("max_messages") or 0),
                                      mode=str(body.get("mode") or ""))
                self.state.log("\u5df2\u5f00\u59cb\u8bfb\u53d6\u5386\u53f2\uff08\u53ef\u4ee5\u5173\u95ed\u6b64\u9875\uff0c\u4f1a\u5728\u540e\u53f0\u8dd1\uff09")
                self._send_json(200, {"ok": True})
            elif path == "/api/history/cancel":
                self.app.cancel_history()
                self._send_json(200, {"ok": True})
            elif path == "/api/analyze":
                role = str(body.get("role") or "order")

                def start() -> None:
                    # 上一轮的流式正文和进度都要清掉：留着的话，新一轮刚点开
                    # 就先看见上次的字，商户会以为分析早就开始了。
                    self.state.reset_summary_stream()
                    self.state.set(analysis_progress="")

                self._send_json(200, {"ok": True, "started": self._run_blocking(
                    "智能整理", lambda: self._do_analyze(role),
                    on_start=start)})
            elif path == "/api/login":
                # 登录是**前置条件**，不是第一步。没登录时智能整理必然失败
                # （排序依赖云端账号），所以页面开头就要能登录。
                self.app.login_requested()
                self._send_json(200, {"ok": True})
            elif path == "/api/logout":
                # 退出不删本地数据。页面上也说清楚这一点。
                self.app.detach()
                self.state.set(account={})
                self._send_json(200, {"ok": True})
            elif path == "/api/account/recharge":
                # 收银放网页（客户端不碰卡号、改价不用发版），这里只负责开浏览器。
                self._send_json(200, self._open_billing())
            elif path == "/api/account/quota":
                # 「智能整理」那一屏进屏时刷一次真额度。**界面自己触发的**，
                # 所以不占 busy、不留提示：商户不该看见"刷新额度完成"这种话。
                self._send_json(200, {"ok": True, "started": self._refresh_quota()})
            elif path == "/api/reference":
                kind = str(body.get("kind") or "")
                text = self._reference_text(body)
                outcome = summary_task.import_reference(self.app, kind, text)
                self.state.log(f"\u5bfc\u5165\u540d\u5355\uff08{kind}\uff09\uff1a{outcome}")
                self._send_json(200, outcome)
            else:
                self._send_json(404, {"error": f"\u6ca1\u6709\u8fd9\u4e2a\u5730\u5740\uff1a{path}"})
        except Exception as exc:
            # 尽量让接口回一个可读的错误，而不是 500 和一面标记。
            LOGGER.exception("request failed: %s", path)
            if path.startswith("/api/") and not path.startswith("/api/export/"):
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"[:300]})
            else:
                # 取页面/下载这类请求回 HTML：浏览器里至少看得见一句人话。
                self._send_failure(exc)

    # ---- 动作 ----

    @staticmethod
    def _reference_text(body: Mapping[str, Any]) -> str:
        """把商户上传的名单变成 CSV 文本。

        现在商户填的是我们发下去的**模板**（.xlsx），浏览器按 base64 传上来；
        老的 ``text`` 字段（纯 CSV）继续认，免得旧页面/脚本一起坏掉。

        模板里那行示例（首列以「示例」开头）会被跳过 —— 商户忘了删也能直接导。
        """
        import base64

        from . import reference_template, xlsx_read

        payload = str(body.get("data") or "")
        if payload:
            try:
                raw = base64.b64decode(payload, validate=False)
            except Exception:
                return ""
            if xlsx_read.is_xlsx(raw):
                rows = xlsx_read.rows_from_bytes(raw)
            else:
                # 不是 xlsx 就当文本 CSV（带不带 BOM 都认）
                rows = reference_template.rows_from_csv_text(
                    raw.decode("utf-8-sig", errors="replace"))
        else:
            rows = reference_template.rows_from_csv_text(str(body.get("text") or ""))
        # 第一行（表头）也会被加上「行号」列，后面每行带着自己的原始行号。
        numbered = reference_template.numbered_rows(rows)
        return xlsx_read.to_csv_text(numbered) if numbered else ""

    def _sync_account(self) -> None:
        """把当前登录状态写进状态。

        **字段名照 Session 的真实定义做，不猜。** 页面上出现一个恒为空的
        「账号」栏，而日志里出现「Logged in as」，两者对不上。
        """
        self.state.set(account=_account_view(self.app))

    def _resolve(self, names: list[str]) -> list[Any]:
        """按名字取回真的 Conversation 对象。

        页面只能拿到**名字**（JSON 里就这些），而 wizard 要的是对象。
        拿不到的名字不能“自己点一下等于那个”——
        选错了读取的就是别的会话，而商户看不出差异。
        """
        known = self.state.conversation_objects()
        return [known[n] for n in names if n in known]

    def _refresh_conversations(self, *, force: bool = False) -> None:
        found = list(self.app.refresh_conversations(force=force))
        rows = [_conversation_view(c) for c in found]
        self.state.set(conversations=rows)
        cache_info = _conversation_cache_info(self.app)
        self.state.set(conversation_cache_info=cache_info)
        self.state.set_conversation_objects(found)
        self._sync_account()
        self._send_json(200, {"conversations": rows, "cache_info": cache_info})

    def _logs_view(self) -> dict[str, Any]:
        """日志：文件在哪 + 最后几十行（给商户复制给客服）。"""
        from . import diagnostics

        data_dir = getattr(self.app, "data_dir", "") or ""
        path = ""
        try:
            path = str(diagnostics.path_of(__import__("pathlib").Path(data_dir)))
        except Exception:
            path = ""
        with self.state._lock:  # noqa: SLF001 - 同一个对象，读一下就走
            lines = list(self.state.logs)[-120:]
        return {"path": path, "lines": lines}

    def _do_restart_wechat(self) -> None:
        """关掉微信再打开，然后重新检测一次（和 Tk 时代同一个动作）。"""
        import time

        from .WeChatTools import Tools

        if not Tools.restart_weixin():
            raise RuntimeError("微信没能重新打开，请手动打开后再点「重新检测」。")
        time.sleep(8)  # 等它把登录态和窗口都摆好再检测
        status = self.app.check_wechat()
        self.state.log(f"重启微信后检测：{getattr(status, 'state', '')}")

    def _open_billing(self) -> dict[str, Any]:
        """打开充值页。打不开就把地址给出来，让人自己开 —— 不算失败。

        地址可配（``WECHAT_AI_BILLING_URL``），默认 JYI 控制台账单页；
        只接受同一个站点（https + 同域）的地址，避免被环境变量指到别处。
        """
        import os
        import webbrowser
        from urllib.parse import urlencode, urlsplit, urlunsplit

        from .jyi_client import JYI_AUTH_ORIGIN

        hint = "请联系客服开通或续期。"
        url = (os.environ.get("WECHAT_AI_BILLING_URL", "") or "").strip()
        if not url:
            billing = JYI_AUTH_ORIGIN.rstrip("/") + "/console/billing"
            parsed, trusted = urlsplit(billing), urlsplit(JYI_AUTH_ORIGIN)
            if (parsed.scheme == "https" and parsed.netloc == trusted.netloc
                    and parsed.path == "/console/billing"):
                # 先到登录页，带一个同站的回跳目标。
                url = urlunsplit((parsed.scheme, parsed.netloc, "/signin",
                                  urlencode({"next": "/console/billing"}), ""))
            else:
                url = billing
        if not url:
            return {"ok": False, "url": "", "hint": hint}
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception:
            opened = False
        self.state.log(f"充值页：{'已打开' if opened else '没能自动打开'} {url}")
        return {"ok": opened, "url": url, "hint": hint}

    def _export_check(self) -> dict[str, Any]:
        """能不能导：只导跑过 AI 的；跳过几条要能说出来。"""
        from . import export_service

        rows = export_service.review_rows(self.app)
        kept, skipped = export_service.exportable(self.app, rows)
        if not rows:
            return {"ok": False, "rows": 0, "skipped": 0,
                    "reason": "还没有读到消息，先读取再导出。"}
        if not kept:
            return {"ok": False, "rows": 0, "skipped": skipped,
                    "reason": "这些消息还没整理过，导出去等于绕开整理。"
                              "请先在「智能整理」里跑一遍。"}
        return {"ok": True, "rows": len(kept), "skipped": skipped, "reason": ""}

    def _role_report_payload(self) -> dict[str, Any]:
        """「查看 AI 整理报告」要的那份报告：内存优先，其次落盘的那份。

        ``?role=`` 可以让页面按当前选中的角色取；没有就取最近跑过的那个。
        """
        role_id = ""
        query = (self.path.split("?", 1) + [""])[1]
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key == "role":
                role_id = urllib.parse.unquote(value)
        role_id = role_id or str((self.state.analysis or {}).get("role_id") or "")
        report = self.state.role_report
        if report and (not role_id or str(report.get("role_id") or "") == role_id):
            # 统一成 ``groups``：内存里那份叫 ``group_reports``（向导汇总的字段名），
            # 落盘那份叫 ``groups``。页面只认一个键 —— 否则"刚跑完能展开、
            # 重启后展不开"，同一个按钮两种行为。
            fresh = dict(report)
            if not fresh.get("groups") and fresh.get("group_reports"):
                fresh["groups"] = list(fresh.get("group_reports") or [])
            return {"report": fresh}
        stored = None
        try:
            stored = (summary_task.load_role_report(self.app.orders, self.app.tenant, role_id)
                      if role_id else summary_task.latest_role_report(self.app.orders, self.app.tenant))
        except Exception:
            LOGGER.debug("read stored role report failed", exc_info=True)
        if not stored:
            return {"report": None, "reason": "还没有跑过智能整理。"}
        role = get_role(str(stored.get("role_id") or ""))
        return {"report": {
            "role_id": role.id,
            "role_name": role.name,
            "total": int(stored.get("total") or 0),
            "summary": str(stored.get("summary") or ""),
            "sections": list(role.result_sections),
            # 分会话那几份也跟着给：多选会话时商户要能按会话展开看。
            "groups": list(stored.get("groups") or []),
            "partial_error": "",
            "stored": True,
        }}

    def _export_bundle(self) -> None:
        """打包并让浏览器存盘。

        AI 报告取"这次跑完留下的那份"（摘要文本或角色报告）；内存里没有就回
        落盘的那份；都没有就只导消息 —— 报告是附加值，不能因为它缺了就不让导。
        """
        from . import export_service

        analysis = dict(self.state.analysis or {})
        role_report = dict(self.state.role_report or {})
        report = str(role_report.get("summary") or "").strip()
        role_name = str(analysis.get("role_name") or "").strip()
        if not report:
            stored = None
            try:
                stored = summary_task.latest_role_report(self.app.orders, self.app.tenant)
            except Exception:
                LOGGER.debug("read stored role report failed", exc_info=True)
            if stored:
                report = str(stored.get("summary") or "")
                role_name = role_name or get_role(str(stored.get("role_id") or "")).name
        try:
            bundle = export_service.build(self.app, analysis_report=report, role_name=role_name)
        except Exception as exc:  # 打包失败也要给一句人话
            LOGGER.exception("导出失败")
            self._send_json(500, {"error": f"导出失败：{type(exc).__name__}: {exc}"[:300]})
            return
        if not bundle["data"]:
            self._send_json(409, {"error": bundle["reason"]})
            return
        info = bundle.get("info") or {}
        if info.get("missing"):
            self.state.log(f"导出：{info['missing']} 个附件没找到，已写进说明文件")
        self.state.log(f"已导出 {bundle['rows']} 条（跳过 {bundle['skipped']} 条未整理）")
        self._send_download(200, bundle["data"], filename=bundle["filename"])

    def _refresh_quota(self) -> bool:
        """后台刷一次额度。返回是否真的开始刷（正在刷就不叠加）。

        **不占 busy、不留提示**：这是界面进屏时自己触发的，不是商户按的按钮
        —— 顶栏和那一屏的数字变了就够了，不该弹一句「刷新额度完成」。
        失败也只是"这次没刷到"：界面继续显示登录时存下的那份。
        """
        if not self.state.begin_quota_refresh():
            return False

        def work() -> None:
            try:
                self.app.check_quota()
            except Exception:
                LOGGER.debug("quota refresh skipped", exc_info=True)
            finally:
                self.state.end_quota_refresh()

        threading.Thread(target=work, name="quota-refresh", daemon=True).start()
        return True

    def _run_blocking(self, what: str, work: Callable[[], Any],
                      *, on_start: Callable[[], None] | None = None) -> bool:
        """另起线程跑阻塞动作。

        **必须另起。** ``analyze`` 和 ``run_summary`` 内部都是 ``join()``\uff0c
        放在处理线程里会把服务卡死 —— 连 ``/api/state`` 都取不到，
        页面看起来像整体挂掉了，而且看不出是哪个接口的问题。
        """
        if self.state.busy:
            self._send_json(409, {"error": f"\u6b63\u5728\u505a\u5176\u4ed6\u4e8b\uff1a{self.state.busy}"})
            return False
        if on_start is not None:
            on_start()
        self.state.start(what)

        def runner() -> None:
            try:
                work()
            except Exception as exc:
                LOGGER.exception("%s failed", what)
                self.state.fail(f"{what}\u5931\u8d25\uff1a{type(exc).__name__}: {exc}"[:300])
            else:
                self.state.finish(f"{what}\u5b8c\u6210")
        threading.Thread(target=runner, name=f"dev-{what}", daemon=True).start()
        return True

    def _do_analyze(self, role: str = "order") -> None:
        summary = self.app.analyze(role=role)
        if summary is None:
            # analyze \u5931\u8d25\u65f6\u81ea\u5df1\u5199\u8fc7 error\uff0c\u8fd9\u91cc\u4e0d\u8986\u76d6\u3002
            return
        role_id = str(summary.get("role_id") or "order")
        role_spec = next((item for item in _roles_view() if item["id"] == role_id), {})
        self.state.set(analysis={
            "processed": summary.get("processed", 0),
            "with_result": summary.get("with_result", 0),
            "needs_review": summary.get("needs_review", 0),
            "already_analyzed": summary.get("already_analyzed", 0),
            "submitted_jobs": summary.get("submitted_jobs", 0),
            "charged_micro_usd": summary.get("charged_micro_usd", 0),
        # 非订单角色的完整报告**不进轮询**：只把角色和条数放进状态，
        # 报告正文由页面按需取（/api/analysis/report）。
            "role_id": role_id,
            "uses_model": bool(summary.get("uses_model", True)),
            "output_kind": str(summary.get("output_kind") or role_spec.get("output_kind") or "orders"),
            "sections": list(summary.get("sections") or role_spec.get("sections") or ()),
            "partial_error": str(summary.get("partial_error") or ""),
            "role_name": str(summary.get("role_name") or role_spec.get("name") or "订单助手"),
            # 页面靠这三个说清"这次是增量 / 显示的是上次的 / 重启恢复的"。
            "is_incremental": bool(summary.get("is_incremental", False)),
            "reused_from_store": bool(summary.get("reused_from_store", False)),
            "restored": bool(summary.get("restored", False)),
        })
        # 角色整理的明细单独存：页面按需拉，别塞进每秒一次的 /api/state。
        self.state.set(role_report=summary.get("role_report") or None)
        # 没有流式正文时（比如"没有新消息，显示上次报告"）把报告回放进直播区：
        # 完成态那一屏的正文框不能空着"等待首段"—— 商户要看的就是它。
        if not self.state.summary_stream:
            report_text = str((summary.get("role_report") or {}).get("summary") or "")
            if report_text:
                self.state.stream_summary(report_text)
        self.state.log(f"\u6574\u7406\u5b8c\u6210\uff1a\u5904\u7406 {summary.get('processed', 0)} \u6761\u6d88\u606f")


def attach_events(app: MerchantApplication, state: WebState) -> None:
    """订阅 ``AppEvents`` —— 和 Tk 界面订阅的是**同一套回调**。"""
    events = app.events

    def on_step(step: str) -> None:
        state.set(step=str(step))

    def on_log(line: str) -> None:
        state.log(str(line))

    def on_error(message: str) -> None:
        state.fail(str(message))

    def on_progress(progress: Any) -> None:
        # 字段名照 HistoryProgress 的真实定义抄的（done/total/current/
        # cancelled/finished/message/messages_read/message_target）——
        # 这里没有 label，取它只会永远拿到空字符串。
        state.set(progress={
            "done": int(getattr(progress, "done", 0) or 0),
            "total": int(getattr(progress, "total", 0) or 0),
            "current": str(getattr(progress, "current", "") or ""),
            "cancelled": bool(getattr(progress, "cancelled", False)),
            "finished": bool(getattr(progress, "finished", False)),
            "message": str(getattr(progress, "message", "") or ""),
            "messages_read": int(getattr(progress, "messages_read", 0) or 0),
        })

    def on_history_done(result: Any) -> None:
        state.set(history=result, progress=None)
        state.finish("\u8bfb\u53d6\u5b8c\u6210")
        total = int(getattr(result, "total_messages", 0) or 0)
        state.log(f"\u8bfb\u53d6\u5b8c\u6210\uff0c\u672c\u6b21 {total} \u6761")

    def on_wechat(status: Any) -> None:
        state.set(wechat=_wechat_view(status))

    def on_conversations(items: Any) -> None:
        state.set(conversations=[_conversation_view(c) for c in (items or [])])
        state.set_conversation_objects(items)

    def on_logged_in() -> None:
        # 登录成功必须自己推一次：以前只有刷会话列表时才同步账号，
        # 商户点完登录、回来还写着「未登录」。
        # 同时清掉尚未消退的登录错误（例如上一次 OAuth 回调超时）。
        state.set(account=_account_view(app), error="")

    events.on_step = on_step
    events.on_log = on_log
    events.on_analysis_stream = state.stream_summary
    events.on_analysis_reasoning = state.stream_reasoning
    events.on_analysis_progress = lambda line: state.set(analysis_progress=str(line or ""))
    events.on_error = on_error
    events.on_history_progress = on_progress
    events.on_history_done = on_history_done
    events.on_conversations = on_conversations
    events.on_wechat = on_wechat
    events.on_logged_in = on_logged_in

    def on_notice(text: str) -> None:
        # 只进读取区那一行小字。横幅留给错误和"做完了"。
        state.set(reading_notice=str(text or ""))

    events.on_notice = on_notice
    # 向导在构造时把回调**拷贝**了一份，那时 events 还是空的 —— 必须回填
    # 一次，否则向导走了步、页面上的 step 永远不动（Tk 界面踩过同一个坑，
    # 见 Wizard.bind_callbacks 的说明）。
    bind = getattr(getattr(app, "wizard", None), "bind_callbacks", None)
    if callable(bind):
        bind(on_step=events.on_step, on_log=events.on_log, on_wechat=events.on_wechat,
             on_history_done=events.on_history_done)


def start_background_update_check(state: WebState, app: Any, *, delay: float = 2.0) -> None:
    """界面起来之后，后台查一次新版（查到就下好暂存，下次启动换版）。

    **放在界面起来之后**：启动路径上不联网，商户双击就该看见窗口；
    查不到、网络不通、校验失败都只是"这次没有新版"，不打扰商户。

    暂存成功写一句话进状态，页面顶栏显示"新版已就绪，下次打开自动更新" ——
    静默更新不能等于一声不吭。以前这句只在 Tk 顶栏，界面换掉时最容易顺手丢掉。
    """

    def work() -> None:
        time.sleep(delay)
        # 上次换版没成过就先说一句。换版失败只写 %TEMP% 的运行日志和安装目录里的
        # 记号，界面上一句话都没有 —— 商户看到的是"怎么一直没更新"，
        # 客服也问不出所以然。查到新版就下、下好会覆盖这句话（那句更要紧）。
        try:
            from .updater import failed_update_note

            note = failed_update_note()
            if note:
                state.set(update_note=note)
                state.log(note)
        except Exception:
            LOGGER.debug("读取换版失败记号失败", exc_info=True)
        try:
            from .updater import check_in_background
        except Exception:  # pragma: no cover - 极端裁剪环境
            LOGGER.debug("没有更新模块，跳过检查")
            return

        def on_staged(info: Any) -> None:
            version = str((info or {}).get("version") or "")
            if version:
                state.set(update_note=f"新版 v{version} 已就绪，下次打开自动更新")

        def on_failed(info: Any, hint: str) -> None:
            # 查到了新版却没下成 —— 必须说出来。静默失败的结果是商户以为
            # "这软件再也不更新了"，而真正的原因可能只是没登录。
            version = str((info or {}).get("version") or "")
            text = (f"发现新版 v{version}，这次没下下来：{hint}" if version
                    else f"发现新版，这次没下下来：{hint}")
            state.set(update_note=text)
            state.log(text)

        try:
            check_in_background(on_staged=on_staged, on_failed=on_failed,
                                jyi_client=getattr(app, "client", None))
        except Exception:
            LOGGER.debug("检查更新失败（不影响使用）", exc_info=True)

    threading.Thread(target=work, name="update-check", daemon=True).start()


def _analysis_from_stored(stored: Mapping[str, Any] | None) -> dict[str, Any]:
    """落盘报告 → 界面用的 ``analysis`` 事实。角色名/章节从角色定义取，不存两份。"""
    role_id = str((stored or {}).get("role_id") or "")
    role = get_role(role_id)
    return {
        "processed": int((stored or {}).get("total") or 0),
        "with_result": 0,
        "needs_review": 0,
        "already_analyzed": None,
        "submitted_jobs": None,
        "charged_micro_usd": None,
        "role_id": role.id,
        "role_name": role.name,
        "uses_model": True,
        "output_kind": role.output_kind,
        "sections": list(role.result_sections),
        "partial_error": "",
        # 重启后恢复的：页面说「上次」，别把昨天的说成"本次"。
        "restored": True,
    }


def _restore_last_role_report(state: "WebState", app: Any) -> None:
    """把最近一份角色报告装回状态。取不到就当没有，不拦启动。"""
    try:
        stored = summary_task.latest_role_report(getattr(app, "orders", None),
                                                getattr(app, "tenant", ""))
    except Exception:
        LOGGER.debug("restore last role report failed", exc_info=True)
        return
    if not stored:
        return
    state.set(analysis=_analysis_from_stored(stored))
    role = get_role(str(stored.get("role_id") or ""))
    state.set(role_report={
        "role_id": role.id,
        "role_name": role.name,
        "total": int(stored.get("total") or 0),
        "summary": str(stored.get("summary") or ""),
        "sections": list(role.result_sections),
        "group_reports": [
            {"group_label": str(item.get("group_label") or ""),
             "report": str(item.get("report") or ""),
             "message_count": int(item.get("message_count") or 0)}
            for item in (stored.get("groups") or [])],
        "partial_error": "",
        "stored": True,
    })


def create_server(app: MerchantApplication, *, host: str = "127.0.0.1",
                  port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """建服务器。**不启动。** 启动交给调用方，免得测试里意外占了端口。"""
    state = WebState(app)
    attach_events(app, state)
    found = list(app.refresh_conversations())
    state.set(conversations=[_conversation_view(c) for c in found])
    state.set(conversation_cache_info=_conversation_cache_info(app))
    state.set_conversation_objects(found)
    state.set(account=_account_view(app))
    # 上次跑过的角色报告**从库里端回来**：关掉软件再打开，「查看 AI 整理报告」
    # 和导出还得拿得到（报告不落盘的话，钱花了、报告没了）。
    _restore_last_role_report(state, app)
    # 起始页由向导给（``first_step`` 恒为登录前置页）：账号是界面之前恢复的，
    # 页面不按登录字段自己猜 —— 恢复着登录也停在登录页，那一屏会写「已经登录」。
    resume = getattr(getattr(app, "wizard", None), "resume", None)
    if callable(resume):
        state.set(step=str(resume()))
    handler = type("BoundWebHandler", (WebHandler,),
                   {"app": app, "state": state, "data_dir": str(getattr(app, "data_dir", "") or "")})
    server = ThreadingHTTPServer((host, port), handler)
    server.web_state = state  # type: ignore[attr-defined]
    return server

