"""商家本地软件：**装配** + 自检。

    python -m wechat_ai_assistant.merchant_app --check    # 只做自检，不开界面

界面入口是 :mod:`wechat_ai_assistant.web_app`（本机 HTTP + 应用窗口），
双击走的是 ``launcher`` → ``web_app``。本模块保留两件事：

* :func:`build_runtime` —— 两个入口共用的一套装配（日志、本地库、凭据、队列、应用）；
* ``--check`` —— 排障用的一行行中文自检。

以前这里还带着 Tk 界面（``merchant_ui.MerchantWindow``）。界面统一到 Web 之后
把它删了：留着就是"每改一次 UI 要改两处"，而两处的措辞一定会漂。
商户不需要命令行、不需要环境变量、不需要填群名、不需要填 API Key —— 全部在界面上点。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path
from typing import Callable

from ._packages import ensure_packages_on_path  # noqa: E402
from . import diagnostics  # noqa: E402
from .paths import default_data_dir as _default_data_dir  # noqa: E402

ensure_packages_on_path()

from .application import MerchantApplication  # noqa: E402
from .client_login import (  # noqa: E402
    CredentialStore,
    DeviceLoginFlow,
    MerchantClient,
    PendingQueue,
    Session,
    restore_session,
)
from .conversations import RealConversationSource  # noqa: E402
from .local_parse import LocalParseService  # noqa: E402
from .jyi_client import JYI_GATEWAY, JYIClient, create_merchant_client  # noqa: E402
from .orders import OrderStore  # noqa: E402
from .wechat_state import RealWeChatLauncher, RealWeChatProbe  # noqa: E402

#: 默认连接 JYI 生产网关；开发环境仍可通过 --gateway 覆盖。
DEFAULT_GATEWAY = JYI_GATEWAY


def default_data_dir() -> Path:
    """Use the shared product path resolver, including the old data directory."""
    return _default_data_dir()


def default_device_name() -> str:
    try:
        return socket.gethostname() or "本机电脑"
    except OSError:  # pragma: no cover - platform dependent
        return "本机电脑"


def ensure_local_store(path: Path) -> tuple[OrderStore, str]:
    """Open (or create) the merchant's local order database.

    The merchant's orders, customers, memory and review queue live here and only
    here. Losing the gateway does not lose any of it.
    """
    store = OrderStore(str(path))
    row = store.db.execute("SELECT id FROM merchants ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        created = store.create_merchant(os.environ.get("WECHAT_AI_MERCHANT_NAME") or "我的店铺", plan="demo", ai_quota=0)
        return store, created["merchant_id"]
    return store, str(row["id"])


def build_login_runner(client: MerchantClient, credentials: CredentialStore, device_name: str) -> Callable[[Callable[[str, str], None]], Session]:
    def run(progress: Callable[[str, str], None]) -> Session:
        if isinstance(client, JYIClient):
            return client.login(credentials, device_name, progress)
        flow = DeviceLoginFlow(
            client=client,
            credentials=credentials,
            device_name=device_name,
            on_progress=lambda p: progress(p.status, p.detail),
        )
        return flow.run()

    return run


def build_service_factory(
    orders: OrderStore, tenant: str, gateway_url: str, queue: PendingQueue
) -> Callable[[Session], LocalParseService]:
    """Build the local parse service for a logged-in session.

    ``group`` is deliberately absent: which conversation to work on is decided in
    the interface, not on the command line.
    """

    def factory(session: Session) -> LocalParseService:
        from .jyi_client import JYIClient
        from .jyi_parse import JYIParseService

        client = create_merchant_client(gateway_url, session=session)
        if isinstance(client, JYIClient):
            # JYI 网关没有任务队列：直调 chat 接口，提示词与校验在本地。
            return JYIParseService(
                orders=orders,
                client=client,
                queue=queue,
                tenant=tenant,
            )
        return LocalParseService(
            orders=orders,
            client=client,
            queue=queue,
            tenant=tenant,
        )

    return factory


def build_application(
    *,
    data_dir: Path,
    orders: OrderStore,
    tenant: str,
    gateway_url: str,
    queue: PendingQueue,
    credentials: CredentialStore,
    device_name: str,
) -> MerchantApplication:
    """Assemble the whole application. Shared by the window and the tests."""
    app = MerchantApplication(
        data_dir=data_dir,
        orders=orders,
        tenant=tenant,
        probe=RealWeChatProbe(),
        launcher=RealWeChatLauncher(),
        conversation_source=RealConversationSource(ui_lock=None),
        history_reader_fn=None,
        gateway_url=gateway_url,
    )
    # The UI queue is created by the application; wire the conversation source to
    # it now that it exists so history reading and listing serialise.
    # **必须走 replace_conversation_source**：直接赋值会把 __init__ 里挂好的
    # 解密库（wcdb_fn）丢掉，界面就永远只走内存扫描 —— 真名真人数全没有。
    app.replace_conversation_source(RealConversationSource(ui_lock=app.ui_lock))
    app.history._ui_lock = app.ui_lock

    client = create_merchant_client(gateway_url, session=restore_session(credentials))
    service_factory = build_service_factory(orders, tenant, gateway_url, queue)
    app.login_runner = build_login_runner(client, credentials, device_name)  # type: ignore[attr-defined]
    app.pending_queue = queue  # type: ignore[attr-defined]
    app.credentials = credentials  # type: ignore[attr-defined]

    def attach_session(session: Session) -> None:
        client.session = session
        app.attach(session, client, service_factory(session))

    app.attach_session = attach_session  # type: ignore[attr-defined]
    app._pending_attach = attach_session  # type: ignore[attr-defined]
    if client.session is not None:
        attach_session(client.session)
    return app


def _force_utf8_stdout() -> None:
    """Windows 控制台默认不是 UTF-8。

    ``--check`` 和启动过程都要打印中文，不重配会抛 UnicodeEncodeError ——
    商户看到的是"程序崩溃了"，而不是缺什么、怎么办。重配放在 ``main`` 里而不是
    ``__main__`` 块里：被别的入口直接调用 ``main()`` 时同样需要它。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _self_check(app: MerchantApplication, data_dir: Path, device_name: str) -> int:
    print(f"数据目录：{data_dir}")
    print(f"本机名称：{device_name}")
    print(f"本地订单库：已就绪（{app.tenant}）")
    print(f"微信状态：{app.monitor.check_once().state}")
    print(f"已登录产品账号：{'是' if app.session is not None else '否'}")
    print(f"当前模式：{app.auto.label()}（{app.auto.describe()}）")
    print("本地订单查看、人工修改和导出不需要网络。")
    return 0


def build_runtime(data_dir: Path, *, gateway: str, device_name: str = "") -> tuple[
        MerchantApplication, OrderStore, PendingQueue]:
    """数据目录之后的**同一套装配**：诊断日志、本地库、凭据、待发队列、应用本体。

    Tk 入口（:func:`main`）和 Web 入口（``web_app``）都调这里。
    两处各写一遍的代价是真实的：一边装了诊断日志、另一边忘了，于是"Web 版怎么
    没日志"这种问题要查半天。

    返回 ``(app, orders, queue)`` —— 调用方负责在退出时关掉后两个。
    """
    from . import diagnostics

    data_dir.mkdir(parents=True, exist_ok=True)
    diagnostics.install(data_dir)
    diagnostics.quiet_noisy_modules()
    orders, tenant = ensure_local_store(data_dir / "orders.db")
    credentials = CredentialStore(data_dir / "credentials.bin")
    queue = PendingQueue(data_dir / "pending.db")
    app = build_application(
        data_dir=data_dir,
        orders=orders,
        tenant=tenant,
        gateway_url=gateway,
        queue=queue,
        credentials=credentials,
        device_name=device_name or default_device_name(),
    )
    return app, orders, queue


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(prog="wechat-ai-assistant-merchant", description="微信AI智能助理")
    parser.add_argument("--gateway", default=os.environ.get("WECHAT_AI_GATEWAY_URL", DEFAULT_GATEWAY), help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", default="", help=argparse.SUPPRESS)
    parser.add_argument("--device-name", default=os.environ.get("WECHAT_AI_DEVICE_NAME", ""), help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="只做自检，不打开界面")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    device_name = args.device_name or default_device_name()
    app, orders, queue = build_runtime(data_dir, gateway=args.gateway, device_name=device_name)
    try:
        return _self_check(app, data_dir, device_name)
    finally:
        app.close()
        orders.close()
        queue.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    _force_utf8_stdout()
    sys.exit(main())
