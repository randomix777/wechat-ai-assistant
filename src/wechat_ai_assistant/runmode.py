"""运行设置（本地）。

## 只有一种模式：后台整理

产品**不会**向微信发送任何消息 —— 读消息、整理、记单、导出，仅此而已。
所以以前那三种模式（后台整理 / 自动接单与回复 / 暂停）只剩下一种，
`RunMode` 和 `AutoOrderSettings.mode` 保留只是为了**兼容老配置**：
老版本 settings.json 里写着 ``auto_take`` 或 ``paused`` 的，读出来一律按
``background`` 处理（读得出来，但不会再发任何东西）。

## 剩下这些设置还有用

合并窗口、金额上限、缺价策略这些是**整理**阶段的行为，与发送无关。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .conversations import Conversation


class RunMode:
    #: 读消息、整理、记单，**不发任何消息**。产品只有这一种模式。
    BACKGROUND = "background"

    ALL = (BACKGROUND,)

    COPY = {
        BACKGROUND: ("后台整理", "读取并分析消息、记单，不发送任何消息。"),
    }


#: 短消息合并窗口：同一客户连续几条短消息合成一单，避免刷屏。
DEFAULT_MERGE_WINDOW_SECONDS = 45.0
DEFAULT_MAX_MERGE_WAIT_SECONDS = 90.0


@dataclass
class AutoOrderSettings:
    """自动记单的可配置项。全部有安全默认值。"""

    #: 恒为 ``background``。字段留着只为读得懂老配置。
    mode: str = RunMode.BACKGROUND
    #: 缺价时是否允许先记单、标价格待定，而不是转人工。
    allow_price_pending: bool = False
    #: 单笔金额上限，超过转人工。None 表示不限制。
    max_order_amount: float | None = 5000.0
    #: 单行数量上限。
    max_line_quantity: float = 200.0
    #: 连续短消息合并窗口。
    merge_window_seconds: float = DEFAULT_MERGE_WINDOW_SECONDS
    #: 合并等待上限。无论客户发多少条，最多等这么久。
    max_merge_wait_seconds: float = DEFAULT_MAX_MERGE_WAIT_SECONDS

    def __post_init__(self) -> None:
        if self.mode not in RunMode.ALL:
            # 老配置（auto_take / paused / 任何未知值）一律落回唯一模式。
            self.mode = RunMode.BACKGROUND
        if self.merge_window_seconds < 0 or self.max_merge_wait_seconds < 0:
            raise ValueError("merge windows must not be negative")
        if self.max_merge_wait_seconds < self.merge_window_seconds:
            # 上限小于窗口是配置错误，会让"最多等这么久"变成空话。
            self.max_merge_wait_seconds = self.merge_window_seconds

    @property
    def recording_allowed(self) -> bool:
        """能不能自动记单。恒为真 —— 产品本来就只做这件事。"""
        return True

    def label(self) -> str:
        return RunMode.COPY[self.mode][0]

    def describe(self) -> str:
        return RunMode.COPY[self.mode][1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allow_price_pending": self.allow_price_pending,
            "max_order_amount": self.max_order_amount,
            "max_line_quantity": self.max_line_quantity,
            "merge_window_seconds": self.merge_window_seconds,
            "max_merge_wait_seconds": self.max_merge_wait_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AutoOrderSettings":
        defaults = cls()
        return cls(
            # 老配置里的 auto_take / paused 在 __post_init__ 里落回 background。
            mode=str(data.get("mode", defaults.mode)),
            allow_price_pending=bool(data.get("allow_price_pending", defaults.allow_price_pending)),
            max_order_amount=(
                None if data.get("max_order_amount") is None else float(data["max_order_amount"])
            ),
            max_line_quantity=float(data.get("max_line_quantity", defaults.max_line_quantity)),
            merge_window_seconds=float(data.get("merge_window_seconds", defaults.merge_window_seconds)),
            max_merge_wait_seconds=float(data.get("max_merge_wait_seconds", defaults.max_merge_wait_seconds)),
        )


@dataclass
class MerchantSettings:
    """跨启动保留的配置。向导走完后不再重走。"""

    account_id: str = ""
    live_conversation: str = ""
    history_conversations: list[str] = field(default_factory=list)
    history_range: str = "last_week"
    history_max_messages: int = 200
    auto: AutoOrderSettings = field(default_factory=AutoOrderSettings)
    #: 向导是否已完成。微信账号切换时会被重置。
    wizard_complete: bool = False
    #: 上次用的模型**产品别名**。换模型只影响新任务，已在跑的不变。
    model_alias: str = ""
    #: 上次用的网关地址。换账号时一并清空。
    gateway_url: str = ""
    #: 上次见到的微信版本号。微信一升级，界面选择器、读取逻辑都可能
    #: 对不上 —— 先记下来，变了就提醒商户重走一次检测，而不是静默读空。
    wechat_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "live_conversation": self.live_conversation,
            "history_conversations": list(self.history_conversations),
            "history_range": self.history_range,
            "history_max_messages": self.history_max_messages,
            "auto": self.auto.to_dict(),
            "wizard_complete": self.wizard_complete,
            "model_alias": self.model_alias,
            "gateway_url": self.gateway_url,
            "wechat_version": self.wechat_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MerchantSettings":
        return cls(
            account_id=str(data.get("account_id", "")),
            live_conversation=str(data.get("live_conversation", "")),
            history_conversations=[str(item) for item in data.get("history_conversations", ())],
            history_range=str(data.get("history_range", "last_week")),
            history_max_messages=int(data.get("history_max_messages", 200)),
            auto=AutoOrderSettings.from_dict(data.get("auto") or {}),
            wizard_complete=bool(data.get("wizard_complete", False)),
            model_alias=str(data.get("model_alias", "")),
            gateway_url=str(data.get("gateway_url", "")),
            wechat_version=str(data.get("wechat_version", "")),
        )

    def reset_for_new_account(self) -> None:
        """微信账号切换时调用。

        账号变了，旧账号的会话绑定、客户记忆和订单一律不能混用。
        模型选择是**账号无关**的产品偏好，刻意保留 —— 换微信账号不等于
        换模型，也不该让商户重新选一次。
        """
        self.account_id = ""
        self.live_conversation = ""
        self.history_conversations = []
        self.wizard_complete = False


class SettingsStore:
    """原子读写的本地配置。

    先写临时文件再替换，避免商户在断电或强制关窗时得到一个半截的配置文件。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def load(self) -> MerchantSettings:
        with self._lock:
            if not self.path.exists():
                return MerchantSettings()
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # 配置坏了不能让软件起不来：回到默认值，让商户重走一次向导。
                return MerchantSettings()
        return MerchantSettings.from_dict(data if isinstance(data, dict) else {})

    def save(self, settings: MerchantSettings) -> None:
        with self._lock:
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(settings.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)


__all__ = [
    "DEFAULT_MAX_MERGE_WAIT_SECONDS",
    "DEFAULT_MERGE_WINDOW_SECONDS",
    "AutoOrderSettings",
    "MerchantSettings",
    "RunMode",
    "SettingsStore",
]
