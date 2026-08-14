param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")]
    [string]$Version,

    [ValidateSet("All", "Standard", "Siril")]
    [string]$Package = "All",

    [switch]$SkipExeBuild,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistRoot = Join-Path $Root "dist"
$Manifest = Import-PowerShellDataFile (Join-Path $Root "release-package-manifest.psd1")
$ExeSource = Join-Path $Root "build\seestar-metcalf-stack.exe"
$CaBundleSource = Join-Path $Root "cacert.pem"
$SirilSource = Join-Path $Root "tools\siril-$($Manifest.SirilVersion)\siril"

function Assert-InsideDist {
    param([string]$Path)
    $ResolvedDist = [IO.Path]::GetFullPath($DistRoot).TrimEnd("\") + "\"
    $ResolvedPath = [IO.Path]::GetFullPath($Path)
    if (-not $ResolvedPath.StartsWith($ResolvedDist, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to write outside dist: $ResolvedPath"
    }
}

function Get-TreeStats {
    param([string]$Path)
    $Files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force)
    return @{
        Count = $Files.Count
        Bytes = [int64](($Files | Measure-Object -Property Length -Sum).Sum)
    }
}

function Copy-ManifestFiles {
    param([string]$PackageRoot)
    foreach ($Relative in $Manifest.CommonFiles) {
        $Source = Join-Path $Root $Relative
        $Destination = Join-Path $PackageRoot $Relative
        if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
            throw "Release manifest source file not found: $Relative"
        }
        $Parent = Split-Path -Parent $Destination
        if ($Parent) {
            New-Item -ItemType Directory -Force -Path $Parent | Out-Null
        }
        Copy-Item -LiteralPath $Source -Destination $Destination
    }
    Copy-Item -LiteralPath $ExeSource -Destination (Join-Path $PackageRoot "seestar-metcalf-stack.exe")
    Copy-Item -LiteralPath $CaBundleSource -Destination (Join-Path $PackageRoot "cacert.pem")
}

function Write-ContentsChecksum {
    param([string]$PackageRoot)
    $ChecksumPath = Join-Path $PackageRoot "PACKAGE-CONTENTS.sha256"
    $Lines = Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Force |
        Where-Object {
            $_.FullName -ne $ChecksumPath -and
            $_.FullName -notmatch "\\__pycache__\\" -and
            $_.Extension -ne ".pyc"
        } |
        Sort-Object FullName |
        ForEach-Object {
            $Relative = $_.FullName.Substring($PackageRoot.Length + 1).Replace("\", "/")
            $Hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            "$Hash  $Relative"
        }
    [IO.File]::WriteAllLines($ChecksumPath, $Lines, [Text.UTF8Encoding]::new($false))
}

function New-Zip {
    param([string]$PackageRoot, [string]$ZipPath)
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Zip = [IO.Compression.ZipFile]::Open($ZipPath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $ZipBase = Split-Path -Parent $PackageRoot
        Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Force |
            Where-Object { $_.FullName -notmatch "\\__pycache__\\" -and $_.Extension -ne ".pyc" } |
            ForEach-Object {
                $Relative = $_.FullName.Substring($ZipBase.Length + 1).Replace("\", "/")
                [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $Zip,
                    $_.FullName,
                    $Relative,
                    [IO.Compression.CompressionLevel]::Optimal
                ) | Out-Null
            }
    }
    finally {
        $Zip.Dispose()
    }
}

function New-ReleasePackage {
    param([bool]$IncludeSiril)
    $Name = if ($IncludeSiril) { "seestar-metcalf-stack-siril-v$Version" } else { "seestar-metcalf-stack-v$Version" }
    $PackageRoot = Join-Path $DistRoot $Name
    $ZipPath = Join-Path $DistRoot "$Name.zip"
    Assert-InsideDist $PackageRoot
    Assert-InsideDist $ZipPath

    if (Test-Path -LiteralPath $PackageRoot) {
        Remove-Item -LiteralPath $PackageRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
    Copy-ManifestFiles $PackageRoot

    if ($IncludeSiril) {
        if (-not (Test-Path -LiteralPath $SirilSource -PathType Container)) {
            throw "Pinned Siril source was not found: $SirilSource"
        }
        foreach ($Relative in $Manifest.SirilRequiredFiles) {
            if (-not (Test-Path -LiteralPath (Join-Path $SirilSource $Relative) -PathType Leaf)) {
                throw "Pinned Siril source is incomplete; missing $Relative"
            }
        }
        $SourceStats = Get-TreeStats $SirilSource
        if ($SourceStats.Count -lt $Manifest.SirilMinimumFileCount -or $SourceStats.Bytes -lt $Manifest.SirilMinimumBytes) {
            throw "Pinned Siril source is incomplete: $($SourceStats.Count) files, $($SourceStats.Bytes) bytes"
        }

        $SirilTarget = Join-Path $PackageRoot "tools\siril-$($Manifest.SirilVersion)\siril"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SirilTarget) | Out-Null
        Copy-Item -LiteralPath $SirilSource -Destination $SirilTarget -Recurse -Force
        $TargetStats = Get-TreeStats $SirilTarget
        if ($TargetStats.Count -ne $SourceStats.Count -or $TargetStats.Bytes -ne $SourceStats.Bytes) {
            throw "Siril copy verification failed: source=$($SourceStats.Count) files/$($SourceStats.Bytes) bytes; target=$($TargetStats.Count) files/$($TargetStats.Bytes) bytes"
        }

        foreach ($Relative in $Manifest.SirilOnlySourceFiles) {
            Copy-Item -LiteralPath (Join-Path $Root $Relative) -Destination (Join-Path $PackageRoot $Relative)
        }
        $LicenseSource = Join-Path $SirilTarget "share\doc\siril\LICENSE.md"
        if (Test-Path -LiteralPath $LicenseSource) {
            Copy-Item -LiteralPath $LicenseSource -Destination (Join-Path $PackageRoot "SIRIL-LICENSE-GPLv3.md") -Force
        }
        elseif (-not (Test-Path -LiteralPath (Join-Path $PackageRoot "SIRIL-LICENSE-GPLv3.md"))) {
            throw "Siril license was not found in the pinned distribution"
        }
        Write-Host ("Copied Siril: {0} files, {1:N2} MiB" -f $TargetStats.Count, ($TargetStats.Bytes / 1MB))
    }

    Write-ContentsChecksum $PackageRoot
    if (-not $NoZip) {
        New-Zip $PackageRoot $ZipPath
        Write-Host "Wrote $ZipPath"
    }
    Write-Host "Wrote $PackageRoot"
}

if (-not $SkipExeBuild) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "build-seestar-metcalf-stack-exe.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Executable build failed with exit code $LASTEXITCODE"
    }
}
if (-not (Test-Path -LiteralPath $ExeSource -PathType Leaf)) {
    throw "Bundled executable was not found: $ExeSource"
}
if ((Get-Item -LiteralPath $ExeSource).Length -lt $Manifest.ExecutableMinimumBytes) {
    throw "Bundled executable is unexpectedly small: $ExeSource"
}
if (-not (Test-Path -LiteralPath $CaBundleSource -PathType Leaf)) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "get-cacert.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "CA bundle download failed with exit code $LASTEXITCODE"
    }
}
if ((Get-Item -LiteralPath $CaBundleSource).Length -lt $Manifest.CaBundleMinimumBytes) {
    throw "CA bundle is unexpectedly small: $CaBundleSource"
}

New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
if ($Package -in @("All", "Standard")) {
    New-ReleasePackage -IncludeSiril $false
}
if ($Package -in @("All", "Siril")) {
    New-ReleasePackage -IncludeSiril $true
}

if (-not $NoZip) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root "verify-release-packages.ps1") -Version $Version -Package $Package -DistRoot $DistRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Release ZIP verification failed with exit code $LASTEXITCODE"
    }
}
