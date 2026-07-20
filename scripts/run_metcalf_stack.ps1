param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"

if ($env:SEESTAR_METCALF_SOURCE) {
    $Arguments = @($env:SEESTAR_METCALF_SOURCE) + @($Arguments)
}

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Pipeline = Join-Path $Root "scripts\moving_target_pipeline.py"
$BundledExe = Join-Path $Root "seestar-metcalf-stack.exe"
$LogDir = Join-Path $Root "metcalf_output"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "metcalf-$Stamp.log"

function Find-Python {
    $Bundled = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $Bundled) {
        return $Bundled
    }
    $Command = Get-Command python -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }
    throw "Python was not found. Install Python 3.10+ and run setup-python-deps.cmd."
}

function Repair-SourceArgument {
    param(
        [string[]]$Values,
        [int]$Index
    )

    $Result = @($Values)
    if ($Result.Count -le $Index -or $Result[$Index].StartsWith("-")) {
        return $Result
    }

    $Candidate = $Result[$Index].Trim().TrimEnd([char[]]@('"', "'"))
    try {
        if (Test-Path -LiteralPath $Candidate -PathType Container -ErrorAction Stop) {
            $Resolved = (Resolve-Path -LiteralPath $Candidate).Path
            $PathRoot = [System.IO.Path]::GetPathRoot($Resolved)
            if ($Resolved.Length -gt $PathRoot.Length) {
                $Resolved = $Resolved.TrimEnd([char[]]@('\', '/'))
            }
            $Result[$Index] = $Resolved
        }
    }
    catch {
        return $Result
    }
    return $Result
}

function Format-CommandArgument {
    param([string]$Value)

    if ($Value -match '[\s"]') {
        return '"' + $Value.Replace('"', '\"') + '"'
    }
    return $Value
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (Test-Path -LiteralPath $BundledExe) {
    $Program = $BundledExe
    $ProgramArguments = @($Arguments)
    $ProgramArguments = @(Repair-SourceArgument $ProgramArguments 0)
    $Runtime = "EXE: $BundledExe"
}
else {
    $Program = Find-Python
    $ProgramArguments = @($Pipeline) + @($Arguments)
    $ProgramArguments = @(Repair-SourceArgument $ProgramArguments 1)
    $Runtime = "Python: $Program"
}

if ($ProgramArguments -notcontains "--verbose" -and $ProgramArguments -notcontains "-v") {
    $ProgramArguments += "--verbose"
}

$VerboseEnabled = $ProgramArguments -contains "--verbose" -or $ProgramArguments -contains "-v"
$FormattedArguments = $ProgramArguments | ForEach-Object { Format-CommandArgument ([string]$_) }
$CommandLine = "$(Format-CommandArgument $Program) $($FormattedArguments -join ' ')"

Write-Host "Seestar Metcalf Stack"
Write-Host "Runtime: $Runtime"
Write-Host "Verbose: $(if ($VerboseEnabled) { 'enabled' } else { 'disabled' })"
Write-Host "Command:  $CommandLine"
Write-Host "Log:     $LogPath"
Write-Host ""

Set-Content -LiteralPath $LogPath -Value @(
    "Runtime: $Runtime"
    "Verbose: $(if ($VerboseEnabled) { 'enabled' } else { 'disabled' })"
    "Command: $CommandLine"
    ""
) -Encoding UTF8

$script:SummaryPath = $null
$script:OutputDir = $null
Push-Location $Root
$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $Program @ProgramArguments 2>&1 | ForEach-Object {
        $Line = [string]$_
        Write-Host $Line
        Add-Content -LiteralPath $LogPath -Value $Line -Encoding UTF8 -ErrorAction Stop
        if ($Line -like "Wrote pipeline summary:*") {
            $script:SummaryPath = $Line.Substring("Wrote pipeline summary:".Length).Trim()
        }
        if ($Line.StartsWith("[pipeline] Work directory:")) {
            $script:OutputDir = $Line.Substring("[pipeline] Work directory:".Length).Trim()
        }
    }
    $ExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $PreviousErrorActionPreference
    Pop-Location
}

if ($ExitCode -ne 0) {
    Write-Host ""
    Write-Host "Processing failed. See log: $LogPath"
    exit $ExitCode
}

if ($script:SummaryPath -and (Test-Path -LiteralPath $script:SummaryPath)) {
    $script:OutputDir = Split-Path -Parent $script:SummaryPath
}

if ($script:OutputDir -and (Test-Path -LiteralPath $script:OutputDir)) {
    Write-Host ""
    Write-Host "Opening output folder: $script:OutputDir"
    Add-Content -LiteralPath $LogPath -Value "Opening output folder: $script:OutputDir" -Encoding UTF8
    Start-Process explorer.exe -ArgumentList "`"$script:OutputDir`""
}

Write-Host ""
Write-Host "Processing complete."
Add-Content -LiteralPath $LogPath -Value "Processing complete." -Encoding UTF8
