param(
    [string]$Version = "0.5.0",
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageName = "seestar-metcalf-stack-v$Version"
$DistRoot = Join-Path $Root "dist"
$PackageRoot = Join-Path $DistRoot $PackageName
$ExeSource = Join-Path $Root "build\seestar-metcalf-stack.exe"

if (-not (Test-Path -LiteralPath $ExeSource)) {
    throw "Bundled executable was not found: $ExeSource. Run build-seestar-metcalf-stack-exe.ps1 first."
}

$ResolvedDistRoot = [System.IO.Path]::GetFullPath($DistRoot)
$ResolvedPackageRoot = [System.IO.Path]::GetFullPath($PackageRoot)
if (-not $ResolvedPackageRoot.StartsWith($ResolvedDistRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to package outside dist: $ResolvedPackageRoot"
}

if (Test-Path $PackageRoot) {
    Remove-Item -LiteralPath $PackageRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot "scripts") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot "tests") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot "macos") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot ".github\workflows") | Out-Null

$Files = @(
    @("scripts\moving_target_pipeline.py", "scripts\moving_target_pipeline.py"),
    @("scripts\moving_target_stack.py", "scripts\moving_target_stack.py"),
    @("scripts\horizons_ephemeris.py", "scripts\horizons_ephemeris.py"),
    @("tests\test_moving_target_options.py", "tests\test_moving_target_options.py"),
    @("scripts\astrometry_solve.py", "scripts\astrometry_solve.py"),
    @("README.md", "README.md"),
    @("README-en.md", "README-en.md"),
    @("README-macOS.md", "README-macOS.md"),
    @("requirements.txt", "requirements.txt"),
    @("seestar-metcalf-stack.cmd", "seestar-metcalf-stack.cmd"),
    @("seestar-metcalf-stack.sh", "seestar-metcalf-stack.sh"),
    @("build-seestar-metcalf-stack-exe.ps1", "build-seestar-metcalf-stack-exe.ps1"),
    @("setup-python-deps.cmd", "setup-python-deps.cmd"),
    @("setup-macos.sh", "setup-macos.sh"),
    @("set-astrometry-api-key.cmd", "set-astrometry-api-key.cmd"),
    @("set-astrometry-api-key.sh", "set-astrometry-api-key.sh"),
    @("macos\SeestarMetcalfStackLauncher.applescript", "macos\SeestarMetcalfStackLauncher.applescript"),
    @("macos\build-droplet.sh", "macos\build-droplet.sh"),
    @("siril-cli.cmd", "siril-cli.cmd"),
    @("THIRD-PARTY-NOTICES.md", "THIRD-PARTY-NOTICES.md"),
    @("LICENSE", "LICENSE"),
    @(".gitignore", ".gitignore"),
    @(".github\workflows\tests.yml", ".github\workflows\tests.yml")
)

foreach ($Pair in $Files) {
    $Source = Join-Path $Root $Pair[0]
    $Destination = Join-Path $PackageRoot $Pair[1]
    $DestinationDir = Split-Path -Parent $Destination
    if ($DestinationDir -and -not (Test-Path -LiteralPath $DestinationDir)) {
        New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null
    }
    Copy-Item -LiteralPath $Source -Destination $Destination
}

Copy-Item -LiteralPath $ExeSource -Destination (Join-Path $PackageRoot "seestar-metcalf-stack.exe")

if (-not $NoZip) {
    $ZipPath = Join-Path $DistRoot "$PackageName.zip"
    if (Test-Path $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Zip = [System.IO.Compression.ZipFile]::Open($ZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        $ZipBase = Split-Path -Parent $PackageRoot
        Get-ChildItem -LiteralPath $PackageRoot -Recurse -File |
            Where-Object { $_.FullName -notmatch "\\__pycache__\\" -and $_.Extension -ne ".pyc" } |
            ForEach-Object {
                $RelativePath = $_.FullName.Substring($ZipBase.Length + 1).Replace("\", "/")
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $Zip,
                    $_.FullName,
                    $RelativePath,
                    [System.IO.Compression.CompressionLevel]::Optimal
                ) | Out-Null
            }
    }
    finally {
        if ($Zip) {
            $Zip.Dispose()
        }
    }
    Write-Host "Wrote $ZipPath"
}

Write-Host "Wrote $PackageRoot"
