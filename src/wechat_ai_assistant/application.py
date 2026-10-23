"""商户应用的编排层：界面只调用这里。

把「应用」和「界面」分开，是因为界面在自动化测试里既跑不起来也不该跑。
:class:`MerchantApplication` 承载全部业务流转，界面（``web_server`` 的接口 +
``web_wizard_page`` 的页面）只负责把状态画出来。

**这保证了一件事**：界面上点「开始智能整理」，走的就是测试里验证过的那条
真实路径，不是另一条只在测试里存在的捷径。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .conversations import (
    Conversation,
    ConversationKind,
    ConversationRegistry,
    ConversationSource,
    RealConversationSource,
)
from .dashboard import ACTIONABLE_STATUSES, DashboardBuilder, HomeView, OrderRow
from .history_read import (
    DEFAULT_MAX_MESSAGES,
    HistoryCursor,
    HistoryProgress,
    HistoryReader,
    HistoryRange,
    HistoryResult,
    ReadMode,
    TimeWindow,
)
from .merging import MergeSettings, MessageMerger
from .reliability import UiOperationQueue
from .runmode import AutoOrderSettings, MerchantSettings, RunMode, SettingsStore
from .service import ApplyOutcome, OrderPipeline
from .wechat_state import (
    RealWeChatLauncher,
    RealWeChatProbe,
    WeChatMonitor,
    WeChatProbe,
    WeChatState,
    WeChatStatus,
)
from .wizard import QuotaView, Wizard, WizardStep

LOGGER = logging.getLogger(__name__)

#: 后台轮询间隔。停止后不会残留线程。
POLL_INTERVAL_SECONDS = 3.0

#: 模型列表刷新失败时给商户看的话。**不带异常原文**。
#:
#: 失败原因分三类，商户能据此决定做什么：网络、登录过期、服务端暂时不可用。
#: 具体异常只写日志（``load_models`` 里的 warning），界面上一律不放类名、
#: 方法名或堆栈 —— 商户看不懂，只会觉得软件坏了。
MODEL_STALE_NOTICE = (
    "暂时连不上服务，下面显示的是最近一次获取的模型列表，可能不是最新。"
    "已读取的消息都保留着，可以稍后点「刷新模型列表」重试。"
)
MODEL_UNAVAILABLE_NOTICE = (
    "暂时无法获取分析模型列表，当前显示已保存的模型选择。"
    "请稍后点「刷新」重试；这次失败不会清除原有选择。"
)


@dataclass
class AppEvents:
    """界面需要刷新时收到通知。全部是可调用项，便于测试替换。"""

    on_home: Callable[[HomeView], None] | None = None
    on_step: Callable[[str], None] | None = None
    on_log: Callable[[str], None] | None = None
    on_history_progress: Callable[[HistoryProgress], None] | None = None
    #: 读取结束。界面据此显示结果并给出「下一步」按钮 —— 不再自动跳页。
    on_history_done: Callable[[Any], None] | None = None
    #: 短消息被合并成一条时触发。商户需要看见这个合并。
    on_merged: Callable[[dict[str, Any]], None] | None = None
    on_conversations: Callable[[list[Conversation]], None] | None = None
    on_error: Callable[[str], None] | None = None
    #: 微信状态**每检测一次**就发一次，不论是否满足条件。
    #:
    #: 以前只在"已就绪"时才发事件，于是关掉微信再点「重新检测」时，
    #: 状态其实变了，界面却一动不动 —— 商户看到的还是"已登录"。
    on_wechat: Callable[[Any], None] | None = None
    #: 产品账号登录刚成功。界面靠它刷新登录框与门禁 —— 以前没人通知，
    #: 登录框一直写着"未登录"，门禁也不放行，商户以为登录没成功。
    #: 注意名字不能叫 on_login：界面上 _on_login 是"发起登录"按钮，
    #: 事件泵按 _on_<kind> 找处理器，重名会点成死循环。
    on_logged_in: Callable[[], None] | None = None
    #: AI 报告的增量文本。Web 界面据此在生成过程中更新实时预览。
    on_analysis_stream: Callable[[str], None] | None = None
    #: 模型的**推理过程**增量（和报告正文分开）。
    #:
    #: 正文之前模型要"想"，实测这段最长 48 秒。以前这段时间界面只有一句
    #: 「正在推理」，商户看不出任何东西在动 —— 推理正文流出来，才叫"看得见在分析"。
    #: 它**不是**分析结论：界面必须标清楚，别让商户把思考过程当报告。
    on_analysis_reasoning: Callable[[str], None] | None = None
    #: AI 分析的**进度**（正在分析第几个会话、这个会话叫什么）。
    #: 模型出第一个字之前可能等几十秒（实测最长 48 秒），这段时间界面要是
    #: 只有一个空框，商户只会以为卡死了 —— 有一句"正在分析第 2/3 个会话"
    #: 才知道软件在动。
    on_analysis_progress: Callable[[str], None] | None = None
    #: 一句**不打断人**的告知（显示在顶部横幅，可以随时被下一条覆盖）。
    #: 用来讲清"软件此刻在动什么"，比如首次读取要扫描微信数据库并提取密钥。
    #: 传空串表示撤掉。
    on_notice: Callable[[str], None] | None = None


class MerchantApplication:
    """把向导、闭环服务、回执和设置串起来的编排层。"""

    def __init__(
        self,
        *,
        data_dir: Any,
        orders: Any,
        tenant: str,
        probe: WeChatProbe | None = None,
        launcher: Any = None,
        conversation_source: ConversationSource | None = None,
        history_reader_fn: Callable[..., Any] | None = None,
        group_members_fn: Callable[[Any], Sequence[str]] | None = None,
        sender_source: str = "",
        gateway_url: str = "",
        client_factory: Callable[[Any], Any] | None = None,
        events: AppEvents | None = None,
        simulated: bool = False,
        merge_settings: Mapping[str, Any] | None = None,
    ):
        self.data_dir = data_dir
        self.orders = orders
        self.tenant = tenant
        self.events = events or AppEvents()
        self.simulated = simulated
        self.gateway_url = gateway_url

        # 所有后台任务共用一条串行队列：解密拷贝互不打断。
        # （以前这里是键鼠串行队列；界面链移除后只剩数据任务。）
        self.ui_queue = UiOperationQueue(thread_name="merchant-ui")
        self.ui_lock = self.ui_queue

        self.settings_store = SettingsStore(data_dir / "settings.json")
        self.settings: MerchantSettings = self.settings_store.load()
        self.auto: AutoOrderSettings = self.settings.auto

        self.cursor = HistoryCursor(str(data_dir / "history.db"))
        self.merger = MessageMerger(
            str(data_dir / "merge.db"), MergeSettings(**merge_settings) if merge_settings else None
        )

        self.probe = probe or RealWeChatProbe()
        self.launcher = launcher or RealWeChatLauncher()
        self.monitor = WeChatMonitor(
            self.probe, self.launcher, on_change=self._on_wechat_change,
            # 探测也要动微信窗口。以前监视器和读取各干各的，两个线程可能同时
            # 驱动同一个窗口。共用这把锁去掉的是一个本来就存在的竞争。
            #
            # 注意：这不是"读到 0 条"的修复。对照实验（real_race_repro.py）
            # 没能复现空读 —— 原因仍未查明。
            ui_lock=self.ui_lock,
        )

        self.conversations = ConversationRegistry()
        #: wxid 显示名备注（内存解不出的群名/人名，手备注一次一直有效）。
        self.wxid_aliases: dict = self.load_aliases()
        self.conversation_source = conversation_source or RealConversationSource(ui_lock=self.ui_lock)
        # 解密库：有钥匙就走真名真人数，没钥匙就列不出会话（界面照实说）。
        self._hook_wcdb(self.conversation_source)
        #: 上一次列举走的是哪条路（`wcdb` / 空串=没列出来）。界面如实显示。
        self.conversation_source_label = ""
        #: 没钥匙时点"读取"自动提一次。每个进程只试一次，重试走界面按钮。
        self._wcdb_ensure_attempted = False
        #: 界面链已移除：没有持续监听，只有定时轮询读解密库。
        #: 实时性要求高的商户把轮询间隔调小，或点「读取」手动补读。

        # 注入了读取器 = 消息由调用方供给（测试替身、外部集成），那就是 members
        # 语义：名单跟着一起递下去。生产路径不注入读取器，走解密库（wcdb），
        # 所以这里只是把"谁在供消息"这件事说清楚，不是回落。
        effective_source = sender_source or (
            "members" if history_reader_fn is not None else "wcdb")

        self.history = HistoryReader(
            history_reader_fn or self._default_history_reader,
            ui_lock=self.ui_lock,
            cursor=self.cursor,
            # ? history_reader_fn ?????:??????????(????
            # 2 ???),????????????,????????
            group_members=group_members_fn or self._default_group_members,
            # 历史消息来源。**空 = 自动**：注入了读取器就按 members 走
            # （消息由调用方供给，名单是它的伴生参数）；生产不注入读取器，
            # 走解密库直读（wcdb，先提取密钥）。退役旧名 localdb/avatar
            # 也认，按 wcdb 走。显式传值一律照用（测试要分别钉死几条路）。
            sender_source=effective_source,
            # 解密库直读：有钥匙才有。只给真实来源接：模拟/测试来源用自己的
            # 替身，不碰真微信。
            wcdb_reader_fn=(self._wcdb_reader
                            if isinstance(self.conversation_source, RealConversationSource)
                            else None),
        )

        self.client = None
        self.session = None
        self.parse_service = None
        self.pipeline: OrderPipeline | None = None
        self.dashboard = DashboardBuilder(orders, tenant)

        self.wizard = Wizard(
            self.settings_store,
            self.monitor,
            self.conversations,
            self.history,
            tenant=tenant,
            orders=orders,
            is_product_logged_in=self.product_logged_in,
            role_analyzer=self._run_role_analysis,
            on_step=self.events.on_step,
            on_log=self.events.on_log,
            on_wechat=self.events.on_wechat,
        )
        self.wizard.conversation_source = self.conversation_source
        # wizard 建好之后才能恢复：恢复要拿当前绑定的账号做比对。
        self._restore_conversation_cache()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self._background: list[threading.Thread] = []
        self._last_home: HomeView | None = None
        #: 客户可见的模型目录。首次使用时拉取，失败保留上一次。
        self.model_catalog: dict[str, Any] = _empty_model_view()

    # ------------------------------------------------------------- 生命周期

    def attach(self, session: Any, client: Any, parse_service: Any, backend_is_model: bool = False) -> None:
        """登录成功后接上云端能力。

        ``backend_is_model`` 决定结果是否被标注为"真实模型"。默认 False——
        未接真实后端时，界面上的订单必须被明确标为演示，不能无提示地变成
        正式经营订单。
        """
        self.session = session
        self.client = client
        self.parse_service = parse_service
        if parse_service is not None and hasattr(parse_service, "model_alias"):
            parse_service.model_alias = str(self.settings.model_alias or "")
        self.backend_is_model = bool(backend_is_model)
        self.wizard.client = client
        self.wizard.pipeline = self._pipeline()
        self.pipeline = self.wizard.pipeline
        # 登录/启动恢复时已经从账户接口抓过一份套餐与额度（登录响应里带着，
        # 也随凭证落了盘），搬到向导里 —— 界面（顶栏、首页、「智能整理」那一屏）
        # 读的就是它，不用再联网查一次。取不到就保持原样。
        self.wizard.adopt_subscription(getattr(session, "subscription", None))
        # 结果可能来自真实模型，也可能来自演示后端。标记必须准确。
        try:
            self.parse_service.model_executed = self.backend_is_model
        except AttributeError:
            # 不可变的桩对象：不强设，OrderPipeline 会按 False 处理。
            pass
        # 登录页是向导外的前置门槛。**刚在浏览器里登录成功**就直接进流程第一屏，
        # 不再要求商户在一个独立的「登录步骤」上再点一次下一步。
        # 注意这不决定启动落点：启动时走的是 attach → ``resume()``（见
        # ``web_server.create_server``），落点由 ``resume()`` 定，恢复出的登录态
        # 照样停在登录页（那一屏写「已经登录」+「下一步」）。
        if self.wizard.step == WizardStep.LOGIN:
            self.wizard.advance()
        if self.events.on_logged_in:
            try:
                self.events.on_logged_in()
            except Exception:
                LOGGER.debug("logged_in handler skipped", exc_info=True)

    def _on_merged(self, message_id: str, event_ids: tuple) -> None:
        """短消息被合并了。告诉界面，让商户能核对。"""
        self._last_merged = {"message_id": message_id, "event_ids": tuple(event_ids),
                             "count": len(event_ids)}
        if self.events.on_merged:
            try:
                self.events.on_merged(self._last_merged)
            except Exception:
                LOGGER.debug("merged handler skipped", exc_info=True)

    def detach(self) -> None:
        """退出登录。

        **必须真的能退出来。** 之前 ``client_login.logout()`` 存在却没人调用，
        商户登录一次之后就再也换不了账号 —— 收银电脑上换个人就登不进去，
        本地已读到的数据和订单也不该跟着别人走。

        这里做四件事：通知服务端作废、删掉本机凭证、断开云端能力、刷新界面。
        本地数据**不删** —— 已读到的消息是商户自己经营出来的。

        本机凭证必须删：只清内存不清文件，下次启动 ``restore_session`` 会
        把这次登出又"恢复"回来 —— 商户明明点了退出，重开发现还登着。
        """
        client = self.client
        if client is not None:
            try:
                client.logout()
            except Exception:
                LOGGER.warning("server logout failed; continuing with local sign-out",
                               exc_info=True)
        # 服务端成没成功，本地凭证都删：断网时退出也必须彻底，
        # 否则下次启动照样自动登回去。
        store = getattr(self, "credentials", None)
        if store is not None:
            try:
                from .client_login import clear_session
                clear_session(store)
            except Exception:
                LOGGER.warning("clearing local credentials failed", exc_info=True)
        self.session = None
        #: 最近一次短消息合并的事实（哪几条合成了哪一条）。
        self._last_merged: dict[str, Any] | None = None
        self.client = None
        self.parse_service = None
        self.backend_is_model = False
        self.wizard.client = None
        self.wizard.pipeline = None
        self.pipeline = None
        # 退出登录要**回到登录前置页**：门禁已经关上，界面不能停在原来的流程里
        # （停在首页/核对页时，顶栏写着「未登录」，正文还是商户的经营数据）。
        self.wizard.sign_out()
        self.refresh_home()

    def _pipeline(self) -> OrderPipeline:
        service = self.parse_service
        if service is not None and hasattr(service, "on_merged"):
            service.on_merged = self._on_merged
        return OrderPipeline(
            orders=self.orders,
            tenant=self.tenant,
            group=self.live_conversation_name(),
            parse_service=self.parse_service,
            settings=self.auto,
            merger=self.merger,
            account_id=self.settings.account_id or "default",
        )

    def start_background(self) -> None:
        """启动后台轮询。**只启动一个**，停止后不留孤儿线程。"""
        with self._lock:
            if self._poller is not None and self._poller.is_alive():
                return
            self._stop.clear()
            self._poller = threading.Thread(target=self._poll_loop, name="merchant-poll", daemon=True)
            self._poller.start()

    def stop(self) -> None:
        """停止所有后台线程。"""
        self._stop.set()
        self.monitor.stop()
        self.history.stop()
        with self._lock:
            poller = self._poller
            self._poller = None
        if poller is not None and poller.is_alive() and poller is not threading.current_thread():
            poller.join(timeout=3.0)
        for thread in self._background:
            if thread.is_alive():
                thread.join(timeout=1.0)
        self._background.clear()
        self.ui_queue.close(timeout=3.0)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_home()
                # 自动发送已移除：回执只整理、不发送，不再有后台派发循环。
            except Exception as exc:  # pragma: no cover - defensive
                self._error(str(exc))
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def _spawn(self, name: str, function: Callable[[], None]) -> threading.Thread:
        def guarded() -> None:
            try:
                function()
            except Exception as exc:  # 后台线程的异常不会自己浮到界面上
                # 以前这里没有 try/except：读历史、查额度、智能整理、加载模型
                # 任何一处抛异常都直接消失，商户只看到界面莫名其妙退回上一步，
                # 既不知道发生了什么，也不知道该不该重试。必须变成看得见的中文。
                LOGGER.exception("background task failed: %s", name)
                self._report_failure(name, exc)

        thread = threading.Thread(target=guarded, name=name, daemon=True)
        with self._lock:
            self._background = [item for item in self._background if item.is_alive()]
            self._background.append(thread)
        thread.start()
        return thread

    #: 后台任务失败时给商户看的话。不含异常原文 —— 类名、模块路径对商户
    #: 没有意义，只会让界面看起来像是坏了。具体原因只进日志。
    FAILURE_TEXT = {
        "history-read": "读取消息失败，已读到的部分保留着。可以点「开始读取」重试，或换一种读法。",
        "quota": "查询额度失败，请检查网络后点「刷新额度」重试。",
        "models": "获取模型列表失败，已保留上一次的列表，可稍后点「刷新模型列表」重试。",
        "analyze": "智能整理失败，已读到的消息保留着，没有产生订单。",
        "login": "登录失败，请检查网络后重试。",
    }
    DEFAULT_FAILURE_TEXT = "这一步没能完成，可以重试。已保存的内容不会丢失。"

    def _report_failure(self, name: str, exc: Exception) -> None:
        text = self.FAILURE_TEXT.get(name, self.DEFAULT_FAILURE_TEXT)
        if self.events.on_error:
            try:
                self.events.on_error(text)
            except Exception:  # pragma: no cover - 界面正在重建
                LOGGER.debug("error handler skipped", exc_info=True)

    # -------------------------------------------------------------- ① 微信

    def product_logged_in(self) -> bool:
        """产品账号是否已登录。进入办理流程的门槛只认它。"""
        return self.session is not None

    #: wxid 显示名备注的文件名。内存解不出的群名/人名，商户手备注一次，
    #: 以后都显示备注。只影响显示，不改库里原文。
    WXID_ALIAS_NAME = "wxid_aliases.json"

    def _aliases_path(self):
        return Path(self.data_dir) / self.WXID_ALIAS_NAME

    def load_aliases(self) -> dict:
        """读备注映射。文件坏了当没有，不挡启动。"""
        try:
            raw = json.loads(self._aliases_path().read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): str(value) for key, value in raw.items() if value}

    def display_name_for(self, wxid_or_name: str) -> str:
        """显示用名：有备注用备注，没有用原文。从不返回空。"""
        text = str(wxid_or_name or "")
        if not text:
            return ""
        return self.wxid_aliases.get(text, text)

    def display_name_for_conversation(self, item: Any) -> str:
        """会话行的显示名：手动备注 > 内存辨认出的真名 > 原始编号。

        之前重画时只查备注表，内存辨认出的真名（``Conversation.name``）
        被 wxid 原文盖掉 —— 好不容易认出来的名字根本没画出来，
            第 1 步看起来"一个群名都读不出"。只有备注和真名都没有时
        才显示原始编号。
        """
        raw = str(getattr(item, "wxid", "") or getattr(item, "name", "") or "")
        if not raw:
            return ""
        shown = self.display_name_for(raw)
        if shown != raw:
            return shown
        name = str(getattr(item, "name", "") or "")
        if name and name != raw:
            return name
        return raw

    def set_alias(self, wxid: str, name: str) -> bool:
        """手备注一个 wxid。空名等于删掉这条。"""
        wxid, name = str(wxid or "").strip(), str(name or "").strip()
        if not wxid:
            return False
        if name:
            self.wxid_aliases[wxid] = name
        else:
            self.wxid_aliases.pop(wxid, None)
        try:
            path = self._aliases_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self.wxid_aliases, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(path)
        except Exception:
            LOGGER.debug("save wxid aliases failed", exc_info=True)
            return False
        return True

    def check_wechat(self) -> WeChatStatus:
        before = self.wizard.settings.account_id
        status = self.wizard.check_wechat()
        after = self.wizard.settings.account_id
        # 切号了：旧号的会话名单、缓存统统作废。不清掉，第 1 步会一直
        # 显示上个号的群 —— 点进去读的也是错的。
        if before and after and before != after:
            self._clear_conversation_cache()
        return status

    def _clear_conversation_cache(self) -> None:
        try:
            self._conversations_cache_path().unlink(missing_ok=True)
        except Exception:
            pass

    def start_wechat(self) -> bool:
        return self.wizard.start_wechat()

    def watch_wechat(self) -> None:
        """后台等登录完成，自动进入下一步。"""
        self._spawn("watch-wechat", self.monitor.start)

    def _on_wechat_change(self, status: WeChatStatus) -> None:
        if status.ready:
            self.wizard.check_wechat()

    # ------------------------------------------------------------ ② 会话

    CONVERSATIONS_CACHE_NAME = "conversations_cache.json"

    def _conversations_cache_path(self) -> Path:
        return Path(self.data_dir) / self.CONVERSATIONS_CACHE_NAME

    def _restore_conversation_cache(self) -> int:
        """启动时恢复上次的会话列表。读不到、坏了、账号对不上都当没有，不挡启动。"""
        try:
            raw = json.loads(self._conversations_cache_path().read_text(encoding="utf-8"))
        except Exception:
            return 0
        if not isinstance(raw, dict):
            return 0
        current = str(getattr(self.wizard.settings, "account_id", "") or "")
        saved_account = str(raw.get("account_id", "") or "")
        # 缓存是按号存的：换号之后旧名单不能再摆出来。
        # settings 为空（全新）时也不恢复 —— 等检测出当前号再说。
        if not current or not saved_account or current != saved_account:
            return 0
        rows = raw.get("available")
        try:
            return self.conversations.restore_available(rows)
        except Exception:
            return 0

    def _save_conversation_cache(self, found: Sequence[Conversation]) -> None:
        """拉到名单就落盘，连同账号一起存。空结果不覆盖 —— 读失败不等于没有会话。"""
        if not found:
            return
        payload = {
            "account_id": str(getattr(self.wizard.settings, "account_id", "") or ""),
            "saved_at": datetime.now().astimezone().strftime("%m-%d %H:%M"),
            "available": [item.to_dict() for item in found],
        }
        path = self._conversations_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            LOGGER.debug("save conversation cache failed", exc_info=True)

    def conversation_cache_info(self) -> str:
        """给界面看的缓存状态：上次更新时间，或还没有拉取过。"""
        try:
            raw = json.loads(self._conversations_cache_path().read_text(encoding="utf-8"))
            saved_at = raw.get("saved_at") if isinstance(raw, dict) else ""
        except Exception:
            return "还没有拉取过"
        return f"上次更新：{saved_at}" if saved_at else "还没有拉取过"

    def refresh_conversations(self, *, force: bool = False) -> list[Conversation]:
        # 没钥匙先自动提一次（提完直接读，商户不用管中间步骤）。
        # 只对真实来源自动提：模拟/测试来源不需要钥匙，别去碰真微信。
        # 微信会新建分片/库（手机导入建 message_1.db），钥匙是旧的就
        # 覆盖不全 —— 磁盘上有没钥匙的库时也重提，但两次自动提取至少
        # 间隔 5 分钟，失败了反复扫内存只会越扫越慢，重试走界面按钮。
        if isinstance(self.conversation_source, RealConversationSource):
            # force：用户点了「刷新列表」就不受 5 分钟节流限制，当场重提密钥。
            self._auto_ensure_wcdb_keys(force=force)
        found = self.wizard.refresh_conversations()
        # 记下这次走的是哪条路（解密库 / 内存扫描），界面要如实显示。
        self.conversation_source_label = str(
            getattr(self.conversation_source, "last_source", "") or "")
        # 只有解密库那份（真名真人数）才值得落盘：内存扫描的名单残缺，
        # 存下来只会让下次看起来还是不全。
        if found and self.conversation_source_label == "wcdb":
            self._save_conversation_cache(found)
        if self.events.on_conversations:
            self.events.on_conversations(found)
        return found

    def _auto_ensure_wcdb_keys(self, *, force: bool = False) -> bool:
        """该补钥匙就补（没钥匙，或磁盘上有没钥匙的新库），返回钥匙齐否。

        5 分钟节流：失败了反复扫内存只会越扫越慢，重试走界面按钮。
        只对真实来源调用 —— 模拟/测试来源不需要钥匙，别去碰真微信。
        """
        if not self._wcdb_auto_ensure_due(force=force):
            return self._wcdb_keys_complete()
        self._wcdb_ensure_attempted = True
        try:
            import time as _time
            self._wcdb_ensure_at = _time.time()
        except Exception:
            pass
        try:
            ok, _ = self.wcdb_status()
            unkeyed: list = []
            if ok:
                unkeyed = self._wcdb_unkeyed()
        except Exception:
            ok, unkeyed = False, []
        if not ok or unkeyed:
            if unkeyed:
                self._log(f"微信新建了 {len(unkeyed)} 个库还没钥匙，自动补提一次…")
            else:
                self._log("本机数据库还没钥匙，自动提取一次（几十秒）…")
            # 合规告知：确实在动微信的数据，就让商户看得见（不弹窗、不挡操作）。
            self._notice("正在读取本机微信数据库（只读副本、用完即删）："
                         "首次需要提取数据库密钥，约几十秒")
            try:
                done, message = self.ensure_wcdb_keys()
            except Exception as exc:
                done, message = False, f"提取失败：{exc}"
            self._notice("")
            self._log("密钥已就绪" if done else
                      f"自动提取没成（{message}），可点按钮重试")
            return done
        return True

    def _wcdb_keys_complete(self) -> bool:
        """钥匙齐否：状态 ok 且磁盘上没有没钥匙的库。"""
        try:
            ok, _ = self.wcdb_status()
            if not ok:
                return False
            return not self._wcdb_unkeyed()
        except Exception:
            return False

    def _wcdb_unkeyed(self) -> list:
        """磁盘上有、钥匙里没有的库。拿不到目录当没有（别误触发提取）。"""
        try:
            from .wcdb import WcdbReader
            storage = self._wcdb_db_storage()
            if storage is None:
                return []
            return WcdbReader(storage, self.wcdb_keys_path()).unkeyed_databases()
        except Exception:
            return []

    def replace_conversation_source(self, source: Any) -> None:
        """换一个会话来源，并**重新接上解密库**。

        出过的事故：宿主先把 `conversation_source` 换成带 ui_lock 的新对象，
        而"注入 wcdb_fn"是在 `__init__` 时对**旧对象**做的 —— 换完就丢了。
        结果界面**永远**走内存扫描（只有群、名字不全、没有好友），而解密库明明
        是好的。所以换来源必须走这里，不要在别处直接赋值。
        """
        self.conversation_source = source
        self._hook_wcdb(source)
        # 向导手里的那份也换掉：**真正去列举的是它**。两处指向不同对象时，
        # 界面读来源、向导读会话，各说各话（这次就是：向导走了解密库，
        # 应用这层还记着旧对象，来源永远显示不出来）。
        wizard = getattr(self, "wizard", None)
        if wizard is not None:
            try:
                wizard.conversation_source = source
            except Exception:
                LOGGER.debug("wizard source sync failed", exc_info=True)

    def _hook_wcdb(self, source: Any) -> None:
        """把"读解密库"这条路挂到会话来源上。没有该属性的替身跳过。"""
        if not hasattr(source, "wcdb_fn"):
            return
        try:
            source.wcdb_fn = self.list_wcdb_conversations
        except Exception:
            LOGGER.debug("wcdb hookup failed", exc_info=True)

    def _wcdb_auto_ensure_due(self, *, force: bool = False) -> bool:
        """这次要不要自动提钥匙：没试过，或上次已超 5 分钟且仍有缺钥匙的库。

        `force` 给用户明确点了刷新列表用：那一秒就是他要重试，节流不该拦。
        """
        if force:
            return True
        if not getattr(self, "_wcdb_ensure_attempted", False):
            return True
        try:
            import time as _time
            if _time.time() - float(getattr(self, "_wcdb_ensure_at", 0) or 0) < 300:
                return False
        except Exception:
            return True
        try:
            storage = self._wcdb_db_storage()
            if storage is None:
                return False
            from .wcdb import WcdbReader
            return bool(WcdbReader(storage, self.wcdb_keys_path()).unkeyed_databases())
        except Exception:
            return False

    # ------------------------------------------------- 解密库（真名真人数）

    def wcdb_keys_path(self) -> Path:
        """密钥文件位置（应用数据目录）。没有=还没提取过。"""
        return Path(self.data_dir) / "wcdb_keys.json"

    def _wcdb_db_storage(self) -> Path | None:
        """当前登录号的 db_storage 目录。拿不到（没登录）返回 None。"""
        try:
            from .WeChatTools import Tools
            folder = Tools.where_wxid_folder()
        except Exception:
            return None
        if not folder:
            return None
        path = Path(folder) / "db_storage"
        return path if path.is_dir() else None

    def wcdb_status(self) -> tuple[bool, str]:
        """解密库能不能用。(ok, 说明)。界面据此显示入口或引导。

        有钥匙还要验一把真能开门（读一页验签，毫秒级）：微信升级换盐后，
        在这里就告诉商户重提，而不是等读一半才炸。
        """
        from .wcdb import WcdbReader

        storage = self._wcdb_db_storage()
        if storage is None:
            return False, "微信还没登录，看不到本机数据库"
        reader = WcdbReader(storage, self.wcdb_keys_path())
        if not reader.available:
            return False, reader.error or "还没有提取密钥，提一次以后一直能用"
        stale = self._wcdb_keys_stale_message()
        if stale:
            return False, stale
        ok, reason = reader.verify_keys()
        if not ok:
            return False, reason
        return True, "本机数据库可读，列表带真名和人数"

    def _wcdb_keys_stale_message(self) -> str:
        """钥匙是上个大版本提的 → 盐很可能换了。版本号任一未知就不催。"""
        from . import wcdb as wcdb_mod

        try:
            from .WeChatTools import Tools
            current = str(Tools.get_weixin_version() or "")
        except Exception:
            return ""
        recorded = wcdb_mod.read_keys_version(self.wcdb_keys_path())
        if wcdb_mod.keys_version_stale(recorded, current):
            return (f"微信已从 {recorded} 升级到 {current}，钥匙可能失效，"
                    "点「重新提取密钥」提一次再读")
        return ""

    def _wcdb_reader(self) -> Any | None:
        """给 HistoryReader 的解密库来源：有钥匙才有，没有返回 None。

        每次调用都新建（钥匙文件可能刚提出来），prepare 失败由上游吞掉回落。
        """
        from .wcdb import WcdbReader

        storage = self._wcdb_db_storage()
        if storage is None:
            return None
        reader = WcdbReader(storage, self.wcdb_keys_path())
        return reader if reader.available else None

    def list_wcdb_conversations(self, kind: str = "") -> list[Any] | None:
        """解密库会话（注入给 RealConversationSource 的优先路）。

        返回 None 或抛异常都等于"这条路不通"，上游按解密库不可用处理。
        """
        from .wcdb import WcdbReader, WcdbUnavailable

        storage = self._wcdb_db_storage()
        if storage is None:
            return None
        reader = WcdbReader(storage, self.wcdb_keys_path())
        try:
            return reader.list_conversations(kind)
        except WcdbUnavailable as exc:
            LOGGER.debug("wcdb conversations unavailable: %s", exc)
            return None

    def ensure_wcdb_keys(self, timeout: float = 300.0) -> tuple[bool, str]:
        """进程内提取一次密钥（后台线程调，会扫几十秒内存）。

        以前是子进程调 third_party 下的脚本：打包一漏就报"提取工具
        不在"，子进程编码（cp1252 中文崩）还要单独伺候。现在调包内
        vendored 的 :mod:`._wcdb_key_tool`，没有外部脚本、没有子进程、
        输出直接吞掉不经过控制台。

        ``timeout`` 保留只为兼容旧签名：进程内扫描中途停不下来，
        传了也不生效。钥匙跟微信大版本走，大版本不变一直能用。
        返回 (ok, 说明)。
        """
        import contextlib
        import io
        from types import SimpleNamespace

        storage = self._wcdb_db_storage()
        if storage is None:
            return False, "微信还没登录，先登录再提取"
        try:
            from ._wcdb_key_tool import cmd_extract
        except Exception as exc:
            return False, f"提取模块加载失败：{exc}"
        keys_path = self.wcdb_keys_path()
        args = SimpleNamespace(db_dir=str(storage), output=str(keys_path),
                               decrypt=False)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_extract(args)
        except SystemExit as exc:
            if exc.code not in (None, 0):
                return False, "提取没成功（没扫到钥匙），微信太忙时重试"
        except Exception as exc:
            return False, f"提取失败：{exc}"
        ok, _ = self.wcdb_status()
        if ok:
            self._seal_wcdb_keys()
            return True, "密钥已就绪，点「刷新列表」刷新即带真名"
        return False, "工具跑完了但钥匙对不上，微信可能刚升级，重提一次试试"

    def _seal_wcdb_keys(self) -> None:
        """提钥匙成功后：记下微信版本 + 钥匙文件 DPAPI 落盘。

        全程 best-effort：这里的任何失败都不该把一次成功的提取
        变成失败，只记日志。
        """
        from . import wcdb as wcdb_mod

        try:
            from .WeChatTools import Tools
            version = str(Tools.get_weixin_version() or "")
        except Exception:
            version = ""
        try:
            if version:
                wcdb_mod.record_keys_version(self.wcdb_keys_path(), version)
            wcdb_mod.protect_keys_file(self.wcdb_keys_path())
        except Exception:
            LOGGER.debug("seal wcdb keys failed", exc_info=True)

    def choose_history_conversations(self, selected: Sequence[Conversation]) -> None:
        self.wizard.choose_history_conversations(selected)

    def choose_live_conversation(self, conversation: Conversation) -> None:
        """第一版实时接单只允许一个会话。"""
        self.wizard.choose_live_conversation(conversation)
        if self.parse_service is not None:
            self.pipeline = self._pipeline()
            self.wizard.pipeline = self.pipeline
        self.refresh_home()

    def live_conversation_name(self) -> str:
        live = self.wizard.conversations.live_selected
        return live.name if live else ""

    def live_conversation(self) -> Conversation | None:
        return self.wizard.conversations.live_selected

    # ------------------------------------------------------------ ③ 历史

    def read_history(
        self,
        *,
        range_kind: str = "",
        custom: TimeWindow | None = None,
        max_messages: int = 0,
        mode: str = "",
        sender_source: str = "",
    ) -> None:
        def work() -> None:
            # 开始读取**不**自动提取密钥：提取过程会扫描微信进程内存，耗时不可
            # 预测，不能挂在"点了读取"后面让商户干等。缺密钥时由解密库读取返回
            # 明确错误（"请先点「重新提取密钥」"），补钥匙有它自己的入口：
            # 「刷新列表」会带 force 补一次（见 _auto_ensure_wcdb_keys）。
            result = self.wizard.read_history(
                range_kind=range_kind, custom=custom, max_messages=max_messages,
                on_progress=self.events.on_history_progress, mode=mode or ReadMode.RECENT,
                sender_source=sender_source,
            )
            if self.events.on_history_done:
                try:
                    self.events.on_history_done(result)
                except Exception:
                    LOGGER.debug("history-done handler skipped", exc_info=True)
            self.refresh_home()

        self._spawn("history-read", work)

    def cancel_history(self) -> None:
        self.wizard.cancel_history()

    # ------------------------------------------------------------ ④ 额度

    def check_quota(self) -> QuotaView:
        view = self.wizard.check_quota()
        self.refresh_home()
        return view

    def login_requested(self) -> None:
        """界面上点了「登录产品账号」。

        浏览器登录是异步的：点了之后界面继续用，其它步骤照常可用。
        登录成功后走 :meth:`attach`，由启动层提供的 ``attach_session`` 负责
        把本地的解析服务接上。
        """
        runner = getattr(self, "login_runner", None)
        if runner is None:
            self._error("尚未配置产品登录，请用默认网关地址启动本软件。")
            return

        def work() -> None:
            try:
                session = runner(self._login_progress)
            except Exception as exc:
                self._error(f"登录失败：{exc}")
                return
            attach = getattr(self, "attach_session", None)
            if callable(attach):
                attach(session)
            self.refresh_home()

        self._spawn("login", work)

    def _login_progress(self, status: str, detail: str = "") -> None:
        if self.events.on_log:
            try:
                self.events.on_log(detail or status)
            except Exception:
                pass

    # ------------------------------------------------------------ ⑤ 整理

    def analyze(self, role: str = "order") -> dict[str, Any] | None:
        """开始智能整理。额度不足时保留已读取的数据。"""
        result_holder: dict[str, Any] = {}

        def work() -> None:
            from .wizard import QuotaBlocked

            try:
                result_holder["summary"] = self.wizard.analyze(role=role)
            except QuotaBlocked as blocked:
                result_holder["quota"] = blocked.view
                self._error(blocked.view.headline())
            except Exception as exc:
                self._error(f"整理失败：{exc}")
            self.refresh_home()

        thread = self._spawn("analyze", work)
        thread.join()
        return result_holder.get("summary")

    def _run_role_analysis(self, role_id: str,
                           message_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Run a non-order role through the same account-backed AI service."""
        from . import summary_task

        messages_override = None
        if message_ids is not None:
            ids = list(dict.fromkeys(str(value) for value in message_ids if value))
            messages_override = []
            if ids:
                marks = ",".join("?" for _ in ids)
                rows = self.orders.db.execute(
                    "SELECT id, group_id, sender, content, sent_at, kind, observed_at FROM messages "
                    f"WHERE merchant_id=? AND id IN ({marks})",
                    [self.tenant, *ids]).fetchall()
                messages_override = [{
                    "id": str(row["id"] or ""),
                    "group_id": str(row["group_id"] or ""),
                    "group_label": str(row["group_id"] or ""),
                    "sender": str(row["sender"] or ""),
                    "content": str(row["content"] or ""),
                    "sent_at": str(row["sent_at"] or ""),
                    "observed_at": str(row["observed_at"] or ""),
                    "kind": str(row["kind"] or "text"),
                } for row in rows]
                try:
                    from .media_attachments import attach_cached_files
                    messages_override = attach_cached_files(messages_override)
                except Exception:
                    pass
        return summary_task.run_summary(
            self, self.wizard.history_result, role_id=role_id,
            messages_override=messages_override,
            on_progress=self._analysis_progress,
            on_stream=getattr(self.events, "on_analysis_stream", None),
            on_reasoning=getattr(self.events, "on_analysis_reasoning", None),
        )

    def _analysis_progress(self, line: str) -> None:
        """分析的进度：日志里留一份，同时给界面一句短的。

        两边都要：日志是给排查用的（"卡住了"时要看它）；界面那句是给商户看的
        （"正在分析第 2/3 个会话"比一个空框有用得多）。
        """
        text = str(line or "")
        if self.events.on_log:
            self.events.on_log(text)
        if self.events.on_analysis_progress:
            self.events.on_analysis_progress(text)

    # ------------------------------------------------------------- 模型

    def load_models(self, force: bool = False) -> dict[str, Any]:
        """拉取模型目录；网络失败时回退到本机上次成功的目录。"""
        if self.client is None:
            self.model_catalog = _empty_model_view()
            return self.model_catalog
        # 桩对象/单测可能没有 data_dir：没有就不走本机缓存，直接调网关。
        # 所有文件读写本来就有 OSError 兜底，这里只是别在拼路径时先炸。
        base = getattr(self, "data_dir", "") or ""
        cache_path = Path(base) / "model_catalog_cache.json"
        cooldown_path = Path(base) / "model_catalog_retry_after.txt"
        if not (self.model_catalog or {}).get("models"):
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and isinstance(cached.get("models"), list):
                    self.model_catalog = cached
            except (OSError, ValueError, TypeError):
                pass
        try:
            retry_at = float(cooldown_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError, TypeError):
            retry_at = 0.0
        remaining = retry_at - time.time()
        if remaining > 0:
            self.model_catalog = dict(self.model_catalog or _empty_model_view())
            self.model_catalog["stale"] = True
            self.model_catalog["notice"] = (
                f"模型列表请求繁忙，请约 {int(remaining + 0.99)} 秒后刷新。"
                "已保存的模型选择仍保留。"
            )
            return self.model_catalog
        try:
            refresh = getattr(self.client, "refresh_models", None) if force else None
            payload = (refresh() if callable(refresh) else self.client.list_models()) or {}
        except Exception as exc:
            # 不用「空列表」伪装成「没有模型」；保留旧数据并说明原因。
            self.model_catalog = dict(self.model_catalog or _empty_model_view())
            self.model_catalog["stale"] = True
            # 异常原文**不能**进商户界面。这里曾经直接 f-string 拼 {exc}，
            # 结果界面上冒出 "'DemoGateway' object has no attribute 'list_models'"
            # 这种东西 —— 商户看不懂，只会觉得软件坏了。技术细节只进日志。
            LOGGER.warning("model catalog refresh failed: %s", exc, exc_info=True)
            retry_after = getattr(exc, "retry_after", None)
            if retry_after:
                try:
                    cooldown_path.parent.mkdir(parents=True, exist_ok=True)
                    cooldown_path.write_text(
                        str(time.time() + min(max(float(retry_after), 1.0), 120.0)),
                        encoding="ascii",
                    )
                except OSError:
                    LOGGER.debug("Could not persist model catalog cooldown", exc_info=True)
            self.model_catalog["notice"] = (
                MODEL_STALE_NOTICE if self.model_catalog.get("models")
                else MODEL_UNAVAILABLE_NOTICE
            )
            return self.model_catalog
        try:
            cooldown_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("Could not clear model catalog cooldown", exc_info=True)
        self.model_catalog = payload.get("catalog") or _empty_model_view()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.model_catalog, ensure_ascii=False), encoding="utf-8")
            temporary.replace(cache_path)
        except OSError:
            LOGGER.debug("Could not persist model catalog cache", exc_info=True)
        self._reconcile_model_choice()
        self._adopt_pricing_version()
        return self.model_catalog

    def _adopt_pricing_version(self) -> None:
        """把服务端当前的计费策略版本记下来，下次提交时带上。

        记下来的只是"商户看过的**那一版**"，不代表商户已经同意按它扣费；
        提交后如果版本对不上，服务端会要求重新确认。
        """
        version = str((self.model_catalog or {}).get("version") or "")
        service = self._parse_service()
        if version and service is not None:
            service.pricing_version = version

    def _parse_service(self) -> Any:
        """拿到真正负责提交的那个解析服务（界面与向导共用一个）。"""
        for holder in (self.service_factory_result(), getattr(self, "wizard", None)):
            service = getattr(holder, "parse_service", None) or getattr(holder, "service", None)
            if service is not None:
                return service
        pipeline = getattr(self, "pipeline", None)
        return getattr(pipeline, "parse_service", None) if pipeline is not None else None

    def service_factory_result(self) -> Any:
        return getattr(self, "_service", None)

    def confirm_pricing(self) -> dict[str, Any]:
        """商户点了"确认新价格"之后才走这里。"""
        service = self._parse_service()
        return service.confirm_pricing() if service is not None else {}

    def pending_pricing(self) -> dict[str, Any]:
        service = self._parse_service()
        return service.pending_pricing() if service is not None else {}

    def pricing_confirmation_text(self) -> str:
        service = self._parse_service()
        return service.pricing_confirmation_text() if service is not None else ""

    def _reconcile_model_choice(self) -> None:
        """记住客户选择；失效时回落到默认免费模型并说明。"""
        catalog = self.model_catalog or {}
        available = {str(item.get("alias")) for item in catalog.get("models") or []}
        default_alias = str(catalog.get("default_alias") or "")
        # JYI 目录不是产品计费目录，没有 default_alias/free_available；
        # 仍应允许使用者选择上游返回的模型。优先用客户端默认值，否则取首项。
        if not default_alias and self.client is not None and callable(getattr(self.client, "text_model", None)):
            try:
                candidate = str(self.client.text_model() or "")
                default_alias = candidate if candidate in available else ""
            except Exception:
                default_alias = ""
        if not default_alias and available and self.client is not None and callable(getattr(self.client, "text_model", None)):
            default_alias = next(
                (str(item.get("alias")) for item in catalog.get("models") or []
                 if str(item.get("alias") or "") in available),
                "",
            )
        chosen = self.settings.model_alias
        if chosen and chosen in available:
            return
        if default_alias and default_alias in available:
            if chosen:
                self._log(f"上次使用的模型已不可用，已改为「{self._label_of(default_alias)}」")
            self.settings.model_alias = default_alias
            self.settings_store.save(self.settings)
            return
        # 没有合格免费模型：保持未选择，由界面如实显示「暂不可用」。
        self.settings.model_alias = ""
        self.settings_store.save(self.settings)

    def _label_of(self, alias: str) -> str:
        for item in (self.model_catalog or {}).get("models") or []:
            if str(item.get("alias")) == alias:
                return str(item.get("display_name") or alias)
        return alias

    def selected_model(self) -> dict[str, Any]:
        """当前选中的模型卡片。"""
        alias = self.settings.model_alias
        for item in (self.model_catalog or {}).get("models") or []:
            if str(item.get("alias")) == alias:
                return item
        return {}

    def set_model(self, alias: str) -> bool:
        """切换模型。

        **只影响新任务**：已提交的任务在服务端冻结了自己的模型快照，
        这里改设置不会让正在跑的任务换模型。历史订单和客户记忆也不受影响。
        """
        available = {str(item.get("alias")) for item in (self.model_catalog or {}).get("models") or []}
        if alias not in available:
            return False
        self.settings.model_alias = alias
        self.settings_store.save(self.settings)
        service = self._parse_service()
        if service is not None and hasattr(service, "model_alias"):
            service.model_alias = alias
        self.refresh_home()
        return True

    def model_notice(self) -> str:
        """给商户看的提示。技术细节不往这里放。

        **没登录时必须说"没登录"，不能说"模型不可用"。**
        未登录压根没去取过模型列表（:meth:`load_models` 直接返回空视图），
        报成"免费模型暂不可用"是**归错了因**：商户会去反复点「刷新模型列表」，
        刷多少次都不会好——因为问题根本不在模型上。
        """
        if self.client is None:
            return (
                "还没有登录产品账号，所以还没有可选的分析模型。"
                "请先点右上角「账号：未登录　点此登录」。"
            )
        catalog = self.model_catalog or {}
        if catalog.get("stale"):
            return str(catalog.get("notice") or "模型列表不是最新状态")
        if catalog.get("models") and self.client is not None and callable(getattr(self.client, "text_model", None)):
            return str(catalog.get("notice") or "")
        if not catalog.get("free_available"):
            # 注意：产品里**没有免费模型**。以前这里写「免费模型暂不可用」，
            # 商户看到会去反复刷新模型列表 —— 问题根本不在模型上。
            # 说人话：分析还没开通，或暂时没有可用模型。
            return ("暂时没有可用于分析消息的模型。已读取的消息和待处理任务"
                    "都保留着，可稍后重试或联系客服。")
        return ""

    #: 换模型重新分析的入口开关。
    #:
    #: 差异流程（差异比较 → 版本校验 → 应用）已经在
    #: ``OrderStore.start_reanalysis`` / ``apply_reanalysis_suggestion`` 里
    #: 实现并有测试覆盖，但它**还没有端到端跑过一次真实模型**，所以界面上
    #: 保持关闭。开着它等于收一笔费然后告诉商户"已处理过"—— 那比没有这个
    #: 功能更糟。等真实链路验过再把它打开。
    REANALYSIS_ENABLED = False

    def reanalysis_notice(self) -> str:
        """换模型重新分析为什么现在不能点。"""
        if self.REANALYSIS_ENABLED:
            return ""
        return (
            "换模型重新分析暂时不可用：它会真实调用模型并消耗分析次数，"
            "而差异对比流程还没有在真实链路上验过。已有订单查看和核对不受影响。"
        )

    def pending_reanalysis_count(self) -> int:
        """等待商户决定要不要应用的差异建议条数。"""
        try:
            return len(self.orders.pending_reanalysis_suggestions(self.tenant))
        except Exception:
            return 0

    def free_quota_remaining(self) -> int:
        """免费额度剩余。免费档位已焊死，恒为 0。

        保留这个方法是为了让调用方不必改 —— 但它**永远返回 0**，
        以后有人拿它做判断会得到"没有免费额度"的正确答案。
        """
        return 0

    def _log(self, text: str) -> None:
        if self.events.on_log:
            try:
                self.events.on_log(text)
            except Exception:
                pass

    # ------------------------------------------------------------- 首页

    def home(self) -> HomeView:
        view = HomeView()
        status = self.monitor.latest
        if status.state == WeChatState.UNREADABLE and status.detail == "尚未检测":
            # 只为了让首页显示真实状态，**不许推动向导**：探测会改状态、状态变化
            # 会触发监视器回调，而那个回调会把向导往前推；首页只取状态，
            # 不能借这个只读展示动作改变商户正在走的步骤。
            status = self.monitor.check_once(notify=False)
        view.wechat_state = status.state
        view.wechat_detail = status.copy()[1]
        view.wechat_ready = status.ready
        view.simulated = status.simulated or self.simulated
        # 消息是不是真的、和订单是不是模型分析的，是两件事。首页以前只有
        # 一个 ``simulated``，于是"真实微信 + 规则引擎"这种组合会被说成
        # "演示数据"—— 消息明明是真的。
        view.model_executed = bool(getattr(self.parse_service, "model_executed", False))
        try:
            view.open_merge_groups = int(self.merger.stats().get("open_groups", 0))
        except Exception:
            # 合并库不可用时不该拖垮首页：没有这一项只是少说一句话。
            LOGGER.debug("merge stats unavailable", exc_info=True)
            view.open_merge_groups = 0

        live = self.live_conversation()
        view.conversation = live.disambiguation if live else "未选择会话"
        if live and live.name in self.wizard.conversations.has_ambiguous_names():
            view.conversation_note = "存在同名会话，已按人数区分，请确认选择的是正确的群"
        elif live and not live.member_count and live.kind == ConversationKind.GROUP:
            view.conversation_note = "未能读到群人数，执行前请再次确认"


        view.effective = self.dashboard.effective_orders()
        view.actionable = self.dashboard.actionable_orders()
        view.auto_recorded = self.dashboard.auto_recorded_count()
        view.stock = self.dashboard.stock_summary(view.effective)
        view.payment_claims = self.dashboard.payment_claims()
        # 独立澄清任务单独一栏：它不是订单，不进订单数、不进备货。
        view.clarifications = self.dashboard.clarifications()
        # 系统解析失败也是单独一栏：不是订单，也不会去问客户。
        view.system_failures = self.dashboard.system_failures()
        view.parse_failures = self.dashboard.parse_failures()
        view.scope_text = self.dashboard.scope_text()

        quota = self.wizard.quota
        # 没查过额度就**不写**这一句：默认 QuotaView 讲的是"未登录产品账号"，
        # 而此刻明明登录着 —— 首页那行会变成「微信正常　未登录产品账号」。
        view.quota_headline = quota.headline() if quota.logged_in else ""
        view.quota_ok = quota.sufficient

        if self.history_result is not None:
            view.last_actual_range = self.history_result.actual_range_text()
        return view

    @property
    def history_result(self) -> HistoryResult | None:
        return self.wizard.history_result

    def refresh_home(self) -> HomeView:
        view = self.home()
        self._last_home = view
        if self.events.on_home:
            try:
                self.events.on_home(view)
            except Exception:
                pass
        return view

    def orders_for_display(self) -> list[OrderRow]:
        """界面上「查看订单」列出的内容：**需要商户处理的**订单。

        也就是 ``review`` / ``cancel_review`` 两种状态 —— 自动记单（``draft``）
        **不在这里逐条列**：那正是自动化的目的，它们已经按商品汇总进首页的
        「今日备货」了。（旧文档写的是"有效订单 + 需要处理的"，和实现对不上；
        实际上 ``actionable_orders()`` 也是按同一组状态过滤的，
        ``or item.order_id in actionable`` 那半边并不放宽范围。）
        """
        actionable = {item.order_id for item in self.dashboard.actionable_orders()}
        rows = self.dashboard.effective_orders()
        return [item for item in rows if item.status in ACTIONABLE_STATUSES or item.order_id in actionable]

    # ------------------------------------------------------------ internals

    def _notice(self, text: str) -> None:
        """给界面一句不打断人的告知。没有订阅者就什么也不做。"""
        if self.events.on_notice:
            try:
                self.events.on_notice(str(text))
            except Exception:
                pass

    def _error(self, message: str) -> None:
        if self.events.on_error:
            try:
                self.events.on_error(message)
            except Exception:
                pass

    @staticmethod
    def _default_history_reader(**kwargs: Any) -> list[dict[str, Any]]:
        # 界面链和内存 carve 都已退役：没有注入读取器时不碰微信窗口，直接明说。
        # 生产里这条走不到（解密库那条路在 HistoryReader 内部），
        # 撞到这里说明装配错了，别静默读个空。
        raise RuntimeError("界面读取已移除：请先点「重新提取密钥」走解密库")

    def _default_group_members(conversation: Any) -> list[str]:
        # 界面链已移除：取成员要打开微信界面，这条路没有了。
        # 生产默认 sender_source 已是 wcdb（解密库），走不到这里；
        # 撞到说明有人显式传了 members 却没注入名单，返回空让发送人写 wxid，
        # 不中断读取（消息本身还能读）。
        return []

    def close(self) -> None:
        self.stop()
        self.cursor.close()
        # merge.db 也是 sqlite 连接。以前这里漏了 merger，于是 Windows 上
        # 临时目录删不掉：测试在 tearDown 报 WinError 32「文件被另一个进程占用」，
        # 而真正漏掉的地方是这儿 —— 报错指向的是清理，不是没关的句柄。
        self.merger.close()


__all__ = ["AppEvents", "MerchantApplication", "POLL_INTERVAL_SECONDS"]


def _empty_model_view() -> dict[str, Any]:
    """空目录视图。**没有目录就说没有**，不用固定模型顶替。"""
    return {
        "version": "",
        "default_alias": "",
        "free_available": False,
        "stale": False,
        "notice": "",
        "models": [],
        "free_quota": {"daily_limit": 0, "used": 0, "reserved": 0, "remaining": 0},
    }
