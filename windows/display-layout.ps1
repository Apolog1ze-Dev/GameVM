# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
<#
.SYNOPSIS
    Switch Windows' display layout the way Win+P does, then put the main display back on the highest
    refresh rate Windows offers (Windows picks 60 Hz when it builds a "Duplicate" layout).

.DESCRIPTION
    Used by `gamevm lg` on Linux: the Looking Glass window only shows a picture while Windows draws on
    the Looking Glass virtual monitor, so gamevm mirrors the screen onto it when the window opens and
    goes back to the main monitor alone when it closes. gamevm starts this script in the signed-in
    user's session (display layouts belong to that session) and reads the result line it appends to
    C:\gamevm\display-layout.log.

        -Mode mirror     what gamevm uses: Duplicate, unless the Looking Glass monitor already shows
                         the desktop (then the layout is yours and stays: "layout=kept")
        -Mode clone      Duplicate: the same picture on every display
        -Mode internal   PC screen only (the first display)
        -Mode external   Second screen only
        -Mode extend     Extend
        -Mode refresh    leave the layout alone, only restore the refresh rate
        -Mode status     change nothing, only report

    The result line ends with the monitors that show the desktop afterwards, e.g.
        active=[DELL U2723QE|Looking Glass]

    By hand:  powershell -ExecutionPolicy Bypass -File C:\gamevm\display-layout.ps1 -Mode clone
#>
param(
    [ValidateSet('mirror', 'clone', 'extend', 'internal', 'external', 'refresh', 'status')][string]$Mode = 'clone',
    # -Mode mirror: part of the name of the monitor that has to show the desktop.
    [string]$Monitor = 'Looking Glass',
    # Marks the result line, so that the caller finds the answer to its own request.
    [string]$Tag = 'manual'
)
$log = 'C:\gamevm\display-layout.log'
# Exists while a Duplicate layout is one that -Mode mirror made (and not one you chose yourself), so
# that gamevm also switches it off again after a Looking Glass window that did not close cleanly.
$marker = 'C:\gamevm\display-mirror.gamevm'
$code = @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace GameVM {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
  public struct DEVMODE {
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
    public short dmSpecVersion; public short dmDriverVersion; public short dmSize; public short dmDriverExtra; public int dmFields;
    public int dmPositionX; public int dmPositionY; public int dmDisplayOrientation; public int dmDisplayFixedOutput;
    public short dmColor; public short dmDuplex; public short dmYResolution; public short dmTTOption; public short dmCollate;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
    public short dmLogPixels; public int dmBitsPerPel; public int dmPelsWidth; public int dmPelsHeight; public int dmDisplayFlags; public int dmDisplayFrequency;
    public int dmICMMethod; public int dmICMIntent; public int dmMediaType; public int dmDitherType; public int dmReserved1; public int dmReserved2;
    public int dmPanningWidth; public int dmPanningHeight;
  }
  // The DISPLAYCONFIG_* structures of QueryDisplayConfig, as far as they are read here.
  [StructLayout(LayoutKind.Sequential)] public struct LUID { public uint Low; public int High; }
  [StructLayout(LayoutKind.Sequential)] public struct PATH_SOURCE { public LUID adapterId; public uint id; public uint modeInfoIdx; public uint statusFlags; }
  [StructLayout(LayoutKind.Sequential)] public struct PATH_TARGET {
    public LUID adapterId; public uint id; public uint modeInfoIdx; public uint outputTechnology; public uint rotation; public uint scaling;
    public uint refreshNumerator; public uint refreshDenominator; public uint scanLineOrdering; public int targetAvailable; public uint statusFlags; }
  [StructLayout(LayoutKind.Sequential)] public struct PATH_INFO { public PATH_SOURCE source; public PATH_TARGET target; public uint flags; }
  [StructLayout(LayoutKind.Sequential)] public struct MODE_INFO {
    public uint infoType; public uint id; public LUID adapterId;
    public ulong u0; public ulong u1; public ulong u2; public ulong u3; public ulong u4; public ulong u5; }
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] public struct TARGET_NAME {
    public uint type; public uint size; public LUID adapterId; public uint id;
    public uint flags; public uint outputTechnology; public ushort edidManufactureId; public ushort edidProductCodeId; public uint connectorInstance;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string friendlyName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string devicePath; }
  public static class Display {
    [DllImport("user32.dll")] public static extern int GetDisplayConfigBufferSizes(uint flags, out uint numPaths, out uint numModes);
    [DllImport("user32.dll")] public static extern int QueryDisplayConfig(uint flags, ref uint numPaths, [Out] PATH_INFO[] paths, ref uint numModes, [Out] MODE_INFO[] modes, IntPtr topology);
    [DllImport("user32.dll")] public static extern int QueryDisplayConfig(uint flags, ref uint numPaths, [Out] PATH_INFO[] paths, ref uint numModes, [Out] MODE_INFO[] modes, out uint topology);
    [DllImport("user32.dll")] public static extern int DisplayConfigGetDeviceInfo(ref TARGET_NAME info);
    // The layout in Win+P's terms (QDC_DATABASE_CURRENT = 4): 1 PC screen only, 2 Duplicate,
    // 4 Extend, 8 Second screen only; 0: unknown.
    public static uint Topology() {
      uint np, nm, topology;
      if (GetDisplayConfigBufferSizes(4, out np, out nm) != 0) return 0;
      PATH_INFO[] paths = new PATH_INFO[np]; MODE_INFO[] modes = new MODE_INFO[nm];
      return QueryDisplayConfig(4, ref np, paths, ref nm, modes, out topology) == 0 ? topology : 0;
    }
    // The monitors that show the desktop now (QDC_ONLY_ACTIVE_PATHS = 2), by the name in their EDID
    // ("DELL U2723QE", "Looking Glass"). Asked from the display system itself: the WMI inventory
    // (Win32_VideoController, WmiMonitorBasicDisplayParams) keeps listing a display that was
    // switched off. null: the query failed.
    public static string Active() {
      uint np, nm;
      if (GetDisplayConfigBufferSizes(2, out np, out nm) != 0) return null;
      PATH_INFO[] paths = new PATH_INFO[np]; MODE_INFO[] modes = new MODE_INFO[nm];
      if (QueryDisplayConfig(2, ref np, paths, ref nm, modes, IntPtr.Zero) != 0) return null;
      List<string> names = new List<string>();
      for (uint i = 0; i < np; i++) {
        TARGET_NAME t = new TARGET_NAME();
        t.type = 2; t.size = (uint)Marshal.SizeOf(typeof(TARGET_NAME)); t.adapterId = paths[i].target.adapterId; t.id = paths[i].target.id;
        string name = "";
        if (DisplayConfigGetDeviceInfo(ref t) == 0) {
          name = t.friendlyName;
          // No name in the EDID: the hardware ID out of \\?\DISPLAY#CND3CF8#...
          if (String.IsNullOrEmpty(name) && t.devicePath != null) { string[] part = t.devicePath.Split('#'); if (part.Length > 1) name = part[1]; }
        }
        if (String.IsNullOrEmpty(name)) name = "display " + paths[i].target.id;
        name = name.Replace("|", " ").Replace("[", "(").Replace("]", ")");
        if (!names.Contains(name)) names.Add(name);
      }
      return String.Join("|", names.ToArray());
    }
    [DllImport("user32.dll")] public static extern int SetDisplayConfig(uint numPaths, IntPtr paths, uint numModes, IntPtr modes, uint flags);
    [DllImport("user32.dll", CharSet = CharSet.Ansi)] public static extern bool EnumDisplaySettings(string device, int modeNum, ref DEVMODE mode);
    [DllImport("user32.dll", CharSet = CharSet.Ansi)] public static extern int ChangeDisplaySettingsEx(string device, ref DEVMODE mode, IntPtr hwnd, uint flags, IntPtr lParam);
  }
}
"@
$result = "$Tag $(Get-Date -Format s) mode=$Mode"
try {
    # Compiling the helper above takes about a second, but 15 to 30 while Windows' idle maintenance
    # rebuilds .NET's native images. So the compiled helper is kept next to this script, named
    # after its source, and compiled again only when the source changed.
    $sha = [BitConverter]::ToString([Security.Cryptography.SHA1]::Create().ComputeHash([Text.Encoding]::ASCII.GetBytes($code)))
    $dll = "C:\gamevm\display-layout-$($sha.Replace('-', '').Substring(0, 12)).dll"
    try { Add-Type -Path $dll -ErrorAction Stop }
    catch {
        Remove-Item C:\gamevm\display-layout-*.dll -Force -ErrorAction SilentlyContinue
        try { Add-Type -TypeDefinition $code -OutputAssembly $dll -ErrorAction Stop; Add-Type -Path $dll -ErrorAction Stop }
        catch { Add-Type -TypeDefinition $code }   # the folder is not writable: compile in memory
    }
    $shown = [GameVM.Display]::Active()
    if ($Mode -eq 'mirror' -and $null -ne $shown -and $shown.Contains($Monitor)) {
        $owned = (Test-Path $marker) -and ([GameVM.Display]::Topology() -eq 2)
        if (-not $owned) { Remove-Item $marker -Force -ErrorAction SilentlyContinue }
        $result += " layout=kept owned=$([int]$owned)"
    } elseif ($Mode -ne 'refresh' -and $Mode -ne 'status') {
        # SDC_APPLY (0x80) with one of the SDC_TOPOLOGY_* flags: the layout Win+P would choose.
        $flag = @{ internal = 0x1; mirror = 0x2; clone = 0x2; extend = 0x4; external = 0x8 }[$Mode]
        $rc = [GameVM.Display]::SetDisplayConfig(0, [IntPtr]::Zero, 0, [IntPtr]::Zero, (0x80 -bor $flag))
        $result += " layout=$rc"
        if ($Mode -eq 'mirror' -and $rc -eq 0) { Get-Date -Format s | Out-File -Encoding ascii $marker }
        else { Remove-Item $marker -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 1500
    }
    # The main display (a null device name; PowerShell would pass "" for $null): highest rate of its
    # current resolution and colour depth. CDS_TEST (2) first, then CDS_UPDATEREGISTRY (1).
    $none = [NullString]::Value
    $size = [System.Runtime.InteropServices.Marshal]::SizeOf([type][GameVM.DEVMODE])
    $cur = New-Object GameVM.DEVMODE; $cur.dmSize = $size
    if ($Mode -eq 'status') {
        if ([GameVM.Display]::EnumDisplaySettings($none, -1, [ref]$cur)) { $result += " refresh=$($cur.dmDisplayFrequency)" }
    } elseif ([GameVM.Display]::EnumDisplaySettings($none, -1, [ref]$cur)) {
        $best = $null; $i = 0
        while ($true) {
            $m = New-Object GameVM.DEVMODE; $m.dmSize = $size
            if (-not [GameVM.Display]::EnumDisplaySettings($none, $i, [ref]$m)) { break }
            $i++
            if ($m.dmPelsWidth -eq $cur.dmPelsWidth -and $m.dmPelsHeight -eq $cur.dmPelsHeight -and $m.dmBitsPerPel -eq $cur.dmBitsPerPel -and
                (-not $best -or $m.dmDisplayFrequency -gt $best.dmDisplayFrequency)) { $best = $m }
        }
        $result += " refresh=$($cur.dmDisplayFrequency)"
        if ($best -and $best.dmDisplayFrequency -gt $cur.dmDisplayFrequency) {
            if ([GameVM.Display]::ChangeDisplaySettingsEx($none, [ref]$best, [IntPtr]::Zero, 2, [IntPtr]::Zero) -eq 0) {
                $rc = [GameVM.Display]::ChangeDisplaySettingsEx($none, [ref]$best, [IntPtr]::Zero, 1, [IntPtr]::Zero)
                $result += "->$($best.dmDisplayFrequency)($rc)"
            } else { $result += "->$($best.dmDisplayFrequency)(refused)" }
        }
    } else { $result += " refresh=unknown" }
    $active = [GameVM.Display]::Active()
    if ($null -ne $active) { $result += " active=[$active]" }
} catch { $result += " error=$($_.Exception.Message -replace '\s+', ' ')" }
$result | Out-File -Append -Encoding ascii $log
