# 开发须知（给 AI 助手/协作者）

## 环境

* 解释器：`.venv\Scripts\python.exe`（Python 3.14）。依赖已装好，**不要**用系统 Python 跑项目代码。
* 源码在 `src/`（包 `wechat_ai_assistant`）与 `packages/*/src`（`business_agent`、`order_agent` 等）。
  `conftest.py` 会替测试挂好这些路径，所以测试不用设任何环境变量。

## 跑测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

单文件/单用例：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_merchant_flow.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_merchant_flow.py -q -k gate
```

## 交付前验收（一条命令）

```powershell
.\.venv\Scripts\python.exe -X utf8 examples\acceptance.py
# 或者双击仓库根目录的 验收.bat
```

它把**回归测试 + 静态卫生 + 症状护栏 + 客户端自检 + 界面契约冒烟**跑一遍，
给一张表和**一个退出码**（`0` = 可以交付，`1` = 哪几项没过）。

* 只有 `pytest` 那一项能证明"行为还对"，别的都是它的补充 —— 别只跑一项就说绿了。
* `--skip-tests` 跳过全套回归（快速看其它几项）；`--with-doc-facts` 追加文档事实检查
  （那个脚本内部会再跑一遍全套测试，所以默认关掉）。
* 它**不碰真微信、不联网、不读密钥**。真机/真模型上的效果要按
  `docs/merchant-software.md` 第 8 节的验收清单另跑，这个工具不替它背书。

## 跑客户端（产品的样子）

界面是 **Web 那一套**：本机 HTTP + 应用窗口。业务逻辑在 `MerchantApplication`，
和界面无关 —— 换界面不动它。

```powershell
.\.venv\Scripts\python.exe src\wechat_ai_assistant\launcher.py --check   # 只报地址/数据目录/登录状态
.\.venv\Scripts\pythonw.exe src\wechat_ai_assistant\launcher.py          # 无控制台，和商户看到的一样
.\.venv\Scripts\python.exe  src\wechat_ai_assistant\launcher.py          # 带控制台，看日志用
```

发布包的启动脚本是 `deploy\启动微信AI智能助理.bat`（**必须 CRLF**，见 `docs/release.md`）。

**所有 `.bat` 都必须 CRLF**（不只 deploy 里那几个）：cmd 按字节读批处理，LF-only 的
UTF-8 文件遇到中文注释会读错行，把注释当命令执行 —— 屏幕上是一串
`'...' is not recognized as an internal or external command`，真正的命令可能没跑。
`tests/test_source_encoding.py` 会扫根目录和 `deploy/` 拦这个。

## 调界面（浏览器里改，比开窗口快）

```powershell
# 要和仓库根目录的「启动开发Web界面.bat」一样带 PYTHONPATH（那就是这个 bat 干的事）
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -X utf8 -m wechat_ai_assistant.web_dev --page wizard --open
```

* `/` 是开发调试页（所有接口 + 日志）；`/wizard` 是**商户版向导页**（第 0～7 步）。
* 改 `web_page.py` / `web_wizard_page.py` 保存即生效（浏览器自己刷新），不用重启、不用重新打包。
* 两者都吃同一份 `/api/state`，所以界面测试断言**数据契约**就够了 ——
  不要为界面文案去点像素（见 `tests/test_web_wizard_flow.py`）。
* `--window` 把页面装进应用窗口（`web_window.py`：pywebview → Edge `--app` → 浏览器），
  窗口关掉服务跟着停。产品入口 `web_app.py` 就是它（`DEV_RELOAD` 关着）：

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m wechat_ai_assistant.web_app --check   # 只报地址/数据目录/登录状态
.\.venv\Scripts\python.exe -m wechat_ai_assistant.web_app           # 起服务 + 应用窗口
```

分层（改界面前先认清在哪一层）：

| 文件 | 管什么 |
| --- | --- |
| `web_wizard_page.py` / `web_page.py` | 页面（字符串常量，只渲染 `/api/state`） |
| `web_server.py` | 接口、状态、路由（产品） |
| `merchant_text.py` | 商户可见文案的纯逻辑：截断、占位、审计、数据来源 |
| `web_dev.py` | 开发外壳：CLI + 保存即生效（产品里不用） |
| `web_window.py` | 窗口宿主：pywebview → Edge `--app` → 浏览器 |
| `web_app.py` | 产品入口：起服务 + 装窗口 |
| `merchant_app.py` | 装配（`build_runtime`）+ `--check` 自检 |

Tk 界面（`merchant_ui.py` / `ui_theme.py`）已经删了 —— **不要再加第二套界面**：
每改一次 UI 要改两处，而两处的措辞一定会漂。

窗口图标有个坑：Web 应用窗口（Edge `--app` / pywebview）是**别人的进程**，
不主动挂图标就显示 Edge 的。`app_icon.set_window_icon_async(pid=...)` 在窗口出现后
用 `WM_SETICON` + 类图标把 `deploy/assets/app.ico` 挂上去（见 `web_window._open_edge`）。

`tkinter` 只剩**更新闪屏**（`launcher._show_update_splash`，换 exe 那 3 秒）和给它挂图标用；
启动失败弹窗已经换成 Win32 `MessageBoxW`。别的地方再 `import tkinter` 就是在长第二套界面
（`tests/test_launcher_ui.py` 会拦）。

## 改代码时注意

* **流程事实只有一处**：步骤编号、标题、短名都在 `wizard.WizardStep`（见 `STEP_SPECS`）。
  页面只渲染它给的数据（`/api/state` 的 `steps`），**不要在页面里写死「第 N 步　xxx」**。
* **判据只有一处**：能不能进下一步问 `Wizard.advance()`，门禁问 `Wizard.product_gate_open()`，
  页面只显示返回的 `reason`，不自己再判一遍。
* 界面测试尽量断言**数据契约**（`WizardStep`、`/api/state` 的输出）而不是源码里的文案字符串 ——
  文案改动不该让测试变红（历史教训见 `tests/test_merchant_text.py` 的说明）。
* **不许给微信发消息**：产品只读消息、整理、记单、导出。`WeChatAuto`（点"发送"、自动加好友、
  自动回复）**不进制品**，顶层也不再导出它。要加"发消息"能力 = 另一个产品，别混进来。
  三道检查在 `tests/test_read_only_boundary.py`，细节见 `docs/read-only-boundary.md`。
* 发版流程见 `docs/release.md`，不要手改 `deploy/merchant.spec` 里的 `console=False` 与批处理换行。

## 更新那条路（曾经的既有失败，已修）

`tests/test_updater.py` 曾经红 8 条。**原因不是"渠道顺序"**：`updater._check_jyi` 漂成了
「带 Bearer 打 `/api/tools/<slug>/updates`」，而模块文档、那批测试和 `_probe_tools.py`
都按**公开详情** `/api/tools/<slug>?lang=zh` 写。已改回文档说的样子：

* **查新版**只读公开详情，**不带任何凭据**（`check_for_update(current)`）；
* **下载**才把短效 Bearer 挂到工具中心的下载地址上（`download_and_stage(..., tool_access_token=)`）；
* 两条路的判据始终是**签名清单**，工具中心报的版本号/哈希只当线索。

改这条链路前先读 `src/wechat_ai_assistant/updater.py` 开头的模块文档。
