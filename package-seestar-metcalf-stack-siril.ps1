param(
    [string]$Version = "0.4.5",
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageName = "seestar-metcalf-stack-siril-v$Version"
$DistRoot = Join-Path $Root "dist"
$PackageRoot = Join-Path $DistRoot $PackageName
$SirilSource = Join-Path $Root "tools\siril-1.4.1\siril"
$SirilTarget = Join-Path $PackageRoot "tools\siril-1.4.1\siril"
$ExeSource = Join-Path $Root "build\seestar-metcalf-stack.exe"

if (-not (Test-Path $SirilSource)) {
    throw "Bundled Siril was not found: $SirilSource"
}

if (-not (Test-Path -LiteralPath $ExeSource)) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "build-seestar-metcalf-stack-exe.ps1")
}
if (-not (Test-Path $ExeSource)) {
    throw "Bundled Python executable was not found: $ExeSource"
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
New-Item -ItemType Directory -Force -Path (Join-Path $PackageRoot "tools\siril-1.4.1") | Out-Null

Copy-Item -LiteralPath (Join-Path $Root "scripts\moving_target_pipeline.py") -Destination (Join-Path $PackageRoot "scripts\moving_target_pipeline.py")
Copy-Item -LiteralPath (Join-Path $Root "scripts\moving_target_stack.py") -Destination (Join-Path $PackageRoot "scripts\moving_target_stack.py")
Copy-Item -LiteralPath (Join-Path $Root "scripts\horizons_ephemeris.py") -Destination (Join-Path $PackageRoot "scripts\horizons_ephemeris.py")
Copy-Item -LiteralPath (Join-Path $Root "scripts\astrometry_solve.py") -Destination (Join-Path $PackageRoot "scripts\astrometry_solve.py")
Copy-Item -LiteralPath (Join-Path $Root "README.md") -Destination (Join-Path $PackageRoot "README.md")
Copy-Item -LiteralPath (Join-Path $Root "README-en.md") -Destination (Join-Path $PackageRoot "README-en.md")
Copy-Item -LiteralPath (Join-Path $Root "README-macOS.md") -Destination (Join-Path $PackageRoot "README-macOS.md")
Copy-Item -LiteralPath (Join-Path $Root "requirements.txt") -Destination (Join-Path $PackageRoot "requirements.txt")
Copy-Item -LiteralPath (Join-Path $Root "seestar-metcalf-stack.cmd") -Destination (Join-Path $PackageRoot "seestar-metcalf-stack.cmd")
Copy-Item -LiteralPath (Join-Path $Root "seestar-metcalf-stack.sh") -Destination (Join-Path $PackageRoot "seestar-metcalf-stack.sh")
Copy-Item -LiteralPath (Join-Path $Root "setup-python-deps.cmd") -Destination (Join-Path $PackageRoot "setup-python-deps.cmd")
Copy-Item -LiteralPath (Join-Path $Root "setup-macos.sh") -Destination (Join-Path $PackageRoot "setup-macos.sh")
Copy-Item -LiteralPath (Join-Path $Root "set-astrometry-api-key.cmd") -Destination (Join-Path $PackageRoot "set-astrometry-api-key.cmd")
Copy-Item -LiteralPath (Join-Path $Root "set-astrometry-api-key.sh") -Destination (Join-Path $PackageRoot "set-astrometry-api-key.sh")
Copy-Item -LiteralPath (Join-Path $Root "macos\SeestarMetcalfStackLauncher.applescript") -Destination (Join-Path $PackageRoot "macos\SeestarMetcalfStackLauncher.applescript")
Copy-Item -LiteralPath (Join-Path $Root "macos\build-droplet.sh") -Destination (Join-Path $PackageRoot "macos\build-droplet.sh")
Copy-Item -LiteralPath (Join-Path $Root "siril-cli.cmd") -Destination (Join-Path $PackageRoot "siril-cli.cmd")
Copy-Item -LiteralPath (Join-Path $Root "THIRD-PARTY-NOTICES.md") -Destination (Join-Path $PackageRoot "THIRD-PARTY-NOTICES.md")
Copy-Item -LiteralPath (Join-Path $Root "SIRIL-SOURCE.txt") -Destination (Join-Path $PackageRoot "SIRIL-SOURCE.txt")
Copy-Item -LiteralPath (Join-Path $Root "build-seestar-metcalf-stack-exe.ps1") -Destination (Join-Path $PackageRoot "build-seestar-metcalf-stack-exe.ps1")
Copy-Item -LiteralPath (Join-Path $Root "tests\test_moving_target_options.py") -Destination (Join-Path $PackageRoot "tests\test_moving_target_options.py")
Copy-Item -LiteralPath (Join-Path $Root "LICENSE") -Destination (Join-Path $PackageRoot "LICENSE")
Copy-Item -LiteralPath (Join-Path $Root ".gitignore") -Destination (Join-Path $PackageRoot ".gitignore")
Copy-Item -LiteralPath (Join-Path $Root ".github\workflows\tests.yml") -Destination (Join-Path $PackageRoot ".github\workflows\tests.yml")
Copy-Item -LiteralPath $ExeSource -Destination (Join-Path $PackageRoot "seestar-metcalf-stack.exe")

Copy-Item -LiteralPath $SirilSource -Destination $SirilTarget -Recurse

$SirilLicenseSource = Join-Path $SirilTarget "share\doc\siril\LICENSE.md"
if (Test-Path $SirilLicenseSource) {
    Copy-Item -LiteralPath $SirilLicenseSource -Destination (Join-Path $PackageRoot "SIRIL-LICENSE-GPLv3.md")
}

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
