param(
    [string]$Output = (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "cacert.pem")
)

$ErrorActionPreference = "Stop"
$Url = "https://curl.se/ca/cacert.pem"
$Output = [System.IO.Path]::GetFullPath($Output)
$Parent = Split-Path -Parent $Output
New-Item -ItemType Directory -Force -Path $Parent | Out-Null

Write-Host "Downloading public CA bundle from $Url"
try {
    Invoke-WebRequest -Uri $Url -OutFile $Output -UseBasicParsing
} catch {
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        & curl.exe --fail --location --output $Output $Url
        if ($LASTEXITCODE -ne 0) {
            throw "Could not download the CA bundle (curl exit code $LASTEXITCODE)."
        }
    } else {
        throw
    }
}

$Content = Get-Content -LiteralPath $Output -Raw -ErrorAction Stop
if ($Content -notmatch "-----BEGIN CERTIFICATE-----") {
    Remove-Item -LiteralPath $Output -Force -ErrorAction SilentlyContinue
    throw "Downloaded file does not look like a PEM CA bundle: $Output"
}

Write-Host "Wrote $Output"
