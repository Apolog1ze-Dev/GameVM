# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
<#
.SYNOPSIS
    gamevm benchmark: the same tests natively and in the VM, to see what the VM costs.

.DESCRIPTION
    Self-contained (nothing to download), about a minute. Run it in an administrator PowerShell:
      - natively:   D:\gamevm-windows\3-benchmark.ps1   (or C:\gamevm\3-benchmark.ps1)
      - in the VM:  from Linux, `gamevm bench` runs it for you and adds the host-side numbers.
    Results go to C:\gamevm\bench\<time>-native.json or -vm.json; both systems use the same C:, so
    `gamevm bench` and this script compare every run with the latest native one.

    Tests:
      compress    zlib compression throughput, 1 thread and all logical CPUs (integer CPU work)
      latency     random pointer chasing over 512 MiB (memory latency; shows the VM's two-level
                  page tables and whether its RAM got huge pages)
      copy        256 MiB memory copy, 1 thread (memory bandwidth)
      stall       one spinning thread per logical CPU: longest interruption and how often it is
                  interrupted for more than 100 us (VM exits, host and Windows interrupts)
      disk        sequential read of C: (winsat)

    Close other programs first and use the same power plan for every run.
#>
param(
    [int]$Seconds = 10,
    [string]$Label = "",
    [switch]$SkipDisk,
    [switch]$Quiet
)
$ErrorActionPreference = 'Stop'

function Info($m) { if (-not $Quiet) { [Console]::Error.WriteLine("    $m") } }

$code = @'
using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class GvBench
{
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentThread();
    [DllImport("kernel32.dll")] static extern UIntPtr SetThreadAffinityMask(IntPtr thread, UIntPtr mask);

    static byte[] Sample(int size)
    {
        // Text-like, compressible and identical on every run.
        Random rnd = new Random(1234);
        string[] words = { "frame", "shader", "texture", "audio", "input", "latency", "vertex", "buffer" };
        StringBuilder sb = new StringBuilder(size + 64);
        while (sb.Length < size) { sb.Append(words[rnd.Next(words.Length)]); sb.Append(rnd.Next(10000)); sb.Append(' '); }
        return Encoding.ASCII.GetBytes(sb.ToString(0, size));
    }

    // zlib (Deflate) compression throughput in MB/s of input, `threads` threads for `seconds`.
    public static double Compress(int threads, double seconds)
    {
        byte[] data = Sample(8 << 20);
        long[] done = new long[threads];
        Thread[] ts = new Thread[threads];
        ManualResetEvent go = new ManualResetEvent(false);
        Stopwatch sw = new Stopwatch();
        for (int t = 0; t < threads; t++)
        {
            int id = t;
            ts[t] = new Thread(() =>
            {
                go.WaitOne();
                while (sw.Elapsed.TotalSeconds < seconds)
                {
                    using (MemoryStream ms = new MemoryStream(data.Length))
                    using (DeflateStream ds = new DeflateStream(ms, CompressionLevel.Optimal))
                        ds.Write(data, 0, data.Length);
                    done[id] += data.Length;
                }
            });
            ts[t].Start();
        }
        Thread.Sleep(200);
        sw.Start();
        go.Set();
        foreach (Thread th in ts) th.Join();
        long sum = 0;
        foreach (long d in done) sum += d;
        return sum / sw.Elapsed.TotalSeconds / 1e6;
    }

    // Random pointer chase over `mib` MiB: nanoseconds per dependent load.
    public static double LatencyNs(int mib, int steps)
    {
        int n = (mib << 20) / 4;
        int[] next = new int[n];
        for (int i = 0; i < n; i++) next[i] = i;
        Random rnd = new Random(42);
        for (int i = n - 1; i > 0; i--) { int j = rnd.Next(i); int tmp = next[i]; next[i] = next[j]; next[j] = tmp; }  // Sattolo: one cycle
        int p = 0;
        for (int i = 0; i < 2000000; i++) p = next[p];
        Stopwatch sw = Stopwatch.StartNew();
        for (int i = 0; i < steps; i++) p = next[p];
        sw.Stop();
        if (p == -1) Console.Error.WriteLine("");
        return sw.Elapsed.TotalMilliseconds * 1e6 / steps;
    }

    // Single-thread copy of a 256 MiB buffer, GB/s copied.
    public static double CopyGBs(double seconds)
    {
        byte[] a = new byte[256 << 20], b = new byte[256 << 20];
        for (int i = 0; i < a.Length; i += 4096) { a[i] = 1; b[i] = 1; }
        Buffer.BlockCopy(a, 0, b, 0, a.Length);
        Stopwatch sw = Stopwatch.StartNew();
        long bytes = 0;
        while (sw.Elapsed.TotalSeconds < seconds) { Buffer.BlockCopy(a, 0, b, 0, a.Length); bytes += a.Length; }
        return bytes / sw.Elapsed.TotalSeconds / 1e9;
    }

    // One spinning thread per logical CPU: "worstUs1,worstUs2,...;count1,count2,..." where count is
    // the number of interruptions longer than thresholdUs.
    public static string Stall(double seconds, double thresholdUs)
    {
        int n = Environment.ProcessorCount;
        double[] worst = new double[n];
        long[] count = new long[n];
        Thread[] ts = new Thread[n];
        double usPerTick = 1e6 / Stopwatch.Frequency;
        for (int c = 0; c < n; c++)
        {
            int cpu = c;
            ts[c] = new Thread(() =>
            {
                Thread.BeginThreadAffinity();
                SetThreadAffinityMask(GetCurrentThread(), new UIntPtr(1UL << cpu));
                long limit = (long)(thresholdUs / usPerTick);
                long end = Stopwatch.GetTimestamp() + (long)(seconds * Stopwatch.Frequency);
                long last = Stopwatch.GetTimestamp(), max = 0, over = 0;
                while (true)
                {
                    long now = Stopwatch.GetTimestamp();
                    long gap = now - last;
                    last = now;
                    if (gap > max) max = gap;
                    if (gap > limit) over++;
                    if (now > end) break;
                }
                worst[cpu] = max * usPerTick;
                count[cpu] = over;
                Thread.EndThreadAffinity();
            });
            ts[c].Priority = ThreadPriority.Highest;
            ts[c].Start();
        }
        foreach (Thread th in ts) th.Join();
        string[] w = new string[n], k = new string[n];
        for (int i = 0; i < n; i++) { w[i] = worst[i].ToString("F1", System.Globalization.CultureInfo.InvariantCulture); k[i] = count[i].ToString(); }
        return string.Join(",", w) + ";" + string.Join(",", k);
    }
}
'@
Add-Type -TypeDefinition $code

$isVM = [bool](Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
               Where-Object { $_.InstanceId -match 'VEN_1AF4' } | Select-Object -First 1)
$n = [Environment]::ProcessorCount
$plan = (powercfg /getactivescheme) -replace '^.*\((.*)\)\s*$', '$1'
$r = [ordered]@{
    version      = 1
    where        = $(if ($isVM) { 'vm' } else { 'native' })
    label        = $Label
    time         = (Get-Date).ToString('s')
    cpu          = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name.Trim()
    logical_cpus = $n
    ram_gb       = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
    power_plan   = "$plan"
    seconds      = $Seconds
}
Info "gamevm benchmark ($($r.where), $n logical CPUs), about a minute; keep other programs closed"

Info "compress, 1 thread ..."
$r.compress_1t_mbs = [math]::Round([GvBench]::Compress(1, $Seconds), 1)
Info "compress, $n threads ..."
$r.compress_all_mbs = [math]::Round([GvBench]::Compress($n, $Seconds), 1)
Info "memory latency ..."
$r.mem_latency_ns = [math]::Round([GvBench]::LatencyNs(512, 20000000), 1)
Info "memory copy ..."
$r.mem_copy_gbs = [math]::Round([GvBench]::CopyGBs([math]::Max(3, $Seconds / 2)), 2)
Info "stalls ..."
$parts = [GvBench]::Stall($Seconds, 100).Split(';')
$worst = @($parts[0].Split(',') | ForEach-Object { [double]::Parse($_, [Globalization.CultureInfo]::InvariantCulture) })
$over = @($parts[1].Split(',') | ForEach-Object { [long]$_ })
$r.stall_worst_us = [math]::Round(($worst | Measure-Object -Maximum).Maximum, 1)
$r.stall_worst_median_us = [math]::Round(@($worst | Sort-Object)[[int]($worst.Count / 2)], 1)
$r.stalls_per_s = [math]::Round((($over | Measure-Object -Sum).Sum) / $Seconds, 1)
$r.stall_worst_per_cpu_us = $worst
if (-not $SkipDisk) {
    Info "disk read (winsat) ..."
    $ErrorActionPreference = 'Continue'  # winsat's stderr must not end the script (Windows PowerShell 5.1)
    $out = try { & winsat disk -seq -read -drive c 2>&1 | Out-String } catch { '' }
    $ErrorActionPreference = 'Stop'
    $m = [regex]::Match($out, 'Sequential 64\.0 Read\s+([\d.,]+) MB/s')
    if ($m.Success) { $r.disk_read_mbs = [math]::Round([double]::Parse($m.Groups[1].Value.Replace(',', '.'), [Globalization.CultureInfo]::InvariantCulture), 0) }
}

$dir = 'C:\gamevm\bench'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$json = $r | ConvertTo-Json -Compress -Depth 4
$file = Join-Path $dir ('{0}-{1}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $r.where)
[IO.File]::WriteAllText($file, $json)
[Console]::Out.WriteLine('GAMEVM-BENCH-JSON:' + $json)

if (-not $Quiet) {
    # Human summary, with the latest native run as the baseline.
    $all = @(Get-ChildItem $dir -Filter '*.json' | Sort-Object Name | ForEach-Object { Get-Content $_.FullName -Raw | ConvertFrom-Json })
    $base = $all | Where-Object { $_.where -eq 'native' } | Select-Object -Last 1
    $rows = foreach ($x in $all) {
        $pct = { param($v, $b) if ($b -and $v) { ' ({0:N0}%)' -f (100 * $v / $b) } else { '' } }
        [pscustomobject]@{
            Run          = '{0} {1} {2}' -f $x.where, $x.time.Replace('T', ' ').Substring(0, 16), $x.label
            CPUs         = $x.logical_cpus
            '1 thread'   = '{0:N0}{1}' -f $x.compress_1t_mbs, (& $pct $x.compress_1t_mbs $base.compress_1t_mbs)
            'All threads'= '{0:N0}{1}' -f $x.compress_all_mbs, (& $pct $x.compress_all_mbs $base.compress_all_mbs)
            'Latency ns' = $x.mem_latency_ns
            'Copy GB/s'  = $x.mem_copy_gbs
            'Worst stall us' = $x.stall_worst_us
            'Stalls/s'   = $x.stalls_per_s
            'Disk MB/s'  = $x.disk_read_mbs
        }
    }
    $rows | Format-Table -AutoSize | Out-String -Width 220 | Write-Host
    Write-Host "Saved: $file"
}
