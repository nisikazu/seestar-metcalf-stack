param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$SourceDir
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Pipeline = Join-Path $Root "scripts\moving_target_pipeline.py"
$LogDir = Join-Path $Root "metcalf_output"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "metcalf-drop-$Stamp.log"

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

function Invoke-And-Capture {
    param(
        [string]$FileName,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )

    $Output = New-Object System.Collections.Generic.List[string]
    $Psi = New-Object System.Diagnostics.ProcessStartInfo
    $Psi.FileName = $FileName
    foreach ($Argument in $Arguments) {
        [void]$Psi.ArgumentList.Add($Argument)
    }
    $Psi.WorkingDirectory = $WorkingDirectory
    $Psi.UseShellExecute = $false
    $Psi.RedirectStandardOutput = $true
    $Psi.RedirectStandardError = $true
    $Psi.CreateNoWindow = $false

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $Psi
    $Process.add_OutputDataReceived({
        param($Sender, $Event)
        if ($null -ne $Event.Data) {
            Write-Host $Event.Data
            Add-Content -LiteralPath $LogPath -Value $Event.Data -Encoding UTF8
            $Output.Add($Event.Data)
        }
    })
    $Process.add_ErrorDataReceived({
        param($Sender, $Event)
        if ($null -ne $Event.Data) {
            Write-Host $Event.Data
            Add-Content -LiteralPath $LogPath -Value $Event.Data -Encoding UTF8
            $Output.Add($Event.Data)
        }
    })

    [void]$Process.Start()
    $Process.BeginOutputReadLine()
    $Process.BeginErrorReadLine()
    $Process.WaitForExit()

    return @{
        ExitCode = $Process.ExitCode
        Lines = [string[]]$Output
    }
}

$ResolvedSource = [System.IO.Path]::GetFullPath($SourceDir)
if (-not (Test-Path -LiteralPath $ResolvedSource -PathType Container)) {
    throw "Source folder was not found: $ResolvedSource"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Python = Find-Python
$Arguments = @($Pipeline, "--source-dir", $ResolvedSource)

Write-Host "Seestar Metcalf Stack"
Write-Host "Source: $ResolvedSource"
Write-Host "Log:    $LogPath"
Write-Host ""

$Result = Invoke-And-Capture -FileName $Python -Arguments $Arguments -WorkingDirectory $Root
if ($Result.ExitCode -ne 0) {
    Write-Host ""
    Write-Host "Processing failed. See log: $LogPath"
    pause
    exit $Result.ExitCode
}

$SummaryLine = $Result.Lines | Where-Object { $_ -like "Wrote pipeline summary:*" } | Select-Object -Last 1
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
pause
