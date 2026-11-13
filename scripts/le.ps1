#Requires -Version 5.1
<#
.SYNOPSIS
    LastEpoch-BuildSimulator 统一操作入口。

.DESCRIPTION
    所有命令都从仓库根目录执行（脚本自行推导 $PSScriptRoot\.. 并切换过去，
    因此可以从任意当前目录调用）。真正的实现位于 scripts/le_commands.py，
    本脚本只负责：定位仓库根、选择 Python 解释器、规范化参数、转发退出码。

    退出码：0 = 成功；1 = 失败/校验不一致；2 = 参数错误；3 = 部分完成。

.EXAMPLE
    公开版不内置任何案例、存档或历史 run；所有输入都由使用者显式提供。
    .\scripts\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator
    .\scripts\le.ps1 doctor
    .\scripts\le.ps1 import --case examples\<case>\case.json --save '<存档路径>'
    .\scripts\le.ps1 import --case examples\<case>\case.json --save '<存档路径>' --main-skill bh2 --model black-hole
    .\scripts\le.ps1 prepare --case examples\<case>\case.json --main-skill <skill-id>
    .\scripts\le.ps1 run --case examples\<case>\case.json
    .\scripts\le.ps1 validate --case examples\<case>\case.json
    .\scripts\le.ps1 serve --case examples\<case>\case.json --port 8765
    .\scripts\le.ps1 test --suite phase3
    .\scripts\le.ps1 knowledge rebuild
    .\scripts\le.ps1 knowledge search --query "<node id 或关键词>"
    .\scripts\le.ps1 package -Out dist
#>
# 注意：这里必须保持“简单脚本”（不使用 [CmdletBinding()] / [Parameter()]），
# 否则 PowerShell 会把 `-Out` 当成通用参数（-OutVariable/-OutBuffer）的歧义前缀，
# 导致 `le.ps1 package -Out <dir>` 直接报参数歧义。简单脚本会把未知的 -X 原样收入 $args。
param()

$rawArguments = @($args)

$ErrorActionPreference = 'Stop'

# --- 仓库根：始终由脚本位置推导，不依赖当前目录 -----------------------------
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repoRoot

function Write-ErrorAndExit([string]$Message, [int]$Code = 1) {
    [Console]::Error.WriteLine("错误：$Message")
    exit $Code
}

if ($rawArguments.Count -eq 0) {
    @'
用法：.\scripts\le.ps1 <命令> [参数]

  editor    [--port 8766] [--tool-home <目录>]   启动本机 BD 配置工作台
  doctor                                        检查环境、模块导入与运行必需数据
  reproduce --case <case.json>                  仅旧 profile/hit 兼容路径；unified definitions_v3/plan-v4 明确 unsupported_operation
  run       --case <case.json> [--out <dir>]    仅从冻结输入执行计算（快速路径；可 --port）
  serve     --case <case.json> [--port 8765] [--report <run-id|run目录>]
                                                启动本机回环条件重算服务；--report 显式选择报告并校验端口
  import    --case <new case.json> --save <存档> [--planner <json>] [--stash <存档>]
                                                为另一个 BD 创建/准备案例目录（拒绝覆盖）
  validate  --case <case.json>                  结构/绑定校验（不重新计算）
  prepare   --case <case.json> --main-skill <id> [--resume]
                                                阶段化准备（冻结输入→BuildSpec→计算输入→词缀→分析产物→快照→最小校验）
  test      [--suite ingest|build_import|save_import|affix_db|phase3|knowledge|all]
  knowledge search --query <词语或ID>           机制/技能知识摘要（另见 show/rebuild）
  package   [-Out <目录或zip>]                  按显式清单打包发布

退出码：0 成功 / 1 失败或校验不一致 / 2 参数错误 / 3 部分完成
'@ | Write-Host
    exit 2
}

# --- Python 解释器：config/project.json 候选 → LE_PYTHON → PATH --------------
$candidates = @()
$configPath = Join-Path $repoRoot 'config\project.json'
if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    try {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($config.python -and $config.python.candidates) {
            $candidates = @($config.python.candidates)
        }
    }
    catch {
        Write-Host '警告：config/project.json 解析失败，使用默认解释器候选。' -ForegroundColor Yellow
    }
}
if ($candidates.Count -eq 0) {
    $candidates = @('.venv\Scripts\python.exe', 'python')
}

$python = $null
if ($env:LE_PYTHON) {
    if (Test-Path -LiteralPath $env:LE_PYTHON -PathType Leaf) {
        $python = $env:LE_PYTHON
    }
    else {
        Write-ErrorAndExit "环境变量 LE_PYTHON 指向的文件不存在：$env:LE_PYTHON"
    }
}
if (-not $python) {
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        $path = if ([System.IO.Path]::IsPathRooted($candidate)) { $candidate } else { Join-Path $repoRoot $candidate }
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $python = $path
            break
        }
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            $python = $command.Source
            break
        }
    }
}
if (-not $python) {
    Write-ErrorAndExit '找不到 Python 解释器。请先创建 .venv（.\.venv\Scripts\python.exe），或设置环境变量 LE_PYTHON 指向 Python 3.12+。'
}

$driver = Join-Path $repoRoot 'scripts\le_commands.py'
if (-not (Test-Path -LiteralPath $driver -PathType Leaf)) {
    Write-ErrorAndExit "缺少统一入口实现：scripts\le_commands.py"
}

# --- 参数规范化：兼容 PowerShell 风格的 -Out/-Case/-Port/-Suite -------------
$normalized = New-Object System.Collections.Generic.List[string]
foreach ($argument in $rawArguments) {
    switch -Regex ($argument) {
        '^-{1}(Out|out)$' { $normalized.Add('--out'); continue }
        '^-{1}(Case|case)$' { $normalized.Add('--case'); continue }
        '^-{1}(Port|port)$' { $normalized.Add('--port'); continue }
        '^-{1}(Suite|suite)$' { $normalized.Add('--suite'); continue }
        '^-{1}(Save|save)$' { $normalized.Add('--save'); continue }
        '^-{1}(Planner|planner)$' { $normalized.Add('--planner'); continue }
        '^-{1}(Stash|stash)$' { $normalized.Add('--stash'); continue }
        '^-{1}(RunId|runid|run-id)$' { $normalized.Add('--run-id'); continue }
        '^-{1}(Zip|zip)$' { $normalized.Add('--zip'); continue }
        default { $normalized.Add($argument) }
    }
}

# 逐次调用设置 PYTHONPATH 等环境变量不会写回系统；这里不设置任何持久环境变量。
$forward = $normalized.ToArray()
& $python $driver @forward
exit $LASTEXITCODE
