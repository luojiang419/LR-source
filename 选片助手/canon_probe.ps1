param([Parameter(Mandatory=$true)][string]$Executable)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Configuration
# 只读程序集元数据，不加载执行 Canon 程序或其类型初始化器。
$assembly=[Reflection.Assembly]::ReflectionOnlyLoadFrom($Executable)
$name=$assembly.GetName()
$company=''
foreach($attribute in [Reflection.CustomAttributeData]::GetCustomAttributes($assembly)) {
    if($attribute.AttributeType.FullName -eq 'System.Reflection.AssemblyCompanyAttribute') {$company=[string]$attribute.ConstructorArguments[0].Value}
}
if(-not $company) {throw 'EOS Utility 缺少公司元数据'}
$blob=New-Object System.Security.Permissions.StrongNamePublicKeyBlob -ArgumentList (,$name.GetPublicKey())
$strong=New-Object System.Security.Policy.StrongName -ArgumentList $blob,$name.Name,([Version]'0.0.0.0')
$stream=New-Object IO.MemoryStream
try {
    $formatter=New-Object Runtime.Serialization.Formatters.Binary.BinaryFormatter
    $formatter.Serialize($stream,$strong)
    $stream.Position=0
    $paths=[System.Configuration.ConfigurationManager].Assembly.GetType('System.Configuration.ClientConfigPaths')
    $hash=$paths.GetMethod('GetHash',[Reflection.BindingFlags]'Static,NonPublic').Invoke($null,[object[]]@($stream.PSObject.BaseObject))
} finally {$stream.Dispose()}
function SafeName([string]$Value) {
    foreach($character in [IO.Path]::GetInvalidFileNameChars()) {$Value=$Value.Replace($character,'_')}
    $Value=$Value.Replace(' ','_');if($Value.Length -gt 25) {$Value=$Value.Substring(0,25)}
    return $Value.TrimEnd('.',' ')
}
$company=SafeName $company.Trim()
$friendly=SafeName ([IO.Path]::GetFileName($Executable))
$directory=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ($company+'\'+$friendly+'_StrongName_'+$hash+'\'+$name.Version.ToString())
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
@{executable=$Executable;version=$name.Version.ToString();config=(Join-Path $directory 'user.config')} | ConvertTo-Json -Compress
