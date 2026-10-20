"""开发用的 Web 界面：CLI + "保存即生效"。

产品形态在 :mod:`wechat_ai_assistant.web_app`（起服务 + 装窗口），
接口与状态在 :mod:`wechat_ai_assistant.web_server`。本模块只做**开发时方便**的三件事：

1. 命令行起服务：``--page wizard --open`` 直接开浏览器、``--window`` 装应用窗口；
2. 页面文件（``web_page.py`` / ``web_wizard_page.py``）保存即生效：按 mtime 重载
   模块，并注入自动刷新脚本（``/api/page-version`` 变了就 ``location.reload()``）；
3. 把 ``web_server`` 的名字转发出来，测试与外部照旧 ``from . import web_dev``。

用 Chrome DevTools 在 Styles 面板里实时改样式（不落盘）**不会**被刷新掉 ——
自动刷新只在页面文件真的变了才触发。调好了把值抄回对应文件的 ``<style>`` 保存。

**产品里不含本模块**：交付走 ``web_app``，那边 ``DEV_RELOAD`` 是关的。
"""
from __future__ import annotations

import argparse
import logging
import os
import threading
from pathlib import Path

from . import web_server
from .web_server import (  # noqa: F401 - 转发：外部与测试直接用这些名字
    DEFAULT_PORT,
    LOG_LINES,
    PAGE_SOURCES,
    WebState,
    attach_events,
    create_server,
    page_html,
    page_mtime_ns,
)

LOGGER = logging.getLogger(__name__)

#: 改页面时浏览器自动刷新。**只在文件真的变了才刷**，所以用 DevTools 手改样式
#: （不落盘）时不会被刷掉。只在开发界面注入，产品包里没有这个模块。
LIVE_RELOAD = """
<script>
(function () {
  let seen = null;
  setInterval(async function () {
    try {
      const r = await fetch("/api/page-version");
      const v = (await r.json()).version;
      if (seen === null) { seen = v; return; }
      if (v !== seen) { location.reload(); }
    } catch (e) { /* 服务重启中，下一次轮询会连上 */ }
  }, 700);
})();
</script>
"""

# 打开开发期行为：页面模块按 mtime 重载 + 注入上面的自动刷新脚本。
web_server.DEV_RELOAD = True
web_server.LIVE_RELOAD = LIVE_RELOAD


def main(argv: list[str] | None = None) -> int:
    from . import diagnostics
    from .merchant_app import (DEFAULT_GATEWAY, build_runtime, default_data_dir,
                              default_device_name)

    parser = argparse.ArgumentParser(prog="wechat-ai-assistant-dev-web",
                                     description="开发调试用的 Web 界面")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--open", action="store_true",
                        help="服务就绪后自动打开浏览器（启动要先取密钥，可能要等几十秒）")
    parser.add_argument("--page", choices=("dev", "wizard"), default="dev",
                        help="--open 打开哪个页面：dev=调试页（默认），wizard=商户版向导页")
    parser.add_argument("--window", action="store_true",
                        help="把页面装进内嵌 WebView2 窗口（失败时显示错误，不打开 Edge），"
                             "窗口关掉服务也跟着停")
    parser.add_argument("--gateway", default=os.environ.get("WECHAT_AI_GATEWAY_URL", DEFAULT_GATEWAY))
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    diagnostics.install(data_dir)  # build_runtime 里还会再装一次，幂等
    app, orders, queue = build_runtime(data_dir, gateway=args.gateway,
                                       device_name=default_device_name())
    server = create_server(app, port=args.port)
    host, port = server.server_address[:2]
    suffix = "" if args.page == "dev" else "wizard"
    url = f"http://{host}:{port}/{suffix}"

    if args.window:
        # 窗口模式下服务要跟窗口同生共死：窗口关掉后
        # 就停服务，不留一个没有窗口的后台进程。
        from .web_window import open_window

        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"应用窗口已启动：{url}（关掉窗口即退出）")
        try:
            used = open_window(url)
            print(f"窗口宿主：{used}")
        finally:
            server.shutdown()
            server.server_close()
            app.close()
            orders.close()
            queue.close()
        return 0

    if args.open:
        import webbrowser

        webbrowser.open(url)
    print(f"开发界面已启动：http://{host}:{port}　（商户版向导页：/{'wizard' if suffix else ''}）")
    print("只允许本机访问。按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        orders.close()
        queue.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
