param([string]$InstallRoot='D:\Program Files\Framekeep')
$ErrorActionPreference='Stop'
$oldRoot=Join-Path $env:LOCALAPPDATA 'Framekeep'
$source=Join-Path $oldRoot 'data'
$dataRoot=Join-Path ([IO.Path]::GetFullPath($InstallRoot)) 'data'
$receiptPath=Join-Path $dataRoot '.framekeep-data.json'
if(-not(Test-Path -LiteralPath $receiptPath)){throw '缺少资料迁移收据，不能清理旧资料。'}
$receipt=Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
if($receipt.schema -ne 'framekeep.data' -or $receipt.source -ne $source -or $receipt.path -ne $dataRoot){throw '迁移来源或目标不一致，不能清理。'}
foreach($folder in @($oldRoot,$dataRoot)){
    $part=[IO.DirectoryInfo]$folder
    while($part){
        if($part.Exists -and ($part.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw ('资料路径包含链接：'+$part.FullName)}
        $part=$part.Parent
    }
}
if(Test-Path -LiteralPath (Join-Path $dataRoot 'metadata-backups')){
    if(@(Get-ChildItem -LiteralPath (Join-Path $dataRoot 'metadata-backups') -File -Recurse).Count){throw '新 data 仍有未转换的整图副本，停止清理，请先完成核验。'}
}
foreach($name in @('framekeep.sqlite3','asset_archive.sqlite3','sku.sqlite3')){
    if(-not(Test-Path -LiteralPath (Join-Path $dataRoot $name) -PathType Leaf)){throw ('新位置缺少数据库：'+$name)}
}
$active=@(Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and ($_.ExecutablePath.StartsWith($oldRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or $_.ExecutablePath.StartsWith($InstallRoot+'\',[StringComparison]::OrdinalIgnoreCase))) -or $_.Name -eq 'update-worker.exe'
})
if($active.Count){throw '请先正常关闭末帧选片及安装/更新窗口，再运行清理。'}
if(Test-Path -LiteralPath $source){
    $files=@(Get-ChildItem -LiteralPath $source -File -Recurse -Force | Where-Object {$_.Name -notmatch '-(wal|shm)$'})
    $saved=@($receipt.sourceStamp.PSObject.Properties)
    if($files.Count -ne $saved.Count){throw '迁移后旧目录文件数量发生变化，请先重新核验。'}
    foreach($file in $files){
        $relative=$file.FullName.Substring($source.Length+1).Replace('\','/')
        $entry=$receipt.sourceStamp.PSObject.Properties[$relative]
        $mtimeNs=[int64](($file.LastWriteTimeUtc.Ticks - 621355968000000000L)*100L)
        if(-not $entry -or $file.Length -ne $entry.Value[0] -or $mtimeNs -ne $entry.Value[1]){throw ('旧资料已改变，停止清理：'+$relative)}
    }
    if(@(Get-ChildItem -LiteralPath $source -File -Recurse -Filter '*-wal' | Where-Object Length -gt 0).Count){throw '旧资料有未合并的 SQLite WAL，停止清理。'}
    foreach($file in (Get-ChildItem -LiteralPath (Join-Path $source 'metadata-backups') -File -Filter '*.bin')){
        $history=Join-Path $dataRoot ('metadata-history\'+$file.BaseName+'.json')
        if(-not(Test-Path -LiteralPath $history)){throw ('缺少轻量恢复档案：'+$file.Name)}
        $record=Get-Content -LiteralPath $history -Raw -Encoding UTF8 | ConvertFrom-Json
        if($record.schema -ne 'framekeep.metadata-history' -or (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $record.journal.before_hash){throw ('恢复副本校验失败：'+$file.Name)}
    }
}
$targets=@($oldRoot,(Join-Path $dataRoot 'previews'),(Join-Path $dataRoot 'catalog_previews'),(Join-Path $dataRoot 'components\downloads'),(Join-Path $dataRoot 'metadata-backups'))
$released=0L
foreach($target in $targets){
    if(-not(Test-Path -LiteralPath $target)){continue}
    $resolved=(Resolve-Path -LiteralPath $target).Path
    if($resolved -ne $oldRoot -and -not $resolved.StartsWith($dataRoot+'\',[StringComparison]::OrdinalIgnoreCase)){throw ('清理路径越界：'+$resolved)}
    $entries=@((Get-Item -LiteralPath $resolved -Force))+@(Get-ChildItem -LiteralPath $resolved -Force -Recurse)
    if(@($entries | Where-Object {($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0}).Count){throw ('目录包含链接，停止清理：'+$resolved)}
    $size=($entries | Where-Object {-not $_.PSIsContainer} | Measure-Object Length -Sum).Sum
    Write-Host ('正在清理：'+$resolved)
    Remove-Item -LiteralPath $resolved -Recurse -Force
    $released+=[int64]$size
}
Write-Host ('清理完成，删除文件总量约 {0:N2} GiB；资产、评级、设置和模型保留在 {1}' -f ($released/1GB),$dataRoot)
