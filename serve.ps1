# 啟動本地 Dashboard
# 用法：
#   .\serve.ps1                          自動尋找空閒埠（從 8790 起往後找）
#   .\serve.ps1 -Port 9000               指定起始埠，被占用時自動往後找
#   .\serve.ps1 -Port 9000 -StrictPort   指定埠被占用就直接失敗
#   .\serve.ps1 -HostAddr 0.0.0.0        讓同網段的其他裝置也能連線
param(
    [string]$Port = "auto",
    [string]$HostAddr = "127.0.0.1",
    [switch]$NoBrowser,
    [switch]$StrictPort
)

$env:PYTHONIOENCODING = "utf-8"
$ErrorActionPreference = "Stop"

$cliArgs = @("serve", "--port", $Port, "--host", $HostAddr)
if ($NoBrowser) { $cliArgs += "--no-browser" }
if ($StrictPort) { $cliArgs += "--strict-port" }

python -m tem @cliArgs
