<#
    Freeze the current code base into a versioned snapshot.

    Usage:
        powershell -NoProfile -ExecutionPolicy Bypass -File tools\snapshot.ps1 -Tag v4
        powershell -NoProfile -ExecutionPolicy Bypass -File tools\snapshot.ps1 -Tag v4 -Date 20261019 -PortableZip dist\tem-portable-20261016.zip

    Output (under dist\versions):
        <tag>-<date>\src\...             source tree (no data\, no dist\)
        <tag>-<date>\samples\...         latest report.html / report.md / screenshots
        tem-src-<tag>-<date>.zip         zipped source
        tem-portable-<tag>-<date>.zip    copy of the portable package (if found)
        VERSION.json                     tag, date, sizes, sha256

    The script never deletes anything outside dist\versions.
#>
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$Date = (Get-Date -Format "yyyyMMdd"),
    [string]$PortableZip = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$outRoot = Join-Path $root "dist\versions"
$name = "$Tag-$Date"
$stage = Join-Path $outRoot $name

function Assert-Under {
    param([string]$Path, [string]$Base)
    $full = [System.IO.Path]::GetFullPath($Path)
    $guard = [System.IO.Path]::GetFullPath($Base)
    if (-not $full.StartsWith($guard + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify path outside $guard : $full"
    }
    return $full
}

New-Item -ItemType Directory -Path $outRoot -Force | Out-Null

if (Test-Path $stage) {
    if (-not $Force) { throw "Snapshot already exists: $stage (use -Force to overwrite)" }
    Remove-Item -LiteralPath (Assert-Under $stage $outRoot) -Recurse -Force
}
New-Item -ItemType Directory -Path $stage | Out-Null

$src = Join-Path $stage "src"
New-Item -ItemType Directory -Path $src | Out-Null

$items = @(
    "tem", "web", "config", "tools",
    "README.md", "VERSIONS.md", "requirements.txt", "package.ps1",
    "run.ps1", "serve.ps1", ".env.example", ".gitignore"
)
foreach ($item in $items) {
    $p = Join-Path $root $item
    if (Test-Path -LiteralPath $p) {
        Copy-Item -LiteralPath $p -Destination $src -Recurse -Force
    }
}

# Drop byte-code caches so the archive stays small and portable.
Get-ChildItem -Path $src -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item -LiteralPath (Assert-Under $_.FullName $src) -Recurse -Force
}

$samples = Join-Path $stage "samples"
New-Item -ItemType Directory -Path $samples | Out-Null
foreach ($f in @("data\exports\report.html", "data\exports\report.md")) {
    $p = Join-Path $root $f
    if (Test-Path -LiteralPath $p) { Copy-Item -LiteralPath $p -Destination $samples -Force }
}
Get-ChildItem -Path (Join-Path $root "data\exports") -Filter "shot-*.png" -ErrorAction SilentlyContinue | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $samples -Force
}

Add-Type -AssemblyName System.IO.Compression.FileSystem

$srcZip = Join-Path $outRoot "tem-src-$name.zip"
if (Test-Path -LiteralPath $srcZip) { Remove-Item -LiteralPath (Assert-Under $srcZip $outRoot) -Force }
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $src, $srcZip, [System.IO.Compression.CompressionLevel]::Optimal, $true
)

if (-not $PortableZip) {
    $latest = Get-ChildItem -Path (Join-Path $root "dist") -Filter "tem-portable-*.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) { $PortableZip = $latest.FullName }
}

$portableOut = ""
if ($PortableZip -and (Test-Path -LiteralPath $PortableZip)) {
    $portableOut = Join-Path $outRoot "tem-portable-$name.zip"
    if (Test-Path -LiteralPath $portableOut) { Remove-Item -LiteralPath (Assert-Under $portableOut $outRoot) -Force }
    Copy-Item -LiteralPath $PortableZip -Destination $portableOut -Force
}

$srcCount = (Get-ChildItem -LiteralPath $src -Recurse -File | Measure-Object).Count
$srcSize = [math]::Round((Get-ChildItem -LiteralPath $src -Recurse -File | Measure-Object Length -Sum).Sum / 1KB, 1)

Write-Output "[snapshot] tag=$Tag date=$Date"
Write-Output "[snapshot] source dir : $stage ($srcCount files, $srcSize KB)"
Write-Output "[snapshot] source zip : $srcZip"
if ($portableOut) { Write-Output "[snapshot] portable   : $portableOut" }
else { Write-Output "[snapshot] portable   : (not found, skipped)" }
