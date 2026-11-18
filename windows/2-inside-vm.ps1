# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    First-boot setup INSIDE the gamevm VM. Run from an elevated PowerShell:

        Set-ExecutionPolicy -Scope Process Bypass -Force
        C:\gamevm\2-inside-vm.ps1

.DESCRIPTION
    - Installs the virtio guest tools from the virtio-win CD if they are missing (network, input,
      guest agent for clean shutdowns from Linux).
    - Offers to install the Looking Glass IDD (virtual display + input driver) when the VM has the Looking
      Glass shared-memory device (iGPU mode). The build MUST match the Linux client's build. The
      driver's "Make LG the only monitor" default is switched off, so the real monitor never goes
      black; `gamevm lg` on Linux mirrors the screen into its window while that window is open.
    - Gives the host-only adapter a fixed address (10.77.0.2, no gateway, no DNS) so it never
      carries internet traffic.
    - Sharing with Linux over the private link only (10.77.0.x): shares the NTFS drives so that Linux
      can open them while Windows runs (`gamevm files`), opens the firewall for that and for KDE Connect
      on that adapter and for the Linux host alone, and installs KDE Connect (shared clipboard, sending
      files in both directions). -SkipSharing leaves all of it out, -SkipKdeConnect only the install.
    - Selects the High performance power plan.
    - Reports: GPU driver, activation, TPM, DirectStorage/BypassIO, Gaming Services, HAGS.
    Safe to run again; it skips what is already done.
#>
[CmdletBinding()]
param(
    [string]$LookingGlassBuild = 'B7-829-cdba29f2',
    [switch]$SkipLookingGlass,
    # Install the Looking Glass display driver without asking (it is only needed for `gamevm lg`).
    [switch]$InstallLookingGlass,
    # Do not set up sharing with the Linux host (drive shares, firewall rules, KDE Connect).
    [switch]$SkipSharing,
    # Set up the drive shares and firewall rules, but do not install KDE Connect.
    [switch]$SkipKdeConnect
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$notes = New-Object System.Collections.Generic.List[string]
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    [!!] $m" -ForegroundColor Yellow; $notes.Add($m) }

$pci = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue
if (-not ($pci | Where-Object { $_.InstanceId -match 'VEN_1AF4|VEN_1B36' })) {
    Write-Host "No virtio/QEMU devices found: this does not look like the gamevm VM. Run it inside the VM." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------------------------
Step "virtio guest tools"
$have = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like 'Virtio-win*' }
if ($have) { Ok "installed: $($have[0].DisplayName) $($have[0].DisplayVersion)" }
else {
    $exe = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root 'virtio-win-guest-tools.exe' } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($exe) {
        Write-Host "    installing from $exe ..."
        $p = Start-Process -FilePath $exe -ArgumentList '/install', '/passive', '/norestart' -Wait -PassThru
        if ($p.ExitCode -in 0, 3010) { Ok "installed (a reboot may be requested)" } else { Warn "installer exit code $($p.ExitCode)" }
        $pci = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue
    } else {
        Warn "virtio-win CD not found. On Linux: put virtio-win.iso in /var/lib/libvirt/images/ and run gamevm define, then restart the VM."
    }
}
if (Get-Service -Name QEMU-GA -ErrorAction SilentlyContinue) { Ok "QEMU guest agent present (clean shutdown from Linux)" }
else { Warn "QEMU guest agent missing: 'gamevm stop' will still work through ACPI." }

# ---------------------------------------------------------------------------------------------
Step "Looking Glass"
$ivshmem = $pci | Where-Object { $_.InstanceId -match 'VEN_1AF4&DEV_1110' }
$lgReg = Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like 'Looking Glass*IDD*' } | Select-Object -First 1
$lgCurrent = $lgReg -and ("$($lgReg.DisplayVersion) $($lgReg.DisplayName)" -like "*$LookingGlassBuild*") -and
             -not $PSBoundParameters.ContainsKey('LookingGlassBuild')
# The driver's helper makes the virtual monitor Windows' ONLY monitor unless this value is 0 (the
# "Make LG the only monitor" box in its tray settings; a missing value counts as on). With a real
# monitor on the graphics card that turns the real one black, so it is switched off: before the
# installer runs, so that it never happens, and again on every run of this script.
function Set-LookingGlassShared {
    $key = 'HKLM:\SOFTWARE\LookingGlass\IDD'
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    if ((Get-ItemProperty $key).ExclusiveMonitor -ne 0) {
        Set-ItemProperty -Path $key -Name ExclusiveMonitor -Value 0 -Type DWord
        return $true
    }
    return $false
}
if ($SkipLookingGlass) { Ok "skipped" }
elseif (-not $ivshmem) { Ok "no Looking Glass device in this VM (single-GPU mode or disabled): nothing to do" }
elseif ($lgCurrent) {
    Ok "Looking Glass IDD $LookingGlassBuild already installed"
    if (Set-LookingGlassShared) {
        Ok "switched off 'Make LG the only monitor': the real monitor stays Windows' screen"
        $notes.Add("If the real monitor is black now: press Win + P and choose 'PC screen only' (or 'Second screen only'), or run 'gamevm lg' on Linux, which sets the layout itself.")
    }
}
else {
    # Asked first: the driver adds a virtual monitor and is only needed to see Windows in a window
    # on Linux. An older version that is already installed, -InstallLookingGlass or
    # -LookingGlassBuild mean yes.
    $want = $InstallLookingGlass -or $lgReg -or $PSBoundParameters.ContainsKey('LookingGlassBuild')
    if (-not $want) {
        Write-Host "    Looking Glass shows Windows in a window on the Linux desktop ('gamevm lg'). For that it needs a"
        Write-Host "    display driver here, which adds a virtual monitor. You do not need it to play on the monitor"
        Write-Host "    that is cabled to the graphics card."
        $want = (Read-Host "    Install the Looking Glass display driver now? [y/N]") -match '^[Yy]'
    }
    if (-not $want) {
        Ok "not installed. Later: this script with -InstallLookingGlass. To drop the question: looking_glass.enabled = false on Linux."
    } else {
        if ($lgReg) { Write-Host "    upgrading Looking Glass IDD from $($lgReg.DisplayVersion) to $LookingGlassBuild" }
        $zip = Join-Path $env:TEMP "looking-glass-idd-$LookingGlassBuild.zip"
        $dir = Join-Path $env:TEMP "looking-glass-idd-$LookingGlassBuild"
        $url = "https://looking-glass.io/artifact/$LookingGlassBuild/idd"
        try {
            # With a time limit: a download that stalls (a VPN connecting meanwhile is enough) must not
            # leave this script waiting.
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
            Write-Host "    downloading the driver (3 minutes at most) ..."
            if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
                & curl.exe --fail --silent --show-error --location --connect-timeout 15 --max-time 180 --output $zip $url
                if ($LASTEXITCODE -ne 0) { throw "the download failed or took longer than 3 minutes (curl exit code $LASTEXITCODE)" }
            } else {
                Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip -TimeoutSec 30
            }
            Expand-Archive -Path $zip -DestinationPath $dir -Force
            $setup = Get-ChildItem -Path $dir -Recurse -Filter 'looking-glass-idd-setup*.exe' | Select-Object -First 1
            if (-not $setup) { throw "looking-glass-idd-setup.exe not found in the archive" }
            $null = Set-LookingGlassShared
            Write-Host "    installing Looking Glass IDD $LookingGlassBuild (with the IVSHMEM driver) ..."
            Write-Host "    The screen may blink while the virtual monitor appears." -ForegroundColor Yellow
            $p = Start-Process -FilePath $setup.FullName -ArgumentList '/S', '/ivshmem' -PassThru
            $null = $p.Handle   # keeps the exit code readable without -Wait
            if (-not $p.WaitForExit(300000)) {
                Warn "The Looking Glass installer is still open after 5 minutes: look for a Windows question behind other windows (Alt+Tab) and answer it. This script goes on meanwhile."
            } elseif ($p.ExitCode -eq 0) { Ok "Looking Glass IDD installed" }
            else { Warn "Looking Glass installer exit code $($p.ExitCode)" }
            $notes.Add("On Linux, 'gamevm lg' opens Windows in a window on the side monitor. While that window is open Windows mirrors its screen into it (as Win + P > Duplicate does); when you close it, Windows goes back to the main monitor alone. Right Ctrl captures the mouse and lets it go again, Right Ctrl + Q closes the window. Windows keeps running on the main monitor either way.")
            $notes.Add("If the main monitor is black after this installation, the virtual monitor took over: press Win + P and choose 'PC screen only' (or 'Second screen only'), or run 'gamevm lg' on Linux, which sets the layout itself.")
            $notes.Add("For a sharp Looking Glass window: tray icon > Open configuration, add a mode with your main monitor's resolution and exact refresh rate (for example 2560x1440 at 239.970 Hz) and mark it preferred.")
        } catch {
            Warn ("Looking Glass download/install failed ($($_.Exception.Message)). Run this script again later, or download the " +
                  "'Windows IDD' for build $LookingGlassBuild from https://looking-glass.io/downloads and run looking-glass-idd-setup.exe.")
        }
    }
}
$pci = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue
foreach ($d in ($pci | Where-Object { $_.InstanceId -match 'VEN_1AF4' -and $_.Status -ne 'OK' })) {
    Warn "virtio device without a working driver: $($d.FriendlyName) [$($d.InstanceId)]"
}

# ---------------------------------------------------------------------------------------------
Step "Host-only network adapter (10.77.0.2 from Linux's DHCP reservation, no internet)"
$hostNic = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like '10.77.0.*' } | Select-Object -First 1
if ($hostNic) {
    $idx = $hostNic.InterfaceIndex
    try {
        Set-DnsClient -InterfaceIndex $idx -RegisterThisConnectionsAddress $false
        Set-NetIPInterface -InterfaceIndex $idx -InterfaceMetric 9999          # never preferred for internet
        Set-NetConnectionProfile -InterfaceIndex $idx -NetworkCategory Private -ErrorAction SilentlyContinue  # allows SMB/ping from Linux
        $name = (Get-NetAdapter -InterfaceIndex $idx).Name
        if ($name -ne 'gamevm host-only') { Rename-NetAdapter -Name $name -NewName 'gamevm host-only' -ErrorAction SilentlyContinue }
        Ok "host-only adapter has $($hostNic.IPAddress) (Linux host = 10.77.0.1), private network, lowest priority"
    } catch { Warn "could not configure the host-only adapter: $($_.Exception.Message)" }
} else { Ok "no host-only adapter found (network.hostonly = false, or no virtio network driver yet)" }

# ---------------------------------------------------------------------------------------------
Step "Sharing with Linux over the private link (drives in the Linux file manager, clipboard through KDE Connect)"
if ($SkipSharing) { Ok "skipped" }
elseif (-not $hostNic) { Warn "No host-only adapter (10.77.0.x), so nothing was shared: it is the only link between Windows and the Linux host." }
else { try {
    $alias = (Get-NetAdapter -InterfaceIndex $hostNic.InterfaceIndex).Name
    # Firewall: these rules apply on the host-only adapter alone and to the Linux host alone. Windows'
    # own file sharing rules are not touched, so nothing changes for your LAN or for a native boot
    # (where this adapter does not exist).
    $rules = @(
        @{ Name = 'gamevm-smb';            Text = 'gamevm: drives for the Linux host (SMB)';        Proto = 'TCP'; Port = '445' },
        @{ Name = 'gamevm-kdeconnect-tcp'; Text = 'gamevm: KDE Connect with the Linux host (TCP)';  Proto = 'TCP'; Port = '1714-1764' },
        @{ Name = 'gamevm-kdeconnect-udp'; Text = 'gamevm: KDE Connect with the Linux host (UDP)';  Proto = 'UDP'; Port = '1714-1764' }
    )
    $opened = 0
    foreach ($r in $rules) {
        try {
            Get-NetFirewallRule -Name $r.Name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
            New-NetFirewallRule -Name $r.Name -DisplayName $r.Text -Group 'gamevm' -Direction Inbound -Action Allow `
                -Protocol $r.Proto -LocalPort $r.Port -RemoteAddress '10.77.0.1' -InterfaceAlias $alias -Profile Any | Out-Null
            $opened++
        } catch { Warn "firewall rule '$($r.Name)': $($_.Exception.Message)" }
    }
    if ($opened -eq $rules.Count) { Ok "firewall: the Linux host (10.77.0.1) may reach file sharing and KDE Connect on '$alias'; nothing else was opened" }

    # Drives: every NTFS drive as \\10.77.0.2\<letter>, for you and for administrators.
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $admins = (New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544').Translate([Security.Principal.NTAccount]).Value   # any language
    if ((Get-Service -Name LanmanServer -ErrorAction SilentlyContinue).Status -ne 'Running') { Warn "The 'Server' service is not running: Windows cannot share drives until it is (services.msc)." }
    foreach ($d in (Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | Where-Object { $_.FileSystem -eq 'NTFS' })) {
        $letter = $d.DeviceID.TrimEnd(':')
        $root = "${letter}:\"
        try {
            $have = Get-SmbShare -Name $letter -ErrorAction SilentlyContinue
            if ($have -and $have.Path -ne $root) { Warn "A share named '$letter' already exists for $($have.Path): left alone, drive ${letter}: is not shared."; continue }
            if (-not $have) {
                New-SmbShare -Name $letter -Path $root -FullAccess @($admins, $me | Select-Object -Unique) -CachingMode None `
                    -Description 'gamevm: this drive for the Linux host' | Out-Null
            }
            Ok "drive ${letter}: is shared as \\10.77.0.2\$letter"
        } catch { Warn "could not share drive ${letter}: $($_.Exception.Message)" }
    }
    $notes.Add("Files: on Linux, 'gamevm files' opens these drives while Windows runs (smb://10.77.0.2). Sign in there as $me with your Windows PASSWORD (a Microsoft account: its e-mail address and password, not the PIN). An account without a password cannot be used over the network.")
    $notes.Add("The drive shares also exist when you boot this Windows natively. They are reachable only where Windows' own file sharing is switched on, and only with your password. To remove them: Get-SmbShare | Where-Object Description -like 'gamevm:*' | Remove-SmbShare ; Get-NetFirewallRule -Group gamevm | Remove-NetFirewallRule")

    # KDE Connect: a clipboard shared with Linux, and 'send file' in both directions.
    if ($SkipKdeConnect) { Ok "KDE Connect: skipped" }
    else {
        $kc = @(Get-AppxPackage -Name '*KDEConnect*' -ErrorAction SilentlyContinue) +
              @(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
                Where-Object { $_.DisplayName -like 'KDE Connect*' })
        if ($kc.Count -gt 0) { Ok "KDE Connect is installed" }
        elseif (Get-Command winget.exe -ErrorAction SilentlyContinue) {
            Write-Host "    installing KDE Connect with winget ..."
            & winget.exe install --id KDE.KDEConnect --exact --source winget --accept-package-agreements --accept-source-agreements --silent
            if ($LASTEXITCODE -eq 0) { Ok "KDE Connect installed" }
            else { Warn "winget could not install KDE Connect (exit code $LASTEXITCODE): install 'KDE Connect' from the Microsoft Store instead." }
        } else { Warn "Install 'KDE Connect' from the Microsoft Store (winget is not available here)." }
        # Started once here, so that nobody has to open it: from then on it starts with Windows.
        try {
            if (-not (Get-Process -Name kdeconnectd -ErrorAction SilentlyContinue)) {
                $apps = @(Get-StartApps | Where-Object { $_.Name -like 'KDE Connect*' })
                $app = @(@($apps | Where-Object { $_.Name -like '*Indicator*' }) + @($apps | Where-Object { $_.Name -eq 'KDE Connect' }) + $apps) | Select-Object -First 1
                if ($app) { Start-Process "shell:AppsFolder\$($app.AppID)"; Ok "KDE Connect started (it starts with Windows from now on)" }
                else { $notes.Add("Open KDE Connect in Windows once, so that it starts with Windows from then on.") }
            } else { Ok "KDE Connect is running" }
        } catch { $notes.Add("Open KDE Connect in Windows once, so that it starts with Windows from then on.") }
        # "Send to > Linux (gamevm)" in the right-click menu, "Send to Linux" in the Start menu.
        $sender = Join-Path $PSScriptRoot 'send-to-linux.ps1'
        if (Test-Path $sender) {
            try { & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sender -Install | Out-Null; Ok "right-click > Send to > Linux (gamevm), and Start menu > Send to Linux" }
            catch { Warn "could not add the Send to Linux entry: $($_.Exception.Message)" }
        }
        $notes.Add("Clipboard and files: nothing more to do here. The gamevm window on Linux (or 'gamevm share --pair') links the two by itself; leave the 'Pairing request' that KDE Connect shows on Linux alone. Text you copy is then on both clipboards. Send a file to Linux with right-click > Send to > Linux (gamevm); files from Linux arrive in Downloads. Windows must be set to the same time zone as Linux for this.")
    }
} catch { Warn "sharing with Linux was not set up completely: $($_.Exception.Message)" } }

# ---------------------------------------------------------------------------------------------
Step "MSI interrupts for the passed-through GPU (avoids slow, line-based INTx, e.g. HDMI audio crackle)"
$changed = $false
foreach ($d in ($pci | Where-Object { $_.InstanceId -match '^PCI\\VEN_10DE' })) {
    $key = "HKLM:\SYSTEM\CurrentControlSet\Enum\$($d.InstanceId)\Device Parameters\Interrupt Management\MessageSignaledInterruptProperties"
    $cur = (Get-ItemProperty $key -ErrorAction SilentlyContinue).MSISupported
    if ($cur -ne 1) {
        if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }   # never -Force on an existing key: it recreates it
        New-ItemProperty -Path $key -Name MSISupported -PropertyType DWord -Value 1 -Force | Out-Null
        Ok "MSI enabled: $($d.FriendlyName)"
        $changed = $true
    } else { Ok "MSI already on: $($d.FriendlyName)" }
}
if ($changed) { $notes.Add("Reboot Windows once so the MSI interrupt setting takes effect.") }

# ---------------------------------------------------------------------------------------------
Step "Drive letters (virtual CD drives off D:-H:, so the shared HDD keeps the letter it has natively)"
$used = @((Get-CimInstance Win32_Volume | Where-Object DriveLetter).DriveLetter)
$cds = @(Get-CimInstance Win32_Volume -Filter "DriveType=5" | Where-Object { $_.DriveLetter -and $_.DriveLetter -lt 'I:' })
if (-not $cds) { Ok "no virtual CD drive on D:-H:" }
foreach ($cd in $cds) {
    $free = [char[]](90..73) | ForEach-Object { "$($_):" } | Where-Object { $_ -notin $used } | Select-Object -First 1
    if (-not $free) { Warn "no free drive letter for the CD drive $($cd.DriveLetter)"; continue }
    $old = $cd.DriveLetter
    Set-CimInstance -InputObject $cd -Property @{ DriveLetter = $free }
    $used += $free
    Ok "virtual CD drive '$($cd.Label)' moved from $old to $free"
}

Step "Power plan"
& powercfg.exe /setactive SCHEME_MIN
if ($LASTEXITCODE -eq 0) { Ok "High performance plan active" } else { Warn "powercfg /setactive failed ($LASTEXITCODE)" }
& powercfg.exe /hibernate off
$notes.Add("The High performance power plan also applies when you boot this Windows natively. To undo there: powercfg /setactive SCHEME_BALANCED")

# ---------------------------------------------------------------------------------------------
Step "Checks"
$gpu = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -like '*NVIDIA*' }
if ($gpu -and $gpu.Status -eq 'OK') { Ok "GPU: $($gpu.Name), driver $($gpu.DriverVersion)" }
else { Warn "NVIDIA GPU driver not working: install the current Game Ready driver from nvidia.com." }
$others = Get-CimInstance Win32_VideoController | Where-Object { $_.Name -notlike '*NVIDIA*' -and $_.Name -notlike '*Looking Glass*' }
foreach ($o in $others) { Warn "extra display adapter '$($o.Name)': games may open on it. Remove rescue_vga from the gamevm config when not needed." }

Ok "SMBIOS UUID $((Get-CimInstance Win32_ComputerSystemProduct).UUID) (should equal the bare-metal value)"
$lic = Get-CimInstance SoftwareLicensingProduct -Filter "PartialProductKey IS NOT NULL AND Name LIKE 'Windows%'" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($lic.LicenseStatus -eq 1) { Ok "Windows is activated" }
else { Warn "Windows is not activated in the VM: Settings > System > Activation > Troubleshoot > 'I changed hardware on this device recently' (sign in with the Microsoft account the licence is linked to)." }

try { $tpm = Get-Tpm; if ($tpm.TpmReady) { Ok "TPM 2.0 ready ($($tpm.ManufacturerIdTxt))" } else { Warn "TPM present but not ready" } } catch { Warn "no TPM visible" }

$bio = (& fsutil.exe bypassio state /v "$env:SystemDrive\" | Out-String).Trim()
if ($bio -match 'is currently supported|est actuellement pris en charge') { Ok "DirectStorage BypassIO supported on C:" }
elseif ($bio -match 'partial|partiel') { Warn "BypassIO only partially supported on C: ($bio)" }
else { Write-Host "    BypassIO: $bio" }

if (Get-AppxPackage -AllUsers -Name Microsoft.GamingServices -ErrorAction SilentlyContinue) { Ok "Gaming Services installed" }
else { Warn "Gaming Services missing: open the Xbox app, it offers to install them." }

$hags = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\GraphicsDrivers' -ErrorAction SilentlyContinue).HwSchMode
if ($hags -eq 2) { Ok "Hardware-accelerated GPU scheduling on" } else { $notes.Add("Optional: Settings > System > Display > Graphics > turn on Hardware-accelerated GPU scheduling.") }

# ---------------------------------------------------------------------------------------------
Write-Host "`n================ Summary ================" -ForegroundColor Cyan
foreach ($n in $notes) { Write-Host " - $n" -ForegroundColor Yellow }
Write-Host @"

Tips:
 - Sign in with your PASSWORD in the VM. Never use 'Set up my PIN' / 'I forgot my PIN' here: it wipes
   the PIN of the bare-metal boot too.
 - Shut Windows down normally (or 'gamevm stop' on Linux). Avoid force-off: the GPU may then need a host reboot.
 - Xbox mode (Win+F11) gives a console-style launcher with less background load.
 - Games with kernel anti-cheat that blocks VMs (Valorant, CoD BO6/BO7/Warzone, Battlefield 6, Destiny 2, ...)
   must be played by booting this same Windows natively.
"@
