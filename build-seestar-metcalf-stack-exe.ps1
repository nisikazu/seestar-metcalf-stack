param(
    [string]$Python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyInstallerPath = Join-Path $Root ".build\pyinstaller"
$BuildRoot = Join-Path $Root "build\pyinstaller"
$DistRoot = Join-Path $Root "build"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python executable was not found: $Python"
}
if (-not (Test-Path -LiteralPath $PyInstallerPath)) {
    Write-Host "Installing PyInstaller build dependencies into $PyInstallerPath"
    New-Item -ItemType Directory -Force -Path $PyInstallerPath | Out-Null
    & $Python -m pip install --target $PyInstallerPath pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Installing PyInstaller failed with exit code $LASTEXITCODE"
    }
}

New-Item -ItemType Directory -Force -Path $BuildRoot, $DistRoot | Out-Null
$env:PYTHONPATH = $PyInstallerPath
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --name seestar-metcalf-stack `
    --distpath $DistRoot `
    --workpath $BuildRoot `
    --specpath $BuildRoot `
    --paths (Join-Path $Root "scripts") `
    --hidden-import astrometry_solve `
    --hidden-import horizons_ephemeris `
    --hidden-import moving_target_stack `
    (Join-Path $Root "scripts\moving_target_pipeline.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}
Copy-Item -LiteralPath (Join-Path $Root "siril-cli.cmd") -Destination (Join-Path $DistRoot "siril-cli.cmd") -Force
Write-Host "Wrote $(Join-Path $DistRoot 'seestar-metcalf-stack.exe')"
