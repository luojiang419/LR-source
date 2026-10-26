param(
    [Parameter(Mandatory=$true)][string]$PhotoFolder,
    [string]$AppFolder,
    [string]$DataFolder,
    [string]$OutputFolder,
    [switch]$HashPhotos
)
$ErrorActionPreference='Stop'
# 只读取原目录、指定配置与文件属性，不调用删除、移动、恢复或修复操作。
$photoRoot=[IO.Path]::GetFullPath($PhotoFolder)
if (-not $OutputFolder) {
    $OutputFolder=Join-Path ([IO.Path]::GetTempPath()) ('Framekeep-photo-evidence-'+[guid]::NewGuid().ToString('N'))
}
$reportRoot=[IO.Path]::GetFullPath($OutputFolder)
foreach ($protected in @($photoRoot,$DataFolder) | Where-Object {$_}) {
    $protectedRoot=[IO.Path]::GetFullPath($protected).TrimEnd('\')
    if ($reportRoot.Equals($protectedRoot,[StringComparison]::OrdinalIgnoreCase) -or $reportRoot.StartsWith($protectedRoot+'\',[StringComparison]::OrdinalIgnoreCase)) {
        throw '报告目录必须放在照片目录和软件资料目录之外。'
    }
}
if (Test-Path -LiteralPath $reportRoot) {throw '报告目录已存在，请使用新的输出目录，避免覆盖。'}
$ancestor=[IO.DirectoryInfo]([IO.Path]::GetDirectoryName($reportRoot))
while ($ancestor) {
    if ($ancestor.Exists -and (($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw '报告目录的父路径含链接，请选择普通目录，避免写入原照片位置。'
    }
    $ancestor=$ancestor.Parent
}
$files=New-Object 'Collections.Generic.List[object]'
$errors=New-Object 'Collections.Generic.List[string]'
$pending=New-Object 'Collections.Generic.Stack[string]'
$pending.Push($photoRoot)
while ($pending.Count -gt 0) {
    $current=$pending.Pop()
    try {
        $directory=Get-Item -LiteralPath $current -Force
        if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            $errors.Add('未遍历重解析目录：'+$current);continue
        }
        foreach ($item in Get-ChildItem -LiteralPath $current -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $errors.Add('未跟随链接：'+$item.FullName);continue
            }
            if ($item.PSIsContainer) {$pending.Push($item.FullName);continue}
            $row=[ordered]@{path=$item.FullName;bytes=$item.Length;modifiedUtc=$item.LastWriteTimeUtc.ToString('o');attributes=$item.Attributes.ToString()}
            if ($HashPhotos) {
                try {$row['sha256']=(Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash}
                catch {$row['readError']=$_.Exception.Message}
            }
            $files.Add($row)
        }
    } catch {$errors.Add($current+': '+$_.Exception.Message)}
}
$configuration=[ordered]@{}
if ($AppFolder) {
    $path=Join-Path $AppFolder 'update-config.json'
    if (Test-Path -LiteralPath $path) {
        $config=Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        $configuration['release']=[ordered]@{version=$config.version;releaseId=$config.releaseId;appFolder=[IO.Path]::GetFullPath($AppFolder)}
    }
}
if ($DataFolder) {
    foreach ($relative in @('folder-watch.json','canon-link/preferences.json')) {
        $path=Join-Path $DataFolder $relative
        if (Test-Path -LiteralPath $path) {
            try {
                $config=Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
                $configuration[$relative]=[ordered]@{enabled=$config.enabled;folder=$config.folder;destination=$config.destination;source=$config.source}
            } catch {$errors.Add($path+': '+$_.Exception.Message)}
        }
    }
}
$report=[ordered]@{
    createdUtc=[DateTime]::UtcNow.ToString('o');photoFolder=$photoRoot;fileCount=$files.Count
    hashPhotos=[bool]$HashPhotos;configuration=$configuration;files=$files;errors=$errors
    note='本报告仅记录现状；不能恢复已删除照片，也不能据此认定删除进程。不读取密钥、评级数据库或照片内容（除显式要求计算哈希）。'
}
New-Item -ItemType Directory -Path $reportRoot -ErrorAction Stop | Out-Null
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $reportRoot 'photo-safety-evidence.json') -Encoding UTF8
Write-Output ('取证完成，原目录未修改。报告：'+(Join-Path $reportRoot 'photo-safety-evidence.json'))
