# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""gamevm doctor: preflight checks. Read-only; never changes the host."""

from __future__ import annotations

import glob
import grp
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
from pathlib import Path

QEMU_MODULE_DIR = Path("/usr/lib/qemu")
INSTALL_DIR = Path("/usr/local/lib/gamevm")
HOOK = Path("/etc/libvirt/hooks/qemu.d/gamevm")
HOST_CARD_LINK = Path("/dev/dri/gamevm-host-card")


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _out(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    env = dict(os.environ, LC_ALL="C")  # output is parsed
    try:
        r = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def _desktop_user(g, cfg) -> str:
    u = g.audio_user(cfg)
    return u.pw_name if u else ""


def usb_devices_under(bdf: str) -> list[str]:
    out = []
    for d in glob.glob("/sys/bus/usb/devices/*"):
        if ":" in os.path.basename(d) or not os.path.exists(f"{d}/idVendor"):
            continue
        if f"/{bdf}/" in os.path.realpath(d):
            name = " ".join(filter(None, (Path(f"{d}/manufacturer").read_text().strip() if os.path.exists(f"{d}/manufacturer") else "",
                                           Path(f"{d}/product").read_text().strip() if os.path.exists(f"{d}/product") else "")))
            vid = Path(f"{d}/idVendor").read_text().strip()
            pid = Path(f"{d}/idProduct").read_text().strip()
            if vid != "1d6b":  # skip root hubs
                out.append(f"{vid}:{pid} {name}")
    return out


INSTALLED_FROM = {  # file in the checkout -> where setup/install-host.sh puts it
    "gamevm/gamevm.py": INSTALL_DIR / "gamevm.py",
    "gamevm/gamevm_doctor.py": INSTALL_DIR / "gamevm_doctor.py",
    "windows/1-prepare-windows.ps1": INSTALL_DIR / "windows" / "1-prepare-windows.ps1",
    "windows/2-inside-vm.ps1": INSTALL_DIR / "windows" / "2-inside-vm.ps1",
    "windows/3-benchmark.ps1": INSTALL_DIR / "windows" / "3-benchmark.ps1",
    "windows/display-layout.ps1": INSTALL_DIR / "windows" / "display-layout.ps1",
    "windows/send-to-linux.ps1": INSTALL_DIR / "windows" / "send-to-linux.ps1",
    "host/libvirt-hook": HOOK,
    "host/gpu-run": Path("/usr/local/bin/gpu-run"),
    "host/systemd/gamevm-kwin.service": Path("/etc/systemd/system/gamevm-kwin.service"),
    "host/systemd/gamevm-shutdown.service": Path("/etc/systemd/system/gamevm-shutdown.service"),
    "host/udev/61-gamevm-host-gpu.rules": Path("/etc/udev/rules.d/61-gamevm-host-gpu.rules"),
}


def stale_files(g) -> tuple[Path | None, list[str]]:
    """(checkout, its files whose installed copy differs). The checkout is the one this doctor runs
    from or, for the installed doctor, the one the installer recorded in INSTALL_DIR/SOURCE.
    (None, []) if that checkout is gone."""
    here = Path(g.__file__).resolve().parent
    recorded = g.read(INSTALL_DIR / "SOURCE")
    repo = here.parent if here != INSTALL_DIR else (Path(recorded) if recorded else None)
    if not repo or not (repo / "gamevm" / "gamevm.py").is_file():
        return None, []
    files = dict(INSTALLED_FROM)
    files["gamevm/gamevm_ui.py"] = INSTALL_DIR / "gamevm_ui.py"
    for page in sorted((repo / "gamevm" / "ui").glob("*")):  # the window's pages: whatever the checkout has
        files[f"gamevm/ui/{page.name}"] = INSTALL_DIR / "ui" / page.name
    return repo, [src for src, dst in files.items()
                  if (repo / src).is_file() and _sha(repo / src) != _sha(dst)]


def doctor(args, g) -> int:
    r = g.Report()
    root = os.geteuid() == 0
    try:
        cfg = g.load_config(args.config)
    except g.GameVMError as e:
        print(e)
        return 1
    print(f"gamevm {g.VERSION} doctor - config {cfg['_path']}" + ("" if root else "  (run with sudo for disk checks)"))

    # ------------------------------------------------------------------ platform
    r.section("Platform")
    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        r.ok("/dev/kvm is usable")
    else:
        r.fail("/dev/kvm missing or not accessible", "enable SVM in the BIOS; kvm_amd must be loaded")
    groups = os.listdir("/sys/kernel/iommu_groups") if os.path.isdir("/sys/kernel/iommu_groups") else []
    if groups:
        r.ok(f"IOMMU enabled ({len(groups)} groups)")
    else:
        r.fail("IOMMU is off", "enable IOMMU in the BIOS (Settings > Miscellaneous > IOMMU)")
    avic = g.read("/sys/module/kvm_amd/parameters/avic")
    if avic == "Y":
        r.ok("kvm_amd AVIC enabled (hardware-accelerated guest interrupts)")
    elif avic and cfg["tuning"]["avic"]:
        r.fail("tuning.avic = true but kvm_amd AVIC is off", "setup/install-host.sh --avic, then reboot")
    elif avic:
        r.ok("kvm_amd AVIC off (this CPU/BIOS hides it; optional experiment, docs/06-performance.md)")
    gone = g.kernel_modules_missing(cfg)
    if gone:
        r.fail(f"the running kernel ({os.uname().release}) can no longer load {', '.join(gone)}: its modules were "
               "removed from disk (kernel or driver update without a reboot)", "reboot before starting Windows")
    else:
        r.ok("the running kernel can load the modules a start needs (vfio-pci, host GPU driver)")
    mem_total = int(dict(l.split(":", 1) for l in g.read("/proc/meminfo").splitlines())["MemTotal"].split()[0]) // 1024
    left = mem_total - cfg["vm"]["memory_mib"]
    if left < 6144:
        r.warn(f"only {left} MiB RAM left for the host while the VM runs", "lower vm.memory_mib")
    else:
        r.ok(f"RAM: {cfg['vm']['memory_mib']} MiB for Windows, {left} MiB for the host")
    shared = set(cfg["cpu"]["guest_cores"]) & set(cfg["cpu"]["host_cores"])
    if shared:
        r.warn(f"Windows shares cores {sorted(shared)} with Linux (isolate_host = false): Linux, its interrupts and "
               "QEMU's own threads interrupt Windows' vCPUs, so expect lower 1% lows", "fine for an experiment; "
               "for gaming keep at least one core for Linux (e.g. guest_cores = [1..7], host_cores = [0])")
    if cfg["vm"]["hugepages"]:
        need = cfg["vm"]["memory_mib"] // 2
        free2m = 0  # free blocks of 2 MiB or larger, counted in 2 MiB pages (buddy orders 9 and up)
        for line in g.read("/proc/buddyinfo").splitlines():
            counts = [int(x) for x in line.split()[4:]]
            free2m += sum(c << (order - 9) for order, c in enumerate(counts) if order >= 9)
        fallback = " (gamevm then starts Windows on transparent huge pages, slightly slower)" \
            if cfg["vm"]["hugepages_fallback"] else " (the start is then refused)"
        if free2m >= need:
            r.ok(f"huge pages: {free2m} contiguous 2 MiB blocks free, Windows needs {need}")
        else:
            r.warn(f"huge pages: only {free2m} contiguous 2 MiB blocks free right now, Windows needs {need}; "
                   f"compaction usually finds more, but may fall short{fallback}",
                   "close big programs before starting; after a reboot RAM is unfragmented. If it falls short at "
                   "every start (journalctl -t gamevm, /var/log/gamevm.log), vm.hugepages = false skips the attempt")
        mode = g.read("/proc/sys/vm/defrag_mode")
        if mode == "0":
            r.warn("vm.defrag_mode is off: free RAM fragments over the uptime, so huge pages often cannot "
                   "be reserved", "re-run setup/install-host.sh (sets vm.defrag_mode = 1)")

    # ------------------------------------------------------------------ mode
    r.section("Mode")
    try:
        mode = g.resolve_mode(cfg)
        others = g.host_display_gpus(cfg)
        r.ok(f"mode = {mode} (configured: {cfg['vm']['mode']})" + (f", host GPU: {', '.join(others)}" if others else ""))
    except g.GameVMError as e:
        r.fail(str(e))
        mode = "single"
    user = _desktop_user(g, cfg)
    if mode == "igpu":
        if not HOST_CARD_LINK.exists():
            r.fail(f"{HOST_CARD_LINK} is missing", "re-run setup/install-host.sh (installs the udev rule), then reboot")
        for old in ("/etc/systemd/user-environment-generators/60-gamevm-host-gpu",
                    "/etc/mkinitcpio.conf.d/90-gamevm-igpu.conf"):
            if os.path.exists(old):
                r.fail(f"{old} is left over from an older gamevm", "re-run setup/install-host.sh, then reboot")
        layout = g.kwin_layout()
        rc, enabled = _out(["systemctl", "is-enabled", "gamevm-kwin.service"])
        if g.gpu_dedicated():
            r.ok("dedicated GPU: the Linux desktop runs on the iGPU only (main monitor shows Windows only)")
        elif enabled != "enabled":
            r.fail("gamevm-kwin.service is not enabled", "re-run setup/install-host.sh, then reboot")
        elif layout == "unmanaged":
            r.fail("the KWin GPU layout was not set up this boot",
                   "reboot; if it persists: journalctl -b -u gamevm-kwin.service")
        else:
            r.ok({"native": "desktop: main monitor through the graphics card, side monitor through the iGPU",
                  "igpu": "desktop: side monitor only (iGPU), the graphics card is free for Windows"}[layout])
            pids = [p for p in g.procs_named(("kwin_wayland",)) if user and g.proc_uid(p) == pwd.getpwnam(user).pw_uid]
            if root and pids:
                env = g.read(f"/proc/{pids[0]}/environ").split("\0")
                if not any(e.startswith("KWIN_DRM_DEVICES=" + str(g.KWIN_DIR)) for e in env):
                    r.fail("your running KWin was started without gamevm's GPU layout", "reboot")
                else:
                    r.ok("KWin was started with gamevm's GPU layout (it can be moved between the GPUs)")
            elif not root:
                r.ok("(run with sudo to check the running KWin's GPU layout)")
            if user:
                cmd = ["systemctl", "--user", *(["-M", f"{user}@"] if root else []), "show-environment"]
                rc, out = _out(cmd)
                want = f"__EGL_VENDOR_LIBRARY_FILENAMES={g.KWIN_DIR}/egl/"
                if rc == 0 and not any(l.startswith(want) for l in out.splitlines()):
                    r.warn("your session started without gamevm's app environment: apps opened while "
                           "Windows runs would try the graphics card", "reboot")
        if layout == "native" and user:
            u = pwd.getpwnam(user)
            closing = g.closing_apps(cfg, u)
            reopen = sorted(set(g.wanted_apps(cfg, u)) & set(closing))
            r.ok("starting Windows restarts the desktop: "
                 + (f"closes {', '.join(closing)}" if closing else "no open app closes")
                 + (f"; reopens {', '.join(reopen)}" if reopen else ""))
            blockers = g.blocking_holders(cfg, u)
            if blockers:
                r.warn(str(g.blocking_error(blockers)), "stop it before gamevm start" + ("" if root else
                       " (run with sudo to also see root's and other users' programs)"))
        if user and layout != "igpu":
            cmd = ["kscreen-doctor", "-j"]
            if root:
                uid = pwd.getpwnam(user).pw_uid
                cmd = ["runuser", "-u", user, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                       "WAYLAND_DISPLAY=wayland-0", f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", *cmd]
            rc, out = _out(cmd, timeout=8)
            try:
                outputs = json.loads(out).get("outputs", []) if rc == 0 else []
            except ValueError:
                outputs = []
            for o in outputs:
                if o.get("enabled") and o.get("vrrPolicy") == 0 and str(o.get("name", "")).startswith("DP"):
                    r.warn(f"adaptive sync (VRR / G-SYNC Compatible) is off on {o['name']} for Linux games",
                           "System Settings > Display & Monitor > Adaptive sync: Automatic")
        # Vulkan for the chip the desktop runs on while Windows has the card (AMD: radeon, Intel: intel).
        host_gpus = g.host_display_gpus(cfg)
        vendor = g.read(g.SYS_PCI / host_gpus[0] / "vendor").strip() if host_gpus else ""
        icd, pkg = {"0x8086": ("intel_icd*.json", "vulkan-intel")}.get(vendor, ("radeon_icd*.json", "vulkan-radeon"))
        if not glob.glob(f"/usr/share/vulkan/icd.d/{icd}"):
            r.warn(f"{pkg} is not installed (apps on the side monitor would lack Vulkan while Windows runs)",
                   f"pacman -S {pkg} lib32-{pkg}")
    else:
        r.warn("single-GPU mode: your Linux desktop closes while Windows runs",
               "enable the iGPU in the BIOS to keep the desktop (docs/01-requirements-and-cabling.md)")
        rc, out = _out(["loginctl", "show-user", user or str(os.getuid()), "-p", "Linger", "--value"])
        if out.strip() != "yes":
            r.warn("lingering is off: your PipeWire stops with the desktop, so the VM would have no sound",
                   f"sudo loginctl enable-linger {user}")

    # ------------------------------------------------------------------ GPU
    r.section("GPU passthrough")
    devs = cfg["gpu"]["devices"]
    for bdf in devs:
        if not g.pci_exists(bdf):
            r.fail(f"{bdf} does not exist", "PCI addresses change when BIOS settings change: check lspci -nn")
            continue
        extra = g.check_iommu_group(bdf, devs)
        if extra:
            r.fail(f"{bdf} shares IOMMU group {g.iommu_group(bdf)} with {extra}")
        else:
            r.ok(f"{bdf} {g.pci_desc(bdf)}  [group {g.iommu_group(bdf)}, driver {g.pci_driver(bdf)}]")
    try:
        g.verify_identity(cfg)
        if cfg["gpu"]["expect_ids"] or cfg["disk"]["expect_serial"]:
            r.ok("GPU and Windows disk match the recorded IDs and serial number")
    except g.GameVMError as e:
        r.fail(str(e), "fix gpu.devices / disk.nvme / disk.boot_disk in /etc/gamevm/gamevm.toml (lspci -nn)")
    rc, klog = _out(["journalctl", "-k", "-b", "-q", "--no-pager", "-g", "1:1 IOMMU mapping"])
    if rc == 0 and klog.strip():
        r.fail("firmware demands identity IOMMU mappings (newer BIOS DMA protection): VFIO will fail",
               "BIOS: Settings > Miscellaneous > Kernel DMA Protection Indicator = Disabled (docs/01)")
    vga = devs[0] if devs else ""
    if vga and g.pci_exists(vga):
        bars = [l.split() for l in g.read(f"/sys/bus/pci/devices/{vga}/resource").splitlines()]
        sizes = [int(e, 16) - int(s, 16) + 1 for s, e, _ in bars if int(s, 16)]
        big = max(sizes) // (1024 * 1024) if sizes else 0
        need = 2 * big if big > 256 else 0
        mm = cfg["gpu"]["mmio64_mib"]
        if big > 256 and mm and mm < need:
            r.warn(f"Resizable BAR is {big} MiB but gpu.mmio64_mib is {mm}", f"set gpu.mmio64_mib >= {need}")
        elif big > 256:
            r.ok(f"Resizable BAR active ({big} MiB); 64-bit MMIO window {mm or 'auto'} MiB")
    for unit in cfg["gpu"]["host_services"]:
        rc, _ = _out(["systemctl", "is-enabled", unit])
        if rc == 0:
            r.warn(f"{unit} is enabled; it will be stopped while the VM runs")

    # ------------------------------------------------------------------ disk
    r.section("Windows disk")
    nvme = cfg["disk"]["nvme"]
    boot = cfg["disk"]["boot_disk"]
    allowed = {os.path.normpath(m) for m in cfg["disk"]["unmount"]}
    blk, mounts, what = [], [], ""  # the Windows drive's block devices and what is mounted from them
    if nvme:
        if not g.pci_exists(nvme):
            r.fail(f"{nvme} does not exist")
        else:
            blk, what = g.block_devices_on(nvme), nvme
            mounts = g.mounts_of(blk)
            dangerous = [m for m in mounts if m["mountpoint"] in g.HOST_MOUNTS]
            if dangerous:
                r.fail(f"{nvme} holds host system mounts {[m['mountpoint'] for m in dangerous]}: this is the HOST disk",
                       "set disk.nvme to the Windows NVMe")
                mounts = []
            else:
                extra = g.check_iommu_group(nvme, [nvme])
                if extra:
                    r.fail(f"{nvme} shares its IOMMU group with {extra}")
                else:
                    r.ok(f"{nvme} {g.pci_desc(nvme)}  [group {g.iommu_group(nvme)}] disks: {', '.join(b for b in blk if 'p' not in b[4:])}")
    elif boot:
        # Windows on a SATA disk: not handed over as hardware, QEMU opens the whole disk.
        problem = g.boot_disk_problem(cfg)
        if problem:
            r.fail(problem, "fix disk.boot_disk in /etc/gamevm/gamevm.toml (gamevm discover shows what it finds)")
        else:
            blk, what = g.disk_devices(boot), boot
            mounts = g.mounts_of(blk)
            model = g.read(f"/sys/block/{blk[0]}/device/model").strip() or blk[0]
            r.ok(f"{blk[0]} ({model}) is the disk Windows starts from; the VM sees it as a SATA disk")
            if g.holders_of_block(blk):
                r.fail(f"{blk[0]} is used by device-mapper/md ({', '.join(g.holders_of_block(blk))}): Linux is using it",
                       "set disk.boot_disk to the Windows disk")
            if not any(g.read(f"/sys/class/block/{b}/partition") for b in blk[1:]):
                r.warn(f"{blk[0]} has no partitions: is Windows installed on it?")
            rc, props = _out(["udevadm", "info", "--query=property", f"--name={boot}"])
            if rc == 0 and "UDISKS_IGNORE=1" not in props.splitlines():
                r.warn(f"{blk[0]} is not hidden from the file manager (Dolphin could mount it while Windows uses it)",
                       "re-run setup/install-host.sh")
    else:
        r.warn("no drive for Windows to start from is set (disk.nvme or disk.boot_disk)",
               "sudo ./setup/install-host.sh fills it in; `gamevm discover` shows what it finds")
    for m in mounts:
        if os.path.normpath(m["mountpoint"]) in allowed:
            r.ok(f"{m['mountpoint']} ({m['dev']}, {m['fstype']}) will be unmounted at VM start")
        else:
            r.fail(f"{m['dev']} is mounted on {m['mountpoint']}, which is not in disk.unmount",
                   "unmount it or add it to disk.unmount")
    if what and root and shutil.which("ntfs-3g.probe"):
        for b in blk:
            rc, fstype = _out(["blkid", "-o", "value", "-s", "TYPE", f"/dev/{b}"])
            if fstype.strip() != "ntfs":
                continue
            rc, _ = _out(["ntfs-3g.probe", "--readwrite", f"/dev/{b}"])
            if rc == 14:
                r.fail(f"/dev/{b} is HIBERNATED (Windows Fast Startup / hibernate)",
                       "boot Windows natively, run windows/1-prepare-windows.ps1, shut down normally")
            elif rc == 15:
                r.warn(f"/dev/{b} was not cleanly unmounted (Windows will check it)")
            elif rc == 16 and any(m["dev"] == b and os.path.normpath(m["mountpoint"]) in allowed for m in mounts):
                r.ok(f"/dev/{b} is mounted on the host; the hook unmounts it and checks hibernation at VM start")
            elif rc == 16:
                r.warn(f"/dev/{b} is in use, hibernation state not checked")
            elif rc == 0:
                r.ok(f"/dev/{b} NTFS is clean (not hibernated)")
    elif what and not root:
        hib = [m["mountpoint"] for m in mounts if os.path.exists(os.path.join(m["mountpoint"], "hiberfil.sys"))]
        for mp in hib:
            r.warn(f"{mp}/hiberfil.sys exists: Windows may use Fast Startup",
                   "sudo gamevm doctor checks it; windows/1-prepare-windows.ps1 turns it off")
    for path in cfg["disk"]["extra"]:
        if not os.path.exists(path):
            r.warn(f"disk.extra {path} not found: Windows starts without it", "check the path: ls -l /dev/disk/by-id/")
            continue
        devs = g.disk_devices(path)
        model = g.read(f"/sys/block/{devs[0]}/device/model") or devs[0]
        emounts = g.mounts_of(devs)
        foreign = [m for m in emounts if os.path.normpath(m["mountpoint"]) not in allowed]
        if foreign:
            r.fail(f"extra disk {devs[0]} ({model}) is in use by the host: "
                   + ", ".join(f"{m['dev']} on {m['mountpoint']}" for m in foreign),
                   "add those mount points to disk.unmount, or remove the disk from disk.extra")
        else:
            r.ok(f"extra disk {devs[0]} ({model}) goes to Windows"
                 + (f"; {', '.join(m['mountpoint'] for m in emounts)} is unmounted while Windows runs" if emounts else ""))
        rc, props = _out(["udevadm", "info", "--query=property", f"--name={path}"])
        if rc == 0 and "UDISKS_IGNORE=1" not in props.splitlines():
            r.warn(f"{devs[0]} is not hidden from the file manager (Dolphin could mount it while Windows uses it)",
                   "re-run setup/install-host.sh")
    # KDE File Search crawling the Windows partition keeps it busy and re-checks it after every run.
    if user:
        for mp in map(os.path.normpath, cfg["disk"]["unmount"]):
            if g.baloo_indexes(pwd.getpwnam(user), mp):
                r.warn(f"File Search (Baloo) indexes {mp}: it keeps the Windows drive busy (gamevm pauses it "
                       "while Windows runs) and re-checks all of Windows' files after every run",
                       f"System Settings > Search > File Search: stop indexing {mp} "
                       f"(or: balooctl6 config remove includeFolders {mp}/)")

    # ------------------------------------------------------------------ USB / input
    r.section("USB and input")
    for bdf in cfg["usb"]["controllers"]:
        if not g.pci_exists(bdf):
            r.fail(f"USB controller {bdf} does not exist")
            continue
        extra = g.check_iommu_group(bdf, cfg["usb"]["controllers"])
        moved = usb_devices_under(bdf) or ["(nothing plugged in)"]
        if extra:
            r.fail(f"USB controller {bdf} shares its IOMMU group with {extra}")
        else:
            r.ok(f"USB controller {bdf} goes to Windows with: {'; '.join(moved)}")
    if cfg["input"]["evdev"]:
        for key in ("keyboard", "mouse"):
            p = cfg["input"][key]
            if p and os.path.exists(p):
                r.ok(f"{key}: {p}")
            elif p:
                r.warn(f"{key} {p} not found (unplugged?)", "wake or plug it in first (ls /dev/input/by-id): "
                       + ("gamevm start asks before it starts Windows without a keyboard" if key == "keyboard"
                          else "gamevm start then boots Windows without it"))
        for p in cfg["input"]["extra"]:
            if not os.path.exists(p):
                r.warn(f"extra input {p} not found", "remove it from input.extra")
        r.ok(f"switch keyboard/mouse with {cfg['input']['toggle']}")

    # ------------------------------------------------------------------ software
    r.section("Software")
    need = {"virsh": "libvirt", "qemu-system-x86_64": "qemu-desktop", "ntfs-3g.probe": "ntfs-3g"}
    if cfg["firmware"]["tpm"]:
        need["swtpm"] = "swtpm"
    if cfg["firmware"]["smbios_host"]:
        need["dmidecode"] = "dmidecode"
    for cmd, pkg in need.items():
        if shutil.which(cmd):
            r.ok(f"{cmd}")
        else:
            r.fail(f"{cmd} missing", f"pacman -S {pkg}")
    qmods = {"audio-pipewire.so": cfg["audio"]["backend"] == "pipewire",
             "hw-usb-host.so": bool(cfg["usb"]["devices"]),
             "ui-spice-core.so": cfg["looking_glass"]["enabled"] and mode == "igpu",
             "chardev-spice.so": cfg["looking_glass"]["enabled"] and mode == "igpu"}
    for mod, wanted in qmods.items():
        if wanted and not (QEMU_MODULE_DIR / mod).exists():
            r.fail(f"QEMU module {mod} missing", "pacman -S qemu-desktop")
    try:
        in_libvirt = pwd.getpwnam(user).pw_name in grp.getgrnam("libvirt").gr_mem if user else False
    except KeyError:
        in_libvirt = False
    if in_libvirt and not root:
        try:
            if grp.getgrnam("libvirt").gr_gid not in os.getgroups():
                r.warn("you were added to the libvirt group, but this session does not have it yet",
                       "reboot once (with lingering, logging out is not enough)")
        except KeyError:
            pass
    if in_libvirt:
        r.ok(f"{user} is in the libvirt group")
    else:
        r.fail(f"{user or 'you'} not in the libvirt group", f"sudo usermod -aG libvirt {user}; then reboot once (with lingering, logging out is not enough)")
    rc, out = _out(["virsh", "-c", g.LIBVIRT_URI, "version"])
    if rc == 0:
        r.ok("libvirt reachable: " + " / ".join(l.split(":", 1)[1].strip() for l in out.splitlines() if "library" in l.lower() or "hypervisor" in l.lower()))
    else:
        r.fail("cannot connect to qemu:///system", "systemctl enable --now virtqemud.socket; check group membership")
    if Path("/etc/libvirt/hooks/qemu").exists():
        r.warn("/etc/libvirt/hooks/qemu exists and runs before gamevm's hook",
               "make sure it does not touch the GPU or the Windows disk")
    if HOOK.exists() and os.access(HOOK, os.X_OK):
        repo, differ = stale_files(g)
        if differ:
            r.warn(f"the installed gamevm is not the one in {repo} (differs: {', '.join(differ)})",
                   "sudo " + shlex.quote(f"{repo}/setup/install-host.sh"))
        else:
            r.ok(f"libvirt hook installed ({HOOK})" + (f"; installed files match {repo}" if repo else ""))
    else:
        r.fail("libvirt hook not installed", "run setup/install-host.sh")
    _, enabled = _out(["systemctl", "is-enabled", "gamevm-shutdown.service"])
    _, active = _out(["systemctl", "is-active", "gamevm-shutdown.service"])
    if enabled == "enabled" and active == "active":
        r.ok(f"shutting down or rebooting the host shuts Windows down first (waits up to "
             f"{cfg['vm']['host_shutdown_wait']}s, gamevm-shutdown.service)")
    else:
        r.warn("gamevm-shutdown.service is not enabled and running: shutting down or rebooting the host while "
               "Windows runs would cut Windows' power after a few seconds", "re-run setup/install-host.sh")
    rc, _ = _out(["systemctl", "is-enabled", "--quiet", "libvirt-guests.service"])
    if rc == 0:
        r.warn("libvirt-guests.service is enabled: at host shutdown it also acts on this VM (by default it tries to "
               "suspend it, which a VM with a passed-through GPU cannot do)",
               "sudo systemctl disable --now libvirt-guests.service, unless other VMs need it")
    if g.ETC_CONFIG.exists():
        st = g.ETC_CONFIG.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            r.fail(f"{g.ETC_CONFIG} must be root-owned and not writable by others")
    if cfg["audio"]["backend"] == "pipewire":
        qu = g.qemu_process_user()
        if qu == user:
            r.ok(f"QEMU runs as {user}: it reaches your PipeWire directly")
        elif cfg["audio"]["qemu_as_user"]:
            r.warn(f"audio.qemu_as_user = true but QEMU runs as {qu}", "re-run setup/install-host.sh")
        else:
            r.ok(f"QEMU runs as {qu}; the hook hands it your PipeWire socket (and nothing else) while the VM runs")
        rc, xml = _out(["virsh", "-c", g.LIBVIRT_URI, "dumpxml", "--inactive", cfg["vm"]["name"]])
        if rc == 0 and qu != user and "type='pipewire'" in xml and str(g.PIPEWIRE_DIR) not in xml:
            r.warn("the defined VM still expects PipeWire in your session directory (defined by an older gamevm): "
                   "started from virt-manager it would have no sound", "gamevm define (gamevm start does it too)")
    if os.path.exists(g.REBOOT_MARKER):
        r.fail("the NVIDIA GPU failed to re-initialise after the last VM run", "reboot the host")

    # ------------------------------------------------------------------ network
    r.section("Network")
    net = cfg["network"]
    if net["mode"] == "macvtap":
        state = g.read(f"/sys/class/net/{net['host_nic']}/operstate")
        if state == "up":
            r.ok(f"macvtap on {net['host_nic']}: Windows gets its own LAN address")
        else:
            r.fail(f"{net['host_nic']} is '{state or 'missing'}'", "set network.host_nic to your wired NIC (ip -br link)")
    if net["hostonly"]:
        rc, out = _out(["virsh", "-c", g.LIBVIRT_URI, "net-info", net["hostonly_network"]])
        if rc == 0 and "Active:         yes" in out:
            r.ok(f"host-only network {net['hostonly_network']} active")
        else:
            r.warn(f"host-only network {net['hostonly_network']} not active",
                   "gamevm start starts it; if it is not defined at all, re-run setup/install-host.sh")

    # ------------------------------------------------------------------ sharing
    r.section("Sharing with Windows while it runs")
    if not net["hostonly"]:
        r.warn("network.hostonly is off: Linux has no link to the running Windows (no shared drives, no clipboard)",
               "set network.hostonly = true")
    else:
        if Path("/usr/lib/qt6/plugins/kf6/kio/smb.so").exists():
            r.ok("files: `gamevm files` opens Windows' drives in the file manager (set up in Windows by 2-inside-vm.ps1)")
        else:
            r.warn("the file manager cannot open Windows' shared drives", "pacman -S kio-extras")
        if shutil.which("kdeconnect-cli"):
            r.ok("clipboard and sending files: KDE Connect is installed (`gamevm share` shows whether it is paired)")
        else:
            r.warn("KDE Connect is not installed: no shared clipboard", "pacman -S kdeconnect, then gamevm share")

    # ------------------------------------------------------------------ looking glass
    if cfg["looking_glass"]["enabled"] and mode == "igpu":
        r.section("Looking Glass")
        if shutil.which("looking-glass-client"):
            r.ok("looking-glass-client installed")
        else:
            r.warn("looking-glass-client not installed", "run looking-glass/install.sh")
        if cfg["looking_glass"]["kvmfr"]:
            if os.path.exists("/dev/kvmfr0"):
                size = g.read("/sys/module/kvmfr/parameters/static_size_mb")
                want = cfg["looking_glass"]["size_mib"]
                if size and int(size.split(",")[0]) != want:
                    r.fail(f"/dev/kvmfr0 is {size} MiB, config wants {want} MiB",
                           "edit /etc/modprobe.d/kvmfr.conf, then: sudo modprobe -r kvmfr && sudo modprobe kvmfr")
                else:
                    r.ok(f"/dev/kvmfr0 ready ({size or '?'} MiB)")
            else:
                r.warn("/dev/kvmfr0 missing (kvmfr module not loaded)", "run looking-glass/install.sh")

    # ------------------------------------------------------------------ domain
    r.section("VM definition")
    try:
        plan = g.cpu_plan(cfg)
        r.ok(f"{len(plan['guest'])} vCPUs ({plan['cores']} cores x {plan['threads']} threads) on CPUs "
             f"{g.cpuset(plan['guest'])}; host keeps {g.cpuset(plan['host'])}")
        xml = g.render_xml(cfg, mode)
        if shutil.which("virt-xml-validate"):
            tmp = Path(f"/tmp/gamevm-doctor-{os.getpid()}.xml")
            tmp.write_text(xml)
            rc, out = _out(["virt-xml-validate", str(tmp), "domain"])
            tmp.unlink(missing_ok=True)
            if rc == 0:
                r.ok("generated domain XML validates against the libvirt schema")
            else:
                r.fail("generated domain XML does not validate", out[-400:])
        state = g.domain_state(cfg["vm"]["name"])
        r.ok(f"domain {cfg['vm']['name']}: {state}")
    except g.GameVMError as e:
        r.fail(str(e))

    print(f"\n{r.fails} problem(s), {r.warns} warning(s)")
    return 1 if r.fails else 0
