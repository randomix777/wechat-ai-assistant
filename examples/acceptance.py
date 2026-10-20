"""一键验收：把"这一版能不能交付"压成一条命令。

    .venv\\Scripts\\python.exe -X utf8 examples\\acceptance.py
    .venv\\Scripts\\python.exe -X utf8 examples\\acceptance.py --with-doc-facts

## 为什么要有这个

仓库里已经有四类检查，但它们各管一摊、各自退出、各自打印：

* ``pytest tests`` —— 行为对不对；
* ``examples/check_hygiene.py`` —— 有没有死导入/死常量；
* ``examples/guard_map.py`` —— 商户看得见的症状有没有断言护着；
* ``launcher.py --check`` —— 客户端在这台机器上能不能起来。

问题是**没人会把四条命令都跑一遍**，于是"我这边是绿的"往往只代表第一条。
这个脚本把四条合成一次，外加一条**界面契约冒烟**（起一个临时服务，读
``/wizard`` 与 ``/api/state``），最后给一张表和**一个退出码**。

## 它不做什么

* 不碰真微信、不联网、不发送任何消息；
* 不读密钥、不打印任何凭据；
* ``--with-doc-facts`` 才会跑 ``check_doc_facts.py`` —— 那个脚本内部会再跑一遍
  全套测试（为了数出真实用例数），所以默认关掉，免得跑两遍。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _bootstrap() -> None:
    """和 ``conftest.py`` / ``launcher.bootstrap_paths`` 同一口径。"""
    candidates = [ROOT / "src", ROOT / "examples"]
    packages = ROOT / "packages"
    if packages.is_dir():
        candidates.extend(sorted(child / "src" for child in packages.iterdir() if child.is_dir()))
    for path in candidates:
        text = str(path)
        if path.is_dir() and text not in sys.path:
            sys.path.insert(0, text)


_bootstrap()

PY = sys.executable
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
#: 跑子进程用哪个解释器：优先仓库自带的 venv（依赖都装在那里）。
PYTHON = str(VENV_PY) if VENV_PY.is_file() else PY


class Result:
    """一条检查的结果。``detail`` 是给人看的一句话（成功也写，别只留个对勾）。"""

    def __init__(self, name: str, ok: bool, detail: str, seconds: float = 0.0):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.seconds = seconds


#: 每项检查的进度行文案。别用 docstring 的第一行 —— 那是写给维护者的，
#: 验收时要看的是"正在验什么"。
LABELS = {
    "check_tests": "跑全套回归测试（这一条才证明行为还对）",
    "check_hygiene": "静态卫生：死导入 / 死常量",
    "check_guard_map": "症状护栏：商户看得见的问题有没有断言护着",
    "check_client": "客户端自检：这台机器能不能起来",
    "check_doc_facts": "文档事实：写死的数字/说法还成立吗（会再跑一遍测试）",
    "check_ui_contract": "界面契约冒烟：起临时服务读 /wizard 与 /api/state",
}


def _run(args: list[str], timeout: float = 2400) -> tuple[int, str]:
    """跑一个子进程，拿 (退出码, stdout+stderr)。超时算失败但**不抛**。"""
    command = [PYTHON, *args]
    try:
        done = subprocess.run(command, cwd=str(ROOT), capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{int(timeout)} 秒）"
    except OSError as exc:
        return 127, f"起不来：{exc}"
    text = (done.stdout or b"").decode("utf-8", "replace")
    text += (done.stderr or b"").decode("utf-8", "replace")
    return int(done.returncode), text


def check_tests() -> Result:
    """全套回归。**这是唯一能证明"行为还对"的一条。**"""
    import time

    started = time.monotonic()
    code, output = _run(["-m", "pytest", "tests", "-q", "--tb=line"])
    seconds = time.monotonic() - started
    tail = [line for line in output.splitlines() if "passed" in line or "failed" in line]
    summary = tail[-1].strip() if tail else (output.strip().splitlines() or ["（没有输出）"])[-1]
    ok = code == 0 and " failed" not in summary and " error" not in summary
    return Result("回归测试", ok, summary, seconds)


def check_hygiene() -> Result:
    """死导入 / 死常量。要求脚本最后一句是 ``clean``。"""
    import time

    started = time.monotonic()
    code, output = _run(["examples/check_hygiene.py"])
    seconds = time.monotonic() - started
    last = (output.strip().splitlines() or [""])[-1].strip()
    return Result("静态卫生", code == 0 and last == "clean", last or "（没有输出）", seconds)


def check_guard_map() -> Result:
    """商户看得见的症状 → 有没有断言护着。"""
    import time

    started = time.monotonic()
    code, output = _run(["examples/guard_map.py"])
    seconds = time.monotonic() - started
    match = re.search(r"共 (\d+) 条症状，已护住 (\d+) 条", output)
    if not match:
        return Result("症状护栏", False, "输出里没有统计行", seconds)
    total, covered = int(match.group(1)), int(match.group(2))
    ok = code == 0 and total == covered
    return Result("症状护栏", ok, f"已护住 {covered}/{total}", seconds)


def check_doc_facts() -> Result:
    """文档里写死的数字/说法是否还成立（**它会再跑一遍全套测试**）。"""
    import time

    started = time.monotonic()
    code, output = _run(["examples/check_doc_facts.py"])
    seconds = time.monotonic() - started
    ok = code == 0 and re.search(r"^OK", output, re.MULTILINE) is not None
    return Result("文档事实", ok, "通过" if ok else "有事实过期", seconds)


def check_client() -> Result:
    """这台机器上客户端能不能起来（只读自检，不启动窗口）。"""
    import time

    started = time.monotonic()
    code, output = _run(["src/wechat_ai_assistant/launcher.py", "--check"], timeout=120)
    seconds = time.monotonic() - started
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    info = {}
    for line in lines:
        if "：" in line:
            key, _, value = line.partition("：")
            info[key.strip()] = value.strip()
    ok = code == 0 and "本机界面" in info and "数据目录" in info
    detail = "；".join(f"{key}={value}" for key, value in info.items()) or "（没有输出）"
    return Result("客户端自检", ok, detail, seconds)


def check_ui_contract() -> Result:
    """界面契约冒烟：起一个临时服务，读 ``/wizard`` 和 ``/api/state``。

    只断言**数据契约**（步骤、门禁、角色、报告入口），不比界面文案 ——
    文案改动不该让验收变红（见 AGENTS.md 的"界面测试"一节）。

    全程用模拟数据：不碰真微信、不联网、不写仓库目录。
    """
    import time

    started = time.monotonic()
    tmp = tempfile.TemporaryDirectory(prefix="pyw-accept-")
    server = None
    try:
        from wechat_ai_assistant import web_dev
        from wechat_ai_assistant.application import AppEvents, MerchantApplication
        from wechat_ai_assistant.conversations import ConversationKind, SimulatedConversations
        from wechat_ai_assistant.history_read import SimulatedHistory
        from wechat_ai_assistant.merchant_app import ensure_local_store
        from wechat_ai_assistant.wechat_state import SimulatedWeChat, WeChatState
        from wechat_ai_assistant.wizard import WizardStep

        data_dir = Path(tmp.name)
        store, tenant = ensure_local_store(data_dir / "orders.db")
        app = MerchantApplication(
            data_dir=data_dir, orders=store, tenant=tenant,
            probe=SimulatedWeChat([WeChatState.LOGGED_IN]),
            conversation_source=SimulatedConversations(
                [("验收群", ConversationKind.GROUP, 8)]),
            history_reader_fn=SimulatedHistory(default_count=0),
            events=AppEvents(), simulated=True)
        server = web_dev.create_server(app, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://%s:%d" % server.server_address[:2]

        with urllib.request.urlopen(base + "/wizard", timeout=20) as response:
            page = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/api/state", timeout=20) as response:
            state = json.loads(response.read().decode("utf-8"))

        problems: list[str] = []
        if "__DATADIR__" in page or "__VERSION__" in page:
            problems.append("页面占位符没被替换")
        if "正在加载" in page and len(page) < 2000:
            problems.append("页面没渲染出来")
        if not state.get("steps"):
            problems.append("步骤条是空的")
        if state.get("step") not in list(WizardStep.ORDER) + [WizardStep.LOGIN, WizardStep.DONE]:
            problems.append(f"未知步骤：{state.get('step')}")
        roles = state.get("roles") or []
        if len(roles) < 3:
            problems.append(f"角色只有 {len(roles)} 个")
        if any(not role.get("uses_model") for role in roles):
            problems.append("有角色没标 uses_model")
        if state.get("product_gate_open"):
            problems.append("没登录却放行了")
        if "role-report" not in page:
            problems.append("页面没有报告入口")
        current = [row for row in state["steps"] if row.get("number") == 0]
        if not current:
            problems.append("步骤条缺少编号 0")
        app.close()
        store.close()
        seconds = time.monotonic() - started
        if problems:
            return Result("界面契约", False, "；".join(problems), seconds)
        return Result("界面契约", True,
                      f"{len(state['steps'])} 步 · {len(roles)} 个角色 · 当前 {state['step']}",
                      seconds)
    except Exception as exc:  # 起不来也要给一句话，不是一串 traceback
        return Result("界面契约", False, f"{type(exc).__name__}: {exc}",
                      time.monotonic() - started)
    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        tmp.cleanup()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="acceptance", description="一键验收：测试 + 静态检查 + 护栏 + 客户端 + 界面契约")
    parser.add_argument("--with-doc-facts", action="store_true",
                        help="同时跑 check_doc_facts（它会再跑一遍全套测试，慢）")
    parser.add_argument("--skip-tests", action="store_true",
                        help="跳过全套回归（只想快速看其它几项时用）")
    args = parser.parse_args(argv)

    checks = []
    if not args.skip_tests:
        checks.append(check_tests)
    checks.extend([check_hygiene, check_guard_map, check_client, check_ui_contract])
    if args.with_doc_facts:
        checks.append(check_doc_facts)

    print("=" * 74)
    print("微信AI智能助理 · 验收")
    print(f"仓库：{ROOT}")
    print(f"解释器：{PYTHON}")
    print("=" * 74)
    results: list[Result] = []
    for check in checks:
        print(f"… {LABELS.get(check.__name__, check.__name__)}")
        result = check()
        results.append(result)
        mark = "通过" if result.ok else "失败"
        print(f"  [{mark}] {result.name}　{result.detail}　（{result.seconds:.1f}s）")

    print("-" * 74)
    for result in results:
        print(f"  {'✓' if result.ok else '✗'}  {result.name:<8} {result.detail}")
    failed = [result for result in results if not result.ok]
    print("=" * 74)
    if failed:
        print(f"验收不通过：{len(failed)} 项失败 —— "
              + "、".join(result.name for result in failed))
        print("这些是**这一版不能交付**的理由，逐条查完再跑一次。")
        return 1
    print(f"验收通过：{len(results)} 项全绿。")
    print("注意：这只证明代码与界面契约是对的，**不证明**真微信/真模型上的效果"
          "（那要按 docs 里的真机验收清单另跑）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
