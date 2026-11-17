$ErrorActionPreference = 'Stop'
$plan = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'cleanup-plan.json') -Raw | ConvertFrom-Json
$root = [IO.Path]::GetFullPath($plan.root).TrimEnd('\')
if ($root -ne 'G:\app\LR') { throw '清理工作区不匹配' }
if (-not (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'publication.json') -Raw | ConvertFrom-Json).anonymousVerification) { throw '发布尚未验收' }

function Inventory([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return @{ bytes = 0L; files = 0; newest = $null } }
    $item = Get-Item -LiteralPath $path -Force
    $files = if ($item.PSIsContainer) { @(Get-ChildItem -LiteralPath $path -Recurse -File -Force) } else { @($item) }
    return @{ bytes = [long](($files | Measure-Object Length -Sum).Sum); files = $files.Count; newest = ($files | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1 -ExpandProperty LastWriteTimeUtc) }
}

$detached = @()
foreach ($link in $plan.detachLinks.PSObject.Properties) {
    $full = [IO.Path]::GetFullPath($link.Name)
    if (-not $full.StartsWith($root + '\build\modelscope-20261109-fixes\source\', [StringComparison]::OrdinalIgnoreCase)) { throw '链接不在本轮构建目录' }
    $item = Get-Item -LiteralPath $full -Force
    if ($item.LinkType -ne 'Junction' -or [IO.Path]::GetFullPath([string]$item.Target) -ne [IO.Path]::GetFullPath([string]$link.Value)) { throw '构建依赖链接目标不匹配' }
    Remove-Item -LiteralPath $full -Force
    $detached += @{ path = $full; retainedTarget = [string]$link.Value }
}

$beforeFree = (Get-PSDrive G).Free
$results = @()
foreach ($target in $plan.targets) {
    $full = [IO.Path]::GetFullPath([string]$target)
    if (-not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '清理目标越出工作区' }
    if (-not (Test-Path -LiteralPath $full)) { continue }
    $resolved = (Resolve-Path -LiteralPath $full).Path
    if ($resolved -ne $full) { throw '清理目标解析不一致' }
    $item = Get-Item -LiteralPath $full -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw '跳过重解析点目标' }
    if ($item.PSIsContainer -and @(Get-ChildItem -LiteralPath $full -Recurse -Force -Attributes ReparsePoint).Count) { throw '目标包含未知重解析点，未清理' }
    $before = Inventory $full
    $failure = $null
    try { Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop } catch { $failure = $_.Exception.Message }
    $after = Inventory $full
    $results += @{ path = $full; bytesRemoved = $before.bytes - $after.bytes; filesRemoved = $before.files - $after.files; newestFileUtc = $before.newest; remainingBytes = $after.bytes; error = $failure }
}
$afterFree = (Get-PSDrive G).Free
$report = @{ reason = '用户明确要求发布后清理旧包及可重建构建缓存；本轮临时发布/隔离验收产物及安卓编译输出可重建，无需按旧产物30天门槛等待'; beforeFree = $beforeFree; afterFree = $afterFree; actualFreeDelta = $afterFree - $beforeFree; bytesRemoved = [long](($results | Measure-Object bytesRemoved -Sum).Sum); filesRemoved = ($results | Measure-Object filesRemoved -Sum).Sum; detachedLinks = $detached; results = $results; retained = $plan.retained }
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'cleanup.json') -Encoding utf8
$report | Select-Object bytesRemoved,filesRemoved,beforeFree,afterFree,actualFreeDelta | ConvertTo-Json -Compress
