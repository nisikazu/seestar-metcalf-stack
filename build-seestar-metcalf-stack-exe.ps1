param(
    [string]$Python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [string]$PyInstallerRuntime = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyInstallerVersion = "6.22.1"
$PyInstallerPath = if ([string]::IsNullOrWhiteSpace($PyInstallerRuntime)) {
    Join-Path $Root ".build\pyinstaller-runtime-$PyInstallerVersion"
} else {
    [System.IO.Path]::GetFullPath($PyInstallerRuntime)
}
$BuildRoot = Join-Path $Root "build\pyinstaller"
$DistRoot = Join-Path $Root "build"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python executable was not found: $Python"
}
$PyInstallerReady = $false
if (Test-Path -LiteralPath $PyInstallerPath) {
    # The isolated runtime is built by pip --target. Checking its entry module
    # avoids a probe subprocess whose exit status is unreliable in PowerShell.
    $PyInstallerReady = Test-Path -LiteralPath (Join-Path $PyInstallerPath "PyInstaller\__main__.py") -PathType Leaf
}
if (-not $PyInstallerReady) {
    Write-Host "Installing PyInstaller build dependencies into $PyInstallerPath"
    New-Item -ItemType Directory -Force -Path $PyInstallerPath | Out-Null
    & $Python -m pip install --target $PyInstallerPath "pyinstaller==$PyInstallerVersion"
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
    --hidden-import fits_preview `
    --hidden-import horizons_ephemeris `
    --hidden-import moving_target_stack `
    --hidden-import sharpcap_stacklog `
    --hidden-import siril_preprocessing `
    --hidden-import sun_pa `
    (Join-Path $Root "scripts\moving_target_pipeline.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}
Copy-Item -LiteralPath (Join-Path $Root "siril-cli.cmd") -Destination (Join-Path $DistRoot "siril-cli.cmd") -Force
Write-Host "Wrote $(Join-Path $DistRoot 'seestar-metcalf-stack.exe')"
