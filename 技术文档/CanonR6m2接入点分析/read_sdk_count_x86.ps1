param(
    [Parameter(Mandatory = $true)][string]$DllPath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)
$ErrorActionPreference = 'Stop'
if ([IntPtr]::Size -ne 4) { throw '请使用 Windows SysWOW64 的 32 位 Windows PowerShell' }
$resolvedDll = (Resolve-Path -LiteralPath $DllPath).Path
$escapedDll = $resolvedDll.Replace('\', '\\').Replace('"', '\"')
$nativeSource = @"
using System;
using System.Runtime.InteropServices;
public static class CanonCountProbe {
    [DllImport("$escapedDll", CallingConvention=CallingConvention.StdCall)]
    public static extern uint EdsInitializeSDK();
    [DllImport("$escapedDll", CallingConvention=CallingConvention.StdCall)]
    public static extern uint EdsGetCameraList(out IntPtr list);
    [DllImport("$escapedDll", CallingConvention=CallingConvention.StdCall)]
    public static extern uint EdsGetChildCount(IntPtr list, out uint count);
    [DllImport("$escapedDll", CallingConvention=CallingConvention.StdCall)]
    public static extern uint EdsRelease(IntPtr item);
    [DllImport("$escapedDll", CallingConvention=CallingConvention.StdCall)]
    public static extern uint EdsTerminateSDK();
}
"@
Add-Type -TypeDefinition $nativeSource
$result = [ordered]@{
    observed_at = [DateTime]::UtcNow.ToString('o')
    process_bits = 32
    dll = $resolvedDll
    sdk_sha256 = (Get-FileHash -LiteralPath $resolvedDll -Algorithm SHA256).Hash.ToLowerInvariant()
    scope = 'Read-only enumeration; no camera session, property write, activation or telemetry'
    calls = @()
    camera_count = $null
}
$cameraList = [IntPtr]::Zero
$initialized = $false
try {
    $code = [CanonCountProbe]::EdsInitializeSDK()
    $result.calls += @{ api = 'EdsInitializeSDK'; code = ('0x{0:X8}' -f $code) }
    if ($code -ne 0) { throw 'EdsInitializeSDK failed' }
    $initialized = $true
    $code = [CanonCountProbe]::EdsGetCameraList([ref]$cameraList)
    $result.calls += @{ api = 'EdsGetCameraList'; code = ('0x{0:X8}' -f $code) }
    if ($code -ne 0) { throw 'EdsGetCameraList failed' }
    [uint32]$count = 0
    $code = [CanonCountProbe]::EdsGetChildCount($cameraList, [ref]$count)
    $result.calls += @{ api = 'EdsGetChildCount'; code = ('0x{0:X8}' -f $code) }
    if ($code -eq 0) { $result.camera_count = $count }
} finally {
    if ($cameraList -ne [IntPtr]::Zero) {
        $code = [CanonCountProbe]::EdsRelease($cameraList)
        $result.calls += @{ api = 'EdsRelease'; code = ('0x{0:X8}' -f $code) }
    }
    if ($initialized) {
        $code = [CanonCountProbe]::EdsTerminateSDK()
        $result.calls += @{ api = 'EdsTerminateSDK'; code = ('0x{0:X8}' -f $code) }
    }
    $json = $result | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($OutputPath, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Write-Output $json
}
