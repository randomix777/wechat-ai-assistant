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
Copy-Item deploy\portable.flag dist\微信AI智能助理\portable.flag -Force
Copy-Item deploy\portable-readme.txt dist\微信AI智能助理\先看我-免安装版.txt -Force
Copy-Item deploy\启动微信AI智能助理.bat dist\微信AI智能助理\启动微信AI智能助理.bat -Force
```

产物：`dist/微信AI智能助理/`（启动脚本、中文说明、`微信AI智能助理.exe`、`_internal/` 和便携标记）。
**不要发 onefile**：pywinauto/pywin32 的原生 DLL 在 onefile 下每次启动
都要解包，又慢又容易被杀软误报（见 `deploy/merchant.spec` 头注）。

用户右键 ZIP 选择“全部解压”，再双击“启动微信AI智能助理.bat”，不需要安装程序。
便携版的数据保存在同级 `data/`，复制整个文件夹即可备份或迁移。自动更新会保留该目录。
请解压到普通可写目录；Windows 加密的登录凭证换电脑后需要重新登录。

### 图标：不要手改位图

`deploy/assets/app.ico`（多尺寸）与 `app.png` 由 `deploy/build_icon.py` **自己画**：
按 4 倍超采样出图再降采样，所以 Windows 真正会用的 16/24/32 px 不糊；
配色取自界面主题（`ui_theme.BRAND` 品牌绿、`INK` 深墨），和软件是同一套颜色。
`deploy/merchant.spec` 里 `icon=` 把它写进 exe 资源（任务栏 / Alt-Tab / 文件图标），
`datas` 把它随包分发，运行时的窗口图标由 `ui_theme.set_window_icon` 读。

```powershell
.\.venv\Scripts\python.exe deploy\build_icon.py             # 出三个方向 + 桌面尺寸预览
.\.venv\Scripts\python.exe deploy\build_icon.py --final b   # 定稿方向 b 并导出 ico/png
```

方向在 `build_icon.py` 的 `DIRECTIONS` 里，加一个函数就是一个新方向。

发版前冒烟：双击 `dist/微信AI智能助理/微信AI智能助理.exe`，
走一遍"登录 → 选群 → 读历史"，标题栏版本号对得上才算数。

## 3. 生成便携 ZIP（文件名是约定，错一个字客户端就认不出）

```powershell
$ver = "1.9.9"  # 与 version.py 一致，去掉开头的 v
Compress-Archive -Path "dist\微信AI智能助理" `
  -DestinationPath "微信AI智能助理-win64-$ver.zip" -Force
(Get-FileHash "微信AI智能助理-win64-$ver.zip" -Algorithm SHA256).Hash.ToLower() `
  | Out-File "微信AI智能助理-win64-$ver.zip.sha256" -NoNewline -Encoding ascii
```

* zip 里**只有** `微信AI智能助理/` 这一层目录（多套一层也行，客户端只扒一层）。
* `.sha256` 文件内容是单个文件的十六进制哈希（可带文件名后缀，取第一段）。
* `.sha256` 现在是给**老客户端**留的（1.9.9 及以前只认它）；新客户端认签名清单，
  见下一节。两个都传，谁都别落。

## 4. 签名（信任根：新客户端只认签过名的清单）

```powershell
# 只在做一次，最好在一台离线机器上：生成发布密钥（私钥写在仓库外，不入库、不进包）
.\.venv\Scripts\python.exe deploy\sign_release.py new-key
# 把打印出来的 PUBLISHER_PUBKEY = "…" 粘进 src/wechat_ai_assistant/update_signature.py

# 每次发版：签安装包，产出 dist\latest.json + dist\latest.json.sig
.\.venv\Scripts\python.exe deploy\sign_release.py sign "微信AI智能助理-win64-$ver.zip" `
  --version $ver --notes "修了…" --out dist

# 发布前自查：拿客户端钉的那把公钥验一遍，顺便核对 zip 哈希
.\.venv\Scripts\python.exe deploy\sign_release.py verify dist\latest.json `
  --zip "微信AI智能助理-win64-$ver.zip"
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

不想让私钥留在发版机器上时，把它交给 JYI 平台 worker（`apps/platform` 的
`app/api/internal/release/sign/route.ts`，两个 secret：`RELEASE_SIGN_JWK` = 私钥的 JWK，
`RELEASE_SIGN_TOKEN` = 进门口令）。签名路由挂在有域名的 worker 上（`jyisolutions.com`），
不在根 `jyi` worker 里——根 worker 没有 route，挂上去也没人访问得到。

```powershell
# 一次性：把私钥转成 WebCrypto 的 JWK 喂给平台 worker（在拿到私钥的那台机器上跑一次）
.\.venv\Scripts\python.exe deploy\sign_release.py export-jwk |
  npx wrangler secret put RELEASE_SIGN_JWK --config apps\platform\wrangler.jsonc

# 每次发版：清单仍在本机生成，只把清单字节发给签名路由，签名拿回来本机先验一遍
$env:RELEASE_SIGN_TOKEN = "<自己生成的长随机串>"
.\.venv\Scripts\python.exe deploy\sign_release.py sign "微信AI智能助理-win64-$ver.zip" `
  --version $ver --remote https://jyisolutions.com/api/internal/release/sign
```

* 信任根没变：客户端认的还是源码里钉死的 `PUBLISHER_PUBKEY`。签名服务自称的公钥与它不一致、
  或签名在本机验不过，`sign` 立刻中止且**什么都不落盘** —— 在线服务被换掉也发不出坏包。
* `export-jwk` 的标准输出**就是私钥**：别重定向进日志、别贴进聊天、别提交。
* worker 只回 `{"signature": "<base64 的 raw r||s>", "public_key": "<128 位 hex>"}`。
* 两端算法一致性已做过互通性自检：Node/WebCrypto 签 → Python/CNG 验通过（ECDSA P-256 +
  SHA-256 + raw `r||s`）。

## 5. 发布到 JYI 工具中心（主渠道）

工具中心是主渠道：国内直连、没有匿名限流，而且清单和签名跟包放在同一个地方。
**这个工具必须设成「免费」**——客户端用的是产品自己的 JYI 凭据（`jyi_client.py` 那套
`api_key`/`device_token`），不是平台 OAuth，取不到「需授权」工具的下载。

1. 管理后台 → 工具 → 微信 AI 助手 → 上传新版本：填版本号、上传 `微信AI智能助理-win64-<版本>.zip`；
2. 同一版的两个字段里粘上签名（就是 `sign` 命令产出的那两个文件的内容）：
   * **签名清单** = `dist\latest.json`
   * **签名** = `dist\latest.json.sig`
3. 把工具设成「免费」，确认 `downloadsPaused` 是关的。

客户端只看**清单里**的 `zip_url`/`sha256`：清单验过就用清单里的下载地址，清单里的哈希对不上
就当作没更新（工具中心换版不用重签，地址是稳定的；但**每次换包都必须重签一次清单**，
否则哈希对不上，客户端会一直跳过这一版）。

### 5.1 GitHub Release（过渡期，给老客户端）

1.9.9 及以前的客户端只认 GitHub，所以过渡期仍要发一次：

1. `git tag v<版本>` 并推送；
2. 在 GitHub 建 Release（tag 同名），上传 `.zip`、`.zip.sha256`、`latest.json`、`latest.json.sig`；
3. Release 正文写人话（修了什么、要不要立即更新）。

仓库默认查 `randomix777/wechat-ai-assistant` 的 releases，换地方发版要同步改
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
