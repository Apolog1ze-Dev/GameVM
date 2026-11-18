# 6. Performance: what is tuned and why

Goal: Windows as close to bare metal as possible, and the host back to 100 % of its CPU and RAM
the moment Windows shuts down. Nothing is reserved at boot: no `isolcpus`, no boot-time hugepages,
no kernel command-line changes.

## What gamevm does

| Area | Setting | Why |
|---|---|---|
| GPU | whole graphics card via VFIO, Resizable BAR (16 GiB) passed through | native driver, native performance; OVMF sizes the 64-bit MMIO window automatically |
| Disk | the whole NVMe controller of the Windows drive via VFIO | Windows' own NVMe driver, DirectStorage/BypassIO intact, **zero host CPU per I/O** |
| Disk (Windows on a SATA disk) | the whole disk, raw, on the VM's SATA controller; opened past Linux' file cache, native asynchronous I/O, TRIM passed on | Windows boots from it with the driver every installation has. Each read and write passes through QEMU on the host's cores, which a SATA disk is slow enough to hide for the most part |
| CPU | whole cores with all their threads (on 8 cores: cores 1–6), `host-passthrough`, topoext, invtsc, real cache topology | the guest sees the processor's real layout and caches; whole cores avoid SMT contention with the host |
| Host isolation (while running) | all host processes, kernel workqueues and IRQs on the host's cores | nothing on the host competes with the guest's cores |
| Idle | deep idle states (exit latency of 100 µs or more) blocked on the guest cores | faster wake-up for guest interrupts and frames |
| Memory | Windows' memory in 2 MiB hugepages, allocated at start and freed at stop; transparent huge pages when RAM is too fragmented for that (`vm.hugepages_fallback`); `vm.defrag_mode = 1` keeps free RAM in 2 MiB blocks | fewer TLB misses; `memballoon` off (VFIO pins memory anyway) |
| Nested virtualization | `svm` hidden from the guest | Windows 11 would otherwise run VBS/HVCI as a nested hypervisor: big slowdowns (one 2026 measurement: GPU memory bandwidth 55 GB/s vs 212 GB/s). Bare metal keeps its VBS. |
| Hyper-V enlightenments | explicit list: relaxed, vapic, spinlocks, vpindex, runtime, synic, stimer+direct, frequencies, tlbflush+extended, ipi, xmm_input | fewer VM exits. On AMD Zen CPUs every exit costs an IBPB (SRSO mitigation), so exits count double |
| Timers | HPET off, Hyper-V reference TSC (hypervclock), PIT `discard` | exit-less timekeeping; PIT `delay` would block AVIC |
| Devices | no emulated display, virtio NIC (vhost, 4 queues), virtio keyboard/mouse, MSI interrupts for the GPU | no slow emulated hardware in hot paths |
| Priorities | ananicy-cpp paused while the VM runs, where it is installed | its rules (CachyOS ships them) renice QEMU to nice 9 / ionice 7 |
| Idle daemons | libvirt only socket-activated (exits 120 s after use), no libvirtd | zero cost while Windows is off |

## What it costs the host while Windows runs

- One or two cores stay with Linux (on an 8-core CPU: 4 of 16 threads) for KWin, the Looking Glass
  client, QEMU's emulator and I/O threads and your background apps. Enough for a desktop, a browser and downloads. Heavy compiles will be slow.
- Half of the memory by default. Lower `vm.memory_mib` if you run heavy Linux workloads at the same time.
- The iGPU's frame buffer (512 MB–1 GB, set in the BIOS), only in iGPU mode.

## How many cores for Windows

The example is an 8-core CPU whose cores all share one cache (a Ryzen 7 9800X3D); set the lists on
the *Resources* page of the gamevm window or in `[cpu]`. On CPUs with two kinds of cores, or two
cache groups, keep Windows' cores within one kind or group.

| Layout | `[cpu]` | Linux keeps | Notes |
|---|---|---|---|
| 6 cores (default) | `guest_cores = [1, 2, 3, 4, 5, 6]`, `host_cores = [0, 7]` | 4 threads | room for KWin, a chat app, the VM's audio and QEMU's own threads |
| 7 cores | `guest_cores = [1, 2, 3, 4, 5, 6, 7]`, `host_cores = [0]` | 2 threads | fine while Linux sits idle; no Looking Glass |
| 8 cores | `guest_cores = [0, 1, 2, 3, 4, 5, 6, 7]`, `host_cores = [0]`, `isolate_host = false` | nothing of its own | Linux, its interrupts and QEMU share Windows' cores: averages can rise while 1 % lows drop. An experiment |

Change the config while Windows is off (`gamevm stop`), then `gamevm start`. Games rarely use
more than 6–8 cores, so measure before you keep a bigger layout.

## Measuring the overhead

Windows in the VM is the same install as natively, so the same benchmark on both shows what the
VM costs. `windows/3-benchmark.ps1` needs no downloads and takes about a minute:

| Test | Measures | What the VM changes |
|---|---|---|
| 1 thread, all threads | zlib compression throughput (integer CPU work) | should be within a few % of native per core |
| mem latency | random access over 512 MiB | two-level page tables add latency; huge pages keep it small |
| mem copy | memory bandwidth, 1 thread | close to native |
| worst stall, stalls>100us | longest interruption of a thread spinning on each CPU | VM exits and host interrupts show up here |
| disk read | sequential read of C: | an NVMe drive is passed through: native speed. A SATA disk goes through the VM: expect somewhat less than native |

1. **Native baseline:** boot Windows natively and, in an administrator PowerShell, run
   `Set-ExecutionPolicy -Scope Process Bypass -Force; C:\gamevm\3-benchmark.ps1`. (The first
   `gamevm bench` copies the script to `C:\gamevm`.)
2. **In the VM:** `gamevm bench --label 6-cores`. It runs the same script in Windows through the
   guest agent and adds the host's side: CPU time of QEMU's own threads next to the vCPUs', and the
   interrupts Linux takes on Windows' cores.
3. **Other layouts:** `gamevm stop`, change `[cpu]` (table above), `gamevm start`, then
   `gamevm bench --label 7-cores`, and so on.
4. **Compare:** `gamevm bench --compare` lists every run against the latest native one (Windows
   keeps the results in `C:\gamevm\bench`, the host keeps its copy in `~/.local/share/gamevm/bench`).

**Games** are what counts in the end. Pick a game with a built-in benchmark (or a repeatable
scene), keep the settings, and run it 3 times natively and in each layout. Compare the average FPS
and the 1 % lows (the game's own report, CapFrameX or PresentMon). GPU-bound scenes show almost no
difference. CPU-bound ones (high frame rates, simulation games) show the VM's cost and the effect of
the core count.

More detail on Linux while a game runs:

```bash
sudo perf kvm stat live                # VM exits per second by reason
ps -eLo tid,psr,ni,comm | grep -E 'CPU [0-9]+/KVM'   # vCPU threads on Windows' cores, nice 0
grep -E 'HugePages_(Total|Free)' /proc/meminfo
cat /sys/fs/cgroup/user.slice/cpuset.cpus.effective  # only Linux' cores while the VM runs
```

## Experiments (off by default; change one thing at a time and keep only what measurably helps)

1. **`tuning.guest_idle_poll = true`** (`-overcommit cpu-pm=on`): the guest idles without exiting,
   which gives the lowest wake-up latency. The guest cores then show 100 % busy on the host (more
   heat and power).
2. **AVIC** (AMD; hardware-virtualised interrupts, posted IOMMU interrupts for the GPU/NVMe): where
   the CPU or BIOS hides the AVIC CPUID bit it needs `force_avic=1`, which taints the kernel.
   `sudo ./setup/install-host.sh --avic`, reboot, set `tuning.avic = true`, `gamevm define`.
   Published gains are small (1–2 %). Undo: delete `/etc/modprobe.d/gamevm-kvm-avic.conf`. Re-check
   after BIOS updates: if the x2AVIC bit appears, Linux enables AVIC by itself.
3. **`nohz_full=<Windows' CPUs> irqaffinity=<Linux' CPUs>`** on the kernel command line: removes the 1000 Hz
   host tick from the guest cores. It is static (applies even when the VM is off), so only add it
   if the measurements show tick noise.
4. **1 GiB hugepages**: only +0.4–2.4 % over 2 MiB pages, and runtime allocation usually fails
   after uptime (would need `hugetlb_cma=` on the kernel command line). Not worth it.

Deliberately **not** done: SCHED_FIFO vCPUs (freezes reported, no gain on isolated cores);
`mitigations=off` (reopens guest-to-host leaks); hiding the hypervisor (`kvm hidden`, `vendor_id`):
NVIDIA has allowed GeForce passthrough since driver R465, and hiding would disable the
enlightenments above.
