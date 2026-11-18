# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
<#
.SYNOPSIS
    Send files from Windows to the Linux side of this PC (gamevm), through KDE Connect.

.DESCRIPTION
    Three ways in. The first two are created by -Install, which gamevm on Linux runs for you once
    Windows and Linux are paired:

      - right-click one or more files > Send to > Linux (gamevm)
      - Start menu > "Send to Linux": a small window to drop files on, or to pick them in
      - this script with file paths

    The files arrive in the Downloads folder on Linux, and the gamevm window there lists them.
    Files that Linux sends arrive in this user's Downloads folder.

        -Install     create the "Send to" entry and the Start menu shortcut
        -Uninstall   remove them
#>
param(
    [switch]$Install,
    [switch]$Uninstall,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Files
)
$ErrorActionPreference = 'Stop'
$me = $MyInvocation.MyCommand.Path
$cli = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\kdeconnect-cli.exe'
$sendTo = Join-Path ([Environment]::GetFolderPath('SendTo')) 'Linux (gamevm).lnk'
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Send to Linux.lnk'

function New-Shortcut([string]$path, [string]$description) {
    $shell = New-Object -ComObject WScript.Shell
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$me`""
    $s.WorkingDirectory = Split-Path $me
    $s.Description = $description
    $s.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',17'
    $s.WindowStyle = 7   # minimised: the console window does not flash up
    $s.Save()
}

if ($Install) {
    New-Shortcut $sendTo 'Send the selected files to the Linux side of this PC'
    New-Shortcut $startMenu 'Send files to the Linux side of this PC'
    'installed: Send to > Linux (gamevm), Start menu > Send to Linux'
    exit 0
}
if ($Uninstall) {
    Remove-Item -LiteralPath $sendTo, $startMenu -Force -ErrorAction SilentlyContinue
    'removed'
    exit 0
}

# The Linux side: the paired KDE Connect device that answers, preferably the one on the private link.
function Get-LinuxDevice {
    if (-not (Test-Path $cli)) {
        throw 'KDE Connect is not installed for this user. gamevm installs it: C:\gamevm\2-inside-vm.ps1'
    }
    $ready = @(& $cli -l 2>$null | Where-Object { $_ -match '\(paired and reachable\)' })
    $pick = @(@($ready | Where-Object { $_ -match ' on 10\.77\.0\.1 ' }) + $ready) | Select-Object -First 1
    if (-not $pick) {
        throw 'Linux does not answer right now. Open the gamevm window on Linux; it links the two by itself.'
    }
    if ($pick -match '^- (.+?): (\S+) ') { return @{ Name = $Matches[1]; Id = $Matches[2] } }
    throw 'could not read the device list of KDE Connect'
}

function Send-ToLinux([string[]]$paths) {
    $dev = Get-LinuxDevice
    $sent = @()
    $skipped = @()
    foreach ($p in $paths) {
        if (-not $p) { continue }
        $name = Split-Path $p -Leaf
        if (Test-Path -LiteralPath $p -PathType Leaf) {
            & $cli -d $dev.Id --share (Resolve-Path -LiteralPath $p).ProviderPath 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $sent += $name } else { $skipped += "$name (KDE Connect refused it)" }
        } elseif (Test-Path -LiteralPath $p -PathType Container) {
            $skipped += "$name (a folder: send its files, or a zip of it)"
        } else {
            $skipped += "$name (not found)"
        }
    }
    return @{ Device = $dev.Name; Sent = $sent; Skipped = $skipped }
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Show-Balloon([string]$title, [string]$text) {
    $n = New-Object System.Windows.Forms.NotifyIcon
    $n.Icon = [System.Drawing.SystemIcons]::Information
    $n.Visible = $true
    $n.ShowBalloonTip(5000, $title, $text, [System.Windows.Forms.ToolTipIcon]::Info)
    $until = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $until) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 100
    }
    $n.Dispose()
}

function Show-Window {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'Send to Linux'
    $form.ClientSize = New-Object System.Drawing.Size(480, 380)
    $form.MinimumSize = New-Object System.Drawing.Size(380, 320)
    $form.StartPosition = 'CenterScreen'
    $form.Font = New-Object System.Drawing.Font('Segoe UI', 10)

    $drop = New-Object System.Windows.Forms.Label
    $drop.Text = "Drop files here to send them to Linux.`n`nThey arrive in the Downloads folder there."
    $drop.TextAlign = 'MiddleCenter'
    $drop.BorderStyle = 'FixedSingle'
    $drop.Dock = 'Top'
    $drop.Height = 150

    $pick = New-Object System.Windows.Forms.Button
    $pick.Text = 'Choose files...'
    $pick.Dock = 'Top'
    $pick.Height = 44

    $log = New-Object System.Windows.Forms.ListBox
    $log.Dock = 'Fill'
    $log.HorizontalScrollbar = $true
    $log.IntegralHeight = $false

    $open = New-Object System.Windows.Forms.Button
    $open.Text = 'Open Downloads (files from Linux arrive there)'
    $open.Dock = 'Bottom'
    $open.Height = 44

    $send = {
        param([string[]]$paths)
        try {
            $r = Send-ToLinux $paths
            foreach ($f in $r.Sent) { [void]$log.Items.Add("sent to $($r.Device): $f") }
            foreach ($s in $r.Skipped) { [void]$log.Items.Add("not sent: $s") }
        } catch {
            [void]$log.Items.Add("not sent: $($_.Exception.Message)")
        }
        $log.TopIndex = [Math]::Max(0, $log.Items.Count - 1)
    }
    $enter = {
        if ($_.Data.GetDataPresent([System.Windows.Forms.DataFormats]::FileDrop)) { $_.Effect = 'Copy' }
    }
    $dropped = { & $send ([string[]]$_.Data.GetData([System.Windows.Forms.DataFormats]::FileDrop)) }
    foreach ($target in @($form, $drop, $log)) {
        $target.AllowDrop = $true
        $target.Add_DragEnter($enter)
        $target.Add_DragDrop($dropped)
    }
    $pick.Add_Click({
        $d = New-Object System.Windows.Forms.OpenFileDialog
        $d.Multiselect = $true
        $d.Title = 'Send to Linux'
        if ($d.ShowDialog($form) -eq 'OK') { & $send $d.FileNames }
    })
    $open.Add_Click({
        Start-Process explorer.exe (New-Object -ComObject Shell.Application).NameSpace('shell:Downloads').Self.Path
    })

    # Docking goes from the last control to the first: the top two take their place, the list fills the rest.
    $form.Controls.AddRange(@($log, $open, $pick, $drop))
    [void]$form.ShowDialog()
}

if ($Files -and $Files.Count -gt 0) {
    try {
        $r = Send-ToLinux $Files
        if ($r.Skipped.Count -gt 0) {
            $text = "Not sent: " + ($r.Skipped -join '; ')
            if ($r.Sent.Count -gt 0) { $text = "Sent to $($r.Device): " + ($r.Sent -join ', ') + "`n`n" + $text }
            [void][System.Windows.Forms.MessageBox]::Show($text, 'Send to Linux', 'OK', 'Warning')
        } else {
            Show-Balloon "Sent to $($r.Device)" (($r.Sent -join ', ') + "`nIn the Downloads folder on Linux.")
        }
    } catch {
        [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Send to Linux', 'OK', 'Error')
    }
} else {
    Show-Window
}
