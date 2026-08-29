param(
    [Parameter(Mandatory = $true)]
    [string]$PlateSolveFits,

    [string]$Python,
    [string]$Siril,
    [string]$AstrometryKeyFile,
    [string]$OutputDir,
    [switch]$ConfirmAstrometryUpload
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Benchmark = Join-Path $Root "developer-tools\plate-solve-benchmark\plate_solve_benchmark.py"

function Resolve-Python {
    param([string]$ExplicitPath)

    $Candidates = @()
    if ($ExplicitPath) {
        $Candidates += $ExplicitPath
    }
    $Candidates += (Join-Path $Root ".venv\Scripts\python.exe")
    $Discovered = Get-Command python -ErrorAction SilentlyContinue
    if ($Discovered) {
        $Candidates += $Discovered.Source
    }

    foreach ($Candidate in $Candidates | Select-Object -Unique) {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            continue
        }
        & $Candidate --version *> $null
        if ($LASTEXITCODE -eq 0) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    throw "A working Python runtime was not found. Pass -Python or run setup-python-deps.cmd."
}

function Invoke-Checked {
    param(
        [string]$Label,
        [string]$Program,
        [string[]]$Arguments
    )

    Write-Host ""
    Write-Host "== $Label =="
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Assert-FitsFile {
    param([string]$Path, [string]$Label)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label did not produce a WCS FITS: $Path"
    }
    $Stream = [IO.File]::OpenRead($Path)
    try {
        $Buffer = New-Object byte[] 8
        $Read = $Stream.Read($Buffer, 0, $Buffer.Length)
    }
    finally {
        $Stream.Dispose()
    }
    $Prefix = [Text.Encoding]::ASCII.GetString($Buffer, 0, $Read)
    if (-not $Prefix.StartsWith("SIMPLE")) {
        throw "$Label returned a non-FITS WCS file: $Path"
    }
}

if (-not $ConfirmAstrometryUpload) {
    throw "Release validation uploads one sanitized FITS to Astrometry.net. Review the input, then add -ConfirmAstrometryUpload."
}

$PlateSolveFits = (Resolve-Path -LiteralPath $PlateSolveFits).Path
$Python = Resolve-Python $Python
if ($Siril) {
    $Siril = (Resolve-Path -LiteralPath $Siril).Path
}
if ($AstrometryKeyFile) {
    $AstrometryKeyFile = (Resolve-Path -LiteralPath $AstrometryKeyFile).Path
}
if (-not $OutputDir) {
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputDir = Join-Path $Root "developer-tools\plate-solve-benchmark\results\release-smoke-$Stamp"
}
$OutputDir = [IO.Path]::GetFullPath($OutputDir)

Push-Location $Root
try {
    Invoke-Checked "Unit tests" $Python @("-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py")
    Invoke-Checked "Python syntax" $Python @(
        "-m", "py_compile",
        "scripts\moving_target_pipeline.py",
        "scripts\moving_target_stack.py",
        "scripts\siril_preprocessing.py",
        "scripts\astrometry_solve.py",
        "developer-tools\plate-solve-benchmark\plate_solve_benchmark.py",
        "developer-tools\wcs-comparison\compare_wcs.py"
    )
    Invoke-Checked "Git whitespace check" "git" @("diff", "--check")

    $BenchmarkArguments = @(
        $Benchmark,
        $PlateSolveFits,
        "--repeats", "1",
        "--solver", "both",
        "--scale-case", "correct",
        "--strip-input-wcs",
        "--timeout-seconds", "900",
        "--astrometry-delay-seconds", "0",
        "--confirm-astrometry-uploads",
        "--output-dir", $OutputDir
    )
    if ($Siril) {
        $BenchmarkArguments += @("--siril", $Siril)
    }
    if ($AstrometryKeyFile) {
        $BenchmarkArguments += @("--astrometry-key-file", $AstrometryKeyFile)
    }
    Invoke-Checked "Live Siril and Astrometry.net plate solves" $Python $BenchmarkArguments

    $RunsPath = Join-Path $OutputDir "benchmark_runs.csv"
    if (-not (Test-Path -LiteralPath $RunsPath -PathType Leaf)) {
        throw "Plate-solve benchmark did not write benchmark_runs.csv"
    }
    $Rows = @(Import-Csv -LiteralPath $RunsPath)
    foreach ($Solver in @("siril", "astrometry")) {
        $Row = $Rows | Where-Object { $_.solver -eq $Solver } | Select-Object -First 1
        if (-not $Row) {
            throw "Plate-solve benchmark did not run $Solver"
        }
        if ($Row.status -ne "success") {
            throw "$Solver plate solve did not succeed: status=$($Row.status); error=$($Row.error)"
        }
        $RunDir = Split-Path -Parent $Row.log_path
        $WcsPath = if ($Solver -eq "siril") {
            Join-Path $RunDir "siril_solved.fit"
        }
        else {
            Join-Path $RunDir "astrometry_wcs.fits"
        }
        Assert-FitsFile $WcsPath "$Solver plate solve"
        Write-Host ("{0}: success in {1:N2}s; WCS={2}" -f $Solver, [double]$Row.elapsed_seconds, $WcsPath)
    }

    Write-Host ""
    Write-Host "Release validation passed: $OutputDir"
}
finally {
    Pop-Location
}
