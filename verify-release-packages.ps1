param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [ValidateSet("All", "Standard", "Siril")]
    [string]$Package = "All",

    [string]$DistRoot,

    [switch]$SkipDirectoryCheck
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $DistRoot) {
    $DistRoot = Join-Path $Root "dist"
}
$Manifest = Import-PowerShellDataFile (Join-Path $Root "release-package-manifest.psd1")

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-NormalizedRelativePath {
    param([string]$Path)
    return $Path.Replace("\", "/").TrimStart("/")
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw $Message
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

function Test-ReleasePackage {
    param(
        [string]$Kind,
        [bool]$IncludeSiril
    )

    $Name = if ($IncludeSiril) {
        "seestar-metcalf-stack-siril-v$Version"
    }
    else {
        "seestar-metcalf-stack-v$Version"
    }
    $PackageRoot = Join-Path $DistRoot $Name
    $ZipPath = Join-Path $DistRoot "$Name.zip"

    Write-Host "Verifying $Kind package: $Name"
    Assert-True (Test-Path -LiteralPath $ZipPath -PathType Leaf) "Release ZIP not found: $ZipPath"

    if (-not $SkipDirectoryCheck) {
        Assert-True (Test-Path -LiteralPath $PackageRoot -PathType Container) "Package directory not found: $PackageRoot"
        foreach ($Relative in @($Manifest.CommonFiles) + @($Manifest.GeneratedFiles)) {
            Assert-True (Test-Path -LiteralPath (Join-Path $PackageRoot $Relative) -PathType Leaf) "Missing package file: $Relative"
        }
        $Exe = Get-Item -LiteralPath (Join-Path $PackageRoot "seestar-metcalf-stack.exe")
        $Ca = Get-Item -LiteralPath (Join-Path $PackageRoot "cacert.pem")
        Assert-True ($Exe.Length -ge $Manifest.ExecutableMinimumBytes) "Bundled executable is unexpectedly small: $($Exe.Length) bytes"
        Assert-True ($Ca.Length -ge $Manifest.CaBundleMinimumBytes) "CA bundle is unexpectedly small: $($Ca.Length) bytes"
    }

    $Zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $Entries = @($Zip.Entries | Where-Object { $_.Name })
        $EntryMap = @{}
        foreach ($Entry in $Entries) {
            $EntryMap[(Get-NormalizedRelativePath $Entry.FullName)] = $Entry
        }
        $Prefix = "$Name/"
        foreach ($Relative in @($Manifest.CommonFiles) + @($Manifest.GeneratedFiles)) {
            $Expected = $Prefix + (Get-NormalizedRelativePath $Relative)
            Assert-True $EntryMap.ContainsKey($Expected) "ZIP is missing required file: $Expected"
        }

        $SirilPrefix = $Prefix + "tools/siril-$($Manifest.SirilVersion)/siril/"
        $SirilEntries = @($Entries | Where-Object { (Get-NormalizedRelativePath $_.FullName).StartsWith($SirilPrefix, [StringComparison]::OrdinalIgnoreCase) })
        if ($IncludeSiril) {
            foreach ($Relative in $Manifest.SirilOnlyFiles) {
                $Expected = $Prefix + (Get-NormalizedRelativePath $Relative)
                Assert-True $EntryMap.ContainsKey($Expected) "Siril ZIP is missing license/source file: $Expected"
            }
            foreach ($Relative in $Manifest.SirilRequiredFiles) {
                $Expected = $SirilPrefix + (Get-NormalizedRelativePath $Relative)
                Assert-True $EntryMap.ContainsKey($Expected) "Siril ZIP is missing runtime file: $Expected"
            }
            $SirilBytes = [int64](($SirilEntries | Measure-Object -Property Length -Sum).Sum)
            Assert-True ($SirilEntries.Count -ge $Manifest.SirilMinimumFileCount) "Siril ZIP has only $($SirilEntries.Count) runtime files; expected at least $($Manifest.SirilMinimumFileCount)"
            Assert-True ($SirilBytes -ge $Manifest.SirilMinimumBytes) "Siril ZIP contains only $SirilBytes uncompressed runtime bytes; expected at least $($Manifest.SirilMinimumBytes)"
        }
        else {
            Assert-True ($SirilEntries.Count -eq 0) "Standard ZIP unexpectedly contains $($SirilEntries.Count) Siril runtime files"
        }

        Write-Host ("  OK: {0} entries, {1:N2} MiB ZIP" -f $Entries.Count, ((Get-Item -LiteralPath $ZipPath).Length / 1MB))
        if ($IncludeSiril) {
            Write-Host ("  Siril: {0} files, {1:N2} MiB uncompressed" -f $SirilEntries.Count, ($SirilBytes / 1MB))
        }
    }
    finally {
        $Zip.Dispose()
    }

    return Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256
}

$Hashes = @()
if ($Package -in @("All", "Standard")) {
    $Hashes += Test-ReleasePackage -Kind "standard" -IncludeSiril $false
}
if ($Package -in @("All", "Siril")) {
    $Hashes += Test-ReleasePackage -Kind "Siril-bundled" -IncludeSiril $true
}

$ChecksumPath = Join-Path $DistRoot "SHA256SUMS-v$Version.txt"
$Lines = $Hashes | ForEach-Object { "$($_.Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($_.Path))" }
[IO.File]::WriteAllLines($ChecksumPath, $Lines, [Text.UTF8Encoding]::new($false))
Write-Host "Wrote $ChecksumPath"
