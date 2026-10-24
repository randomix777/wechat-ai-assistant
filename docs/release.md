# 发版流程：出包 → 传 JYI 工具中心 → 客户端静默更新

客户端的自动更新**先问 JYI 工具中心**（`https://jyisolutions.com/api/tools/wechat-ai-assistant`，
见 `src/wechat_ai_assistant/updater.py` 的 `_check_jyi`），问不到才回落到 GitHub Releases。
两条路下到的包都要过同一套签名校验，所以换下载入口不等于放宽信任。

约定的每一条都必须做到，否则客户端会静默跳过（不报错、不安装）。

## 1. 改版本号（必须一致）

* `src/wechat_ai_assistant/version.py` 的 `__version__`
* `src/pyproject.toml` 的 `version`（以及 `src/setup.py`，如果还在用）

## 2. 打包（免安装便携版，onedir）

```powershell
.\.venv\Scripts\pyinstaller.exe deploy\merchant.spec
Copy-Item deploy\portable.flag dist\威信AI智能助理\portable.flag -Force
Copy-Item deploy\portable-readme.txt dist\威信AI智能助理\先看我-免安装版.txt -Force
Copy-Item deploy\启动威信AI智能助理.bat dist\威信AI智能助理\启动威信AI智能助理.bat -Force
# 界面打不开时的诊断入口（跑 exe --diagnose，把报告用记事本打开让商户发回来）
Copy-Item deploy\诊断-发给客服.bat dist\威信AI智能助理\诊断-发给客服.bat -Force
```

产物：`dist/威信AI智能助理/`（启动脚本、中文说明、`威信AI智能助理.exe`、`_internal/` 和便携标记）。
**不要发 onefile**：pywinauto/pywin32 的原生 DLL 在 onefile 下每次启动
都要解包，又慢又容易被杀软误报（见 `deploy/merchant.spec` 头注）。

客户端窗口由 pywebview 承载，Windows 渲染器固定为 WebView2（Chromium）。
便携包包含 pywebview 和 Python/.NET 桥接依赖；目标电脑还需要 Microsoft Edge
WebView2 Runtime。Windows 11 通常已预装；较旧 Windows 设备缺少 Runtime 时：

0. **开窗之前先装**（`web_window.ensure_webview_runtime`）。Electron 是自带内核、
   Tauri 是安装器先装 WebView2 —— 我们只能在启动时补这一步，但顺序必须是"先装再开窗"，
   否则第一轮永远是降级体验。装不上才退兜底。
1. 客户端**现场下载**微软官方的引导安装包并静默安装（`/silent /install`，
   约 1.8 MB + 运行时约 100 MB，需要联网）。下载经过三重把关：官方跳转地址
   （HTTPS）、大小与 PE 头像不像安装程序、Authenticode 签名是否可信
   （`web_window.download_webview2_installer` / `signature_is_trusted`）；
   装完再查注册表确认，**安装器的退出码不算数**（断网时它也会"跑完"）。
   **普通权限装不上就再走一次 UAC 提权**（`_run_installer_elevated`）：写机器级目录
   必须有管理员权限，这正是"做了依赖却没装上"的常见原因。
2. 同一个进程里装完不会立刻生效（pywebview 的渲染器在导入时就定了），所以这一轮
   仍然**先用系统 Edge `--app` 把界面开出来**，界面顶栏写明这件事；下次启动回到内嵌窗口。
3. 连 Edge 都没有才退到默认浏览器（那条路会一直挂着进程，日志里写明）。

三条路全断才会弹「客户端窗口启动失败」。不会退回旧版 MSHTML —— 那是 IE 兼容模式，
界面会花。

> **兜底窗口的两个硬规矩**（线上事故换来的，别改回去）：
> ① `--user-data-dir` 每次启动都不同（`wechat-ai-assistant-ui-<pid>`）。用固定目录时，
> 上一次的 Edge 还占着它，新启动的 `msedge.exe` 会把窗口交给已在跑的浏览器进程然后
> **立刻退出**（实测 0.3 秒），我们等的是这个启动器，于是误判"窗口关了"把服务一关 ——
> 商户看到的就是「127.0.0.1 页面不可用」。
> ② 启动器退出 ≠ 窗口关了：要接着看"我们的 profile 还有没有浏览器在用"；连一个都
> 找不到时**宁可继续挂着**（日志写明），也不能把服务关掉。
> ③ 开窗之前先 `wait_until_serving`，服务不应答就不开窗，直接报错（带日志路径）。
>
> **本机回环一律不走代理。** 装了 Clash / 公司代理的机器上，`urllib` 和浏览器都会把
> `127.0.0.1` 送进代理（`proxy_bypass('127.0.0.1')` 默认是 False），表现为"页面不可用"，
> 而命令行 curl 常常自己跳过回环、看起来一切正常。所以：探测用 `loopback_opener()`；
> Edge 兜底加 `--no-proxy-server`；内嵌走 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--no-proxy-server`。

**便携包里不带安装包**（`deploy/merchant.spec` 只管图标）。刻意如此：多一个二进制
不如让缺组件的那台机器自己取。装在**没有外网**的客户机器上时，请手工安装一次
Runtime（管理员权限，装一次永久有效）：[Evergreen WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)，
或取离线独立包（约 130 MB）`https://go.microsoft.com/fwlink/?linkid=2124701` 双击安装。

用户右键 ZIP 选择“全部解压”，再双击“启动威信AI智能助理.bat”，不需要安装程序。
**直接双击 `威信AI智能助理.exe` 也一样**（数据目录按 exe 自己的位置算，与工作目录无关；
升级流程也是直接起 exe，见 `updater.build_updater_script`）。bat 只剩两个作用：把工作目录
切到程序目录、透传参数（`--page dev` 之类）。所以它**不是**唯一入口，删掉也不会影响启动 ——
只是商户手册和客服话术都指着它，暂时留着更省事。
启动脚本里**必须**用 `start "" "威信AI智能助理.exe"`，不能直接写 `"威信AI智能助理.exe"`：
后者会让 cmd 一直等窗口版 exe 退出，黑框从头挂到尾（exe 本身已经是 `console=False`）。
.bat **必须是 CRLF 换行**（`.gitattributes` 里 `*.bat eol=crlf` 钉住了）：cmd 读 LF 结尾的
批处理时，`chcp 65001` 之后的含中文行会被截断成"命令"，exe 名正好是中文，启动直接失败。
便携版的数据保存在同级 `data/`，复制整个文件夹即可备份或迁移。自动更新会保留该目录。
请解压到普通可写目录；Windows 加密的登录凭证换电脑后需要重新登录。

### 图标：不要手改位图

`deploy/assets/app.ico`（多尺寸）与 `app.png` 由 `deploy/build_icon.py` **自己画**：
按 4 倍超采样出图再降采样，所以 Windows 真正会用的 16/24/32 px 不糊；
配色取自界面本体（`#0e9f6e` 品牌绿、`#1b2430` 深墨），和界面是同一套颜色。
`deploy/merchant.spec` 里 `icon=` 把它写进 exe 资源（任务栏 / Alt-Tab / 文件图标），
`datas` 把它随包分发，运行时的窗口图标由 `app_icon.set_window_icon` 读（更新闪屏还在用它）。

```powershell
.\.venv\Scripts\python.exe deploy\build_icon.py             # 出三个方向 + 桌面尺寸预览
.\.venv\Scripts\python.exe deploy\build_icon.py --final b   # 定稿方向 b 并导出 ico/png
```

方向在 `build_icon.py` 的 `DIRECTIONS` 里，加一个函数就是一个新方向。

发版前冒烟：双击 `dist/威信AI智能助理/威信AI智能助理.exe`，
走一遍"登录 → 选群 → 读历史"，**顶栏那个版本号**（`v1.11.0` 这种）和这次发的号对得上才算数。
（版本号由 `version.py` 一处给出，`/api/state` 的 `version` 字段带到界面顶栏。）

> **冒烟必须看界面真的出来，不能只看 `/api/state` 通。** 页面模块是按名字动态加载的
> （`web_server._PAGE_MODULES` → `import_module`），PyInstaller 静态分析看不见它们 ——
> 1.11.0～1.11.7 的包里就漏了这两个模块：`/api/state` 正常，但 `/wizard` 抛
> `ModuleNotFoundError`，HTTP 处理器异常退出、连接直接关闭，客户看到的是
> `ERR_EMPTY_RESPONSE`（"empty response"）。源码运行完全正常，所以这个问题躲了好几版。
> `tests/test_packaging_spec.py` 现在会静态核对 hiddenimports，别绕过它。

## 3. 生成便携 ZIP（文件名是约定，错一个字客户端就认不出）

```powershell
$ver = "1.9.9"  # 与 version.py 一致，去掉开头的 v
Compress-Archive -Path "dist\威信AI智能助理" `
  -DestinationPath "wx-ai-assistant-win64-$ver.zip" -Force
(Get-FileHash "wx-ai-assistant-win64-$ver.zip" -Algorithm SHA256).Hash.ToLower() `
  | Out-File "wx-ai-assistant-win64-$ver.zip.sha256" -NoNewline -Encoding ascii
```

* zip 里**只有** `威信AI智能助理/` 这一层目录（多套一层也行，客户端只扒一层）。
* `.sha256` 文件内容是单个文件的十六进制哈希（可带文件名后缀，取第一段）。
* `.sha256` 现在是给**老客户端**留的（1.9.9 及以前只认它）；新客户端认签名清单，
  见下一节。两个都传，谁都别落。

## 4. 签名（信任根：新客户端只认签过名的清单）

```powershell
# 只在做一次，最好在一台离线机器上：生成发布密钥（私钥写在仓库外，不入库、不进包）
.\.venv\Scripts\python.exe deploy\sign_release.py new-key
# 把打印出来的 PUBLISHER_PUBKEY = "…" 粘进 src/wechat_ai_assistant/update_signature.py

# 每次发版：签安装包，产出 dist\latest.json + dist\latest.json.sig
.\.venv\Scripts\python.exe deploy\sign_release.py sign "wx-ai-assistant-win64-$ver.zip" `
  --version $ver --notes "修了…" --out dist

# 发布前自查：拿客户端钉的那把公钥验一遍，顺便核对 zip 哈希
.\.venv\Scripts\python.exe deploy\sign_release.py verify dist\latest.json `
  --zip "wx-ai-assistant-win64-$ver.zip"
```

* 私钥默认在 `%USERPROFILE%\.wechat-ai-assistant\publisher-key.json`。`new-key --force` 换钥匙后
  **所有已发出的客户端从此拒收更新**，要换就跟着发一次装机包。
* 签名/验签都走 Windows 自带的 CNG（`bcrypt.dll`）做 ECDSA P-256，不引入新依赖。
* 清单 `latest.json` 里有 `version` / `zip_url` / `sha256` / `notes` / `released_at`。
  `zip_url` 默认就是工具中心里这个工具的**稳定下载地址**（不带版本号），客户端**先验清单签名**，
  信不过就整个跳过（不下载、不安装），验过了再按清单里的哈希校验下到的包。
* 缺清单、签名不对、清单版本对不上（GitHub 那条路会拿 tag 比，工具中心那条路拿工具当前版本比）
  → 一律拒绝更新。**所以漏了这一步，新客户端收不到这次更新**（老客户端仍然照 `.sha256` 走）。
* 想临时改渠道（测试用）：`WECHAT_AI_TOOLS_BASE` 换域名、`WECHAT_AI_TOOL_SLUG` 换工具；
  把 `WECHAT_AI_TOOLS_BASE` 设成空串就跳过工具中心、只走 GitHub。

### 4.1 私钥只存在 worker 里（在线签名服务）

不想让私钥留在发版机器上时，把它交给 AI 平台 worker（仓库根目录的
`worker/release-sign.ts` 由 `apps/ai-platform` 挂载，两个 secret：`RELEASE_SIGN_JWK` = 私钥的 JWK，
`RELEASE_SIGN_TOKEN` = 进门口令）。签名路由挂在 `ai.jyisolutions.com`，由该 worker
提供 `POST /api/internal/release/sign`；客户端只拿内置公钥验签，私钥不进安装包。

```powershell
# 一次性：把私钥转成 WebCrypto 的 JWK 喂给平台 worker（在拿到私钥的那台机器上跑一次）
.\.venv\Scripts\python.exe deploy\sign_release.py export-jwk |
  npx wrangler secret put RELEASE_SIGN_JWK --config apps\platform\wrangler.jsonc

# 每次发版：清单仍在本机生成，只把清单字节发给签名路由，签名拿回来本机先验一遍
$env:RELEASE_SIGN_TOKEN = "<自己生成的长随机串>"
.\.venv\Scripts\python.exe deploy\sign_release.py sign "wx-ai-assistant-win64-$ver.zip" `
  --version $ver --remote https://ai.jyisolutions.com/api/internal/release/sign
```

* 信任根没变：客户端认的还是源码里钉死的 `PUBLISHER_PUBKEY`。签名服务自称的公钥与它不一致、
  或签名在本机验不过，`sign` 立刻中止且**什么都不落盘** —— 在线服务被换掉也发不出坏包。
* `export-jwk` 的标准输出**就是私钥**：别重定向进日志、别贴进聊天、别提交。
* worker 只回 `{"signature": "<base64 的 raw r||s>", "public_key": "<128 位 hex>"}`。
* 两端算法一致性已做过互通性自检：Node/WebCrypto 签 → Python/CNG 验通过（ECDSA P-256 +
  SHA-256 + raw `r||s`）。

## 5. 发布到 JYI 工具中心（主渠道）

工具中心是主渠道：国内直连、没有匿名限流，而且清单和签名跟包放在同一个地方。
工具可以保留「需授权」。桌面客户端用现有 JYI 连接凭据向 `ai.jyisolutions.com`
换取仅用于工具中心的 120 秒短时 Bearer token；原始设备令牌不会发给工具中心。
工具中心再把已验证邮箱映射到 JYI 用户，并对每次更新检查和下载执行现有审批授权校验。

1. 管理后台 → 工具 → 微信 AI 助手 → 上传新版本：填版本号、上传 `wx-ai-assistant-win64-<版本>.zip`；
2. 同一版的两个字段里粘上签名（就是 `sign` 命令产出的那两个文件的内容）：
   * **签名清单** = `dist\latest.json`
   * **签名** = `dist\latest.json.sig`
3. 保留「需授权」，确认已授权账号能使用客户端更新，并确认 `downloadsPaused` 是关的。

客户端只看**清单里**的 `zip_url`/`sha256`：清单验过就用清单里的下载地址，清单里的哈希对不上
就当作没更新（工具中心换版不用重签，地址是稳定的；但**每次换包都必须重签一次清单**，
否则哈希对不上，客户端会一直跳过这一版）。

### 5.1 GitHub Release（过渡期，给老客户端）

1.9.9 及以前的客户端只认 GitHub，所以过渡期仍要发一次：

1. `git tag v<版本>` 并推送；
2. 在 GitHub 建 Release（tag 同名），上传 `.zip`、`.zip.sha256`、`latest.json`、`latest.json.sig`；
3. Release 正文写人话（修了什么、要不要立即更新）。

仓库默认查 `randomix777/wx-ai-assistant` 的 releases，换地方发版要同步改
`src/wechat_ai_assistant/updater.py` 的 `DEFAULT_REPO`。
清单里没有 `notes` 时，客户端就用工具中心那一版的发布说明（再退到 GitHub 正文）。

## 6. 生效与回滚

* 检查更新在**界面起来之后**才在后台做（不再卡启动）：有新版就安静下载、校验、暂存到
  `_updates/`，顶栏提示「新版 vX 已就绪，下次打开自动更新」；换版发生在**下一次启动**。
* 无新版、网络不可用、工具中心或 GitHub 限流、下载或校验失败 → 照常打开当前版本，不打扰用户。
* 换版流程：等旧进程退出 → 新版目录搬进来 → 起新版等 15 秒还活着就算成（删备份）→
  起不来就整块换回旧版。便携 `data/` 会并进新版（按内容合并，不会嵌成 `data\data`）。
* 换版脚本每一步都写日志到 `%TEMP%\pyw-apply-<版本>.log`，商户机器上没换成功就看它；
  出大问题：把工具中心那一版下线（或删掉 Release / 把工具设成暂停下载），客户端查不到就地不动；
  已暂存但还没应用的，让商户删掉 `_updates/` 和 `pending.json` 即可。

## 7. 换版演练（改过换版脚本就必须跑一次）

换版脚本是 cmd 批处理，跑在软件已经退出、目录可能还被占着的那个时间窗里；
单元测试只能钉住脚本文本和用 `ping.exe` 冒充主程序的真跑用例，环境问题得靠真机演练：

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean deploy\merchant.spec
pwsh -File deploy\rehearse_update.ps1 success    # 新版能起来：应该换过去、删备份、便携数据保留
pwsh -File deploy\rehearse_update.ps1 rollback   # 新版起不来：应该换回旧版、留记号、数据不丢
```

它在 `%TEMP%\pyw-real-rehearsal` 里造一份旧版（带便携数据）和一份暂存的"新版"，
启动真 exe 让启动器自己拉脚本，最后打印结果表和换版日志。这两个用例都过，才敢发版。

* 起不来的版本会在安装目录留 `blocked-<版本>.txt`，后台查更新从此跳过它 ——
  不会被同一版反复下载、反复失败；装了新版就随目录一起没了。
* 脚本每个分支都会删掉 `pending.json`（否则重启后又会换一遍，起不来就成了死循环）。

换版踩过的三个坑，别改回去：`timeout.exe` 在标准输入被重定向时直接退出（等待会消失，
改用 `ping -n`）；`ren` 给运行中的 exe 改名会成功（不能当存活判据，改用 `tasklist` 按 PID）；
PATH 里的 `find.exe` 可能是 MSYS 的（会跑去全盘搜文件，脚本里一律用 `%SystemRoot%\System32` 绝对路径）。
