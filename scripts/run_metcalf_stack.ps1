param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"

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

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (Test-Path -LiteralPath $BundledExe) {
    $Program = $BundledExe
    $ProgramArguments = @($Arguments)
    $Runtime = "EXE: $BundledExe"
}
else {
    $Program = Find-Python
    $ProgramArguments = @($Pipeline) + @($Arguments)
    $Runtime = "Python: $Program"
}

Write-Host "Seestar Metcalf Stack"
Write-Host "Runtime: $Runtime"
Write-Host "Log:     $LogPath"
Write-Host ""

# Windows PowerShell 5.1 does not provide ProcessStartInfo.ArgumentList.
# Build the legacy Arguments string and read both streams after exit.
$QuotedArguments = @($ProgramArguments | ForEach-Object {
    $Value = [string]$_
    if ($Value -match '[\s"]') {
        $Value = $Value -replace '(\\+)$', '$1$1'
        '"' + ($Value -replace '"', '\\"') + '"'
    }
    else {
        $Value
    }
})

$Psi = New-Object System.Diagnostics.ProcessStartInfo
$Psi.FileName = $Program
$Psi.Arguments = $QuotedArguments -join ' '
$Psi.WorkingDirectory = $Root
$Psi.UseShellExecute = $false
$Psi.RedirectStandardOutput = $true
$Psi.RedirectStandardError = $true
$Psi.CreateNoWindow = $false
$Process = New-Object System.Diagnostics.Process
$Process.StartInfo = $Psi
[void]$Process.Start()
$StdoutTask = $Process.StandardOutput.ReadToEndAsync()
$StderrTask = $Process.StandardError.ReadToEndAsync()
$Process.WaitForExit()
$Stdout = $StdoutTask.Result
$Stderr = $StderrTask.Result

$LogLines = New-Object System.Collections.Generic.List[string]
$LogLines.Add("Runtime: $Runtime")
$LogLines.Add("Command: $Program $($ProgramArguments -join ' ')")
$LogLines.Add("")
if ($Stdout) {
    $LogLines.Add($Stdout.TrimEnd())
}
if ($Stderr) {
    $LogLines.Add($Stderr.TrimEnd())
}
Set-Content -LiteralPath $LogPath -Value ($LogLines -join [Environment]::NewLine) -Encoding UTF8

if ($Stdout) {
    Write-Host $Stdout.TrimEnd()
}
if ($Stderr) {
    Write-Host $Stderr.TrimEnd()
}

if ($Process.ExitCode -ne 0) {
    Write-Host ""
    Write-Host "Processing failed. See log: $LogPath"
    exit $Process.ExitCode
}

$SummaryLine = $Stdout -split "`r?`n" |
    Where-Object { $_ -like "Wrote pipeline summary:*" } |
    Select-Object -Last 1
if ($SummaryLine) {
    $SummaryPath = $SummaryLine.Substring("Wrote pipeline summary:".Length).Trim()
    if (Test-Path -LiteralPath $SummaryPath) {
        $OutputDir = Split-Path -Parent $SummaryPath
        Write-Host ""
        Write-Host "Opening output folder: $OutputDir"
        Start-Process explorer.exe -ArgumentList "`"$OutputDir`""
    }
}

Write-Host ""
Write-Host "Processing complete."
