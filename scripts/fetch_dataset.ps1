#Requires -Version 5.1
<#
.SYNOPSIS
    下载并校验 LastEpoch-BuildSimulator 使用的离线 dataset（data.sqlite）。

.DESCRIPTION
    离线 dataset 体积超过 GitHub 单文件限制，因此作为 GitHub Release 附件分发，
    不随仓库提交。本脚本从 Release 下载 `data.sqlite`，按仓库内 `data/current.json`
    记录的 `file_sha256`（以及 dataset manifest 的 size_bytes，若可读）校验，
    校验通过后才原子替换到 `data/current.json` 指向的数据库路径。

    数据集 ID 与哈希来自仓库内的 data/current.json / manifest.json，脚本本身不
    写死任何作者/组织名；`-Repo` 由使用者提供。

    兼容 Windows PowerShell 5.1（仅使用 Invoke-WebRequest / Get-FileHash 等）。

.PARAMETER Repo
    GitHub 仓库，`BlaCatFishLE/LastEpoch-AIBuildSimulator` 形式，例如 `BlaCatFishLE/LastEpoch-AIBuildSimulator`。

.PARAMETER Tag
    Release tag，默认 `v0.4.0`。Release 附件名固定为 `data.sqlite`。

.PARAMETER Url
    直接指定完整下载 URL（覆盖 -Repo/-Tag），适合 GitHub 镜像或代理链接
    （必须为 http/https 直链）。

.PARAMETER OutFile
    覆盖输出路径（默认取 data/current.json 的 database 字段）。

.PARAMETER Force
    目标文件已存在且哈希正确时不重复下载（默认）；指定 -Force 时重新下载。

.EXAMPLE
    .\scripts\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator
    .\scripts\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator -Tag v0.4.0
    .\scripts\fetch_dataset.ps1 -Url https://example.com/data.sqlite
#>
param(
    [string]$Repo,
    [string]$Tag = 'v0.4.0',
    [string]$Url,
    [string]$OutFile,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repoRoot

function Write-Fail([string]$Message) {
    [Console]::Error.WriteLine("错误：$Message")
    exit 1
}

function Write-Usage([string]$Message) {
    [Console]::Error.WriteLine("错误：$Message")
    [Console]::Error.WriteLine('')
    [Console]::Error.WriteLine('用法示例：')
    [Console]::Error.WriteLine('  .\scripts\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator')
    [Console]::Error.WriteLine('  .\scripts\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator -Tag v0.4.0')
    [Console]::Error.WriteLine('  .\scripts\fetch_dataset.ps1 -Url https://example.com/data.sqlite')
    exit 2
}

# --- 读取仓库内的 dataset 指针与 manifest（哈希的权威来源） -------------------
$pointerPath = Join-Path $repoRoot 'data\current.json'
if (-not (Test-Path -LiteralPath $pointerPath -PathType Leaf)) {
    Write-Fail "缺少 dataset 指针：data/current.json（发布树不完整？）"
}
try {
    $pointer = Get-Content -LiteralPath $pointerPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    Write-Fail "data/current.json 不是合法 JSON：$($_.Exception.Message)"
}
$expectedSha = [string]$pointer.file_sha256
if ([string]::IsNullOrWhiteSpace($expectedSha)) {
    Write-Fail 'data/current.json 未记录 file_sha256，无法校验下载文件'
}
$expectedSha = $expectedSha.Trim().ToLowerInvariant()

if ($OutFile) {
    $targetPath = if ([System.IO.Path]::IsPathRooted($OutFile)) { $OutFile } else { Join-Path $repoRoot $OutFile }
}
elseif ($pointer.database) {
    $targetPath = Join-Path $repoRoot ([string]$pointer.database -replace '\\', '\')
}
else {
    Write-Fail 'data/current.json 未记录 database 路径；请用 -OutFile 显式指定'
}
$targetPath = [System.IO.Path]::GetFullPath($targetPath)

# --- manifest 提供 size_bytes 时可加一层大小校验 -----------------------------
$expectedSize = $null
if ($pointer.manifest) {
    $manifestPath = Join-Path $repoRoot ([string]$pointer.manifest -replace '\\', '\')
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $db = $manifest.database
            if ($db -and $db.sha256 -and ([string]$db.sha256).Trim().ToLowerInvariant() -ne $expectedSha) {
                [Console]::Error.WriteLine('警告：data/current.json 与 dataset manifest 的 database.sha256 不一致；以 data/current.json 为准。')
            }
            if ($db -and $db.size_bytes) { $expectedSize = [int64]$db.size_bytes }
        }
        catch {
            [Console]::Error.WriteLine("警告：dataset manifest 不可解析，跳过大小校验（$($_.Exception.Message)）")
        }
    }
}

function Test-FileHash([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try { $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
    catch { return $false }
    return ($hash -eq $expectedSha)
}

# 已存在且哈希正确：不重复下载。
if ((-not $Force) -and (Test-FileHash $targetPath)) {
    $bytes = (Get-Item -LiteralPath $targetPath).Length
    Write-Host "dataset 已存在且校验通过：$targetPath（$bytes bytes）"
    Write-Host '无需重新下载。'
    Write-Host '下一步：.\scripts\le.ps1 doctor'
    exit 0
}

# --- 解析下载 URL ------------------------------------------------------------
if ($Url) {
    $downloadUrl = $Url
}
elseif ($Repo) {
    if ($Repo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
        Write-Usage "-Repo 必须是 BlaCatFishLE/LastEpoch-AIBuildSimulator 形式（例如 BlaCatFishLE/LastEpoch-AIBuildSimulator），实际：$Repo"
    }
    if ([string]::IsNullOrWhiteSpace($Tag)) { $Tag = 'v0.4.0' }
    $downloadUrl = "https://github.com/$Repo/releases/download/$Tag/data.sqlite"
}
else {
    Write-Usage '缺少 -Repo（GitHub 仓库 BlaCatFishLE/LastEpoch-AIBuildSimulator）或 -Url（完整下载链接）'
}

# --- 下载到临时文件，校验后原子替换 -----------------------------------------
$targetDir = Split-Path -Parent $targetPath
if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) {
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}
$tempPath = "$targetPath.part"

if (Test-Path -LiteralPath $tempPath) {
    Remove-Item -LiteralPath $tempPath -Force
}

Write-Host "dataset 下载：$downloadUrl"
Write-Host "目标路径：$targetPath"

try {
    # PowerShell 5.1 的 Invoke-WebRequest 默认依赖 IE 引擎，-UseBasicParsing 规避。
    Invoke-WebRequest -Uri $downloadUrl -OutFile $tempPath -UseBasicParsing -MaximumRedirection 10
}
catch {
    if (Test-Path -LiteralPath $tempPath) { Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue }
    $detail = $_.Exception.Message
    [Console]::Error.WriteLine("错误：dataset 下载失败：$detail")
    [Console]::Error.WriteLine('')
    [Console]::Error.WriteLine('排查建议：')
    [Console]::Error.WriteLine("  1) 确认 Release 已发布且附件名为 data.sqlite：$downloadUrl")
    [Console]::Error.WriteLine('  2) 网络受限时手动下载后放到目标路径（文件哈希必须与 data/current.json 的 file_sha256 一致）')
    [Console]::Error.WriteLine("     期望 sha256：$expectedSha")
    [Console]::Error.WriteLine('  3) 也可用 -Url 指定镜像或本地文件：.\scripts\fetch_dataset.ps1 -Url <直链或本地路径>')
    exit 1
}

if (-not (Test-Path -LiteralPath $tempPath -PathType Leaf)) {
    Write-Fail '下载命令结束但没有生成临时文件'
}

$actualSize = (Get-Item -LiteralPath $tempPath).Length
if ($null -ne $expectedSize -and $actualSize -ne $expectedSize) {
    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    Write-Fail "dataset 大小校验失败：期望 $expectedSize bytes，实际 $actualSize bytes（已删除临时文件；请重新下载或手动放置正确文件）"
}

$actualSha = (Get-FileHash -LiteralPath $tempPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSha -ne $expectedSha) {
    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    [Console]::Error.WriteLine("错误：dataset SHA-256 校验失败（已删除临时文件）")
    [Console]::Error.WriteLine("  期望：$expectedSha")
    [Console]::Error.WriteLine("  实际：$actualSha")
    [Console]::Error.WriteLine('  请确认下载源与 data/current.json 指向同一 dataset；不要使用被篡改或其它版本的文件。')
    exit 1
}

# 同卷 Move-Item 覆盖；失败时保留 .part 并给出明确提示。
try {
    Move-Item -LiteralPath $tempPath -Destination $targetPath -Force
}
catch {
    Write-Fail "哈希校验已通过，但替换目标文件失败：$($_.Exception.Message)（临时文件保留在 $tempPath）"
}

$finalSize = (Get-Item -LiteralPath $targetPath).Length
Write-Host ''
Write-Host "dataset 校验通过：$targetPath"
Write-Host "  sha256（期望 = 实际）：$expectedSha"
Write-Host "  size：$finalSize bytes"
Write-Host '下一步：.\scripts\le.ps1 doctor'
exit 0
