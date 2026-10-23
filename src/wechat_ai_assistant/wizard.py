"""首次使用向导：可恢复、可测试、无需命令行。

    登录前置页 → ⓪ 微信 → ① 选会话（含读取范围）→ ② 读历史 → ③ 智能整理 → ④ 分析详情 → 首页

少了两屏，都是同一件事被拆开画的：

* 「核对读取明细」不再单独占一步：读到的结果表就画在「读历史」那一屏上，
  再看一屏只是把同一条数据再画一遍，却多一次点击。
* 「确认读取范围」并进「选择会话」：它列的就是商户刚勾的那些名字，
  而「更新消息 / 读取全部」两个按钮本来在读取那一屏也有。

登录产品账号是进入向导的前置门槛，不占办理流程的步骤编号；未登录时显示独立登录页，
登录后从第 0 步「检查微信」开始。门禁判据仍只认 :meth:`Wizard.product_gate_open`。

每次启动都回到向导起点：**就是那页登录前置页**（已登录时它写着「已经登录」+ 额度，
点一下「下一步」就进流程），落点不由账号是什么时候恢复的来决定。上次选的会话同时被
装回来（:meth:`Wizard.restore`），选择会话那一屏照样显示成已勾选，不用重新挑群。
走完过向导的机器多一个动作：「重新开始」直接回第一步（:meth:`Wizard.finish`
回到「检查微信」），不设仪表盘式的首页 —— 跑完一轮，要么在结果屏看产物，
要么从头再来。

## 状态机而不是一堆 if

向导的每一步都有明确的进入条件和离开条件，因此可以在没有界面的情况下
完整跑一遍（测试就是这么做的），界面只负责把状态画出来。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

LOGGER = logging.getLogger(__name__)

from .conversations import Conversation, ConversationRegistry
from .history_read import (
    DEFAULT_MAX_MESSAGES,
    HistoryProgress,
    HistoryReader,
    HistoryResult,
    HistoryRange,
    ReadMode,
    TimeWindow,
)
from .runmode import AutoOrderSettings, MerchantSettings, SettingsStore
from .wechat_state import WeChatMonitor, WeChatState, WeChatStatus

#: 没勾会话就想读/往下走时给商户看的那句话。**只此一处**：
#: 「下一步」的判据（:meth:`Wizard.advance`）和接口里的读取前置检查都用它，
#: 各写一句迟早变成两种说法。
NO_CONVERSATION_SELECTED = "至少勾选一个要读取的会话。"


class WizardStep:
    #: 登录页是向导前的身份门槛，不是办理步骤；登录状态仍用独立状态值承载。
    LOGIN = "login"
    WECHAT = "wechat"
    CONVERSATIONS = "conversations"
    READ = "read"
    QUOTA = "quota"
    ANALYZE = "analyze"
    RESULTS = "results"

    #: **流程事实只此一处**：办理步骤 id → (页面标题, 步骤条上的短名)。
    #: 登录是进入流程的前置页，不进入列表、不占编号；编号从「检查微信」开始。
    #: 界面（Tk 的页面标题、步骤条圆点、Web 的 /api/state）一律从这里取，
    #: 不许再各自写一份 —— 加一步/改个措辞只改这张表。
    STEP_SPECS: dict[str, tuple[str, str]] = {
        WECHAT: ("检查微信", "检查微信"),
        CONVERSATIONS: ("选择会话", "选择会话"),
        READ: ("读取历史消息", "读取消息"),
        ANALYZE: ("智能整理", "智能整理"),
        RESULTS: ("AI分析详情", "AI分析"),
    }

    #: 商户看到的步骤，顺序即 :data:`STEP_SPECS` 的顺序。
    ORDER = tuple(STEP_SPECS)

    #: 步骤标题。不在向导里的额度页也留着：它与智能整理同一屏。
    TITLES = {LOGIN: "登录产品账号", **{step: spec[0] for step, spec in STEP_SPECS.items()}}
    TITLES[QUOTA] = STEP_SPECS[ANALYZE][0]

    #: 步骤条上的短名（窄窗口放不下长标题）。
    SHORT = {step: spec[1] for step, spec in STEP_SPECS.items()}

    @classmethod
    def number(cls, step: str) -> int:
        """办理步骤编号。登录前置页不编号，额度页与智能整理同屏。"""
        if step == cls.QUOTA:
            step = cls.ANALYZE
        return cls.ORDER.index(step)

    @classmethod
    def page_title(cls, step: str) -> str:
        """页面标题。登录前置页和首页不编号，其余按流程顺序编号。

        界面不要自己拼这个字符串：编号和标题都从这里来，才不会出现
        "标题写第 1 步、步骤条圆点写 2" 这种自相矛盾。
        """
        if step == cls.LOGIN:
            return cls.TITLES[step]
        if step == cls.QUOTA:
            step = cls.ANALYZE
        return f"第 {cls.number(step)} 步　{cls.TITLES[step]}"


@dataclass
class QuotaView:
    """额度。商户只看得到这些中文，不看单位名。

    展示形式是**进度条 + 百分比**（像 ChatGPT 的用量条），
    **不换算成“还够多少次”。商户的口头：

        额度给一个额度条，按百分比显示，不显示具体能做多少次

    额度不是 token，是产品自己的计量单位。换算成消息条数会让商户
    根据假设去决定充多少（一条消息并不等于一笔订单），
    而百分比是认真的。
    """

    logged_in: bool = False
    balance_known: bool = True
    unlimited_quota: bool | None = None
    active: bool = False
    remaining: int = 0
    plan: str = ""
    period_end: str = ""
    #: 额度总额、0 = 不限量（那时**不显示百分比**）。
    total_micro_usd: int = 0
    used_micro_usd: int = 0
    #: 提预中的（已接单尚未结算）。展示上算已用量。
    reserved_micro_usd: int = 0
    purchase_method: str = "manual"
    purchase_hint: str = ""
    message: str = ""

    @property
    def sufficient(self) -> bool:
        return self.logged_in and self.active and self.remaining > 0

    @property
    def unlimited(self) -> bool:
        if not self.balance_known:
            return False
        if self.unlimited_quota is not None:
            return self.unlimited_quota
        return self.total_micro_usd <= 0

    @property
    def used_percent(self) -> int:
        """已用百分比 0-100。不限量时返回 0。"""
        if self.unlimited or self.total_micro_usd <= 0:
            return 0
        return max(0, min(100, round((self.used_micro_usd + self.reserved_micro_usd)
                                     * 100 / self.total_micro_usd)))

    @property
    def remaining_percent(self) -> int:
        """剩余百分比 0-100。不限量套餐不展示百分比。"""
        if self.unlimited or self.total_micro_usd <= 0:
            return 0
        return 100 - self.used_percent

    def headline(self) -> str:
        if self.message and self.logged_in:
            return self.message
        if not self.logged_in:
            return "未登录产品账号"
        if not self.balance_known:
            return "额度未知，联网后自动刷新"
        if not self.active:
            return "套餐已到期"
        if self.remaining <= 0:
            return "分析额度已用完，重新开通或续期后即可继续整理"
        if self.unlimited:
            return "分析额度：不限"
        return f"分析额度剩余 {self.remaining_percent}%"

    def to_dict(self) -> dict[str, Any]:
        return {
            "logged_in": self.logged_in,
            "active": self.active,
            "remaining": self.remaining,
            "plan": self.plan,
            "period_end": self.period_end,
            "purchase_method": self.purchase_method,
            "purchase_hint": self.purchase_hint,
            "message": self.message,
            "headline": self.headline(),
            "total_micro_usd": self.total_micro_usd,
            "used_micro_usd": self.used_micro_usd,
            "reserved_micro_usd": self.reserved_micro_usd,
            "used_percent": self.used_percent,
            "unlimited": self.unlimited,
        }


class QuotaBlocked(Exception):
    """额度不足。读取结果保留，等恢复后继续。"""

    def __init__(self, view: QuotaView):
        super().__init__(view.headline())
        self.view = view


def _quota_view_from_payload(payload: Mapping[str, Any] | None) -> QuotaView:
    """把账户接口返回的 ``subscription`` 变成界面用的 :class:`QuotaView`。

    ``check_quota``（联网查）和 :meth:`Wizard.adopt_subscription`（搬运登录时
    已有的那份）都走这里 —— 两处各解析一遍，迟早一处认 ``plan_code``
    另一处认 ``plan``，界面上就会出现两个不一样的额度。
    """
    payload = payload or {}
    subscription = payload.get("subscription") or {}
    balance_known = bool(subscription.get("balance_known", True))
    total = int(subscription.get("quota_micro_usd", 0) or 0)
    used = int(subscription.get("used_micro_usd", 0) or 0)
    reserved = int(subscription.get("reserved_micro_usd", 0) or 0)
    # remaining 缺了就自己算（JYI 只给总量/已用/预留，不给剩余额）。
    # 有就信服务端的，不覆盖。
    remaining = subscription.get("remaining_micro_usd", None)
    try:
        remaining = int(remaining) if remaining is not None else max(0, total - used - reserved)
    except (TypeError, ValueError):
        remaining = max(0, total - used - reserved)
    return QuotaView(
        logged_in=True,
        balance_known=balance_known,
        unlimited_quota=(bool(subscription["unlimited"])
                         if "unlimited" in subscription else None),
        active=bool(subscription.get("active")),
        remaining=remaining,
        plan=str(subscription.get("plan_code", "")),
        period_end=str(subscription.get("period_end", "")),
        # 进度条要的是这三个。缺任何一个都画不出百分比 ——
        # 那是「已用 / 共多少」，不是「还剩多少次」。
        total_micro_usd=total,
        used_micro_usd=used,
        reserved_micro_usd=reserved,
        purchase_method="manual",
        purchase_hint=str(payload.get("purchase_hint") or "请联系客服开通或续期。"),
        message=str(payload.get("message") or ""),
    )


class Wizard:
    """向导状态机。

    所有外部依赖都注入，因此可以完整地无界面跑通。
    """

    def __init__(
        self,
        settings_store: SettingsStore,
        monitor: WeChatMonitor,
        conversations: ConversationRegistry,
        history_reader: HistoryReader,
        *,
        tenant: str,
        orders: Any = None,
        client: Any = None,
        pipeline: Any = None,
        on_step: Callable[[str], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_wechat: Callable[[WeChatStatus], None] | None = None,
        on_history_done: Callable[[HistoryResult], None] | None = None,
        is_product_logged_in: Callable[[], bool] | None = None,
        role_analyzer: Callable[[str, Sequence[str] | None], Mapping[str, Any]] | None = None,
    ):
        self.store = settings_store
        self.settings = settings_store.load()
        self.monitor = monitor
        self.conversations = conversations
        self.history = history_reader
        self.tenant = tenant
        #: 商户本地的订单库。**读历史、存消息、看订单、导出**都用它，
        #: 与有没有登录云端账号无关。
        self._orders = orders
        self.client = client
        self.pipeline = pipeline
        self.on_step = on_step
        self.on_log = on_log
        #: 每次检测都发，**不论结果**。界面靠它刷新。
        self.on_wechat = on_wechat
        #: 读取结束。界面据此显示结果并给出下一步，**不再自动跳页**。
        self.on_history_done: Callable[[HistoryResult], None] | None = on_history_done
        #: 产品账号是否已登录。未登录不许进入办理流程 —— 这是商户定的规则
        #: （"不登录产品不应该允许进入下一步"），门禁判据只认这一个回调。
        #: 为 None 时不限行：单测里直接构造向导，不走产品登录。
        self.is_product_logged_in = is_product_logged_in
        #: 非订单角色也通过共享 AI 任务分析，由应用装配层提供网关调用器。
        self.role_analyzer = role_analyzer
        #: 构造时先处于登录门槛；界面起来时 :meth:`first_step` 会按恢复出的登录态
        #: 进入登录页或真正的第 0 步。
        self.step = WizardStep.LOGIN
        self.history_result: HistoryResult | None = None
        #: 这一轮智能整理**跑完了没有**。跑完先停在整理屏让商户看流式结论
        #: （看完自己点「下一步」），不许自动跳到分析详情 —— 以前跑完直接
        #: 换屏，正文一闪而过。判据只此一处，``advance()`` 和界面都问它。
        self._analysis_ready = False
        self.quota = QuotaView()
        self._lock = threading.RLock()
        self._analyzed_ids: list[str] = []

    def bind_callbacks(
        self,
        on_step: Callable[[str], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_wechat: Callable[[WeChatStatus], None] | None = None,
        on_history_done: Callable[[HistoryResult], None] | None = None,
    ) -> None:
        """界面建好之后重新绑定回调。

        构造向导时界面还不存在，那时 ``app.events`` 上的回调都是空的。把它们
        **拷贝**进构造参数，等界面把 ``app.events`` 换成真正的实现之后，向导
        里存的仍然是那批 ``None`` —— 于是「重新检测」跑完了，界面收不到任何
        通知，标题纹丝不动。这里让界面绑定完再回填一次。
        """
        if on_step is not None:
            self.on_step = on_step
        if on_log is not None:
            self.on_log = on_log
        if on_wechat is not None:
            self.on_wechat = on_wechat
        if on_history_done is not None:
            self.on_history_done = on_history_done

    # ------------------------------------------------------------------ log

    def _log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:
                pass

    def _go(self, step: str) -> None:
        with self._lock:
            self.step = step
        self._log(f"进入步骤：{WizardStep.TITLES.get(step, step)}")
        if self.on_step:
            try:
                self.on_step(step)
            except Exception:
                pass

    def save(self) -> None:
        self.store.save(self.settings)

    # ---------------------------------------------------------- 登录前置门槛

    def first_step(self) -> str:
        """起点对谁都一样：登录前置页（它不占流程编号，见 :data:`WizardStep.STEP_SPECS`）。

        反转记录：这里**又**被改成"按登录状态裁落点"（已登录直接跳到「检查微信」），
        于是同一个软件每次打开停的地方都不一样 —— 取决于账号是什么时候恢复的。
        商户要的是每次打开都从头看一眼；已登录时那一屏写「已经登录」+ 额度，
        点一下「下一步」就进流程，不会白挡一道。
        """
        return WizardStep.LOGIN

    def resume(self) -> str:
        """界面起来时定起始页，并把上次选的会话绑定装回去。

        起点由 :meth:`first_step` 给（恒为登录前置页）；绑定则要装回来 ——
        「选择会话」那一屏得把上次勾过的会话显示成已勾选，不能因为"从头开始"
        就让商户重新挑一遍（``restore`` 不动当前步骤）。

        **界面只调它一处**：写两份就会出现一个界面从登录页开始、
        另一个从检查微信开始。
        """
        self.step = self.first_step()
        self.restore()
        return self.step

    def sign_out(self) -> None:
        """退出登录：门禁关上，回到登录前置页。

        **退出后不许留在流程里。** 门禁判据是"产品账号已登录"，而退出登录是
        唯一一次让它由真变假的操作 —— 向导里那条回登录页的路径长在
        :meth:`check_wechat` 里，只在微信状态**变化**时才跑；商户停在首页或
        核对页点退出时它一次都不会跑，于是页面留在原地、只有顶栏变成「未登录」
        （真实报障：退出登录没有回到登录页）。
        """
        self._go(WizardStep.LOGIN)

    def enter_wechat(self) -> bool:
        """离开登录前置页进入第 0 步。没登录就不放行，返回 False。"""
        if not self.product_gate_open():
            self._log("还没登录产品账号，先登录再进入第 0 步")
            return False
        self._go(WizardStep.WECHAT)
        return True

    @property
    def analysis_ready(self) -> bool:
        """这一轮整理跑完了没有（界面渲染和 :meth:`advance` 的门禁都问它）。"""
        return self._analysis_ready

    def advance(self) -> dict[str, Any]:
        """把向导往前推一步 —— 界面上所有「下一步」共用这一个判据。

        返回 ``{"ok": bool, "step": str, "reason": str}``。不放行时 ``reason``
        是**给商户看的一句中文**，界面直接显示，不许自己再编一套 ——
        两套判据的下场是一处放行、另一处弹"还没读到消息"（这个坑踩过）。
        """
        with self._lock:
            step = self.step
        if step == WizardStep.LOGIN:
            if not self.product_gate_open():
                return {"ok": False, "step": step,
                        "reason": "还没登录产品账号，先登录才能继续。"}
            self._go(WizardStep.WECHAT)
        elif step == WizardStep.WECHAT:
            if not getattr(self.monitor.latest, "ready", False):
                return {"ok": False, "step": step,
                        "reason": "微信还没登录或窗口没打开。先在微信里登录，"
                                  "再点「重新检测」。"}
            self._go(WizardStep.CONVERSATIONS)
        elif step == WizardStep.CONVERSATIONS:
            if not self.conversations.history_selected:
                return {"ok": False, "step": step, "reason": NO_CONVERSATION_SELECTED}
            # 「更新消息 / 读取全部」在读取那一屏：这一步只是把范围定下来。
            self._go(WizardStep.READ)
        elif step == WizardStep.READ:
            if self.history_result is None:
                return {"ok": False, "step": step,
                        "reason": "还没有读取结果，先点「更新消息」或「读取全部」。"}
            # 读到的结果就画在这一屏上：「核对」与「读取」是同一屏，
            # 不再单开一步把同一张表再画一遍。
            self._analysis_ready = False
            self._go(WizardStep.ANALYZE)
        elif step == WizardStep.ANALYZE:
            if not self._analysis_ready:
                return {"ok": False, "step": step,
                        "reason": "先点「开始 AI 整理」跑完这一轮，再看结果。"}
            self._go(WizardStep.RESULTS)
        elif step == WizardStep.RESULTS:
            # 分析详情是最后一屏：这里的「回到首页」就是结束向导。
            self.finish()
        else:
            # 确认范围（要选「更新/全部」，不是「下一步」）和智能整理
            # （按钮是「开始智能整理」）都没有下一步。
            return {"ok": False, "step": step,
                    "reason": "这一步请用页面上的按钮继续。"}
        with self._lock:
            return {"ok": True, "step": self.step, "reason": ""}

    def back(self) -> dict[str, Any]:
        """回上一步。第 0 步没有上一步，登录页也不属于向导步骤。"""
        with self._lock:
            step = self.step
        if step not in WizardStep.ORDER:
            return {"ok": False, "step": step, "reason": "这一步没有上一步。"}
        index = WizardStep.ORDER.index(step)
        if index == 0:
            return {"ok": False, "step": step, "reason": "这已经是第一步了。"}
        self._go(WizardStep.ORDER[index - 1])
        with self._lock:
            return {"ok": True, "step": self.step, "reason": ""}

    def product_gate_open(self) -> bool:
        """放行判据：产品账号已登录。判据只此一处，界面与跳转都问它。"""
        gate = self.is_product_logged_in
        if gate is None:
            return True
        try:
            return bool(gate())
        except Exception:
            return False

    # --------------------------------------------------------------- ① 微信

    def check_wechat(self) -> WeChatStatus:
        """检测一次微信，并把**新状态**通知出去。

        无论结果如何都要通知：以前只在"已就绪"时才通知，于是关掉微信再点
        「重新检测」，状态变了界面却一动不动。

        已就绪才往下走；**从就绪掉回未就绪**（微信被关掉）则退回第 0 步 ——
        读不到群消息时继续停在后面几步，只会让商户对着一个永远不动的界面
        反复点。
        """
        status = self.monitor.check_once()
        if self.on_wechat:
            try:
                self.on_wechat(status)
            except Exception:
                pass
        if status.ready:
            if self.settings.account_id and self.settings.account_id != status.account_id and status.account_id:
                # 账号切换：旧绑定一律作废，重新走选择。
                self._log("检测到微信账号已切换，重新绑定会话")
                self.settings.reset_for_new_account()
                self.conversations = ConversationRegistry()
            self.settings.account_id = status.account_id
            self._note_wechat_version()
            self.save()
            # 只有**还停在登录前置页或第 0 步、且门禁放行**时才推进。监视器每几秒就检测
            # 一次；以前不判断当前在哪一步，于是商户正在读历史（或者已经走到
            # 首页了）时，一次后台检测就能把他弹回选择页 —— 表现为"读完没有
            # 下一步"、进度条不动。状态好着的时候没有理由打断正在进行的流程。
            # 门禁规则是硬的（"不登录不许进入向导"）：微信就绪但产品账号未登录
            # 时回到登录前置页。
            if self.step == WizardStep.LOGIN and self.product_gate_open():
                # 登录刚完成：进第 0 步，再接着往下判 —— 微信早就
                # 就绪的话没有理由让商户在两屏之间多点一次。
                self._go(WizardStep.WECHAT)
            if self.step == WizardStep.WECHAT:
                if self.product_gate_open():
                    self._go(WizardStep.CONVERSATIONS)
                else:
                    # 门禁是硬的：登录过期时回登录前置页，别停在流程里干等。
                    self._log("微信已就绪，还差产品账号登录，回到登录页")
                    self._go(WizardStep.LOGIN)
        elif self.step not in (WizardStep.WECHAT, WizardStep.LOGIN):
            self._log(f"微信状态变为「{status.state}」，回到第 0 步")
            self._go(WizardStep.WECHAT)
        return status

    def _note_wechat_version(self) -> None:
        """记下这次见到的微信版本。变了就吱一声 —— 微信一升级，
        界面选择器、读取逻辑都可能对不上，商户看到"读不到"时至少知道
        先重走一次检测，而不是以为软件坏了。读版本失败不挡路。"""
        try:
            from .Config import GlobalConfig
            current = str(GlobalConfig.Version or "")
        except Exception:
            return
        if not current:
            return
        previous = str(getattr(self.settings, "wechat_version", "") or "")
        if previous and previous != current:
            self._log(f"微信版本从 {previous} 变为 {current}，已重走检测")
        self.settings.wechat_version = current

    def start_wechat(self) -> bool:
        """用户点了「打开微信」。这是唯一允许主动启动微信的入口。"""
        ok = self.monitor.launch()
        if ok:
            self._log("已请求启动微信，等待登录")
        return ok

    def start_monitoring(self) -> None:
        """后台等商户扫码；登录后自动进入下一步。不叠加线程。"""
        self.monitor.start()

    def stop_monitoring(self) -> None:
        self.monitor.stop()

    # ------------------------------------------------------------- ② 会话

    def refresh_conversations(self, kind: str = "") -> list[Conversation]:
        source = getattr(self, "conversation_source", None)
        if source is None:
            return list(self.conversations.available)
        self.conversations.refresh(source.list_conversations(kind))
        return list(self.conversations.available)

    def choose_history_conversations(self, selected: Sequence[Conversation]) -> None:
        self.conversations.set_history(selected)
        self.settings.history_conversations = [item.identity for item in selected]
        self.save()

    def choose_live_conversation(self, conversation: Conversation) -> None:
        """第一版实时接单只允许一个会话。结构上限制，不靠提示。"""
        self.conversations.set_live(conversation)
        self.settings.live_conversation = conversation.identity
        self.save()
        # 选完会话往下走：范围确认已经并进选择会话那一屏，下一屏就是读取。
        self._go(WizardStep.READ)

    # ------------------------------------------------------------- ③ 历史

    def read_history(
        self,
        *,
        range_kind: str = "",
        custom: TimeWindow | None = None,
        max_messages: int = 0,
        on_progress: Callable[[HistoryProgress], None] | None = None,
        resume: bool = True,
        mode: str = ReadMode.RECENT,
        sender_source: str = "",
    ) -> HistoryResult:
        if mode not in ReadMode.ALL_KINDS:
            raise ValueError(f"unknown read mode: {mode}")
        kind = range_kind or self.settings.history_range
        # The current merchant UI offers update/all modes, not a custom date
        # range. Older settings can still contain ``custom`` from a previous
        # UI, so normalize that stale value instead of failing before reading.
        if kind == HistoryRange.CUSTOM and custom is None:
            kind = HistoryRange.LAST_WEEK
        if mode in (ReadMode.ALL, ReadMode.UPDATE):
            # These modes are defined by the cursor / full traversal. Applying
            # a saved date filter here could silently omit older messages.
            window = TimeWindow(label=mode)
        else:
            window = HistoryRange.window(kind, custom)
        limit = int(max_messages or self.settings.history_max_messages or DEFAULT_MAX_MESSAGES)
        self.settings.history_range = kind
        self.settings.history_max_messages = limit
        self.save()
        # 执行是独立的一步：进确认页只是看看范围，真正动手读才进执行页。
        self._go(WizardStep.READ)

        importer = self._make_importer()
        result = self.history.read(
            self.conversations.history_selected,
            window,
            merchant_id=self.tenant,
            max_messages=limit,
            on_progress=on_progress,
            resume=resume,
            importer=importer,
            mode=mode,
            sender_source=sender_source,
        )
        self.history_result = result
        self._log(f"历史读取完成：{result.headline()}")
        for gap in result.gaps:
            self._log(f"注意：{gap}")
        # **不自动跳页**。商户需要先看到"读到了什么、覆盖哪段时间、有没有缺口"，
        # 再自己点下一步。以前这里 `_go(QUOTA)` 直接跳走，这些信息一闪而过；
        # 读取失败时界面还会莫名其妙退回上一步且一言不发。
        if self.on_history_done is not None:
            try:
                self.on_history_done(result)
            except Exception:
                pass
        return result

    def cancel_history(self) -> None:
        self.history.cancel()
        self._log("已请求取消读取")

    def _make_importer(self) -> Callable[[Conversation, Sequence[Mapping[str, Any]], TimeWindow], dict[str, Any]]:
        """把读到的消息写进现有订单库，并绑定已知客户别名。"""

        def import_messages(conversation: Conversation, messages: Sequence[Mapping[str, Any]], window: TimeWindow) -> dict[str, Any]:
            source = f"history:{conversation.identity}"
            return self.orders().add_import(
                self.tenant, conversation.name, list(messages), source, "wechat_history", ""
            )

        return import_messages

    def orders(self) -> Any:
        """商户本地订单库。

        以前这里只认 ``pipeline.orders``，而 ``pipeline`` 只有在**登录云端账号
        之后**才会被建起来。结果：商户第一次用，还没登录，去点「开始读取」，
        必然抛 ``wizard needs an order store to import history`` —— 读历史
        100% 失败，消息一条都存不进来。

        本地读历史、存消息、看订单、导出都**不该**要求云端登录，也不该扣额度。
        所以订单库由宿主直接交给向导，pipeline 只是备选。
        """
        if self._orders is not None:
            return self._orders
        if self.pipeline is not None and getattr(self.pipeline, "orders", None) is not None:
            return self.pipeline.orders
        raise RuntimeError("wizard has no local order store")

    # ------------------------------------------------------------- ④ 额度

    def check_quota(self) -> QuotaView:
        """查询额度。未登录时引导网页登录，不显示任何 Key。"""
        if self.client is None:
            self.quota = QuotaView(logged_in=False, message="尚未登录产品账号")
            return self.quota
        try:
            payload = self.client.account()
        except Exception as exc:
            if getattr(self.client, "session", None) is not None:
                previous = self.quota
                self.quota = QuotaView(
                    logged_in=True,
                    balance_known=previous.balance_known,
                    unlimited_quota=previous.unlimited_quota,
                    remaining=previous.remaining,
                    plan=previous.plan,
                    period_end=previous.period_end,
                    total_micro_usd=previous.total_micro_usd,
                    used_micro_usd=previous.used_micro_usd,
                    reserved_micro_usd=previous.reserved_micro_usd,
                    purchase_hint=previous.purchase_hint,
                    message=(previous.message if previous.balance_known
                             else "额度未知，联网后自动刷新"),
                )
            else:
                self.quota = QuotaView(logged_in=False, message=_network_or_login(exc))
            return self.quota
        # 额度不足**不跳页**：读取结果与额度留在同一屏里，跳走的话
        # "实际读到多少条、覆盖哪段时间、有没有缺口"商户就看不到了。
        self.quota = _quota_view_from_payload(payload)
        return self.quota

    def adopt_subscription(self, subscription: Mapping[str, Any] | None) -> QuotaView:
        """把**登录时已经抓到**的那份套餐/额度装进界面状态。不联网。

        登录（``client_login._build_session``）和启动恢复（``restore_session``）
        都已经从账户接口拿过一次 ``subscription`` 并落盘，但以前没人把它交给
        向导 —— 于是 ``quota`` 一直是默认值，界面上表现为「已登录」旁边写着
        「未登录产品账号」，还顺带宣称不限量（默认 total=0 时 ``unlimited``
        属性为真）。这里只做搬运。

        空字典/缺字段就**保持原样**：把已有的一份真数字擦成"未知"，
        比不显示更糟。
        """
        if not isinstance(subscription, Mapping) or not subscription:
            return self.quota
        self.quota = _quota_view_from_payload({"subscription": dict(subscription)})
        return self.quota

    def require_quota(self) -> None:
        """额度不足时抛出，但**保留已读取的数据**。

        JYI 网关没有额度接口（按账号计量，不在客户端结算）——
        门禁无从判断，不过门，有问题服务端会直接拒单。
        """
        if self._is_jyi():
            return
        view = self.quota.sufficient and self.quota or self.check_quota()
        if not view.sufficient:
            raise QuotaBlocked(view)

    def _is_jyi(self) -> bool:
        """当前是不是 JYI 通道。鸭子类型，不硬依赖导入（防循环）。"""
        try:
            from .jyi_client import JYIClient

            return isinstance(getattr(self, "client", None), JYIClient)
        except Exception:
            return False

    # ------------------------------------------------------------ ⑤ 整理

    def analyze(self, message_ids: Sequence[str] | None = None,
                role: str = "order") -> dict[str, Any]:
        """对读取到的消息跑一遍智能整理。

        ``role`` 是整理角色（每次整理时选，见 :mod:`wechat_ai_assistant.roles`）：
        订单助手走原来的订单链路（模型解析 → 规则校验 → 订单草稿，
        要过额度门）；其它角色通过共享 AI 分析服务生成角色报告，不创建订单。

        额度不足时抛 :class:`QuotaBlocked`，但**不重复读取、不重复扣费**：
        消息还在本地库，恢复额度后重新调用即可。

        **不盲目地推算扣费。** 之前这里按"每个任务算 1 次"累加，既把免费任务
        算成了收费，也和运营配的每任务扣费数对不上。计费只有网关说了算；
        这里只统计"提交了多少个任务"，金额一律读服务端返回的值。
        """
        from .roles import ROLE_ORDER, get_role

        role_def = get_role(role)
        if role_def.id != ROLE_ORDER:
            return self._analyze_with_role(role_def, message_ids)
        if not self.product_gate_open():
            # 和角色那条路同一句话：没登录时说"未登录产品账号"这种状态词，
            # 商户读不出下一步该干什么。
            raise RuntimeError("还没登录产品账号，先登录再整理。")
        self.require_quota()
        self._go(WizardStep.ANALYZE)
        self._analysis_ready = False
        ids = list(message_ids) if message_ids is not None else self._pending_message_ids()
        outcomes: list[dict[str, Any]] = []
        submitted = 0
        billing: list[dict[str, Any]] = []
        for message_id in ids:
            outcome = self.pipeline.process(message_id, source="history", on_progress=None)
            outcomes.append(outcome.to_dict())
            if outcome.trace.job_id:
                submitted += 1
                fact = self.pipeline.billing_fact(outcome.trace.job_id)
                if fact:
                    billing.append(fact)
        self._analyzed_ids = ids
        # 汇总口径来自服务端：网关说扣了多少就是多少。
        charged_micro_usd = sum(int(fact.get("charged_micro_usd", 0)) for fact in billing)
        jyi = self._is_jyi()
        summary = {
            "processed": len(ids),
            "with_result": sum(1 for item in outcomes if item.get("order_id")),
            "needs_review": sum(1 for item in outcomes if item.get("needs_review")),
            "ignored": sum(1 for item in outcomes if item.get("outcome") == "ignored"),
            "already_analyzed": self.already_analyzed_count(),
            # submitted 是任务数，charged_micro_usd 是服务端判定的扣费量 —— 两者
            # 不一定相等，不能互相替代。
            "submitted_jobs": submitted,
            "charged_micro_usd": charged_micro_usd,
            "billing": billing,
            "provider": "jyi" if jyi else "product",
            # JYI 按账号计量，不在本地结算：charged 恒 0，别让界面
            # 把它读成"这次没花钱"。
            "billing_note": ("用量按 JYI 账号计量，明细去 JYI 网站看。"
                             if jyi else
                             "扣费金额由云端账号按服务端策略计算，此处只做汇总。"),
            "traces": [item["trace"] for item in outcomes],
            "details": outcomes,
        }
        self._log(
            f"整理完成：处理 {summary['processed']} 条，生成订单 {summary['with_result']} 条，"
            f"需要核对 {summary['needs_review']} 条，本次实际扣费 {charged_micro_usd} 次"
        )
        # **不自动跳屏**：跑完先停在整理屏，让商户看完流式结论自己点
        # 「下一步」进分析详情。以前跑完直接换屏，正文一闪而过。
        self._analysis_ready = True
        return summary

    def _analyze_with_role(self, role_def: Any, message_ids: Sequence[str] | None) -> dict[str, Any]:
        """用角色提示词调用 AI，保存报告而不生成订单。

        角色整理**同样在花账号的钱**，所以先过额度门 —— 判据和订单链路共用
        :meth:`require_quota` 这一处，不许有一条不过门的旁路。JYI 通道按账号
        计量、不在本地结算，那边它本就不拦（有问题服务端直接拒单）。
        """
        if self.role_analyzer is None:
            raise RuntimeError("AI 整理服务还没接上，请重启软件后再试一次。")
        if not self.product_gate_open():
            # 没登录时别说"额度不足"，更别说"服务没接上"：那两句都指向错误的下一步。
            raise RuntimeError("还没登录产品账号，先登录再整理。")
        self.require_quota()
        self._go(WizardStep.ANALYZE)
        self._analysis_ready = False
        outcome = dict(self.role_analyzer(role_def.id, message_ids) or {})
        if outcome.get("error"):
            # "没有新消息"不是失败：这一批该角色已经分析完了（水位线到头）。
            # 以前这里直接报错，于是**重启之后**商户既看不到上次那份报告
            # （它只存在内存里），重跑又被判失败 —— 钱花了，报告拿不回来。
            stored = self._stored_role_report(role_def.id)
            if str(outcome.get("error")) == "没读到消息" and stored:
                self._log(f"本次没有新消息，「{role_def.name}」显示上次生成的报告")
                summary = self._role_summary(role_def, str(stored.get("summary") or ""),
                                             int(stored.get("total") or 0),
                                             reused_from_store=True)
                self._analysis_ready = True
                return summary
            raise RuntimeError(str(outcome["error"]))
        if outcome.get("cancelled"):
            raise RuntimeError("AI 整理已取消。")
        report_text = str(outcome.get("summary") or "").strip()
        if not report_text:
            raise RuntimeError("本次没有可供 AI 分析的新消息。")
        summary = self._role_summary(role_def, report_text,
                                     int(outcome.get("message_count") or 0),
                                     outcome=outcome)
        self._log(f"AI 整理完成（{role_def.name}）：处理 {summary['processed']} 条消息")
        # 停在整理屏给商户看结论，点「下一步」再去分析详情。
        self._analysis_ready = True
        return summary

    def _stored_role_report(self, role_id: str) -> dict[str, Any] | None:
        """这个角色上次落盘的报告。读不到就当没有（表还没建、库是旧的）。"""
        from . import summary_task

        try:
            return summary_task.load_role_report(self.orders(), self.tenant, role_id)
        except Exception:
            LOGGER.debug("read stored role report failed", exc_info=True)
            return None

    def _role_summary(self, role_def: Any, report_text: str, processed: int, *,
                      outcome: Mapping[str, Any] | None = None,
                      reused_from_store: bool = False) -> dict[str, Any]:
        """角色报告的汇总。**报告正文与条数只从这里出**，落盘复用和现跑共用一条路。"""
        result = dict(outcome or {})
        partial_error = str(result.get("partial_error") or "")
        role_report = {
            "role_id": role_def.id,
            "role_name": role_def.name,
            "total": processed,
            "summary": report_text,
            "sections": list(role_def.result_sections),
            "group_reports": list(result.get("group_reports") or []),
            "partial_error": partial_error,
        }
        return {
            "processed": processed,
            "with_result": 0,
            "needs_review": 0,
            "ignored": 0,
            # 订单链路那个"已整理过"概念对报告角色不成立（它只认订单解析过的
            # 消息）。以前这里填订单的数字，界面就写"已整理过、本次没重复提交 N 条"
            # —— 对活动/通用整理是假话。None = 不适用，页面不显示这一句。
            "already_analyzed": None,
            # 任务数/扣费同样只由账号侧结算：不知道就写 None，不写 0 假装没花钱。
            "submitted_jobs": None,
            "charged_micro_usd": None,
            "billing": [],
            "billing_note": "本轮调用 AI，实际用量按当前账号规则结算。",
            "traces": [],
            "role_id": role_def.id,
            "role_name": role_def.name,
            "uses_model": True,
            "output_kind": role_def.output_kind,
            "sections": list(role_def.result_sections),
            "cost": result.get("cost"),
            "is_incremental": bool(result.get("is_incremental", True)),
            "reused_from_store": bool(reused_from_store),
            "partial_error": partial_error,
            "role_report": role_report,
        }

    def _pending_message_ids(self) -> list[str]:
        """待处理的消息，已按合并关系过滤，并**排除已分析过的**。

        短消息合并开启时，被并入其它消息的原始消息**不会**出现在这里 ——
        否则它们会被再解析一次、再计费一次、再记一次单。

        已分析过的消息同样排除：再次提交会真的产生一次模型调用和一次扣费，
        而结果只会因为"已处理过"被判成 duplicate。商户点第二次"开始智能
        整理"，等于花钱买一个"已处理过"。换模型重新分析要走独立的差异流程，
        在它做好之前这里一律不再重复提交。
        """
        batch_ids = self._current_read_message_ids()
        if batch_ids is not None:
            if not batch_ids:
                return []
            rows = [{"id": message_id} for message_id in batch_ids]
        else:
            # 兼容没有读取结果的旧调用方；商户端正常流程始终限定在本次导入批次。
            rows = self.orders().db.execute(
                "SELECT id FROM messages WHERE merchant_id=? ORDER BY COALESCE(sent_at_epoch, 0), id",
                (self.tenant,),
            ).fetchall()
        already = self.orders().analyzed_message_ids(self.tenant)
        merger = self._merger()
        result: list[str] = []
        for row in rows:
            message_id = row["id"]
            if message_id in already:
                continue
            if merger is None or not merger.is_merged(message_id) or merger.representative_of(message_id) == message_id:
                result.append(message_id)
        return result

    def _current_read_message_ids(self) -> list[str] | None:
        """读取结果存在时返回本次导入的消息；无读取结果时返回 None。"""
        read_result = self.history_result
        if read_result is None:
            return None
        import_ids = [str(value) for value in getattr(read_result, "import_ids", ()) if value]
        if not import_ids:
            return []
        marks = ",".join("?" for _ in import_ids)
        rows = self.orders().db.execute(
            "SELECT id FROM messages WHERE merchant_id=? AND import_id IN (" + marks + ")"
            " ORDER BY COALESCE(sent_at_epoch, 0), id",
            [self.tenant, *import_ids],
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def already_analyzed_count(self) -> int:
        """本次读取批次里已整理的消息数；没有读取批次时返回历史总数。"""
        try:
            already = self.orders().analyzed_message_ids(self.tenant)
            current = self._current_read_message_ids()
            return len(already) if current is None else sum(mid in already for mid in current)
        except Exception:
            return 0

    def _merger(self):
        merger = getattr(self, "merger", None)
        if merger is None and self.pipeline is not None:
            merger = getattr(self.pipeline, "merger", None)
        return merger

    def merge_pending(self, source: str = "live") -> list[Any]:
        """把待结算的短消息组合并，返回结算结果。

        历史导入默认**不**合并：历史消息本来就带着原始时间，
        逐条处理才能还原当时的因果。合并只用于实时接单。
        """
        merger = self._merger()
        if merger is None:
            return []
        return merger.flush()

    # ------------------------------------------------------------- ⑥ 完成

    def finish(self) -> None:
        """结束向导：记下「走完了」，回到第一步重新开始。

        「重新开始」就是回到第一步，不设仪表盘式的首页 —— 跑完一轮，
        商户要么在结果屏看产物，要么从头再来。
        """
        self.settings.wizard_complete = True
        self.save()
        self._go(WizardStep.WECHAT)

    def has_finished(self) -> bool:
        """向导走完过没有。界面据此决定读取那屏要不要摆「重新开始」。

        第一次用的商户还在他的第一轮里，用「上一步」退就够了；走完过的
        商户读了新消息后改主意，要能直接回到第一步重来，不用一路点回去。
        """
        return bool(self.settings.wizard_complete)

    def restore(self) -> bool:
        """把设置里存的会话绑定装回注册表；返回有没有装回来。**不动当前步骤。**

        启动从登录前置页开始（见 :meth:`first_step`），但这个绑定还得装：
        第 1 步要把上次勾过的会话显示成已勾选，否则"从头看一眼"就变成了
        "重新挑一遍群"。

        **必须在会话列表刷新之后调**（``web_server.create_server`` 就是这个顺序）：
        绑定按 ``identity`` 还原，列表还是空的时候一个都对不上。

        一个都对不上就**不算恢复成功**：换了微信账号、群被删了都会这样，
        那时该老老实实重选，而不是假装还记得。
        """
        if not (self.settings.history_conversations or self.settings.live_conversation):
            return False
        restored = ConversationRegistry.from_dict(
            {
                "history": self.settings.history_conversations,
                "live": self.settings.live_conversation,
            },
            self.conversations.available,
        )
        if not restored.history_selected and restored.live_selected is None:
            return False
        self.conversations = restored
        return True


def _network_or_login(exc: Exception) -> str:
    """区分网络断开与登录过期。

    把所有异常都显示成"网络暂不可用"会让商户一直等一个不会自己好的网络。
    """
    code = str(getattr(exc, "code", "") or "")
    if code in ("network_unavailable", "backend_unavailable"):
        return "网络暂不可用，读取的消息已保留"
    if code == "unauthenticated":
        return "登录已过期，请重新登录"
    if code == "quota_exhausted":
        return "分析额度已用完"
    if code == "subscription_inactive":
        return "套餐已到期"
    from .client_login import merchant_safe_error
    return "查询额度失败：" + merchant_safe_error(
        code, getattr(exc, "merchant_message", "") or str(exc),
        fallback="请稍后重试",
    )


__all__ = [
    "NO_CONVERSATION_SELECTED",
    "QuotaBlocked",
    "QuotaView",
    "Wizard",
    "WizardStep",
]
