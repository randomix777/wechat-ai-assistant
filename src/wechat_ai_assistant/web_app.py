"""商户端的 **Web 界面入口**：起本机服务 + 装一个应用窗口。

## 它和别的入口的关系

    launcher.py                双击/EXE 的入口：定数据目录、检查依赖、必要时换版
      └── web_app.main         本模块（本机 HTTP + 应用窗口）—— 商户用的就是它
    merchant_app.build_runtime 装配（日志、本地库、凭据、队列、应用本体）
    merchant_app.main --check  排障用的一行行自检

业务逻辑都在 ``MerchantApplication`` 里，和界面无关 —— 所以换界面不动它，
而界面只有一份（Web），不再是 Tk + Web 两套并存。

## 窗口宿主

:mod:`wechat_ai_assistant.web_window`：默认使用 pywebview（WebView2）；Edge ``--app``
和默认浏览器只供显式排障使用，WebView2 启动失败时不会自动打开外部窗口。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

from . import web_server
from .web_window import open_window

LOGGER = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    from .merchant_app import (DEFAULT_GATEWAY, build_runtime, default_data_dir,
                              default_device_name)

    parser = argparse.ArgumentParser(prog="wx-ai-assistant-web",
                                     description="威信AI智能助理（Web 界面）")
    parser.add_argument("--port", type=int, default=0,
                        help="本机端口，0 = 让系统挑一个空闲的")
    parser.add_argument("--data-dir", default="", help=argparse.SUPPRESS)
    parser.add_argument("--gateway", default=os.environ.get("WECHAT_AI_GATEWAY_URL", DEFAULT_GATEWAY),
                        help=argparse.SUPPRESS)
    parser.add_argument("--page", choices=("wizard", "dev"), default="wizard",
                        help="开哪个页面：wizard=商户向导页（默认），dev=调试页")
    parser.add_argument("--browser", action="store_true",
                        help="不开应用窗口，用默认浏览器打开（排障用）")
    parser.add_argument("--check", action="store_true", help="只做自检，不开窗口")
    args = parser.parse_args(argv)

    # 已经有一个实例在跑就别再开一个：两个窗口长得一模一样，商户分不清在用哪一版；
    # 业务上更糟 —— 两个实例盯着同一个微信、写同一份订单库。
    # 自检和 `--allow-multiple`（排障/联调）不拦。
    if not args.check:
        from .instance_guard import describe, multiples_allowed, other_instances

        others = [] if multiples_allowed() else other_instances()
        if others:
            detail = describe(others)
            print(detail)
            from .web_window import notify_and_exit

            notify_and_exit(detail)
            return 1

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    app, orders, queue = build_runtime(data_dir, gateway=args.gateway,
                                      device_name=default_device_name())
    server = web_server.create_server(app, port=args.port)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/" + ("" if args.page == "dev" else "wizard")

    if args.check:
        print(f"本机界面：{url}")
        print(f"数据目录：{data_dir}")
        print(f"已登录产品账号：{'是' if app.session is not None else '否'}")
        app.close()
        orders.close()
        queue.close()
        server.server_close()
        return 0

    # 服务跑在后台线程：主线程留给窗口（pywebview 的 GUI loop 要求主线程）。
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # 更新检查放在界面起来之后（启动路径上不联网，双击就该看见窗口）。
    web_server.start_background_update_check(server.web_state, app)
    print(f"界面已启动：{url}")
    # 开窗之前先确认服务真的能应答。开一个连不上服务的窗口，商户看到的是
    # "127.0.0.1 页面不可用" —— 那句话既不像出错，也没告诉他该怎么办。
    from .web_window import wait_until_serving

    if not wait_until_serving(url):
        raise RuntimeError(
            f"本机界面服务没有起来（{url}）。\n"
            "常见原因：本机安全软件拦住了程序监听端口，或系统代理把 127.0.0.1 也接管了。\n"
            "请把本软件加入安全软件白名单后重试；仍然不行请把日志发给客服。"
        )
    try:
        if args.browser:
            import webbrowser

            webbrowser.open(url)
            threading.Event().wait()  # Ctrl+C 退出
        else:
            used = open_window(url)
            print(f"窗口宿主：{used}（关掉窗口即退出）")
            if used != "pywebview":
                # 缺内嵌窗口组件时用的是兜底窗口：**要告诉商户**，否则他会以为
                # 软件坏了（任务栏上是 Edge、图标也不对）。文案如实说装没装得上。
                from .web_window import fallback_notice

                server.web_state.set(notice=fallback_notice(used))
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        orders.close()
        queue.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
