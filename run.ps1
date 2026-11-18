# 執行完整流程：抓賠率 + 抓新聞 + AI 分析 + 融合預測 + 產出報告
# 用法：
#   .\run.ps1                              分析上限 150 則
#   .\run.ps1 -Limit 300 -Window 30
#   .\run.ps1 -Force                       重新分析已分析過的新聞
#   .\run.ps1 -NoLlm                       強制使用規則式引擎
param(
    [int]$Limit = 150,
    [int]$Days = 7,
    [int]$Window = 7,
    [switch]$Force,
    [switch]$NoLlm
)

$env:PYTHONIOENCODING = "utf-8"
$ErrorActionPreference = "Stop"

$cliArgs = @("run", "--limit", $Limit, "--days", $Days, "--window", $Window)
if ($Force) { $cliArgs += "--force" }
if ($NoLlm) { $cliArgs += "--no-llm" }

python -m tem @cliArgs
