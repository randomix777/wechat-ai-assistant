# 换版演练：拿真正打好的包，在临时目录里走一遍「暂存 → 启动 → 换版」。
#
#   pwsh -File deploy\rehearse_update.ps1 success    # 新版能起来 → 应该换过去、删备份
#   pwsh -File deploy\rehearse_update.ps1 rollback   # 新版起不来 → 应该换回旧版、留记号
#
# 为什么非得来这一遍：换版脚本是 cmd 批处理，跑在软件已经退出的那个时间窗里，
# 单元测试只能钉住脚本文本和几个真跑用例（tests/test_updater.py 的
# ScriptExecutionTests 用 ping.exe 冒充主程序）。真正会踩的是环境：进程占着目录、
# 标准输入被重定向、PATH 里混进 MSYS 的 find.exe —— 这些只有真机演练能发现。
#
# 演练前先打包：.venv\Scripts\pyinstaller.exe --noconfirm --clean deploy\merchant.spec
param([Parameter(Mandatory = $true)][ValidateSet('success', 'rollback')][string]$Case)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$tmp = Join-Path $env:TEMP 'pyw-real-rehearsal'
$base = Join-Path $tmp '微信AI智能助理'
$version = '9.9.9'
$backup = Join-Path $tmp "微信AI智能助理_backup-$version"
$scriptPath = Join-Path $env:TEMP "pyw-apply-$version.bat"
$logPath = Join-Path $env:TEMP "pyw-apply-$version.log"

$packaged = Join-Path $repo 'dist\微信AI智能助理'
if (-not (Test-Path $packaged)) { throw "先打包：没有 $packaged" }
if (-not (Test-Path (Join-Path $packaged '_internal'))) { throw "$packaged 不像打好的包（缺 _internal）" }

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-Text([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, $Utf8NoBom)
}
function Read-TextOrEmpty([string]$Path) {
    if (Test-Path $Path) { [System.IO.File]::ReadAllText($Path, $Utf8NoBom) } else { '' }
}
function Stamp { (Get-Date).ToString('HH:mm:ss') }

Get-Process -Name '微信AI智能助理' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Remove-Item $scriptPath, $logPath -Force -ErrorAction SilentlyContinue
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Force $tmp | Out-Null
Copy-Item -Recurse $packaged $base

# 便携数据：假装商户已经用过一阵子
New-Item -ItemType Directory -Force (Join-Path $base 'data') | Out-Null
Write-Text (Join-Path $base 'data\shop.json') '{"旧版数据": 1}'
$oldExeSize = (Get-Item (Join-Path $base '微信AI智能助理.exe')).Length

# 暂存的"新版"：同一份包 + 一个只有新版才有的标记文件
$staged = Join-Path $base "_updates\$version"
New-Item -ItemType Directory -Force $staged | Out-Null
Copy-Item -Recurse (Join-Path $packaged '*') $staged
Write-Text (Join-Path $staged '新版标记.txt') '这是新版'
if ($Case -eq 'rollback') {
    # 起不来：主程序换成 ping（一跑就退），目录结构仍然是真的
    Copy-Item "$env:SystemRoot\System32\ping.exe" (Join-Path $staged '微信AI智能助理.exe') -Force
}
# pending.json 必须是无 BOM 的 UTF-8：带 BOM 的话 Python 那边 json.loads 直接失败
Write-Text (Join-Path $base 'pending.json') "{`"version`": `"$version`", `"dir`": `"$version`", `"notes`": `"换版演练`"}"

Write-Host "=== [$Case] $(Stamp) 启动旧版：$base\微信AI智能助理.exe"
$proc = Start-Process -FilePath (Join-Path $base '微信AI智能助理.exe') -PassThru
$deadline = (Get-Date).AddSeconds(150)
while (-not $proc.HasExited -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
Write-Host "$(Stamp) 启动器退出：$($proc.HasExited)"

# 更新脚本收尾时会自删，抓一份副本留证
$copy = Join-Path $tmp "pyw-apply-$version.copy.bat"
for ($i = 1; $i -le 300; $i++) {
    if (Test-Path $scriptPath) { Copy-Item $scriptPath $copy -Force; break }
    Start-Sleep -Milliseconds 200
}
Write-Host "$(Stamp) 拿到脚本副本：$(Test-Path $copy)"

$deadline2 = (Get-Date).AddSeconds(150)
while ((Test-Path $scriptPath) -and (Get-Date) -lt $deadline2) { Start-Sleep -Seconds 1 }
Write-Host "$(Stamp) 脚本自删（换版收尾）：$(-not (Test-Path $scriptPath))"
Start-Sleep -Seconds 3

Write-Host '--- 结果 ---'
$expectNew = $Case -eq 'success'
$newExeSize = (Get-Item (Join-Path $base '微信AI智能助理.exe')).Length
Write-Host "新版标记.txt 在位      : $(Test-Path (Join-Path $base '新版标记.txt'))   (期望 $expectNew)"
Write-Host "备份目录还在           : $(Test-Path $backup)   (期望 False)"
Write-Host "暂存目录还在           : $(Test-Path (Join-Path $base "_updates\$version"))   (期望 False)"
Write-Host "便携数据 shop.json     : $(Test-Path (Join-Path $base 'data\shop.json'))   (期望 True)"
Write-Host "错误嵌套 data\data     : $(Test-Path (Join-Path $base 'data\data'))   (期望 False)"
Write-Host "pending.json 已被接管  : $(Test-Path (Join-Path $base 'pending.json'))   (期望 False)"
Write-Host "主程序大小 现在=$newExeSize 旧包=$oldExeSize"
if (-not $expectNew) {
    Write-Host "回滚记号 blocked-$version : $(Test-Path (Join-Path $base "blocked-$version.txt"))   (期望 True)"
    Write-Host "  内容：$(Read-TextOrEmpty (Join-Path $base "blocked-$version.txt"))"
}
Write-Host "进程在跑               : $((Get-Process -Name '微信AI智能助理' -ErrorAction SilentlyContinue | Measure-Object).Count)"
Write-Host "换版脚本残留           : $((Get-ChildItem $env:TEMP -Filter 'pyw-apply-*.bat' -ErrorAction SilentlyContinue).Name -join ',')"
Write-Host "--- 换版日志 $logPath ---"
if (Test-Path $logPath) { Write-Host (Read-TextOrEmpty $logPath).TrimEnd() } else { Write-Host '（没有日志：脚本没跑起来，或死在写第一行之前）' }

Get-Process -Name '微信AI智能助理' -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "演练目录：$tmp"
