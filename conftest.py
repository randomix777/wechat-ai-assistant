"""pytest 的公共入口：把源码目录挂上 ``sys.path``。

测试里有三种写法都在用：自己插 ``src``、只插 ``packages/*/src``、或者干脆
依赖外部环境变量。结果就是同一条命令在有的人机器上能跑、在 CI 上收不到用例
（``ModuleNotFoundError: No module named 'wechat_ai_assistant'``）。

这里统一做一次，和 ``launcher.bootstrap_paths`` 口径一致：

    .venv\\Scripts\\python.exe -m pytest tests -q

不需要设任何环境变量。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _bootstrap() -> None:
    candidates = [ROOT / "src"]
    packages = ROOT / "packages"
    if packages.is_dir():
        candidates.extend(sorted(child / "src" for child in packages.iterdir() if child.is_dir()))
    for path in candidates:
        text = str(path)
        if path.is_dir() and text not in sys.path:
            sys.path.insert(0, text)


_bootstrap()
