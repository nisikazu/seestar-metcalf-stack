[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$SeestarHost = "",

    [string]$ShareName = "EMMC Images",
    [string]$Subfolder = "MyWorks",

    [switch]$FindOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-IPv4Address {
    param([Parameter(Mandatory = $true)][string]$NameOrAddress)

    $parsed = $null
    if ([System.Net.IPAddress]::TryParse($NameOrAddress, [ref]$parsed)) {
        if ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
            return @($parsed.IPAddressToString)
        }
        return @()
    }

    $addresses = @()
    try {
        $addresses += Resolve-DnsName -Name $NameOrAddress -Type A -ErrorAction Stop |
            Where-Object { $_.Type -eq "A" -and $_.IPAddress } |
            ForEach-Object { $_.IPAddress }
    } catch {
        # Resolve-DnsName may not use every mDNS provider. Try the .NET resolver too.
    }

    try {
        $addresses += [System.Net.Dns]::GetHostAddresses($NameOrAddress) |
            Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
            ForEach-Object { $_.IPAddressToString }
    } catch {
        # Discovery continues with the AP address and subnet scan.
    }

    return @($addresses | Where-Object { $_ } | Sort-Object -Unique)
}

function Test-TcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$Address,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMs = 700
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect($Address, $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            return $false
        }
        $client.EndConnect($pending)
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-Local24Prefixes {
    $prefixes = @()
    try {
        $prefixes = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object {
                $_.PrefixLength -eq 24 -and
                $_.AddressState -eq "Preferred" -and
                -not $_.SkipAsSource -and
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*"
            } |
            ForEach-Object {
                $parts = $_.IPAddress.Split(".")
                if ($parts.Count -eq 4) { $parts[0..2] -join "." }
            } |
            Sort-Object -Unique
    } catch {
        return @()
    }
    return @($prefixes | Select-Object -First 4)
}

function Find-SeestarOnLocalSubnets {
    param([int]$Port = 4700)

    foreach ($prefix in Get-Local24Prefixes) {
        Write-Host "Scanning $prefix.0/24 for a Seestar..."
        $attempts = @()
        for ($lastOctet = 1; $lastOctet -le 254; $lastOctet += 1) {
            $address = "$prefix.$lastOctet"
            $client = New-Object System.Net.Sockets.TcpClient
            try {
                $pending = $client.BeginConnect($address, $Port, $null, $null)
                $attempts += [pscustomobject]@{
                    Address = $address
                    Client = $client
                    Pending = $pending
                }
            } catch {
                $client.Close()
            }
        }

        Start-Sleep -Milliseconds 900
        $found = @()
        foreach ($attempt in $attempts) {
            try {
                if ($attempt.Pending.IsCompleted -and $attempt.Client.Connected) {
                    $attempt.Client.EndConnect($attempt.Pending)
                    $found += $attempt.Address
                }
            } catch {
                # A refused or timed-out connection is expected for most addresses.
            } finally {
                $attempt.Client.Close()
            }
        }
        if ($found.Count -gt 0) {
            return @($found | Sort-Object -Unique)
        }
    }
    return @()
}

function Get-OptionalBooleanProperty {
    param(
        [Parameter(Mandatory = $true)][object]$RegistryValues,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $property = $RegistryValues.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        return $null
    }
    return [bool]([int]$property.Value)
}

function Get-SmbClientPolicy {
    $policy = [ordered]@{
        EnableInsecureGuestLogons = $null
        RequireSecuritySignature = $null
        RequireEncryption = $null
        Source = "unavailable"
    }

    try {
        $configuration = Get-SmbClientConfiguration -ErrorAction Stop
        $policy.EnableInsecureGuestLogons = Get-OptionalBooleanProperty $configuration "EnableInsecureGuestLogons"
        $policy.RequireSecuritySignature = Get-OptionalBooleanProperty $configuration "RequireSecuritySignature"
        $policy.RequireEncryption = Get-OptionalBooleanProperty $configuration "RequireEncryption"
        $policy.Source = "Get-SmbClientConfiguration"
        return [pscustomobject]$policy
    } catch {
        # Reading the effective SMB configuration can require elevation on some PCs.
    }

    try {
        $registryPath = "HKLM:\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters"
        $registryValues = Get-ItemProperty -LiteralPath $registryPath -ErrorAction Stop
        $policy.EnableInsecureGuestLogons = Get-OptionalBooleanProperty $registryValues "AllowInsecureGuestAuth"
        $policy.RequireSecuritySignature = Get-OptionalBooleanProperty $registryValues "RequireSecuritySignature"
        $policy.RequireEncryption = Get-OptionalBooleanProperty $registryValues "RequireEncryption"
        $policy.Source = "LanmanWorkstation registry"
    } catch {
        # The guidance below remains useful even when policy values cannot be read.
    }
    return [pscustomobject]$policy
}

function Format-PolicyValue {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) { return "not explicitly configured / unavailable" }
    if ([bool]$Value) { return "enabled" }
    return "disabled"
}

function Test-SmbPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        $null = Get-Item -LiteralPath $Path -ErrorAction Stop
        return [pscustomobject]@{
            Path = $Path
            Status = "available"
            Message = ""
            HResult = ""
        }
    } catch {
        $hresult = "0x{0:X8}" -f ($_.Exception.HResult -band 0xffffffffL)
        $status = if ($_.Exception -is [System.UnauthorizedAccessException] -or $hresult -eq "0x80070005") {
            "access-denied"
        } elseif ($hresult -in @("0x80070035", "0x80070043")) {
            "path-not-found"
        } else {
            "error"
        }
        return [pscustomobject]@{
            Path = $Path
            Status = $status
            Message = $_.Exception.Message
            HResult = $hresult
        }
    }
}

function Test-SeestarCandidates {
    param(
        [Parameter(Mandatory = $true)][object[]]$Candidates,
        [Parameter(Mandatory = $true)][string]$ShareName,
        [Parameter(Mandatory = $true)][string]$Subfolder,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Reachable,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Failures
    )

    foreach ($candidate in $Candidates) {
        $controlOpen = Test-TcpPort $candidate.Address 4700
        $smbOpen = Test-TcpPort $candidate.Address 445
        Write-Host ("Candidate {0} ({1}): TCP 4700={2}; TCP 445={3}" -f `
            $candidate.Address, $candidate.Source, $controlOpen, $smbOpen)
        if (-not $controlOpen -and -not $smbOpen) { continue }

        $Reachable.Add([pscustomobject]@{
            Address = $candidate.Address
            Source = $candidate.Source
            ControlOpen = $controlOpen
            SmbOpen = $smbOpen
        })

        if (-not $smbOpen) { continue }

        $shareRoot = "\\$($candidate.Address)\$ShareName"
        $targetPath = if ($Subfolder) { Join-Path $shareRoot $Subfolder } else { $shareRoot }
        $targetResult = Test-SmbPath $targetPath
        if ($targetResult.Status -eq "available") {
            return [pscustomobject]@{
                Candidate = $candidate
                Path = $targetPath
            }
        }
        $Failures.Add([pscustomobject]@{
            Address = $candidate.Address
            Source = $candidate.Source
            Result = $targetResult
        })

        # Access denial applies to the SMB session, so retrying the share root adds no information.
        if ($targetResult.Status -eq "access-denied" -or $targetPath -eq $shareRoot) { continue }

        $rootResult = Test-SmbPath $shareRoot
        if ($rootResult.Status -eq "available") {
            return [pscustomobject]@{
                Candidate = $candidate
                Path = $shareRoot
            }
        }
        $Failures.Add([pscustomobject]@{
            Address = $candidate.Address
            Source = $candidate.Source
            Result = $rootResult
        })
    }
    return $null
}

function Write-SmbFailureGuidance {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Reachable,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.Generic.List[object]]$Failures,
        [Parameter(Mandatory = $true)][string]$ExpectedPath
    )

    Write-Host "The Seestar storage could not be opened." -ForegroundColor Red
    Write-Host "Expected path: $ExpectedPath"

    $smbReachable = @($Reachable | Where-Object { $_.SmbOpen })
    if ($smbReachable.Count -eq 0) {
        if ($Reachable.Count -gt 0) {
            Write-Host "A Seestar control service answered, but TCP 445 (SMB) was not reachable."
        } else {
            Write-Host "No candidate answered on TCP 4700 or TCP 445."
        }
        Write-Host "This does not identify whether the cause is firmware state, Wi-Fi isolation, or another network condition."
        return
    }

    Write-Host "TCP 445 answered, so an SMB server was reachable. A 'net view' error is not used as evidence that sharing is off."
    foreach ($failure in $Failures) {
        Write-Host ("Direct UNC attempt: {0}" -f $failure.Result.Path)
        Write-Host ("Windows result: {0} {1} ({2})" -f `
            $failure.Result.Status, $failure.Result.Message, $failure.Result.HResult)
    }

    $policy = Get-SmbClientPolicy
    $guestText = Format-PolicyValue $policy.EnableInsecureGuestLogons
    $signingText = Format-PolicyValue $policy.RequireSecuritySignature
    $encryptionText = Format-PolicyValue $policy.RequireEncryption
    Write-Host "Windows SMB client policy ($($policy.Source)):"
    Write-Host "  Insecure guest logons: $guestText"
    Write-Host "  Required SMB signing:  $signingText"
    Write-Host "  Required encryption:   $encryptionText"

    $hasGuestCompatibleFailure = @($Failures | Where-Object {
        $_.Result.Status -in @("access-denied", "path-not-found")
    }).Count -gt 0
    $showGuestCommand = $policy.EnableInsecureGuestLogons -eq $false -or `
        ($hasGuestCompatibleFailure -and $null -eq $policy.EnableInsecureGuestLogons)
    $showSigningCommand = $policy.RequireSecuritySignature -eq $true
    $showEncryptionCommand = $policy.RequireEncryption -eq $true

    if ($showGuestCommand -or $showSigningCommand -or $showEncryptionCommand) {
        Write-Host "Windows is likely blocking the Seestar's unauthenticated guest SMB connection." -ForegroundColor Yellow
        Write-Host "If you accept the security trade-off, open PowerShell as Administrator and run only the needed command(s):"
        Write-Host ""
        if ($showGuestCommand) {
            Write-Host '> Set-SmbClientConfiguration -EnableInsecureGuestLogons $true -Force'
        }
        if ($showSigningCommand) {
            Write-Host '> Set-SmbClientConfiguration -RequireSecuritySignature $false -Force'
        }
        if ($showEncryptionCommand) {
            Write-Host '> Set-SmbClientConfiguration -RequireEncryption $false -Force'
        }
        Write-Host ""
        Write-Host "These are machine-wide SMB client changes. They weaken protection against malicious or spoofed SMB servers."
        Write-Host "Use them only on a trusted network, record the original values, and restore those values after copying files."
    } else {
        Write-Host "The readable Windows policy values do not show a guest/signing/encryption block."
        Write-Host "The share name may be unavailable, a managed policy may override these values, or the SMB negotiation may have failed."
    }
}

$candidates = New-Object System.Collections.Generic.List[object]
$seen = @{}

function Add-Candidate {
    param(
        [string]$Address,
        [string]$Source
    )
    if (-not $Address -or $seen.ContainsKey($Address)) { return }
    $seen[$Address] = $true
    $candidates.Add([pscustomobject]@{
        Address = $Address
        Source = $Source
    })
}

if ($SeestarHost) {
    foreach ($address in Resolve-IPv4Address $SeestarHost) {
        Add-Candidate $address "command line"
    }
}
if ($env:SEESTAR_HOST) {
    foreach ($address in Resolve-IPv4Address $env:SEESTAR_HOST) {
        Add-Candidate $address "SEESTAR_HOST"
    }
}
foreach ($name in @("seestar.local", "seestar")) {
    foreach ($address in Resolve-IPv4Address $name) {
        Add-Candidate $address $name
    }
}

# 10.0.0.1 is the Seestar host-mode address. Some firmware/devices use 192.168.4.1.
Add-Candidate "10.0.0.1" "AP mode"
Add-Candidate "192.168.4.1" "AP mode fallback"

if ($FindOnly) {
    $selected = $null
    if ($SeestarHost -and $candidates.Count -gt 0) {
        # This deterministic path is useful for diagnostics and offline package tests.
        $selected = $candidates[0]
    } else {
        foreach ($candidate in $candidates) {
            if ((Test-TcpPort $candidate.Address 4700) -or (Test-TcpPort $candidate.Address 445)) {
                $selected = $candidate
                break
            }
        }
    }
    if (-not $selected) {
        foreach ($address in Find-SeestarOnLocalSubnets) {
            Add-Candidate $address "TCP 4700 subnet scan"
            $selected = $candidates[$candidates.Count - 1]
            break
        }
    }
    if (-not $selected) {
        Write-Host "Seestar was not found." -ForegroundColor Red
        Write-Host "Connect this PC to the Seestar Wi-Fi/AP or the same STA network, then retry."
        Write-Host "You can also specify a known IPv4 address:"
        Write-Host ""
        Write-Host "> seestar-open-storage.cmd 192.168.x.x"
        Write-Host ""
        exit 2
    }

    $shareRoot = "\\$($selected.Address)\$ShareName"
    $targetPath = if ($Subfolder) { Join-Path $shareRoot $Subfolder } else { $shareRoot }
    Write-Host "Seestar IPv4: $($selected.Address) ($($selected.Source))"
    Write-Host "Storage path: $targetPath"
    Write-Output "host=$($selected.Address)"
    Write-Output "unc=$targetPath"
    exit 0
}

$reachable = New-Object System.Collections.Generic.List[object]
$failures = New-Object System.Collections.Generic.List[object]
$accessible = Test-SeestarCandidates `
    -Candidates ($candidates.ToArray()) `
    -ShareName $ShareName `
    -Subfolder $Subfolder `
    -Reachable $reachable `
    -Failures $failures

if (-not $accessible) {
    $hasSmbResponder = @($reachable | Where-Object { $_.SmbOpen }).Count -gt 0
    if (-not $hasSmbResponder) {
        $beforeScanCount = $candidates.Count
        foreach ($address in Find-SeestarOnLocalSubnets) {
            Add-Candidate $address "TCP 4700 subnet scan"
        }
        if ($candidates.Count -gt $beforeScanCount) {
            $scanCandidates = @($candidates | Select-Object -Skip $beforeScanCount)
            $accessible = Test-SeestarCandidates `
                -Candidates $scanCandidates `
                -ShareName $ShareName `
                -Subfolder $Subfolder `
                -Reachable $reachable `
                -Failures $failures
        }
    }
}

if (-not $accessible) {
    $expectedHost = if ($failures.Count -gt 0) {
        $failures[0].Address
    } elseif ($reachable.Count -gt 0) {
        $reachable[0].Address
    } else {
        "IPv4"
    }
    $expectedRoot = "\\$expectedHost\$ShareName"
    $expectedPath = if ($Subfolder) { Join-Path $expectedRoot $Subfolder } else { $expectedRoot }
    Write-SmbFailureGuidance -Reachable $reachable -Failures $failures -ExpectedPath $expectedPath
    exit 3
}

Write-Host "Seestar IPv4: $($accessible.Candidate.Address) ($($accessible.Candidate.Source))"
Write-Host "Opening $($accessible.Path)"
Start-Process -FilePath "explorer.exe" -ArgumentList ('"{0}"' -f $accessible.Path)
exit 0
