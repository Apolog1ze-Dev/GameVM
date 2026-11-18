# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Prepare your existing Windows install so it can ALSO boot inside the gamevm VM.
    Run this once on bare-metal Windows (normal boot), from an elevated PowerShell:

        Set-ExecutionPolicy -Scope Process Bypass -Force
        D:\gamevm-windows\1-prepare-windows.ps1        # path of the shared HDD copy

.DESCRIPTION
    Changes (all reversible, listed at the end):
      1. Turns off Fast Startup and hibernation. Linux refuses to hand a hibernated disk to the VM,
         because resuming a hibernated Windows on different (virtual) hardware can corrupt it.
      2. Re-enables password sign-in ("Require Windows Hello sign-in" off). Your PIN is sealed in the
         motherboard's TPM and will not work in the VM, which has its own TPM.
      3. Installs the virtio guest tools (network/input/guest-agent drivers) if the installer file is
         next to this script, so Windows has network the first time it boots in the VM.
      4. Blocks automatic device encryption (BitLocker would bind the disk to ONE of the two TPMs).
      5. Makes sure the disk has the EFI fallback loader (\EFI\Boot\bootx64.efi) the VM firmware boots.
      6. Copies these scripts to C:\gamevm so they are available inside the VM.
    Checks (no changes): BitLocker, Windows version, activation, SMBIOS UUID, clock mode, Memory Integrity.
#>
[CmdletBinding()]
param(
    [switch]$SkipVirtio,
    # Also create a local administrator with a password, as a way in if sign-in fails in the VM.
    [switch]$CreateRescueAdmin
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$notes = New-Object System.Collections.Generic.List[string]

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    [!!] $m" -ForegroundColor Yellow; $notes.Add($m) }

# ---------------------------------------------------------------------------------------------
Step "Windows version"
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$build = [int]$cv.CurrentBuildNumber
Ok "$($cv.ProductName -replace 'Windows 10','Windows 11') $($cv.DisplayVersion) (build $build.$($cv.UBR))"
if ($build -lt 22000) { Warn "This is Windows 10. NVIDIA's last Game Ready driver for Windows 10 ships in October 2026." }
elseif ($cv.DisplayVersion -eq '24H2') { Warn "24H2 stops receiving updates on 2026-10-13: install 25H2/26H2 from Windows Update." }

# ---------------------------------------------------------------------------------------------
Step "Fast Startup and hibernation"
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled -Value 0 -Type DWord
& powercfg.exe /hibernate off
if (Test-Path "$env:SystemDrive\hiberfil.sys") { Warn "hiberfil.sys still exists; it disappears after the next full shutdown." }
Ok "Fast Startup and hibernation are off"

# ---------------------------------------------------------------------------------------------
Step "Drive encryption (BitLocker / device encryption)"
try {
    $bl = Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction Stop
    if ($bl.ProtectionStatus -eq 'On' -or $bl.VolumeStatus -ne 'FullyDecrypted') {
        Warn ("C: is encrypted ($($bl.VolumeStatus)). The VM has a different TPM, so Windows will ask for the " +
              "BitLocker recovery key when it boots in the VM. Back the key up first (https://aka.ms/myrecoverykey), " +
              "or decrypt with: manage-bde -off C:")
    } else { Ok "C: is not encrypted" }
} catch { Warn "Could not query BitLocker ($($_.Exception.Message)); check with: manage-bde -status C:" }

# ---------------------------------------------------------------------------------------------
Step "Sign-in (Windows Hello PIN is tied to this PC's TPM)"
$pwless = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'
if ((Get-ItemProperty $pwless -ErrorAction SilentlyContinue).DevicePasswordLessBuildVersion -eq 2) {
    Set-ItemProperty $pwless -Name DevicePasswordLessBuildVersion -Value 0 -Type DWord
    Ok "Password sign-in re-enabled (was hidden by 'Only allow Windows Hello sign-in')"
} else { Ok "Password sign-in is available" }
$notes.Add("In the VM your PIN will say 'not available': choose Sign-in options > Password and use your account " +
           "PASSWORD (the Microsoft account password if you use one). Do NOT click 'Set up my PIN' or 'I forgot my PIN' " +
           "in the VM: that wipes the PIN for bare metal too (it is tied to the motherboard's TPM).")
Ok "Reminder noted (see summary)"
if ($CreateRescueAdmin) {
    if (Get-LocalUser -Name 'vmrescue' -ErrorAction SilentlyContinue) { Ok "local admin 'vmrescue' already exists" }
    else {
        $pw = Read-Host -AsSecureString "Password for the new local administrator 'vmrescue'"
        New-LocalUser -Name 'vmrescue' -Password $pw -PasswordNeverExpires -Description 'gamevm rescue login' | Out-Null
        Add-LocalGroupMember -SID 'S-1-5-32-544' -Member 'vmrescue'   # Administrators, any language
        Ok "local administrator 'vmrescue' created"
    }
}

# ---------------------------------------------------------------------------------------------
Step "Automatic device encryption"
$bk = 'HKLM:\SYSTEM\CurrentControlSet\Control\BitLocker'
if (-not (Test-Path $bk)) { New-Item -Path $bk | Out-Null }   # never -Force here: it would recreate the key
New-ItemProperty -Path $bk -Name PreventDeviceEncryption -PropertyType DWord -Value 1 -Force | Out-Null
Ok "Windows will not turn on device encryption by itself"

# ---------------------------------------------------------------------------------------------
Step "Activation and machine identity"
$csp = Get-CimInstance Win32_ComputerSystemProduct
Ok "SMBIOS UUID $($csp.UUID)  (gamevm copies the host's UUID; the VM should report the same)"
$lic = Get-CimInstance SoftwareLicensingProduct -Filter "PartialProductKey IS NOT NULL AND Name LIKE 'Windows%'" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($lic.LicenseStatus -eq 1) { Ok "Windows is activated ($($lic.Description))" } else { Warn "Windows is not activated on bare metal." }
$notes.Add("If the VM ever says Windows is not activated: Settings > System > Activation > Troubleshoot > " +
           "'I changed hardware on this device recently' (needs the licence linked to your Microsoft account: " +
           "check that Settings > Activation says 'digital license linked to your Microsoft account').")

# ---------------------------------------------------------------------------------------------
Step "EFI fallback loader (what the VM's fresh firmware boots)"
$letter = (68..90 | ForEach-Object { [char]$_ } | Where-Object { -not (Test-Path "$($_):\") } | Select-Object -Last 1)
& mountvol.exe "$($letter):" /s
try {
    if (Test-Path "$($letter):\EFI\Boot\bootx64.efi") { Ok "\EFI\Boot\bootx64.efi present on the EFI partition" }
    else {
        Warn "\EFI\Boot\bootx64.efi is missing: the VM could not boot this disk."
        $a = Read-Host "    Recreate the boot files with 'bcdboot C:\Windows /f UEFI /p' (keeps your boot order)? [Y/n]"
        if ($a -eq '' -or $a -match '^[Yy]') {
            & bcdboot.exe "$env:SystemRoot" /s "$($letter):" /f UEFI /p
            if (Test-Path "$($letter):\EFI\Boot\bootx64.efi") { Ok "fallback loader created" } else { Warn "bcdboot did not create it" }
        }
    }
} finally { & mountvol.exe "$($letter):" /d }

# ---------------------------------------------------------------------------------------------
Step "Real-time clock"
$rtu = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\TimeZoneInformation' -ErrorAction SilentlyContinue).RealTimeIsUniversal
if ($rtu -eq 1) {
    Warn 'Windows keeps the RTC in UTC: set   clock = "utc"   under [vm] in /etc/gamevm/gamevm.toml'
} else { Ok 'Windows keeps the RTC in local time (gamevm default: clock = "localtime")' }

# ---------------------------------------------------------------------------------------------
Step "Memory Integrity / VBS"
$hvci = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity' -ErrorAction SilentlyContinue).Enabled
if ($hvci -eq 1) {
    Ok "Memory Integrity is on for bare metal. In the VM it cannot run (gamevm hides nested virtualisation for speed); that is expected."
} else { Ok "Memory Integrity is off" }

# ---------------------------------------------------------------------------------------------
Step "virtio guest drivers"
$tools = Get-ChildItem -Path $here -Filter 'virtio-win-guest-tools*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
$installed = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like 'Virtio-win*' }
if ($installed) { Ok "already installed: $($installed[0].DisplayName) $($installed[0].DisplayVersion)" }
elseif ($SkipVirtio) { Warn "Skipped. Install virtio-win-guest-tools.exe inside the VM from the virtio-win CD (network works after that)." }
elseif ($tools) {
    Write-Host "    installing $($tools.Name) (drivers are staged now and used only inside the VM) ..."
    $p = Start-Process -FilePath $tools.FullName -ArgumentList '/install', '/passive', '/norestart' -Wait -PassThru
    if ($p.ExitCode -in 0, 3010) { Ok "virtio guest tools installed" } else { Warn "installer exit code $($p.ExitCode)" }
} else {
    Warn ("virtio-win-guest-tools.exe not found next to this script. The VM will boot without network the first time: " +
          "install it from the virtio-win CD that gamevm attaches (E:\virtio-win-guest-tools.exe).")
}

# ---------------------------------------------------------------------------------------------
Step "Copy gamevm scripts to C:\gamevm (for use inside the VM)"
$dest = Join-Path $env:SystemDrive 'gamevm'
if ((Resolve-Path $here).ProviderPath.TrimEnd('\') -ieq $dest) { Ok "already running from $dest" }
else {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item -Path (Join-Path $here '*') -Destination $dest -Recurse -Force -Exclude 'virtio-win-guest-tools*.exe'
    Ok "copied to $dest"
}

# ---------------------------------------------------------------------------------------------
Write-Host "`n================ Summary ================" -ForegroundColor Cyan
foreach ($n in $notes) { Write-Host " - $n" -ForegroundColor Yellow }
Write-Host @"

Next:
 1. Shut down normally (Start > Power > Shut down). Fast Startup is off, so this is a full shutdown.
 2. Boot Linux and run:     sudo gamevm doctor     (it must report the NTFS volume as clean)
 3. Start the VM:            gamevm start
 4. Inside the VM, run       C:\gamevm\2-inside-vm.ps1   as administrator.

To undo: powercfg /hibernate on ; Settings > Accounts > Sign-in options to re-enable 'Only Windows Hello'.
"@
