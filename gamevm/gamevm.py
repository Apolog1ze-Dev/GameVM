#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""gamevm - near-native Windows gaming VM (GPU + NVMe passthrough) on Arch-based Linux.

User commands (no root needed once you are in the 'libvirt' group):
  gamevm doctor              preflight checks (sudo gamevm doctor adds disk/hibernation checks)
  gamevm render              print the libvirt domain XML generated from the config
  gamevm define              (re)define the libvirt domain from the config
  gamevm start               start the VM; the libvirt hook does the host-side hand-off
  gamevm stop                clean shutdown (also of a paused VM), waits until the host got the GPU back
  gamevm kill                force power-off (like pulling the plug)
  gamevm status              VM, GPU binding, hugepages and CPU isolation state
  gamevm holders             processes that keep the NVIDIA GPU busy
  gamevm usb attach|detach   hot-plug a USB device (VID:PID) into the running VM
  gamevm input               plug the keyboard and mouse into the running VM again (after a re-plug)
  gamevm files               open Windows' drives in the file manager (through Windows while it runs)
  gamevm share               clipboard and file sharing with the running Windows: status and setup
  gamevm view vm|host        switch the shared monitor's input (DDC/CI)
  gamevm lg                  open the Looking Glass client
  gamevm back                bring the Linux desktop back to the main monitor (desktop.return = "ask")
Root commands:
  gamevm recover             undo host changes after a crash (CPU isolation, hugepages, GPU driver, desktop)
  gamevm desktop native|igpu move the Linux desktop to the graphics card (main monitor) or the iGPU
  gamevm bench [--compare]   benchmark Windows in the VM against native runs (docs/06)
  gamevm hook ...            libvirt qemu hook entry point (called by libvirt, not by you)

Everything a start changes on the host is recorded in /run/gamevm/state.json and undone in
reverse order when the VM stops, or immediately if a step fails.
"""

from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import glob
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import syslog
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

VERSION = "1.0.0"
ETC_CONFIG = Path("/etc/gamevm/gamevm.toml")
ETC_SMBIOS = Path("/etc/gamevm/smbios.toml")  # host SMBIOS identity, captured by the installer (root)
REPO_CONFIG = Path(__file__).resolve().parent.parent / "config" / "gamevm.toml"
STATE_DIR = Path("/run/gamevm")
STATE_FILE = STATE_DIR / "state.json"
# QEMU's PipeWire runtime directory while the VM runs: holds nothing but a bind mount of the
# desktop user's PipeWire socket (step_audio_access). Short: socket paths end at 108 bytes.
PIPEWIRE_DIR = STATE_DIR / "pipewire"
LOG_FILE = Path("/var/log/gamevm.log")
LIBVIRT_URI = "qemu:///system"
SYS_PCI = Path("/sys/bus/pci/devices")
HUGEPAGES_2M = Path("/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages")
# Marks the refusal that `gamevm start` answers by starting on transparent huge pages instead.
HUGEPAGES_SHORT = "[hugepages-short]"
DOMAIN_XML = ""  # the domain XML libvirt hands the hook (on stdin)
WORKQUEUE_MASK = Path("/sys/devices/virtual/workqueue/cpumask")
ISOLATED_UNITS = ("system.slice", "user.slice", "init.scope")
PIPEWIRE_UNITS = ("pipewire.socket", "pipewire-pulse.socket", "pipewire.service", "pipewire-pulse.service",
                  "wireplumber.service")
GSP_FAILURE_RE = r"RmInitAdapter failed|WPR2 already up|GPU has fallen off the bus|Failed to allocate NvKmsKapiDevice"
REBOOT_MARKER = STATE_DIR / "gpu-needs-reboot"
# desktop.return = "ask": Windows has stopped and the Linux desktop was left on the side monitor.
# Holds where the windows were while Linux had both monitors, for the way back.
PENDING_FILE = STATE_DIR / "return-pending.json"
# The host-only network (libvirt/gamevm-host-network.xml): the only path between Linux and the running
# Windows, whose LAN adapter (macvtap) cannot reach its own host. Windows gets its address from the
# DHCP reservation the installer makes.
HOST_LINK_IP, GUEST_LINK_IP = "10.77.0.1", "10.77.0.2"
WINDOW_SCRIPT = "gamevm-windows"  # the one KWin script gamevm loads (reads or places windows)
DEDICATED_CONF = Path("/etc/modprobe.d/gamevm-vfio-dedicated.conf")  # written by install-host.sh --dedicated-gpu
# iGPU mode: KWin drives the main monitor through the graphics card while the VM is off. KWin's GPU list
# (fixed for the session by kwin_wayland_wrapper) names links in KWIN_DIR; the hook adds or removes
# the links and restarts KWin to move the desktop between the graphics card and the iGPU.
KWIN_DIR = STATE_DIR / "kwin"
KWIN_UNITS = ("plasma-kwin_wayland.service", "plasma-login-kwin_wayland.service")
USER_UNIT_DROPIN_DIR = Path("/run/systemd/user")
# Read by every user manager when it starts (environment.d): apps reach NVIDIA's EGL only through
# the link in KWIN_DIR.
APP_ENV_FILE = Path("/run/environment.d/60-gamevm.conf")
NVIDIA_EGL_JSON = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
MESA_EGL_JSON = "/usr/share/glvnd/egl_vendor.d/50_mesa.json"
# User services that must never be restarted to free the graphics card: restarting them ends the session
# or cuts the audio of the VM. If one of them holds the card, the start is refused instead.
SESSION_CRITICAL_UNITS = {*KWIN_UNITS, "plasma-ksmserver.service", "plasma-workspace.target",
                          "dbus.service", "dbus-broker.service", "pipewire.service",
                          "pipewire-pulse.service", "wireplumber.service", "init.scope"}
# Session services that keep starting NVIDIA tools while anything shows GPU sensors: KDE's System
# Monitor daemon runs `nvidia-smi dmon`, D-Bus activated by panel widgets. Restarting them does not
# help, so they are held off (runtime-masked) just for the driver unload and come back without the
# graphics card; at VM stop they are restarted to see it again.
GPU_MONITOR_UNITS = {"plasma-ksystemstats.service"}
# Terminal emulators (comm): a program started in one that uses the graphics card is closed on its own.
TERMINALS = {"konsole", "yakuake", "kitty", "alacritty", "foot", "wezterm-gui", "ghostty", "xterm",
             "gnome-terminal-", "ptyxis", "tilix", "terminator", "urxvt", "st"}
COMPOSITORS = ("kwin_wayland", "kwin_x11", "Xwayland", "Xorg", "gnome-shell", "sway", "Hyprland", "plasmashell")
BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
USB_ID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")
EVDEV_TOGGLES = ("ctrl-ctrl", "alt-alt", "shift-shift", "meta-meta", "scrolllock", "ctrl-scrolllock")

DRY_RUN = False

DEFAULTS: dict = {
    "vm": {"name": "win11", "title": "Windows 11", "mode": "auto", "memory_mib": 8192,
           "hugepages": True, "hugepages_fallback": True, "uuid": "", "clock": "localtime", "rescue_vga": False,
           "virtio_iso": "/var/lib/libvirt/images/virtio-win.iso", "install_iso": "",
           "host_shutdown_wait": 300},
    "cpu": {"guest_cores": [1], "host_cores": [0], "emulator_cores": [],  # the installer sets them for the CPU
            "isolate_host": True},
    "tuning": {"irq_affinity": True, "block_deep_idle_us": 100, "pause_ananicy": True,
               "avic": False, "guest_idle_poll": False},
    "gpu": {"devices": [], "expect_ids": [], "host_modules": ["nvidia_drm", "nvidia_modeset", "nvidia_uvm", "nvidia"],
            "host_services": ["nvidia-persistenced.service", "nvidia-powerd.service"], "mmio64_mib": 0},
    "disk": {"nvme": "", "boot_disk": "", "expect_id": "", "expect_serial": "", "unmount": [], "extra": [],
             "refuse_hibernated": True,
             "remount_after_stop": True, "d3cold_off": False},
    "usb": {"controllers": [], "devices": []},
    "input": {"evdev": True, "keyboard": "", "mouse": "", "extra": [], "toggle": "ctrl-ctrl", "reattach": True},
    "network": {"mode": "macvtap", "host_nic": "", "queues": 4, "hostonly": True,
                "hostonly_network": "gamevm-host", "mac": "", "hostonly_mac": ""},
    "audio": {"backend": "pipewire", "user": "", "latency_us": 20000, "qemu_as_user": False},
    "looking_glass": {"enabled": True, "kvmfr": True, "size_mib": 64, "spice_port": 5905,
                      "build": "B7-829-cdba29f2", "fullscreen": False, "escape_key": "KEY_RIGHTCTRL",
                      "auto_mirror": True},
    "firmware": {"secure_boot": False, "tpm": True, "smbios_host": True},
    "desktop": {"relaunch_all": True, "relaunch_exclude": [], "relaunch_autostart": True, "relaunch": [],
                "quit_timeout": 5, "restore_windows": True, "return": "auto", "restart_timeout": 25},
    "display": {"switch_inputs": False, "monitor_model": "", "vm_input": "0x0f",
                "host_input": "0x11"},
    "single_gpu": {"display_manager": "display-manager.service", "teardown_timeout": 30,
                   "reboot_on_gpu_failure": True},
}


# ---------------------------------------------------------------------------------------------
# output / process helpers
# ---------------------------------------------------------------------------------------------

class GameVMError(Exception):
    pass


def stderr_is_journal() -> bool:
    """stderr is the journal (systemd sets JOURNAL_STREAM=<dev>:<inode> for it, e.g. in
    gamevm-kwin.service): the syslog copy then already is in the journal."""
    try:
        dev, ino = os.environ["JOURNAL_STREAM"].split(":")
        st = os.fstat(sys.stderr.fileno())
        return st.st_dev == int(dev) and st.st_ino == int(ino)
    except (KeyError, ValueError, OSError):
        return False


def log(msg: str, level: str = "info") -> None:
    """Log to stderr; as root also to /var/log/gamevm.log and the journal."""
    prefix = {"info": "", "warn": "WARNING: ", "error": "ERROR: "}[level]
    root = os.geteuid() == 0 and not DRY_RUN
    if not (root and stderr_is_journal()):
        print(f"gamevm: {prefix}{msg}", file=sys.stderr, flush=True)
    if root:
        try:
            with LOG_FILE.open("a") as f:
                f.write(f"{time.strftime('%F %T')} [{level}] {msg}\n")
        except OSError:
            pass
        prio = {"info": syslog.LOG_INFO, "warn": syslog.LOG_WARNING, "error": syslog.LOG_ERR}[level]
        syslog.syslog(prio, msg)


def run(cmd: list[str], *, check: bool = True, capture: bool = False, mutate: bool = True,
        timeout: float | None = 120, input: str | None = None) -> subprocess.CompletedProcess:
    """Run a command. Commands that change host state (mutate=True) are only printed in dry-run."""
    if DRY_RUN and mutate:
        print(f"[dry-run] {' '.join(cmd)}", file=sys.stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    try:
        res = subprocess.run(cmd, text=True, capture_output=capture, timeout=timeout, input=input)
    except FileNotFoundError:
        if check:
            raise GameVMError(f"{cmd[0]} is not installed") from None
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not installed")
    if check and res.returncode != 0:
        err = (res.stderr or "").strip() if capture else ""
        raise GameVMError(f"command failed ({res.returncode}): {' '.join(cmd)}" + (f": {err}" if err else ""))
    return res


def write_sysfs(path: Path | str, value: str) -> None:
    if DRY_RUN:
        print(f"[dry-run] echo {value!r} > {path}", file=sys.stderr)
        return
    with open(path, "w") as f:
        f.write(value)


def read(path: Path | str, default: str = "") -> str:
    try:
        return Path(path).read_bytes().decode("utf-8", "replace").strip()
    except OSError:
        return default


def virsh(*args: str, check: bool = True, capture: bool = True, mutate: bool = True,
          timeout: float | None = 120) -> subprocess.CompletedProcess:
    # C locale: state names and labels are parsed below (env exits 127 if virsh is missing).
    return run(["env", "LC_ALL=C", "virsh", "-q", "-c", LIBVIRT_URI, *args], check=check,
               capture=capture, mutate=mutate, timeout=timeout)


def require_root(what: str) -> None:
    if os.geteuid() != 0 and not DRY_RUN:
        raise GameVMError(f"{what} needs root (sudo gamevm ...)")


DECLINED = False  # the user answered a question with no: not an error (see --hold)


def confirm(question: str, assume_yes: bool) -> bool:
    global DECLINED
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    yes = input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    DECLINED = DECLINED or not yes
    return yes


def cpuset(cpus: list[int]) -> str:
    """[0,1,2,8,9] -> '0-2,8-9'."""
    cpus = sorted(set(cpus))
    out, start, prev = [], None, None
    for c in cpus + [None]:
        if start is None:
            start = prev = c
        elif c is not None and c == prev + 1:
            prev = c
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = c
    return ",".join(out)


def parse_cpuset(text: str) -> list[int]:
    cpus: list[int] = []
    for part in filter(None, text.strip().split(",")):
        if "-" in part:
            a, b = part.split("-")
            cpus.extend(range(int(a), int(b) + 1))
        else:
            cpus.append(int(part))
    return sorted(set(cpus))


def cpumask_hex(cpus: list[int]) -> str:
    mask = 0
    for c in cpus:
        mask |= 1 << c
    return format(mask, "x")


# ---------------------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------------------

def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None, *, trusted: bool = False) -> dict:
    """Load the config. trusted=True (hook/recover, running as root) only accepts a root-owned,
    non-writable-by-others file under /etc, so a user cannot make root run arbitrary actions."""
    if path is None:
        path = ETC_CONFIG if (trusted or ETC_CONFIG.exists()) else REPO_CONFIG
    path = Path(path)
    if trusted:
        for p in (path, path.parent):
            st = p.stat()
            if st.st_uid != 0 or st.st_mode & 0o022:
                raise GameVMError(f"{p} must be owned by root and not group/world writable")
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise GameVMError(f"config not found: {path} (run setup/install-host.sh)") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise GameVMError(f"{path}: {e}") from None
    cfg = deep_merge(DEFAULTS, data)
    cfg["_path"] = str(path)
    validate_config(cfg)
    return cfg


def type_errors(cfg: dict) -> list[str]:
    """Every known key must have the type of its default (lists: ints for *_cores, else strings),
    so the semantic checks below and the hook never trip over a TypeError."""
    errs = []
    for sec, keys in DEFAULTS.items():
        if not isinstance(cfg.get(sec), dict):
            errs.append(f"[{sec}] must be a table")
            continue
        for k, default in keys.items():
            v = cfg[sec].get(k)
            if isinstance(default, bool):
                ok, want = isinstance(v, bool), "true or false"
            elif isinstance(default, int):
                ok, want = isinstance(v, int) and not isinstance(v, bool), "an integer"
            elif isinstance(default, str):
                ok, want = isinstance(v, str), "a string"
            else:
                elem = int if k.endswith("_cores") else str
                ok = isinstance(v, list) and all(isinstance(x, elem) and not isinstance(x, bool) for x in v)
                want = f"a list of {'integers' if elem is int else 'strings'}"
            if not ok:
                errs.append(f"{sec}.{k} must be {want}")
    return errs


def validate_config(cfg: dict) -> None:
    errs = type_errors(cfg)
    if errs:
        raise GameVMError("invalid config " + cfg["_path"] + ":\n  - " + "\n  - ".join(errs))
    vm = cfg["vm"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", vm["name"]):
        errs.append("vm.name may only contain letters, digits, '_', '.', '-'")
    if vm["mode"] not in ("auto", "igpu", "single"):
        errs.append("vm.mode must be auto, igpu or single")
    if not isinstance(vm["memory_mib"], int) or vm["memory_mib"] < 4096 or vm["memory_mib"] % 2:
        errs.append("vm.memory_mib must be an even integer >= 4096")
    if vm["clock"] not in ("localtime", "utc"):
        errs.append("vm.clock must be localtime or utc")
    if vm["uuid"] and not re.fullmatch(r"[0-9a-fA-F-]{36}", vm["uuid"]):
        errs.append("vm.uuid must be a UUID")
    if not 30 <= vm["host_shutdown_wait"] <= 840:  # gamevm-shutdown.service allows 900 s in all
        errs.append("vm.host_shutdown_wait must be between 30 and 840 seconds")
    c = cfg["cpu"]
    for key in ("guest_cores", "host_cores", "emulator_cores"):
        if not all(isinstance(x, int) and x >= 0 for x in c[key]):
            errs.append(f"cpu.{key} must be a list of core IDs")
    if not c["guest_cores"] or not c["host_cores"]:
        errs.append("cpu.guest_cores and cpu.host_cores must not be empty")
    if set(c["guest_cores"]) & set(c["host_cores"]) and c["isolate_host"]:
        errs.append("cpu.guest_cores and cpu.host_cores overlap (only possible with isolate_host = false, "
                    "e.g. to try giving Windows every core)")
    for key in ("gpu.devices", "usb.controllers"):
        sec, k = key.split(".")
        for bdf in cfg[sec][k]:
            if not BDF_RE.match(bdf):
                errs.append(f"{key}: '{bdf}' is not a PCI address like 0000:01:00.0")
    if not cfg["gpu"]["devices"]:
        errs.append("gpu.devices is empty: this configuration was not filled in for this PC yet. The installer does "
                    "that (sudo ./setup/install-host.sh); `gamevm discover` shows what it finds")
    if cfg["disk"]["nvme"] and not BDF_RE.match(cfg["disk"]["nvme"]):
        errs.append("disk.nvme must be a PCI address like 0000:02:00.0")
    for path in cfg["disk"]["extra"]:
        if not path.startswith("/dev/disk/by-id/") or "-part" in os.path.basename(path):
            errs.append(f"disk.extra: {path} must be a whole disk under /dev/disk/by-id/ (stable across boots)")
    boot = cfg["disk"]["boot_disk"]
    if boot and (not boot.startswith("/dev/disk/by-id/") or "-part" in os.path.basename(boot)):
        errs.append(f"disk.boot_disk: {boot} must be a whole disk under /dev/disk/by-id/ (stable across boots)")
    if boot and cfg["disk"]["nvme"]:
        errs.append("Windows starts from one drive: set disk.nvme (an NVMe drive, handed over whole) or disk.boot_disk "
                    "(a SATA disk), not both")
    if boot and boot in cfg["disk"]["extra"]:
        errs.append("disk.boot_disk is also listed in disk.extra")
    for u in cfg["usb"]["devices"]:
        if not USB_ID_RE.match(u.lower()):
            errs.append(f"usb.devices: '{u}' is not VID:PID")
    if cfg["input"]["toggle"] not in EVDEV_TOGGLES:
        errs.append(f"input.toggle must be one of {', '.join(EVDEV_TOGGLES)}")
    if cfg["network"]["mode"] not in ("macvtap", "nat"):
        errs.append("network.mode must be macvtap or nat")
    if cfg["network"]["mode"] == "macvtap" and not cfg["network"]["host_nic"]:
        errs.append("network.host_nic is required for macvtap")
    if cfg["audio"]["backend"] not in ("pipewire", "none"):
        errs.append("audio.backend must be pipewire or none")
    if cfg["desktop"]["return"] not in ("auto", "ask"):
        errs.append("desktop.return must be auto or ask")
    if not 0 <= cfg["desktop"]["quit_timeout"] <= 30:
        errs.append("desktop.quit_timeout must be between 0 and 30 seconds")
    lg = cfg["looking_glass"]
    if not re.fullmatch(r"KEY_[A-Z0-9_]+", lg["escape_key"]):
        errs.append("looking_glass.escape_key must be a key name such as KEY_RIGHTCTRL "
                    "(looking-glass-client input:escapeKey=help lists them)")
    if lg["size_mib"] not in (16, 32, 64, 128, 256, 512):
        errs.append("looking_glass.size_mib must be a power of two between 16 and 512")
    if errs:
        raise GameVMError("invalid config " + cfg["_path"] + ":\n  - " + "\n  - ".join(errs))


def machine_id() -> str:
    return read("/etc/machine-id") or "gamevm"


def derived_mac(cfg: dict, idx: int) -> str:
    h = hashlib.sha256(f"{machine_id()}:{cfg['vm']['name']}:{idx}".encode()).digest()
    return "52:54:00:%02x:%02x:%02x" % (h[0], h[1], h[2])


def derived_uuid(cfg: dict) -> str:
    h = hashlib.sha256(f"{machine_id()}:{cfg['vm']['name']}:uuid".encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def audio_user(cfg: dict) -> pwd.struct_passwd | None:
    name = cfg["audio"]["user"] or os.environ.get("SUDO_USER") or ""
    if not name and os.geteuid() != 0:
        name = pwd.getpwuid(os.getuid()).pw_name
    try:
        return pwd.getpwnam(name) if name else None
    except KeyError:
        return None


# ---------------------------------------------------------------------------------------------
# hardware discovery (sysfs)
# ---------------------------------------------------------------------------------------------

def pci_exists(bdf: str) -> bool:
    return (SYS_PCI / bdf).exists()


def pci_attr(bdf: str, name: str) -> str:
    return read(SYS_PCI / bdf / name)


def pci_driver(bdf: str) -> str | None:
    link = SYS_PCI / bdf / "driver"
    return os.path.basename(os.readlink(link)) if link.is_symlink() else None


def pci_desc(bdf: str) -> str:
    try:
        out = subprocess.run(["lspci", "-nn", "-s", bdf], text=True, capture_output=True).stdout.strip()
        return out.split(" ", 1)[1] if " " in out else bdf
    except OSError:
        return bdf


def iommu_group(bdf: str) -> int | None:
    link = SYS_PCI / bdf / "iommu_group"
    return int(os.path.basename(os.readlink(link))) if link.is_symlink() else None


def iommu_group_devices(group: int) -> list[str]:
    return sorted(os.listdir(f"/sys/kernel/iommu_groups/{group}/devices"))


def is_pci_bridge(bdf: str) -> bool:
    return pci_attr(bdf, "class").startswith("0x0604")


def check_iommu_group(bdf: str, allowed: list[str]) -> list[str]:
    """Return the devices sharing bdf's IOMMU group that are neither passed through nor bridges."""
    group = iommu_group(bdf)
    if group is None:
        return ["<no IOMMU group: IOMMU disabled?>"]
    return [d for d in iommu_group_devices(group) if d not in allowed and not is_pci_bridge(d)]


def host_display_gpus(cfg: dict) -> list[str]:
    """Display controllers other than the passed-through GPU (e.g. the CPU's iGPU)."""
    out = []
    for dev in sorted(SYS_PCI.iterdir()):
        if dev.name in cfg["gpu"]["devices"]:
            continue
        if read(dev / "class").startswith("0x03"):
            out.append(dev.name)
    return out


def resolve_mode(cfg: dict) -> str:
    mode = cfg["vm"]["mode"]
    others = host_display_gpus(cfg)
    if mode == "auto":
        return "igpu" if others else "single"
    if mode == "igpu" and not others:
        raise GameVMError("vm.mode = igpu but no second display GPU is on the PCI bus "
                          "(enable the iGPU in the BIOS, see docs/01-requirements-and-cabling.md)")
    return mode


def cpu_cores() -> dict[int, list[int]]:
    """{core_id: [logical cpus]} for package 0 (online CPUs only)."""
    cores: dict[int, list[int]] = {}
    pkgs = set()
    for d in glob.glob("/sys/devices/system/cpu/cpu[0-9]*"):
        topo = Path(d) / "topology"
        if not (topo / "core_id").exists():
            continue
        cpu = int(os.path.basename(d)[3:])
        pkgs.add(read(topo / "physical_package_id"))
        cores.setdefault(int(read(topo / "core_id")), []).append(cpu)
    if len(pkgs) > 1:
        raise GameVMError("multi-socket hosts are not supported by gamevm's CPU planner")
    return {k: sorted(v) for k, v in cores.items()}


def cpu_plan(cfg: dict) -> dict:
    cores = cpu_cores()
    c = cfg["cpu"]
    missing = [x for x in c["guest_cores"] + c["host_cores"] + c["emulator_cores"] if x not in cores]
    if missing:
        raise GameVMError(f"core IDs {missing} do not exist (available: {sorted(cores)})")
    threads = {len(cores[x]) for x in c["guest_cores"]}
    if len(threads) != 1:
        raise GameVMError("guest cores have different SMT thread counts")
    guest = [cpu for core in c["guest_cores"] for cpu in cores[core]]  # vCPU order: core-major
    host = sorted(cpu for core in c["host_cores"] for cpu in cores[core])
    emu = sorted(cpu for core in (c["emulator_cores"] or c["host_cores"]) for cpu in cores[core])
    return {"guest": guest, "host": host, "emulator": emu, "cores": len(c["guest_cores"]),
            "threads": threads.pop(), "all": sorted(cpu for v in cores.values() for cpu in v)}


def block_devices_on(bdf: str) -> list[str]:
    """Block devices (disks and partitions) that live under PCI device bdf."""
    out = []
    for b in Path("/sys/class/block").iterdir():
        if f"/{bdf}/" in os.path.realpath(b):
            out.append(b.name)
    return sorted(out)


def mounts_of(devnames: list[str]) -> list[dict]:
    """Mounts whose source is one of devnames (matched by major:minor or by source path,
    which also catches FUSE mounts such as ntfs-3g that report an anonymous major:minor)."""
    majmin = {read(f"/sys/class/block/{d}/dev"): d for d in devnames}
    paths = {f"/dev/{d}": d for d in devnames}
    found = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, _, right = line.partition(" - ")
            lf, rf = left.split(), right.split()
            src = rf[1] if len(rf) > 1 else ""
            real_src = os.path.realpath(src) if src.startswith("/dev/") else src
            dev = majmin.get(lf[2]) or paths.get(real_src)
            if dev:
                found.append({"dev": dev, "mountpoint": lf[4].replace("\\040", " "), "fstype": rf[0]})
    with open("/proc/swaps") as f:
        for line in list(f)[1:]:
            src = os.path.realpath(line.split()[0])
            if src in paths:
                found.append({"dev": paths[src], "mountpoint": "[swap]", "fstype": "swap"})
    return found


def holders_of_block(devnames: list[str]) -> list[str]:
    """device-mapper / md / bcache users of the given block devices."""
    out = []
    for d in devnames:
        h = Path(f"/sys/class/block/{d}/holders")
        if h.is_dir():
            out += [f"{d} -> {x}" for x in os.listdir(h)]
    return out


def gpu_device_nodes(cfg: dict) -> set[str]:
    nodes = set()
    vga = cfg["gpu"]["devices"][0]
    if pci_attr(vga, "vendor") == "0x10de":
        nodes.update(glob.glob("/dev/nvidia*"))
        nodes.update(glob.glob("/dev/nvidia-caps/*"))
    for bdf in cfg["gpu"]["devices"]:
        for suffix in ("card", "render"):
            p = Path(f"/dev/dri/by-path/pci-{bdf}-{suffix}")
            if p.exists():
                nodes.add(os.path.realpath(p))
        # The GPU's DDC/I2C buses (ddcutil, OpenRGB, sensors) also pin the driver.
        for adapter in glob.glob(f"/sys/bus/pci/devices/{bdf}/i2c-*"):
            nodes.add(f"/dev/{os.path.basename(adapter)}")
    return {n for n in nodes if os.path.exists(n) and not os.path.isdir(n)}


def module_refcount(name: str) -> int:
    try:
        return int(read(f"/sys/module/{name}/refcnt", "0"))
    except ValueError:
        return 0


def fb_on_devices(bdfs: list[str]) -> bool:
    """Is a kernel framebuffer (fbdev, used by the text console) registered by one of bdfs?"""
    targets = {os.path.realpath(SYS_PCI / b) for b in bdfs}
    for fb in glob.glob("/sys/class/graphics/fb[0-9]*"):
        dev = os.path.realpath(f"{fb}/device")
        if dev in targets:
            return True
    return False


def find_holders(nodes: set[str]) -> list[dict]:
    """Processes with an open fd or mmap on one of nodes. Unprivileged callers only see their own."""
    if not nodes:
        return []
    me = os.getpid()
    out = []
    for pdir in Path("/proc").iterdir():
        if not pdir.name.isdigit() or int(pdir.name) == me:
            continue
        hits = set()
        try:
            for fd in os.listdir(pdir / "fd"):
                try:
                    t = os.readlink(pdir / "fd" / fd)
                except OSError:
                    continue
                if t in nodes:
                    hits.add(t)
            for line in (pdir / "maps").read_text().splitlines():
                parts = line.split(maxsplit=5)
                if len(parts) == 6 and parts[5] in nodes:
                    hits.add(parts[5])
        except (OSError, ValueError):
            continue
        if hits:
            try:
                uid = (pdir / "status").stat().st_uid
                user = pwd.getpwuid(uid).pw_name
            except (OSError, KeyError):
                user = "?"
            out.append({"pid": int(pdir.name), "comm": read(pdir / "comm"), "user": user,
                        "cmdline": read(pdir / "cmdline").replace("\0", " ")[:120],
                        "nodes": sorted(hits)})
    return sorted(out, key=lambda h: h["pid"])


# GPU fences in sync_file fds survive the process that made them (e.g. inherited by a child) and do
# not pin the driver module: if the NVIDIA driver is unloaded while one exists, releasing it later
# jumps into freed module code and crashes the kernel (seen 2026-10-10 with an Electron app's
# helper processes). A completed fence no longer names its driver ("detached-driver" since Linux 6.17), so
# a process from before the desktop left the graphics card counts with any fence it holds; a later one (it
# never had NVIDIA's EGL) only with a fence that names NVIDIA. Fences of these drivers are harmless.
SAFE_FENCE_DRIVERS = {"amdgpu", "i915", "xe", "drm_sched", "stub"}
SYNC_IOC_FILE_INFO = 0xC0383E04  # _IOWR('>', 4, struct sync_file_info)
SYS_PIDFD_GETFD = 438


class _SyncFenceInfo(ctypes.Structure):
    _fields_ = [("obj_name", ctypes.c_char * 32), ("driver_name", ctypes.c_char * 32),
                ("status", ctypes.c_int32), ("flags", ctypes.c_uint32), ("timestamp_ns", ctypes.c_uint64)]


class _SyncFileInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * 32), ("status", ctypes.c_int32), ("flags", ctypes.c_uint32),
                ("num_fences", ctypes.c_uint32), ("pad", ctypes.c_uint32), ("sync_fence_info", ctypes.c_uint64)]


_LIBC = None


def proc_started(pid: int) -> float:
    """Start time of a process, in seconds since boot (CLOCK_BOOTTIME)."""
    try:
        stat = read(f"/proc/{pid}/stat")
        return int(stat.rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")
    except (IndexError, ValueError, OSError):
        return 0.0


def sync_file_drivers(pid: int, fd: int) -> set[str] | None:
    """Drivers of the fences in another process's sync_file fd: a copy of the fd (pidfd_getfd,
    needs root) asked with SYNC_IOC_FILE_INFO, then closed. None if it cannot be inspected."""
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(None, use_errno=True)
    try:
        pidfd = os.pidfd_open(pid)
    except OSError:
        return None
    try:
        local = _LIBC.syscall(ctypes.c_long(SYS_PIDFD_GETFD), ctypes.c_long(pidfd), ctypes.c_long(fd), ctypes.c_long(0))
        if local < 0:
            return None
        try:
            info = _SyncFileInfo()
            if _LIBC.ioctl(local, ctypes.c_ulong(SYNC_IOC_FILE_INFO), ctypes.byref(info)) != 0:
                return None
            n = info.num_fences
            if not n:
                return set()
            fences = (_SyncFenceInfo * n)()
            info = _SyncFileInfo(num_fences=n, sync_fence_info=ctypes.addressof(fences))
            if _LIBC.ioctl(local, ctypes.c_ulong(SYNC_IOC_FILE_INFO), ctypes.byref(info)) != 0:
                return None
            return {f.driver_name.decode(errors="replace") for f in fences}
        finally:
            os.close(local)
    finally:
        os.close(pidfd)


def igpu_since() -> float | None:
    """When the desktop last moved to the iGPU (the layout file is written as the graphics card leaves
    KWin's GPU list), in seconds since boot; None while the desktop is on the graphics card."""
    if kwin_layout() != "igpu":
        return None
    try:
        age = time.time() - (KWIN_DIR / "layout").stat().st_mtime
    except OSError:
        return None
    return time.clock_gettime(time.CLOCK_BOOTTIME) - age


def unsafe_fences(drivers: set[str] | None, old: bool) -> list[str]:
    """Why one sync_file may hold an NVIDIA fence (empty: harmless). drivers None: unreadable."""
    if drivers is None:
        return ["sync_file (unreadable)"] if old else []
    if old:
        return [f"sync_file ({'completed fence' if d == 'detached-driver' else d})"
                for d in sorted(drivers - SAFE_FENCE_DRIVERS)]
    return [f"sync_file ({d})" for d in sorted(drivers) if "nvidia" in d.lower()]


def fence_holders() -> list[dict]:
    """Processes that may hold GPU fences of the NVIDIA driver (see SAFE_FENCE_DRIVERS): sync_file
    fds as judged by unsafe_fences(), and DRM syncobj fds (their fence cannot be read here) of
    processes from before the switch to the iGPU. Only as root, and only while the NVIDIA DRM
    driver is loaded: never inspect after the unload (dropping the copy could release a fence)."""
    if os.geteuid() != 0 or not module_loaded("nvidia_drm"):
        return []
    since = igpu_since()
    out = []
    me = os.getpid()
    for pdir in Path("/proc").iterdir():
        if not pdir.name.isdigit() or int(pdir.name) == me:
            continue
        pid, found = int(pdir.name), set()
        old = since is None or proc_started(pid) < since
        try:
            fds = os.listdir(pdir / "fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(pdir / "fd" / fd)
            except OSError:
                continue
            if target == "anon_inode:sync_file":
                drivers = sync_file_drivers(pid, int(fd))
                if drivers is not None or Path(f"/proc/{pid}").exists():
                    found.update(unsafe_fences(drivers, old))
            elif target == "anon_inode:syncobj" and old:
                found.add("syncobj")  # its fence cannot be read here
        if found:
            try:
                user = pwd.getpwuid((pdir / "status").stat().st_uid).pw_name
            except (OSError, KeyError):
                user = "?"
            out.append({"pid": pid, "comm": read(pdir / "comm"), "user": user,
                        "cmdline": read(pdir / "cmdline").replace("\0", " ")[:120], "nodes": sorted(found)})
    return out


def gpu_users(cfg: dict) -> list[dict]:
    """Everything that ties a process to the NVIDIA driver: open nodes of the graphics card (not its HDMI
    audio) and, as root, the driver's GPU fences."""
    users = {h["pid"]: h for h in find_holders(gpu_device_nodes(cfg))}
    for h in fence_holders():
        if h["pid"] in users:
            users[h["pid"]]["nodes"] = sorted(set(users[h["pid"]]["nodes"]) | set(h["nodes"]))
        else:
            users[h["pid"]] = h
    return sorted(users.values(), key=lambda h: h["pid"])


def procs_named(names: tuple[str, ...]) -> list[int]:
    out = []
    for pdir in Path("/proc").iterdir():
        if pdir.name.isdigit() and read(pdir / "comm") in names:
            out.append(int(pdir.name))
    return out


def module_loaded(name: str) -> bool:
    return Path(f"/sys/module/{name.replace('-', '_')}").exists()


def unit_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0


def gpu_dedicated() -> bool:
    """install-host.sh --dedicated-gpu: vfio-pci owns the GPU from boot; never give it to nvidia."""
    return DEDICATED_CONF.exists()


def domain_state(name: str) -> str:
    res = virsh("domstate", name, check=False, mutate=False)
    if res.returncode == 127:
        return "unknown (libvirt not installed)"
    return res.stdout.strip() if res.returncode == 0 else "undefined"


def domain_off(state: str) -> bool:
    """A domain_state() that owns nothing on the host. Everything else (running, paused, in
    shutdown, crashed, ...) still holds the GPU and the Windows disk."""
    return state in ("shut off", "undefined") or state.startswith("unknown")


def host_going_down() -> bool:
    """The host itself is shutting down or rebooting (systemd has queued shutdown.target)."""
    try:
        res = run(["systemctl", "is-system-running"], check=False, capture=True, mutate=False, timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return (res.stdout or "").strip() == "stopping"


def notify_user(cfg: dict, summary: str, body: str, critical: bool = False) -> None:
    """Desktop notification for the desktop user, so a refused start or a GPU that did not come
    back is seen without a terminal (Windows shut down from inside, a launcher whose window is
    gone, a terminal the desktop restart closed). Best effort. Detached with its own stdio:
    libvirt waits until the hook's output pipes are closed. While plasmashell reconnects after a
    KWin restart the call simply waits for it (busctl --timeout)."""
    u = audio_user(cfg)
    if DRY_RUN:
        print(f"[dry-run] notify {u.pw_name if u else '(no desktop user)'}: {summary}: {body}", file=sys.stderr)
        return
    if not u or not os.path.exists(f"/run/user/{u.pw_uid}/bus"):
        return
    call = ["busctl", "--user", "--timeout=20", "call", "org.freedesktop.Notifications",
            "/org/freedesktop/Notifications", "org.freedesktop.Notifications", "Notify", "susssasa{sv}i",
            "gamevm", "0", "computer", summary, escape(" ".join(body.split())), "0",
            "1", "urgency", "y", "2" if critical else "1",   # hints: critical ones stay until dismissed
            "0" if critical else "20000"]
    try:
        subprocess.Popen(user_bus_cmd(u, *call) if os.geteuid() == 0 else call, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# host state journal (what prepare changed, so release can undo exactly that)
# ---------------------------------------------------------------------------------------------

class HostState:
    def __init__(self, data: dict | None = None):
        self.data = data or {"version": 1, "steps": []}

    @classmethod
    def load(cls) -> "HostState | None":
        try:
            return cls(json.loads(STATE_FILE.read_text()))
        except (OSError, ValueError):
            return None

    def save(self) -> None:
        if DRY_RUN:
            return
        STATE_DIR.mkdir(mode=0o755, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.chmod(tmp, 0o644)
        tmp.replace(STATE_FILE)

    def push(self, kind: str, **info) -> None:
        self.data["steps"].append({"kind": kind, **info})
        self.save()

    def clear(self) -> None:
        if not DRY_RUN:
            STATE_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------------------------
# prepare steps (run as root from the libvirt hook)
# ---------------------------------------------------------------------------------------------

def pci_id(bdf: str) -> str:
    return f"{pci_attr(bdf, 'vendor')[2:]}:{pci_attr(bdf, 'device')[2:]}"


def nvme_serial(bdf: str) -> str:
    for ctrl in glob.glob(f"/sys/bus/pci/devices/{bdf}/nvme/nvme*"):
        return read(f"{ctrl}/serial").strip()
    return ""


# Mount points that make a disk the host's own. A disk carrying one of these never goes to Windows,
# whatever disk.unmount says.
HOST_MOUNTS = ("/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[swap]")


def disk_serial(path: str) -> str:
    """The serial number udev reports for a whole disk (ID_SERIAL_SHORT), '' if it has none."""
    res = run(["udevadm", "info", "--query=property", f"--name={path}"], check=False, capture=True, mutate=False)
    m = re.search(r"^ID_SERIAL_SHORT=(.+)$", res.stdout or "", re.M)
    return m.group(1).strip() if m else ""


def whole_disk_facts(path: str) -> dict | None:
    """What the kernel says about the disk behind a /dev/disk/by-id/ path; None if it is not there."""
    if not os.path.exists(path):
        return None
    name = os.path.basename(os.path.realpath(path))
    q = Path(f"/sys/block/{name}/queue")
    return {"name": name, "whole": os.path.isdir(f"/sys/block/{name}"), "serial": disk_serial(path),
            "sector": read(q / "logical_block_size").strip() or "512",
            "physical": read(q / "physical_block_size").strip() or "512",
            "rotating": read(q / "rotational").strip() == "1",
            "mounts": mounts_of(disk_devices(path))}


def boot_disk_problem(cfg: dict) -> str:
    """Why disk.boot_disk cannot be the drive Windows starts from; '' if it can. (A SATA disk is
    not a PCI device of its own like an NVMe drive, so it is not handed over as hardware: QEMU
    opens the whole disk and shows it to Windows on an emulated SATA controller.)"""
    d = cfg["disk"]
    boot = d["boot_disk"]
    if not boot:
        return ""
    f = whole_disk_facts(boot)
    if f is None:
        return (f"disk.boot_disk {boot} is not there: the disk Windows starts from is missing "
                "(unplugged, or its name changed: ls -l /dev/disk/by-id/)")
    if not f["whole"]:
        return f"disk.boot_disk {boot} is not a whole disk (it resolves to {f['name']})"
    if d["expect_serial"] and f["serial"] != d["expect_serial"]:
        return (f"{boot} has serial {f['serial'] or '(none)'}, expected {d['expect_serial']}: "
                "this is not the Windows disk")
    # QEMU writes to the whole disk. Linux' own disk must never be it, also not when its mount
    # points were put into disk.unmount by mistake.
    own = [m for m in f["mounts"] if m["mountpoint"] in HOST_MOUNTS]
    if own:
        return (f"disk.boot_disk {boot} is the disk Linux runs from ("
                + ", ".join(f"{m['dev']} on {m['mountpoint']}" for m in own)
                + "): Windows needs a disk of its own. Set disk.boot_disk to the Windows disk")
    # QEMU's emulated SATA disk always has 512-byte sectors. A disk that really has 4096-byte
    # sectors ("4Kn") would show Windows a partition table it cannot read.
    if f["sector"] != "512":
        return (f"{boot} has {f['sector']}-byte sectors: Windows cannot start from it in the VM (a SATA disk there "
                "always has 512-byte sectors)")
    return ""


def verify_identity(cfg: dict) -> None:
    """PCI addresses can shift when BIOS settings or hardware change (e.g. enabling the iGPU or
    adding an M.2 drive). Refuse to pass anything through unless it is the device we expect."""
    gpu = cfg["gpu"]
    for bdf, want in zip(gpu["devices"], gpu["expect_ids"]):
        if not pci_exists(bdf):
            raise GameVMError(f"GPU function {bdf} does not exist (PCI numbering changed?)")
        if pci_id(bdf) != want.lower():
            raise GameVMError(f"{bdf} is {pci_id(bdf)}, expected {want}: PCI numbering changed, fix gpu.devices")
    d = cfg["disk"]
    if d["nvme"] and pci_exists(d["nvme"]):
        if d["expect_id"] and pci_id(d["nvme"]) != d["expect_id"].lower():
            raise GameVMError(f"{d['nvme']} is {pci_id(d['nvme'])}, expected {d['expect_id']}: "
                              "PCI numbering changed, fix disk.nvme")
        serial = nvme_serial(d["nvme"])
        if d["expect_serial"] and serial and serial != d["expect_serial"]:
            raise GameVMError(f"{d['nvme']} has serial {serial}, expected {d['expect_serial']}: "
                              "this is not the Windows disk")
    problem = boot_disk_problem(cfg)
    if problem:
        raise GameVMError(problem)


def mount_units(path: str) -> tuple[str, str]:
    esc = subprocess.run(["systemd-escape", "-p", path], text=True, capture_output=True).stdout.strip()
    return f"{esc}.automount", f"{esc}.mount"


def hiberfile_signature(dev: str) -> bytes | None:
    """First bytes of hiberfil.sys (b'hibr' = Windows is really hibernated / Fast Startup), b'' if
    there is none, None if it could not be checked. Read-only ntfs-3g mounts skip the hibernation
    and journal checks, so this works on exactly the volumes ntfs-3g.probe rejects."""
    mnt = STATE_DIR / f"probe-{os.path.basename(dev)}"
    try:
        mnt.mkdir(parents=True, exist_ok=True)
        if run(["mount", "-t", "ntfs-3g", "-o", "ro,noatime", dev, str(mnt)], check=False,
               capture=True, mutate=False).returncode != 0:
            return None
        try:
            for name in ("hiberfil.sys", "Hiberfil.sys", "HIBERFIL.SYS"):
                f = mnt / name
                if f.exists():
                    with f.open("rb") as fh:
                        return fh.read(4)
            return b""
        finally:
            run(["umount", str(mnt)], check=False, mutate=False)
    except OSError:
        return None
    finally:
        try:
            mnt.rmdir()
        except OSError:
            pass


# File indexers (comm): they crawl a mounted Windows partition and keep it busy, but are made to be
# stopped and started again. Anything else using the partition makes the start refuse.
FILE_INDEXERS = {"baloo_file", "baloo_file_extr", "localsearch-3", "tracker-miner-f", "tracker-extract"}


def path_users(path: str) -> list[dict]:
    """Processes with their working directory, an open file or a mapping below path."""
    base = os.path.normpath(path)

    def under(p: str) -> bool:
        return p == base or p.startswith(base + "/")

    out = []
    for pdir in Path("/proc").iterdir():
        if not pdir.name.isdigit() or int(pdir.name) == os.getpid():
            continue
        try:
            links = [os.readlink(pdir / "cwd")]
        except OSError:
            links = []
        try:
            for fd in os.listdir(pdir / "fd"):
                try:
                    links.append(os.readlink(pdir / "fd" / fd))
                except OSError:
                    continue
            links += [l.split(maxsplit=5)[5] for l in (pdir / "maps").read_text().splitlines()
                      if len(l.split(maxsplit=5)) == 6]
        except (OSError, ValueError):
            pass
        if any(under(l) for l in links):
            pid = int(pdir.name)
            out.append({"pid": pid, "comm": read(pdir / "comm"), "uid": proc_uid(pid), "unit": proc_unit(pid)})
    return out


def busy_error(path: str) -> GameVMError:
    users = path_users(path)
    names = ", ".join(sorted({f"{p['comm']} ({uid_name(p['uid'])})" for p in users})) or "an unknown program"
    return GameVMError(f"{path} is busy: {names} uses it. Close it and start again (sudo fuser -vm {path})")


def real_mount_at(path: str) -> bool:
    """A real (not autofs) filesystem is mounted at path. Read from mountinfo: looking below path
    (os.path.ismount checks path/..) would itself trigger the automount."""
    target = os.path.normpath(path)
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, _, right = line.partition(" - ")
            lf, rf = left.split(), right.split()
            mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), lf[4]) if len(lf) > 4 else ""
            if mp == target and rf and rf[0] != "autofs":
                return True
    return False


def fuse_server_for(path: str) -> int | None:
    """PID of a FUSE filesystem process (mount.ntfs-3g <device> <path> ...) that serves mount point
    path. If nothing is mounted there any more, it serves a detached copy that some program still
    has files open on: mounting the partition a second time then would put two NTFS drivers on it."""
    for pdir in Path("/proc").iterdir():
        if not pdir.name.isdigit():
            continue
        argv = read(pdir / "cmdline").split("\0")
        if os.path.normpath(path) in argv[1:] and re.match(r"(mount\.)?(low)?ntfs|mount\.(fuse|exfat)",
                                                            os.path.basename(argv[0])):
            return int(pdir.name)
    return None


def baloo_indexes(u: pwd.struct_passwd, path: str) -> bool:
    """KDE File Search of user u is set to index path (System Settings > Search > File Search)."""
    home = u.pw_dir
    text = read(Path(home) / ".config/baloofilerc")

    def folders(key: str) -> list[str]:
        m = re.search(rf"^{key}(?:\[\$e\])?=(.*)$", text, re.M)
        return [os.path.normpath(f.replace("$HOME", home)) for f in m.group(1).split(",") if f] if m else []

    def inside(dirs: list[str]) -> bool:
        p = os.path.normpath(path)
        return any(p == d or p.startswith(d.rstrip("/") + "/") for d in dirs)

    return inside(folders("folders")) and not inside(folders("exclude folders"))


def pause_indexers(cfg: dict, st: HostState, path: str) -> None:
    """File indexers (KDE's Baloo, GNOME's LocalSearch) crawling the Windows partition keep it busy
    now and then and mount it again through the automount: stop them while Windows runs (undo
    starts them again once the partition is back). Baloo is stopped whenever its settings include
    the partition, not only when caught with a file open. If anything else uses the partition,
    nothing is stopped: the unmount then refuses and names it."""
    users = path_users(path)
    if any(p["comm"] not in FILE_INDEXERS or not p["unit"].endswith(".service") or p["uid"] < 0 for p in users):
        return
    units = {(p["unit"], p["uid"]) for p in users}
    u = audio_user(cfg)
    if u and baloo_indexes(u, path) and user_unit_active(u.pw_name, "kde-baloo.service"):
        units.add(("kde-baloo.service", u.pw_uid))
    paused = {(x["unit"], x["user"]) for x in st.data["steps"] if x["kind"] == "indexer_stopped"}
    for unit, uid in sorted(units):
        if (unit, uid_name(uid)) in paused:
            continue
        user = uid_name(uid)
        log(f"pausing file indexing ({unit} of {user}) while Windows runs: it keeps {path} busy")
        if uid == 0:
            run(["systemctl", "stop", unit])
        else:
            user_systemctl(user, "stop", unit, check=True)
        st.push("indexer_stopped", user=user, unit=unit)
    deadline = time.monotonic() + 10
    while not DRY_RUN and time.monotonic() < deadline and path_users(path):
        time.sleep(0.25)


def wait_unit_settled(unit: str, seconds: float) -> str:
    """ActiveState of a system unit once no job runs on it any more (or after `seconds`)."""
    deadline = time.monotonic() + seconds
    while True:
        state = run(["systemctl", "show", "-p", "ActiveState", "--value", unit], check=False, capture=True,
                    mutate=False).stdout.strip()
        if state in ("active", "inactive", "failed") or time.monotonic() > deadline:
            return state
        time.sleep(0.2)


# ntfs-3g.probe exit codes (ntfs_volume_status) that refuse the hand-over.
NTFS_PROBE_REASONS = {12: "not NTFS", 13: "corrupt", 16: "in use", 17: "part of a RAID/LDM set",
                      18: "unknown reason", 19: "no privilege", 20: "out of memory", 21: "FUSE error"}


def extra_disks(cfg: dict) -> list[str]:
    """disk.extra entries that are present (a missing disk is skipped with a warning: Windows
    then starts without it)."""
    out = []
    for path in cfg["disk"]["extra"]:
        if os.path.exists(path):
            out.append(path)
        else:
            log(f"disk.extra {path} not found: Windows starts without it", "warn")
    return out


def disk_devices(path: str) -> list[str]:
    """Kernel names of a whole disk and its partitions, e.g. [sda, sda1, sda2]."""
    name = os.path.basename(os.path.realpath(path))
    return [name] + sorted(os.path.basename(p) for p in glob.glob(f"/sys/class/block/{name}/{name}*"))


def release_block_devices(cfg: dict, st: HostState, what: str, devs: list[str]) -> None:
    """Free a disk (devs: the disk and its partitions) for the VM: refuse if the host uses it other
    than through disk.unmount, unmount those (never lazily), make sure nothing holds it any more,
    and refuse a hibernated NTFS volume."""
    if holders_of_block(devs):
        raise GameVMError(f"{what} is used by device-mapper/md: {holders_of_block(devs)}")
    mounts = mounts_of(devs)
    allowed = {os.path.normpath(m) for m in cfg["disk"]["unmount"]}
    foreign = [m for m in mounts if os.path.normpath(m["mountpoint"]) not in allowed]
    if foreign:
        lst = ", ".join(f"{m['dev']} on {m['mountpoint']}" for m in foreign)
        raise GameVMError(f"refusing to pass {what} through: the host is using it ({lst}). "
                          "If that is intended, add the mount point to disk.unmount.")
    # Unmount deepest mount points first. Never lazily: a busy mount must stop the start.
    for m in sorted(mounts, key=lambda m: -m["mountpoint"].count("/")):
        pause_indexers(cfg, st, m["mountpoint"])
        log(f"unmounting {m['mountpoint']} ({m['dev']})")
        res = run(["umount", m["mountpoint"]], check=False, capture=True)
        if res.returncode != 0:
            raise busy_error(m["mountpoint"])
        st.push("unmounted", mountpoint=m["mountpoint"], dev=m["dev"])
    if DRY_RUN:
        return
    left = mounts_of(devs)
    if left:
        raise GameVMError(f"still mounted after umount: {left}")
    # Exclusive open fails if anything still holds a partition: lazily detached mounts, mounts in
    # other namespaces, swap, device-mapper... (A filesystem that was just detached may need a
    # moment to let go, hence the short wait.)
    deadline = time.monotonic() + 5
    for d in devs:
        while True:
            try:
                os.close(os.open(f"/dev/{d}", os.O_RDONLY | os.O_EXCL))
                break
            except OSError as e:
                if time.monotonic() >= deadline:
                    raise GameVMError(f"/dev/{d} is still in use on the host ({e.strerror}); "
                                      "refusing to hand it over") from None
                time.sleep(0.25)
    if cfg["disk"]["refuse_hibernated"]:
        ntfs = [d for d in devs if run(["blkid", "-o", "value", "-s", "TYPE", f"/dev/{d}"], check=False,
                                       capture=True, mutate=False).stdout.strip() == "ntfs"]
        if ntfs and not shutil.which("ntfs-3g.probe"):
            raise GameVMError(f"cannot check whether Windows left {what} hibernated: ntfs-3g.probe is not installed "
                              "(pacman -S ntfs-3g). Refusing to hand the disk over. (disk.refuse_hibernated = false "
                              "skips the check, at your own risk.)")
        for d in ntfs:
            for attempt in range(3):  # 16/18 can be a transient open from a finishing mount helper
                res = run(["ntfs-3g.probe", "--readwrite", f"/dev/{d}"], check=False, capture=True,
                          mutate=False)
                rc = res.returncode
                if rc not in (16, 18):
                    break
                time.sleep(1)
            if rc == 14:
                # ntfs-3g reports both a hibernated Windows and an abruptly stopped one (forced
                # off, crash) as 'hibernated'; only the hiberfil.sys signature tells them apart.
                sig = hiberfile_signature(f"/dev/{d}")
                if sig is None or sig.lower() == b"hibr":
                    raise GameVMError(
                        f"/dev/{d} is hibernated: Windows was shut down with Fast Startup or hibernation. "
                        "Boot Windows natively, run windows/1-prepare-windows.ps1 (it turns Fast Startup "
                        "and hibernation off), then shut down normally.")
                log(f"/dev/{d} was stopped abruptly last time (forced off / crash), not hibernated; "
                    "Windows replays its journal on boot", "warn")
            elif rc == 15:
                log(f"/dev/{d} was not cleanly unmounted; Windows will check it on boot", "warn")
            elif rc != 0:
                said = " ".join((res.stderr or res.stdout or "").split())[:200]
                raise GameVMError(f"ntfs-3g.probe /dev/{d}: {NTFS_PROBE_REASONS.get(rc, 'error')} (code {rc})"
                                  + (f": {said}" if said else "") + "; refusing to hand the disk over")
        if mounts_of(devs):
            raise GameVMError(f"{what} is still mounted after the hibernation check")


def step_disk(cfg: dict, st: HostState) -> None:
    nvme = cfg["disk"]["nvme"]
    if nvme and not pci_exists(nvme):
        raise GameVMError(f"disk.nvme {nvme} does not exist")
    # fstab automounts: stop the real mount first (a normal umount, which fails if busy) and only
    # then the .automount unit - stopping the automount alone would detach a busy mount lazily.
    for path in cfg["disk"]["unmount"]:
        automount, mount = mount_units(path)
        if not unit_active(automount):
            continue
        # First stop whatever keeps accessing it (an indexer would mount it again at once).
        pause_indexers(cfg, st, path)
        # Journaled before anything can end the automount (starting an active one changes nothing).
        st.push("automount_stopped", unit=automount, path=path)
        # Stop the mount unconditionally: an access just before may have started mounting it (state
        # "activating", not yet in mountinfo), and a stop job cancels that too. A real unmount is
        # strict, so a busy mount stops the start here, with nothing changed.
        # The stop job is irreversible, because the desktop looks at every mount point the moment
        # the mount table changes (KDE's kded6 and its trash service do): that access asks the
        # automount for the partition while it is still being unmounted, 15 ms after the unmount
        # began when this was found. A normal stop job is replaced by that request: systemctl reports a failure
        # and the partition is mounted again right after. Against an irreversible job the request
        # fails instead, and the automount unit gives up (detaching only its own trigger).
        res = run(["systemctl", "stop", "--job-mode=replace-irreversibly", mount], check=False, capture=True)
        if res.returncode != 0:
            raise busy_error(path)
        # Now the automount itself, if no such request has ended it already. In one transaction
        # with the mount: a mount that slipped in after the unmount goes first, and systemd answers
        # no request while the automount's stop is pending.
        run(["systemctl", "stop", "--job-mode=replace-irreversibly", mount, automount], check=False, capture=True)
        run(["systemctl", "reset-failed", automount], check=False, capture=True)  # "failed" = gave up, as intended
        if not DRY_RUN:
            # What counts is that nothing is mounted, nothing can mount it and no filesystem
            # process is left. The mount unit itself may be "failed": when the unmount is quick,
            # the desktop's request arrives just after it, and the stop above then ends that mount
            # while it is starting ("Mount process exited, code=killed": seen 2026-11-18).
            state = wait_unit_settled(mount, 10)
            deadline = time.monotonic() + 5
            while fuse_server_for(path) and not real_mount_at(path) and time.monotonic() < deadline:
                time.sleep(0.2)  # the helper of such a mount, on its way out
            server = fuse_server_for(path)
            if state not in ("inactive", "failed") or real_mount_at(path) or unit_active(automount) or server:
                raise GameVMError(f"{path} could not be taken away from Linux cleanly (mount unit {state}"
                                  + (f", filesystem process {server} still running" if server else "")
                                  + f"; {busy_error(path)})")
            run(["systemctl", "reset-failed", mount], check=False, capture=True)
    if nvme and pci_driver(nvme) != "vfio-pci":  # vfio-pci: already detached, nothing mounted then
        devs = block_devices_on(nvme)
        if not devs:
            raise GameVMError(f"{nvme} has no block devices (is it really the Windows NVMe?)")
        release_block_devices(cfg, st, nvme, devs)
        if cfg["disk"]["d3cold_off"]:
            # Workaround for NVMe passthrough failing with 'Unknown PCI header type 127' (D3cold).
            write_sysfs(SYS_PCI / nvme / "d3cold_allowed", "0")
    boot = cfg["disk"]["boot_disk"]
    if boot:  # there and the expected disk: verify_identity ran first
        release_block_devices(cfg, st, boot, disk_devices(boot))
    for path in extra_disks(cfg):
        release_block_devices(cfg, st, path, disk_devices(path))


def step_gpu_services(cfg: dict, st: HostState) -> None:
    for unit in cfg["gpu"]["host_services"]:
        if unit_active(unit):
            log(f"stopping {unit}")
            run(["systemctl", "stop", unit])
            st.push("service_stopped", unit=unit)


# Never signal these, even if they hold the GPU (e.g. logind still owning a closing session's
# DRM fd): they are waited for instead, and the unload then fails safely if they keep it.
PROTECTED_COMMS = {"systemd", "systemd-logind", "systemd-udevd", "systemd-journal", "systemd-userdbd",
                   "dbus-daemon", "dbus-broker", "dbus-broker-lau", "virtqemud", "libvirtd", "virtlogd",
                   "virtlockd", "virtnodedevd", "polkitd", "sshd"}


def killable(h: dict) -> bool:
    return h["pid"] not in (1, os.getpid(), os.getppid()) and h["comm"] not in PROTECTED_COMMS


def kill_holders(cfg: dict, grace: float = 4.0) -> None:
    holders = [h for h in gpu_users(cfg) if killable(h)]
    if not holders:
        return
    for h in holders:
        log(f"terminating {h['comm']} (pid {h['pid']}, user {h['user']}) holding the GPU")
        if not DRY_RUN:
            try:
                os.kill(h["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and not DRY_RUN:
        if not gpu_users(cfg):
            return
        time.sleep(0.25)
    for h in [h for h in gpu_users(cfg) if killable(h)]:
        log(f"killing {h['comm']} (pid {h['pid']})", "warn")
        if not DRY_RUN:
            try:
                os.kill(h["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    time.sleep(0.5)


def user_systemctl(user: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return run(["systemctl", "--user", "-M", f"{user}@", *args], check=check, capture=True)


def user_unit_active(user: str, unit: str) -> bool:
    return subprocess.run(["systemctl", "--user", "-M", f"{user}@", "is-active", "--quiet", unit]).returncode == 0


def step_desktop_teardown(cfg: dict, st: HostState) -> None:
    """single-GPU mode: stop the display manager and the user's graphical units, then wait for
    the session to go away. With lingering enabled, apps live on in user@UID.service after the
    login session ends, so stopping the display manager alone does not free the GPU."""
    dm = cfg["single_gpu"]["display_manager"]
    if unit_active(dm):
        log(f"single-GPU mode: stopping {dm} (the Linux desktop closes now)")
        run(["systemctl", "stop", dm])  # Restart=always: always stop the unit, never kill it
        st.push("dm_stopped", unit=dm)
    u = audio_user(cfg)
    if u:
        user_systemctl(u.pw_name, "stop", "plasma-workspace.target", "graphical-session.target")
        # Socket-activated audio could reopen the GPU between the kill and the unload.
        audio = [x for x in PIPEWIRE_UNITS if user_unit_active(u.pw_name, x)]
        if audio:
            user_systemctl(u.pw_name, "stop", *audio)
            st.push("user_units_stopped", user=u.pw_name, units=audio)
    deadline = time.monotonic() + cfg["single_gpu"]["teardown_timeout"]
    while time.monotonic() < deadline and not DRY_RUN:
        if not procs_named(COMPOSITORS):
            break
        time.sleep(0.5)
    left = procs_named(COMPOSITORS)
    for pid in left:
        log(f"compositor pid {pid} still alive, killing it", "warn")
        if not DRY_RUN:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    for drv in ("efi-framebuffer", "simple-framebuffer", "vesa-framebuffer"):
        for dev in glob.glob(f"/sys/bus/platform/drivers/{drv}/{drv}.*"):
            name = os.path.basename(dev)
            write_sysfs(f"/sys/bus/platform/drivers/{drv}/unbind", name)
            st.push("fb_unbound", driver=drv, device=name)


def console_on_devices(bdfs: list[str]) -> bool:
    """Is any virtual console mapped to a framebuffer that belongs to one of bdfs? Uses the
    FBIOGET_CON2FBMAP ioctl; if that is not possible, assume yes when such a framebuffer exists."""
    import fcntl
    import struct
    targets = {os.path.realpath(SYS_PCI / b) for b in bdfs}
    gpu_fbs = set()
    for fb in glob.glob("/sys/class/graphics/fb[0-9]*"):
        if os.path.realpath(f"{fb}/device") in targets:
            gpu_fbs.add(int(os.path.basename(fb)[2:]))
    if not gpu_fbs:
        return False
    any_fb = sorted(glob.glob("/dev/fb[0-9]*"))
    if not any_fb:
        return True
    try:
        fd = os.open(any_fb[0], os.O_RDONLY)
    except OSError:
        return True
    try:
        for con in range(1, 64):
            buf = struct.pack("II", con, 0)
            try:
                _, fbidx = struct.unpack("II", fcntl.ioctl(fd, 0x460F, buf))  # FBIOGET_CON2FBMAP
            except OSError:
                continue
            if fbidx in gpu_fbs:
                return True
        return False
    finally:
        os.close(fd)


def step_console(cfg: dict, st: HostState, mode: str) -> None:
    """Release the framebuffer console if it sits on the GPU, otherwise the driver cannot unload.
    (In iGPU mode the console normally lives on the iGPU and nothing happens here.)"""
    if mode != "single" and not console_on_devices(cfg["gpu"]["devices"]):
        return
    unbound = []
    for vt in sorted(glob.glob("/sys/class/vtconsole/vtcon*")):
        if read(f"{vt}/bind") == "1" and "frame buffer" in read(f"{vt}/name"):
            write_sysfs(f"{vt}/bind", "0")
            unbound.append(vt)
    if unbound:
        log("released the framebuffer console from the GPU")
        st.push("vtcon_unbound", paths=unbound)


def pause_gpu_monitors(cfg: dict, st: HostState) -> list[tuple[str, str]]:
    """Hold off GPU_MONITOR_UNITS that use the graphics card for the driver unload: runtime-mask them, so
    D-Bus activation cannot start them again in between, and stop them."""
    paused = []
    for unit, uid in sorted({(proc_unit(h["pid"]), proc_uid(h["pid"])) for h in nvidia_holders(cfg)}):
        if unit in GPU_MONITOR_UNITS and uid > 0:
            user = uid_name(uid)
            log(f"holding off {unit} of {user} (GPU sensors, runs nvidia-smi) while the NVIDIA driver unloads")
            user_systemctl(user, "mask", "--runtime", unit, check=True)
            st.push("monitor_paused", user=user, unit=unit)
            user_systemctl(user, "stop", unit)
            paused.append((user, unit))
    if paused:
        wait_holders_settle(cfg, 5)
    return paused


def resume_gpu_monitors(paused: list[tuple[str, str]], restart: bool) -> None:
    for user, unit in paused:
        user_systemctl(user, "unmask", "--runtime", unit)
        user_systemctl(user, "try-restart" if restart else "start", unit)


def step_unload_driver(cfg: dict, st: HostState, mode: str) -> None:
    """Free the GPU by UNLOADING the NVIDIA modules. Never unbind nvidia through sysfs (or let
    libvirt do it): on nvidia-open 595-615 an unbind that races with a remaining user can deadlock
    until reboot, whereas `modprobe -r` refuses immediately while anything holds the driver.
    libvirt (managed='yes') then only binds an already driverless device to vfio-pci."""
    mods = [m for m in cfg["gpu"]["host_modules"] if module_loaded(m)]
    vga = cfg["gpu"]["devices"][0]
    if not mods:
        if pci_driver(vga) not in (None, "vfio-pci"):
            raise GameVMError(f"{vga} is bound to {pci_driver(vga)}, which is not in gpu.host_modules")
        return
    # From here on only vfio-pci may bind the GPU, even if something reloads nvidia.ko
    # (nvidia-modprobe is setuid) between our unload and libvirt's detach.
    for bdf in cfg["gpu"]["devices"][:1]:
        write_sysfs(SYS_PCI / bdf / "driver_override", "vfio-pci")
    st.push("driver_override", devices=cfg["gpu"]["devices"][:1])
    attempts = 3 if mode == "single" else 1
    paused = pause_gpu_monitors(cfg, st)
    for attempt in range(1, attempts + 1):
        # The last check before the driver goes: device nodes AND its fences (see fence_holders).
        holders = gpu_users(cfg)
        if holders and mode == "single":
            kill_holders(cfg)
            holders = gpu_users(cfg)
        if holders and DRY_RUN:
            log(f"[dry-run] would refuse: GPU held by {', '.join(h['comm'] for h in holders)}", "warn")
        elif holders:
            names = ", ".join(f"{h['comm']}({h['pid']}: {', '.join(h['nodes'])})" for h in holders)
            raise GameVMError(f"the GPU is still in use by: {names}. gamevm could not close these: "
                              "find them with `sudo gamevm holders`, stop them and start again.")
        log(f"unloading {' '.join(mods)}")
        res = run(["modprobe", "-r", *mods], check=False, capture=True)
        if res.returncode == 0:
            st.push("modules_unloaded", modules=mods)
            resume_gpu_monitors(paused, restart=False)  # back at once, now without the graphics card
            return
        log(f"modprobe -r failed (attempt {attempt}): {res.stderr.strip()}", "warn")
        time.sleep(1)
    # Modules that did unload must be reloaded on rollback.
    gone = [m for m in mods if not module_loaded(m)]
    if gone:
        st.push("modules_unloaded", modules=gone)
    raise GameVMError("could not unload the NVIDIA driver (something still uses it)")


def grow_hugepages(target: int, attempts: int) -> int:
    """Grow the 2 MiB hugepage pool towards target and return its size. Each page needs a
    physically contiguous 2 MiB block, so free RAM is not enough: between attempts the page cache
    is dropped and memory compacted (the kernel keeps what it got; a write only adds pages)."""
    if DRY_RUN:
        write_sysfs(HUGEPAGES_2M, str(target))
        return target
    got = int(read(HUGEPAGES_2M, "0"))
    for attempt in range(attempts):
        if got >= target:
            break
        if attempt:
            run(["sync"])
            write_sysfs("/proc/sys/vm/drop_caches", "3")
            write_sysfs("/proc/sys/vm/compact_memory", "1")
            time.sleep(0.5)
        write_sysfs(HUGEPAGES_2M, str(target))
        got = int(read(HUGEPAGES_2M, "0"))
    return got


def step_hugepages(cfg: dict, st: HostState) -> None:
    """First part of the guest RAM, before anything disruptive. Whatever is missing is allocated
    by step_hugepages_finish, once the closed apps and the NVIDIA driver have freed their memory."""
    if not cfg["vm"]["hugepages"] or (DOMAIN_XML and not re.search(r"<hugepages[\s>/]", DOMAIN_XML)):
        return  # this start runs on transparent huge pages (gamevm start's fallback)
    need = cfg["vm"]["memory_mib"] // 2
    meminfo = dict(re.findall(r"^(\w+):\s+(\d+)", read("/proc/meminfo"), re.M))
    total, free = int(meminfo.get("HugePages_Total", 0)), int(meminfo.get("HugePages_Free", 0))
    if free >= need:
        return
    target = total + (need - free)
    if (need - free) * 2048 > int(meminfo.get("MemTotal", 0)) - 4 * 1024 * 1024:
        raise GameVMError(f"vm.memory_mib = {cfg['vm']['memory_mib']} does not fit next to the host (lower it)")
    log(f"allocating {need - free} hugepages ({(need - free) * 2} MiB)")
    st.push("hugepages", previous=total, target=target)
    got = grow_hugepages(target, 4)
    if got < target and cfg["vm"]["hugepages_fallback"]:
        # Cheap to stop here: nothing disruptive has happened yet. gamevm start then starts
        # Windows on transparent huge pages instead (no second desktop hand-off).
        raise GameVMError(f"only {got - total} of {need - free} hugepages available (RAM too fragmented) "
                          f"{HUGEPAGES_SHORT}")
    if got < target:
        log(f"{got - total} of {need - free} hugepages so far: the rest once apps are closed "
            "and the NVIDIA driver is unloaded")


def step_hugepages_finish(cfg: dict, st: HostState) -> None:
    """Complete the hugepage pool right before QEMU starts: the apps closed by the desktop
    hand-off (or the ended session) and the NVIDIA driver have given their memory back by now."""
    step = next((x for x in st.data["steps"] if x["kind"] == "hugepages"), None)
    if not step or DRY_RUN:
        return
    target, previous = step["target"], step["previous"]
    if int(read(HUGEPAGES_2M, "0")) >= target:
        return
    got = grow_hugepages(target, 6)
    if got < target:
        raise GameVMError(f"only {got - previous} of {target - previous} hugepages could be allocated "
                          "even after closing apps (RAM too fragmented). Close more programs and start "
                          "again, or set vm.hugepages = false (Windows then uses transparent huge pages: "
                          "slightly slower, but never refused).")
    log(f"all {target - previous} hugepages allocated")


def qemu_process_user() -> str:
    """The account libvirt runs QEMU as (qemu.conf `user`, Arch default libvirt-qemu)."""
    m = re.search(r'^\s*user\s*=\s*"([^"]+)"', read("/etc/libvirt/qemu.conf"), re.M)
    return m.group(1) if m else "libvirt-qemu"


def step_audio_access(cfg: dict, st: HostState) -> None:
    """Let QEMU (an unprivileged system user) reach the desktop user's PipeWire while the VM runs:
    the socket alone is bind-mounted into PIPEWIRE_DIR, which the VM's <audio> names as its runtime
    directory. The socket is 0666 and PipeWire does not check the client's UID. Avoids running
    QEMU as the desktop user, and leaves /run/user/UID alone: an ACL on it (what gamevm did until
    2026-11) shows as mode 0710, and Qt then refuses it as runtime directory for as long as
    Windows runs. Called late: in single-GPU mode the user's PipeWire has just been started again."""
    if cfg["audio"]["backend"] != "pipewire":
        return
    u = audio_user(cfg)
    if not u or qemu_process_user() == u.pw_name:
        return
    if DOMAIN_XML and str(PIPEWIRE_DIR) not in DOMAIN_XML:
        log(f"the VM definition does not take its sound from {PIPEWIRE_DIR} (defined by an older gamevm): "
            "Windows has no sound this time. `gamevm define` (or gamevm start) renews the definition", "warn")
        return
    sock, target = Path(f"/run/user/{u.pw_uid}/pipewire-0"), PIPEWIRE_DIR / "pipewire-0"
    deadline = time.monotonic() + 5
    while not DRY_RUN and not sock.is_socket() and time.monotonic() < deadline:
        time.sleep(0.25)
    if not DRY_RUN and not sock.is_socket():
        log(f"{sock} does not exist ({u.pw_name} not logged in?): the VM will have no sound", "warn")
        return
    if not DRY_RUN:
        PIPEWIRE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        while real_mount_at(str(target)):  # left over from a crash
            if run(["umount", str(target)], check=False, capture=True).returncode != 0:
                break
        target.touch(exist_ok=True)
    res = run(["mount", "--bind", str(sock), str(target)], check=False, capture=True)
    if res.returncode == 0:
        st.push("pipewire_bound", target=str(target))
    else:
        log(f"could not hand the PipeWire socket to QEMU ({(res.stderr or '').strip()}): "
            "the VM will have no sound", "warn")


def step_inhibit_sleep(cfg: dict, st: HostState) -> None:
    """A VFIO guest running through host suspend can lock up the host; block sleep meanwhile."""
    unit = f"gamevm-inhibit-{cfg['vm']['name']}"
    res = run(["systemd-run", f"--unit={unit}", "--collect", "--quiet", "systemd-inhibit",
               "--what=sleep:idle:handle-suspend-key:handle-hibernate-key:handle-lid-switch",
               "--who=gamevm", f"--why=Windows VM {cfg['vm']['name']} is running", "--mode=block",
               "sleep", "infinity"], check=False, capture=True)
    if res.returncode == 0:
        st.push("sleep_inhibited", unit=f"{unit}.service")
    else:
        log(f"could not block host sleep: {(res.stderr or '').strip()}", "warn")


def step_isolate(cfg: dict, st: HostState) -> None:
    if not cfg["cpu"]["isolate_host"]:
        return
    plan = cpu_plan(cfg)
    host = cpuset(plan["host"])
    log(f"confining host processes to CPUs {host}")
    st.push("isolated", units=list(ISOLATED_UNITS), all=cpuset(plan["all"]))
    for unit in ISOLATED_UNITS:
        run(["systemctl", "set-property", "--runtime", "--", unit, f"AllowedCPUs={host}"])
    if WORKQUEUE_MASK.exists():
        prev = read(WORKQUEUE_MASK)
        st.push("workqueue", previous=prev)
        write_sysfs(WORKQUEUE_MASK, cpumask_hex(plan["host"]))


# ---------------------------------------------------------------------------------------------
# desktop hand-off (iGPU mode): KWin on the graphics card while the VM is off, on the iGPU while it runs
# ---------------------------------------------------------------------------------------------

SIMULATE_KWIN = False  # selftest: act as if gamevm-kwin.service had set the desktop up


def kwin_managed() -> bool:
    """Set up at boot by `gamevm kwin-boot` (iGPU mode with the GPU handed back to Linux)."""
    return SIMULATE_KWIN or (KWIN_DIR / "layout").exists()


def kwin_layout() -> str:
    return "native" if SIMULATE_KWIN else (read(KWIN_DIR / "layout").strip() or "unmanaged")


def gpu_kernel_nodes(cfg: dict) -> tuple[str | None, str | None]:
    """The graphics card's /dev/dri card and render nodes. KWin is given links to the KERNEL nodes: udev
    deletes its own by-path symlinks before it announces a removal."""
    vga = cfg["gpu"]["devices"][0]
    out = []
    for kind in ("card", "render"):
        link = Path(f"/dev/dri/by-path/pci-{vga}-{kind}")
        out.append(os.path.realpath(link) if link.exists() else None)
    return out[0], out[1]


def kwin_links(cfg: dict, native: bool) -> None:
    """native: KWin's first GPU (its primary) is the graphics card, with NVIDIA's EGL. Otherwise the links
    are removed: KWin skips listed paths that do not exist and ignores the graphics card's udev events, and
    glvnd skips the missing NVIDIA vendor file, so KWin (and apps) then run on the iGPU alone."""
    card, render = gpu_kernel_nodes(cfg)
    if native and not (card and render):
        raise GameVMError("the graphics card has no /dev/dri nodes (NVIDIA driver not loaded?)")
    links = {KWIN_DIR / "dgpu-card": card, KWIN_DIR / "dgpu-render": render,
             KWIN_DIR / "egl" / "10_nvidia.json": NVIDIA_EGL_JSON}
    for link, target in links.items():
        if DRY_RUN:
            print(f"[dry-run] {'ln -sfn ' + str(target) if native else 'rm -f'} {link}", file=sys.stderr)
            continue
        link.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        try:
            link.unlink()
        except FileNotFoundError:
            pass
        if native:
            os.symlink(target, link)
    if not DRY_RUN:
        (KWIN_DIR / "layout").write_text("native\n" if native else "igpu\n")


def kwin_dropin_text() -> str:
    return ("# gamevm (written at boot by `gamevm kwin-boot`): KWin shows the main monitor through the\n"
            "# graphics card while Windows is off. The libvirt hook removes /run/gamevm/kwin/dgpu-* and restarts\n"
            "# KWin to hand the card to Windows, and does the reverse when Windows shuts down.\n"
            "[Service]\n"
            f"Environment=KWIN_DRM_DEVICES={KWIN_DIR}/dgpu-card:/dev/dri/gamevm-host-card\n"
            f"Environment=KWIN_RENDER_NODES={KWIN_DIR}/dgpu-render:/dev/dri/gamevm-host-render\n"
            "Environment=KWIN_DISABLE_VULKAN=1\n"
            f"Environment=__EGL_VENDOR_LIBRARY_FILENAMES={KWIN_DIR}/egl/10_nvidia.json:{MESA_EGL_JSON}\n")


def app_env_text() -> str:
    return ("# gamevm (written at boot by `gamevm kwin-boot`): apps find NVIDIA's EGL through a link that\n"
            "# exists only while the graphics card belongs to Linux, so whatever starts while Windows runs uses\n"
            "# the iGPU, and its children get the graphics card again once Windows has shut down.\n"
            f"__EGL_VENDOR_LIBRARY_FILENAMES={KWIN_DIR}/egl/10_nvidia.json:{MESA_EGL_JSON}\n")


def proc_uid(pid: int) -> int:
    """Owner of a process. Unlike /proc/PID/status, the directory keeps the real owner for
    non-dumpable processes such as kwin_wayland."""
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return -1


def uid_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def kwin_pids(u: pwd.struct_passwd) -> list[int]:
    return [p for p in procs_named(("kwin_wayland",)) if proc_uid(p) == u.pw_uid]


def user_bus_cmd(u: pwd.struct_passwd, *cmd: str) -> list[str]:
    return ["runuser", "-u", u.pw_name, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{u.pw_uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{u.pw_uid}/bus", *cmd]


def kwin_restart(cfg: dict, u: pwd.struct_passwd) -> None:
    """Restart KWin inside the running session: org.kde.KWin.replace makes kwin_wayland exit with
    code 133, and kwin_wayland_wrapper (which keeps the Wayland socket) starts it again with a
    reset crash counter. Never SIGHUP the wrapper (it then stops respawning) and never restart the
    systemd unit (that ends the session)."""
    old = kwin_pids(u)
    if not old:
        log(f"{u.pw_name} has no desktop session: nothing to restart (the next login uses the new layout)")
        return
    log("restarting KWin (the session stays; non-KDE apps close)")
    run(user_bus_cmd(u, "busctl", "--user", "--timeout=5", "call", "org.kde.KWin", "/KWin",
                     "org.kde.KWin", "replace"), check=False, capture=True)
    if DRY_RUN:
        return
    limit = cfg["desktop"]["restart_timeout"]
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and any(Path(f"/proc/{p}").exists() for p in old):
        time.sleep(0.25)
    while time.monotonic() < deadline:
        fresh = [p for p in kwin_pids(u) if p not in old]
        if fresh and run(user_bus_cmd(u, "busctl", "--user", "status", "org.kde.KWin"),
                         check=False, capture=True, mutate=False).returncode == 0:
            return
        time.sleep(0.5)
    raise GameVMError(f"KWin did not come back within {limit}s after the restart "
                      "(log out and back in, then: sudo gamevm recover)")


# --- the user's apps ---------------------------------------------------------------------------

def proc_unit(pid: int) -> str:
    """The systemd unit (last cgroup component) a process runs in, e.g. app-firefox@autostart.service."""
    return read(f"/proc/{pid}/cgroup").rsplit("/", 1)[-1]


def unit_unescape(s: str) -> str:
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


def unit_app(unit: str) -> tuple[str | None, bool]:
    """(desktop file ID, started by autostart) for the units Plasma starts applications in:
    app-<id>@<instance>.service (instance "autostart" for login autostart, from systemd's
    xdg-autostart generator), app-<id>.service (D-Bus activated, e.g. Spectacle) and
    app-<id>-<pid>.scope. (None, False) for anything else."""
    m = (re.match(r"^app-(.+?)(?:@([^@/]*))?\.service$", unit)
         or re.match(r"^app-(.+)-()\d+\.scope$", unit))
    if not m:
        return None, False
    return unit_unescape(m.group(1)), m.group(2) == "autostart"


def app_unit_name(app: str) -> str:
    """A new app-<id>@<instance>.service name, escaped the way systemd does (round-trips through
    unit_app)."""
    esc = "".join(chr(b) if (chr(b).isalnum() and b < 128) or chr(b) in ":_.-" else "\\x%02x" % b
                  for b in app.encode())
    return f"app-{esc}@gamevm{int(time.time())}.service"


def user_procs(u: pwd.struct_passwd) -> list[dict]:
    out = []
    for pdir in Path("/proc").iterdir():
        if pdir.name.isdigit() and proc_uid(int(pdir.name)) == u.pw_uid:
            pid = int(pdir.name)
            out.append({"pid": pid, "comm": read(pdir / "comm"), "unit": proc_unit(pid)})
    return out


def app_units(u: pwd.struct_passwd) -> dict[str, dict]:
    """The user's app units that have processes: unit -> {app, autostart, pids}."""
    units: dict[str, dict] = {}
    for p in user_procs(u):
        app, autostart = unit_app(p["unit"])
        if app:
            units.setdefault(p["unit"], {"app": app, "autostart": autostart, "pids": []})["pids"].append(p["pid"])
    return units


def unit_main_pids(u: pwd.struct_passwd, units: list[str]) -> dict[str, int]:
    """The live main process of each unit (0 = gone; its helpers may still run): the MainPID of a
    service, the leader named in a scope."""
    mains: dict[str, int] = {}
    services = sorted(x for x in units if x.endswith(".service"))
    if services:
        target = ["-M", f"{u.pw_name}@"] if os.geteuid() == 0 else []
        res = run(["systemctl", "--user", *target, "show", "-p", "Id", "-p", "MainPID", "--", *services],
                  check=False, capture=True, mutate=False)
        for block in (res.stdout or "").strip().split("\n\n"):
            kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if kv.get("Id"):
                mains[kv["Id"]] = int(kv.get("MainPID") or 0)
    for x in units:
        m = re.search(r"-(\d+)\.scope$", x)
        if m:
            mains[x] = int(m.group(1))
    return {x: pid if pid and Path(f"/proc/{pid}").exists() else 0 for x, pid in mains.items()}


DISPLAY_SOCKET_RE = re.compile(r"^@?(/run/user/\d+/wayland-\d+|/tmp/\.X11-unix/X\d+)$")


def display_conns() -> set[int] | None:
    """Socket inodes of the client ends of all Wayland and X11 connections, from one `ss`
    snapshot: the server end of an accepted connection shows the display socket's path, its peer
    is the client. Mapping libwayland/libxcb proves nothing (libpulse's pactl maps libxcb).
    None if ss is unavailable."""
    res = run(["ss", "-xaH"], check=False, capture=True, mutate=False)
    if res.returncode != 0:
        return None
    conns = set()
    for line in (res.stdout or "").splitlines():
        f = line.split()
        if len(f) >= 8 and f[1] == "ESTAB" and DISPLAY_SOCKET_RE.match(f[4]) and f[7].isdigit():
            conns.add(int(f[7]))
    return conns


def proc_sockets(pid: int) -> set[int]:
    out = set()
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return out
    for fd in fds:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:["):
            out.add(int(target[8:-1]))
    return out


def proc_maps(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/maps").read_bytes()
    except OSError:
        return b""


def display_client(pid: int, conns: set[int] | None) -> bool:
    """A program with windows: connected to the compositor or the X server, or a Qt 6 Wayland
    client (it reconnects after a KWin restart, so it may be between two connections)."""
    maps = proc_maps(pid)
    if b"libQt6WaylandClient" in maps:
        return True
    if conns is None:
        return b"libwayland-client" in maps or b"libX11.so" in maps
    return bool(proc_sockets(pid) & conns)


def dies_with_kwin(pid: int, conns: set[int] | None) -> bool:
    """A program with windows that cannot reconnect to a restarted compositor: everything on the
    display except Qt 6 Wayland clients."""
    return b"libQt6WaylandClient" not in proc_maps(pid) and display_client(pid, conns)


def running_apps(u: pwd.struct_passwd) -> dict[str, str]:
    """Apps of the user with windows, i.e. with a process on the display: desktop file ID -> its
    unit (its autostart unit if it has one). Helpers a closed app left behind and tray programs
    without a display connection (which no KWin restart closes) do not count."""
    units, conns = app_units(u), display_conns()
    shown = {info["app"] for info in units.values() if any(display_client(p, conns) for p in info["pids"])}
    apps: dict[str, str] = {}
    for unit, info in sorted(units.items()):
        if info["app"] in shown and (info["app"] not in apps or info["autostart"]):
            apps[info["app"]] = unit
    return apps


def wanted_apps(cfg: dict, u: pwd.struct_passwd) -> dict[str, str]:
    """Running apps to reopen if the KWin restart closes them: all of them (desktop.relaunch_all)
    or the ones started at login (desktop.relaunch_autostart) plus those in desktop.relaunch;
    never the ones in desktop.relaunch_exclude."""
    d = cfg["desktop"]
    return {app: unit for app, unit in running_apps(u).items() if app not in d["relaunch_exclude"]
            and (d["relaunch_all"] or (unit_app(unit)[1] and d["relaunch_autostart"]) or app in d["relaunch"])}


def doomed_procs(u: pwd.struct_passwd) -> set[int]:
    """Processes of the user's apps that the next KWin restart closes."""
    conns = display_conns()
    return {p for info in app_units(u).values() for p in info["pids"] if dies_with_kwin(p, conns)}


def wait_gone(pids: set[int], seconds: float) -> None:
    """Let apps that lost the compositor finish exiting (Electron takes a few seconds)."""
    deadline = time.monotonic() + seconds
    while not DRY_RUN and time.monotonic() < deadline and any(Path(f"/proc/{p}").exists() for p in pids):
        time.sleep(0.25)


def desktop_file(u: pwd.struct_passwd, app: str) -> Path | None:
    home = Path(u.pw_dir)
    for d in (home / ".local/share/applications", Path("/usr/local/share/applications"),
              Path("/usr/share/applications"), home / ".local/share/flatpak/exports/share/applications",
              Path("/var/lib/flatpak/exports/share/applications")):
        f = d / f"{app}.desktop"
        if f.is_file():
            return f
    return None


def desktop_exec(path: Path) -> tuple[list[str], str | None]:
    """argv (field codes dropped) and working directory of a .desktop file's [Desktop Entry]."""
    entry, group = {}, None
    for line in read(path).splitlines():
        line = line.strip()
        if line.startswith("["):
            group = line
        elif group == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            entry.setdefault(k.strip(), v.strip())
    if entry.get("Terminal", "false").lower() == "true":
        return [], None
    try:
        words = shlex.split(entry.get("Exec", ""))
    except ValueError:
        return [], None
    argv = [w.replace("%%", "\0") for w in words if not re.fullmatch(r"%[fFuUdDnNickvm]", w)]
    argv = [re.sub(r"%[fFuUdDnNickvm]", "", w).replace("\0", "%") for w in argv]
    return [w for w in argv if w], entry.get("Path") or None


def merge_apps(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    out = dict(old)
    for app, unit in new.items():
        if app not in out or unit_app(unit)[1]:
            out[app] = unit
    return out


def relaunch_apps(u: pwd.struct_passwd, apps: dict[str, str]) -> None:
    """Reopen the apps (desktop file ID -> unit) that no longer run. An app started at login is
    restarted in its own autostart unit: same arguments (e.g. --hidden), still an autostart app
    next time, and helpers its old instance left behind (an audio tool's PipeWire filter chains, say) end.
    Others get a new app-<id>@gamevm<time>.service, like the ones Plasma creates. Never raises:
    a failed reopen must not fail a VM start or stop."""
    if not apps:
        return
    if not kwin_pids(u):
        log(f"{u.pw_name} has no desktop session: apps are not reopened")
        return
    units = {} if DRY_RUN else app_units(u)
    conns = display_conns()
    for app, unit in sorted(apps.items()):
        try:
            mine = [x for x, info in units.items() if info["app"] == app]
            if any(display_client(p, conns) for x in mine for p in units[x]["pids"]):
                continue  # its windows survived the restart
            log(f"reopening {app}")
            if unit_app(unit)[1]:
                user_systemctl(u.pw_name, "restart", unit, check=True)
                mine = [x for x in mine if x != unit]
            for x in mine:  # what is left of the old instance
                user_systemctl(u.pw_name, "stop", x)
            if unit_app(unit)[1]:
                continue
            f = desktop_file(u, app)
            argv, wd = desktop_exec(f) if f else ([], None)
            if not argv:
                raise GameVMError("no usable .desktop file found")
            argv += SESSION_RESTORE_ARGS.get(app, [])
            run(["systemd-run", "--user", "-M", f"{u.pw_name}@", "--collect", "--quiet", "--no-block",
                 f"--unit={app_unit_name(app)}", "-p", "ExitType=cgroup",
                 *(["-p", f"WorkingDirectory={wd}"] if wd else []), "--", *argv], capture=True)
        except Exception as e:  # noqa: BLE001 - logged, never fatal
            log(f"could not reopen {app}: {e}", "warn")


# --- keeping the session as it was ---------------------------------------------------------------
# A KWin restart closes every app that is not a Qt Wayland client. What can be kept: the apps
# quit the way they do when you close them (so they save what they save), all of them come back,
# with their own "restore the last session" switch where they have one, and their windows return
# to the monitor and place they had. What cannot: whatever an app does not save itself.

# Desktop file IDs (exact) of Chromium browsers: they reopen their tabs only when told to.
SESSION_RESTORE_ARGS = {app: ["--restore-last-session"] for app in (
    "chromium", "google-chrome", "google-chrome-beta", "google-chrome-unstable", "brave-browser", "brave",
    "vivaldi-stable", "vivaldi", "microsoft-edge", "microsoft-edge-stable", "thorium-browser",
    "ungoogled-chromium", "org.chromium.Chromium", "com.google.Chrome", "com.brave.Browser",
    "com.vivaldi.Vivaldi", "com.microsoft.Edge")}


def as_user(u: pwd.struct_passwd, *cmd: str) -> list[str]:
    """cmd on the user's session bus: through runuser for root (the hook), directly for the user."""
    return list(cmd) if os.geteuid() == u.pw_uid else user_bus_cmd(u, *cmd)


def quit_apps(cfg: dict, u: pwd.struct_passwd) -> None:
    """Before a KWin restart: ask the apps it would close to quit by themselves (SIGTERM to each
    app's main process, then wait desktop.quit_timeout seconds). They then leave as when you
    quit them: session and settings written, no "did not shut down correctly" at the next start.
    Left alone: Firefox and its relatives, which bring their tabs back after an unexpected end but
    not after a normal one (unless set to restore the previous session); terminals, whose programs
    are not gamevm's to end early; and the app gamevm itself was started from. Best effort: what
    is still there goes with the restart, as before."""
    wait = cfg["desktop"]["quit_timeout"]
    if wait <= 0:
        return
    conns = display_conns()
    doomed = {unit: info for unit, info in app_units(u).items()
              if any(dies_with_kwin(p, conns) for p in info["pids"])
              and not any(b"libxul.so" in proc_maps(p) for p in info["pids"])}
    own = {proc_unit(os.getpid()), proc_unit(os.getppid())}
    mains = unit_main_pids(u, list(doomed))
    asked: set[int] = set()
    for unit, info in sorted(doomed.items()):
        main = mains.get(unit, 0)
        if unit in own or not main or read(f"/proc/{main}/comm") in TERMINALS:
            continue
        log(f"asking {info['app']} to quit before the desktop restarts")
        if not DRY_RUN:
            try:
                os.kill(main, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
        asked.update(info["pids"])
    wait_gone(asked, wait)


CAPTURE_JS = """\
const out = [];
for (const w of workspace.windowList()) {
    if (!w.normalWindow) continue;
    const g = w.frameGeometry;
    out.push({pid: w.pid, key: String(w.desktopFileName) || String(w.resourceClass), caption: String(w.caption),
              x: g.x, y: g.y, w: g.width, h: g.height, output: w.output ? String(w.output.name) : "",
              desktops: w.desktops.map(d => String(d.id)), all: w.onAllDesktops, max: w.maximizeMode,
              full: w.fullScreen, min: w.minimized});
}
print("@MARKER@" + JSON.stringify(out));
"""

# Places the windows that are open, and for 90 s the ones that appear (the reopened apps). A window
# is matched to a remembered one of the same app, by title if one fits, else in order. Each is
# placed again after 0.8 and 2.5 s, because apps arrange their own window right after it appears.
RESTORE_JS = """\
const saved = @SAVED@;
const taken = new Set();
const timers = [];
function later(ms, fn) {
    const t = new QTimer();
    t.interval = ms; t.singleShot = true; t.timeout.connect(fn); t.start();
    timers.push(t);
}
function place(w, s) {
    try {
        if (w.deleted || !workspace.screens.some(o => String(o.name) === s.output)) return;
        if (w.fullScreen && !s.full) w.fullScreen = false;
        if (w.maximizeMode) w.setMaximize(false, false);
        w.frameGeometry = {x: s.x, y: s.y, width: s.w, height: s.h};
        if (s.max) w.setMaximize(!!(s.max & 1), !!(s.max & 2));
        if (s.full) w.fullScreen = true;
        if (s.all) w.onAllDesktops = true;
        else {
            const d = workspace.desktops.filter(x => s.desktops.includes(String(x.id)));
            if (d.length) w.desktops = d;
        }
        if (s.min) w.minimized = true;
    } catch (e) { print("gamevm: could not place a window: " + e); }
}
function put(w) {
    if (!w.normalWindow) return;
    const key = String(w.desktopFileName) || String(w.resourceClass);
    let i = saved.findIndex((s, n) => !taken.has(n) && s.key === key && s.caption === String(w.caption));
    if (i < 0) i = saved.findIndex((s, n) => !taken.has(n) && s.key === key);
    if (i < 0) return;
    taken.add(i);
    const s = saved[i];
    place(w, s);
    later(800, () => place(w, s));
    later(2500, () => place(w, s));
}
workspace.windowList().forEach(put);
workspace.windowAdded.connect(put);
later(90000, () => { workspace.windowAdded.disconnect(put); });
"""


def kwin_script(u: pwd.struct_passwd, text: str) -> bool:
    """Load and run text as gamevm's script in the user's KWin, replacing the previous one. Scripts
    loaded this way are gone when KWin restarts."""
    path = STATE_DIR / f"{WINDOW_SCRIPT}.js"
    STATE_DIR.mkdir(mode=0o755, exist_ok=True)
    path.write_text(text)
    os.chmod(path, 0o644)

    def kwin(*args: str) -> subprocess.CompletedProcess:
        return run(as_user(u, "busctl", "--user", "--timeout=5", "call", "org.kde.KWin", *args), check=False,
                   capture=True, timeout=15)

    kwin("/Scripting", "org.kde.kwin.Scripting", "unloadScript", "s", WINDOW_SCRIPT)
    res = kwin("/Scripting", "org.kde.kwin.Scripting", "loadScript", "ss", str(path), WINDOW_SCRIPT)
    m = re.fullmatch(r"i (\d+)", (res.stdout or "").strip())
    if res.returncode != 0 or not m:
        return False
    return kwin(f"/Scripting/Script{m.group(1)}", "org.kde.kwin.Script", "run").returncode == 0


def capture_windows(cfg: dict, u: pwd.struct_passwd) -> list[dict]:
    """Where the user's windows are right now (monitor, place, size, maximised, virtual desktop),
    asked from KWin through a script whose answer comes back in the journal. Best effort: [] if
    desktop.restore_windows is off or KWin does not answer within 3 s."""
    if not cfg["desktop"]["restore_windows"] or DRY_RUN or not kwin_pids(u):
        return []
    marker = f"GAMEVM-WINDOWS-{os.getpid()}-{time.monotonic_ns()}:"
    since = int(time.time()) - 1
    try:
        if not kwin_script(u, CAPTURE_JS.replace("@MARKER@", marker)):
            return []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            out = run(["journalctl", "--no-pager", "-o", "cat", f"--since=@{since}", f"_UID={u.pw_uid}", "-g", marker],
                      check=False, capture=True, mutate=False, timeout=15).stdout or ""
            for line in out.splitlines():
                if marker in line:
                    windows = json.loads(line.split(marker, 1)[1])
                    log(f"remembering where {len(windows)} windows are")
                    return windows
            time.sleep(0.2)
    except Exception as e:  # noqa: BLE001 - a convenience, never a reason to fail
        log(f"could not read the window positions from KWin: {e}", "warn")
    return []


def restore_windows(cfg: dict, u: pwd.struct_passwd, windows: list[dict] | None) -> None:
    """Put the windows back where capture_windows found them: the open ones now, the ones of the
    reopened apps as they appear. Windows of a monitor Linux does not have right now stay put."""
    if not windows or not cfg["desktop"]["restore_windows"]:
        return
    if DRY_RUN:
        print(f"[dry-run] put {len(windows)} windows back where they were", file=sys.stderr)
        return
    if not kwin_pids(u):
        return
    keys = ("key", "caption", "x", "y", "w", "h", "output", "desktops", "all", "max", "full", "min")
    try:
        if not kwin_script(u, RESTORE_JS.replace("@SAVED@", json.dumps([{k: w.get(k) for k in keys} for w in windows]))):
            log("KWin did not take the script that puts the windows back", "warn")
    except Exception as e:  # noqa: BLE001
        log(f"could not put the windows back: {e}", "warn")


def pending_windows() -> list[dict] | None:
    """The window positions kept in PENDING_FILE (None: there is none)."""
    try:
        return json.loads(PENDING_FILE.read_text()).get("windows", [])
    except (OSError, ValueError, AttributeError):
        return None


def save_pending(windows: list[dict]) -> None:
    if DRY_RUN:
        return
    try:
        STATE_DIR.mkdir(mode=0o755, exist_ok=True)
        PENDING_FILE.write_text(json.dumps({"windows": windows}))
        os.chmod(PENDING_FILE, 0o644)
    except OSError as e:
        log(f"could not write {PENDING_FILE}: {e}", "warn")


def ask_return(u: pwd.struct_passwd, closing: list[str]) -> None:
    """desktop.return = "ask": a notification with a button that brings the main monitor back
    (pkexec gamevm desktop native: it asks for the password). Run in the user's service manager,
    where polkit finds the session's password dialog. `gamevm back` does the same by hand."""
    body = ("Linux stays on the side monitor, so your apps keep running. Bringing the main monitor back closes "
            + (", ".join(closing[:6]) + (" ..." if len(closing) > 6 else "") if closing else "apps that are not KDE apps")
            + " and reopens them. Later: gamevm back")
    script = ('a=$(notify-send -a gamevm -i computer -t 0 -A "back=Bring the main monitor back" "$1" "$2"); '
              '[ "$a" = back ] && exec pkexec /usr/local/bin/gamevm desktop native')
    run(["systemd-run", "--user", "-M", f"{u.pw_name}@", "--collect", "--quiet", "--no-block",
         f"--unit=gamevm-ask-{int(time.time())}", "sh", "-c", script, "sh", "Windows is off", body],
        check=False, capture=True)


# --- the graphics card's users ---------------------------------------------------------------------------

def session_device_copy(h: dict) -> bool:
    """systemd-logind's hold on the desktop session's DRM devices: logind itself, and the copies
    it parks in PID 1's file descriptor store so they survive a logind restart. logind drops both
    as soon as KWin releases the device."""
    return h["comm"] == "systemd-logind" or (h["pid"] == 1 and all(n.startswith("/dev/dri/card") for n in h["nodes"]))


def nvidia_holders(cfg: dict) -> list[dict]:
    """Processes tied to the NVIDIA driver (gpu_users): open nodes of the graphics card and, as root, its fences."""
    return gpu_users(cfg)


def blocking_holders(cfg: dict, u: pwd.struct_passwd) -> list[dict]:
    """Users of the graphics card that neither the KWin restart nor gamevm frees, found BEFORE anything
    changes: other users' and root's processes (the login screen, NVML/CUDA daemons), the user's
    session services, CUDA/NVML/DDC users in the user's services (they would open the card again
    after a restart) and the user's programs outside app units (SSH, tmux, scripts), which gamevm
    never kills behind the user's back."""
    host_services = {x if "." in x else x + ".service" for x in cfg["gpu"]["host_services"]}
    out = []
    for h in nvidia_holders(cfg):
        pid, unit, uid = h["pid"], proc_unit(h["pid"]), proc_uid(h["pid"])
        if uid < 0 or unit in host_services:
            continue  # exited since the scan / stopped by the hook (step_gpu_services) itself
        if session_device_copy(h):
            continue  # goes away when KWin lets go of the graphics card
        if uid == u.pw_uid and unit == KWIN_UNITS[0]:
            continue  # KWin and its Xwayland
        if uid != u.pw_uid or unit in SESSION_CRITICAL_UNITS or unit.startswith("user@"):
            out.append(h)
        elif unit.startswith("app-"):
            continue  # closed with its app
        elif unit.endswith(".service"):
            if unit in GPU_MONITOR_UNITS:
                continue  # held off around the driver unload (pause_gpu_monitors)
            if h["comm"] == "nvidia-smi" or any(n.startswith(("/dev/nvidia-uvm", "/dev/i2c-")) for n in h["nodes"]):
                out.append(h)
        else:
            out.append(h)
    return out


def blocking_error(blockers: list[dict]) -> GameVMError:
    if any(h["comm"] == "kwin_wayland" for h in blockers):
        return GameVMError("the login screen is using the graphics card: log in to the desktop first")
    names = ", ".join(f"{h['comm']} (pid {h['pid']}, {uid_name(proc_uid(h['pid']))})" for h in blockers)
    return GameVMError(f"the graphics card is in use by {names}. gamevm does not close these for you (another "
                       "user, a system service, a CUDA/monitoring service, or a program in an SSH/tmux session): "
                       "stop it and start again (`sudo gamevm holders` shows details). The desktop was not touched.")


def wait_holders_settle(cfg: dict, seconds: float) -> list[dict]:
    """Give apps that lost their display connection time to exit: wait until the set of holders
    stops changing (1.5 s) or `seconds` pass."""
    holders = nvidia_holders(cfg)
    if DRY_RUN:
        return holders
    deadline, stable_since, last = time.monotonic() + seconds, time.monotonic(), None
    while holders and time.monotonic() < deadline:
        pids = {h["pid"] for h in holders}
        if pids != last:
            last, stable_since = pids, time.monotonic()
        elif time.monotonic() - stable_since >= 1.5:
            break
        time.sleep(0.25)
        holders = nvidia_holders(cfg)
    return holders


def classify_app_holders(u: pwd.struct_passwd, holders: list[dict]) -> tuple[list[str], list[dict]]:
    """Split the user's programs that use the graphics card, in app units into units to stop as a whole (the app draws with
    the graphics card, or only its helpers are left) and single programs to close: ones started in a
    terminal, whose terminal stays open, and any in the unit gamevm itself runs in."""
    units: dict[str, list[dict]] = {}
    for h in holders:
        unit = proc_unit(h["pid"])
        if proc_uid(h["pid"]) == u.pw_uid and unit.startswith("app-") and killable(h):
            units.setdefault(unit, []).append(h)
    mains = unit_main_pids(u, list(units))
    own = {proc_unit(os.getpid()), proc_unit(os.getppid())}
    stop, single = [], []
    for unit, hs in sorted(units.items()):
        main = mains.get(unit, 0)
        if unit in own or (main and main not in {h["pid"] for h in hs}
                           and read(f"/proc/{main}/comm") in TERMINALS):
            single += [h for h in hs if h["pid"] != main]
        else:
            stop.append(unit)
    return stop, single


def release_gpu_holders(cfg: dict, u: pwd.struct_passwd) -> None:
    """After KWin moved to the iGPU: the user's apps and services that still have the graphics card open
    are moved off it. Apps are closed as a whole unit (with their helpers), a program started in
    a terminal alone; user services (kded6, ...) are restarted and come back on the iGPU, since
    the NVIDIA EGL link is gone. Everything else was refused by blocking_holders()."""
    holders = wait_holders_settle(cfg, 8)
    stop, single = classify_app_holders(u, holders)
    restart = sorted({proc_unit(h["pid"]) for h in holders if proc_uid(h["pid"]) == u.pw_uid and killable(h)
                      and proc_unit(h["pid"]).endswith(".service") and not proc_unit(h["pid"]).startswith(("app-", "user@"))
                      and proc_unit(h["pid"]) not in SESSION_CRITICAL_UNITS | GPU_MONITOR_UNITS})
    for unit in stop:
        log(f"closing {unit_app(unit)[0] or unit}: it still uses the graphics card")
        user_systemctl(u.pw_name, "stop", unit)
    for unit in restart:
        log(f"restarting {unit} on the iGPU")
        user_systemctl(u.pw_name, "restart", unit)
    for h in single:
        log(f"closing {h['comm']} (pid {h['pid']}): it uses the graphics card")
        if DRY_RUN:
            continue
        try:
            pgid = os.getpgid(h["pid"])
            if pgid != os.getsid(h["pid"]):  # a job of the shell (e.g. `watch nvidia-smi`): all of it
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(h["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if restart or single:
        holders = wait_holders_settle(cfg, 5)
        again = classify_app_holders(u, holders)[1] if single and not DRY_RUN else []
        if again:
            raise GameVMError(f"{again[0]['comm']} (pid {again[0]['pid']}), started in a terminal, keeps using "
                              "the graphics card: stop it there (Ctrl+C) and start again")


def desktop_to_igpu(cfg: dict, u: pwd.struct_passwd) -> dict[str, str]:
    """Move the desktop to the iGPU alone (KWin restart) and free the graphics card from the user's apps.
    Returns the apps to reopen."""
    blockers = blocking_holders(cfg, u)
    if blockers and DRY_RUN:
        log(f"[dry-run] would refuse: {blocking_error(blockers)}", "warn")
    elif blockers:
        raise blocking_error(blockers)
    apps = wanted_apps(cfg, u)
    if kwin_layout() != "igpu":  # already there (gamevm desktop igpu): KWin has no graphics card to let go
        doomed = doomed_procs(u)
        quit_apps(cfg, u)
        kwin_links(cfg, native=False)
        kwin_restart(cfg, u)
        wait_gone(doomed, 12)
    release_gpu_holders(cfg, u)
    return apps


def step_desktop_to_igpu(cfg: dict, st: HostState) -> None:
    """iGPU mode: move the Linux desktop off the graphics card. KWin cannot drop its primary GPU while it
    runs, so it is restarted on the iGPU alone. The session survives; Qt/KDE apps reconnect, other
    apps (Electron, GTK, X11 through Xwayland) close, and the wanted ones reopen on the iGPU at
    the end of prepare()."""
    if not kwin_managed():
        return
    u = audio_user(cfg)
    if not u:
        raise GameVMError("audio.user (the desktop user) is not set in the config")
    blockers = blocking_holders(cfg, u)  # refuse before the journal records anything to undo
    if blockers and not DRY_RUN:
        raise blocking_error(blockers)
    # Where the windows are while Linux still has both monitors. Already on the side monitor alone
    # (gamevm desktop igpu, or desktop.return = "ask" after the last run): the places kept from then.
    windows = pending_windows() if kwin_layout() == "igpu" else None
    step = {"kind": "desktop_igpu", "user": u.pw_name, "relaunch": wanted_apps(cfg, u),
            "started": st.data.get("started", time.time()),
            "windows": capture_windows(cfg, u) if windows is None else windows}
    st.data["steps"].append(step)
    st.save()
    step["relaunch"] = merge_apps(step["relaunch"], desktop_to_igpu(cfg, u))
    st.save()


def desktop_to_native(cfg: dict, step: dict, may_stay: bool = False) -> None:
    """Undo of step_desktop_to_igpu: KWin back on the graphics card (when the GPU came back healthy), the
    apps this closes reopened and the windows back where they were before Windows started.
    may_stay (Windows has stopped normally): with desktop.return = "ask" the desktop stays on the
    side monitor if the way back would close apps, and a notification offers it instead."""
    if not DRY_RUN and kwin_layout() == "native":
        return  # the start was refused before the desktop moved
    u = pwd.getpwnam(step["user"])
    now = wanted_apps(cfg, u)
    # After a normal VM run, reopen what runs now (an app quit during the Windows session stays
    # closed); after a failed start, also what the restart at the start closed.
    apps = now if step.get("reopened") else merge_apps(step.get("relaunch", {}), now)
    if not DRY_RUN:
        for _ in range(40):
            if all(gpu_kernel_nodes(cfg)):
                break
            time.sleep(0.25)
    if not (DRY_RUN or (all(gpu_kernel_nodes(cfg)) and gpu_reinit_ok(cfg, step.get("started", time.time() - 600)))):
        log("the graphics card is not usable: the Linux desktop stays on the side monitor (iGPU). "
            "Reboot to get the main monitor back.", "error")
        if not step.get("reopened"):
            relaunch_apps(u, apps)
        return
    windows = step.get("windows")
    if windows is None:  # gamevm desktop native / recover: kept from the switch, else as they are now
        windows = pending_windows()
    if windows is None:
        windows = capture_windows(cfg, u)
    doomed = doomed_procs(u)
    if (may_stay and cfg["desktop"]["return"] == "ask" and step.get("reopened") and doomed and kwin_pids(u)
            and not DRY_RUN):
        closing = sorted({info["app"] for info in app_units(u).values() if doomed & set(info["pids"])})
        save_pending(windows)
        log('Windows has stopped. desktop.return = "ask": the Linux desktop stays on the side monitor, because '
            f"moving back would close {', '.join(closing)}. `gamevm back` moves it")
        ask_return(u, closing)
        return
    quit_apps(cfg, u)
    kwin_links(cfg, native=True)
    kwin_restart(cfg, u)
    wait_gone(doomed, 12)
    relaunch_apps(u, apps)
    restore_windows(cfg, u, windows)
    if not DRY_RUN:
        PENDING_FILE.unlink(missing_ok=True)


def kwin_boot(cfg: dict) -> bool:
    """Boot-time setup (gamevm-kwin.service, before user managers and the login manager): in iGPU
    mode with the GPU handed back to Linux, give KWin the graphics card as its primary GPU through the
    links + a unit drop-in, and apps the link-based EGL environment. In any other case remove
    them, so KWin behaves exactly as without gamevm. False: iGPU mode is wanted but the GPUs' DRM
    nodes did not appear (KWin then keeps its default layout this boot)."""
    try:
        wanted = (resolve_mode(cfg) == "igpu" and not gpu_dedicated()
                  and pci_attr(cfg["gpu"]["devices"][0], "vendor") == "0x10de")
    except GameVMError:
        wanted = False
    missing = []
    if wanted and not DRY_RUN:
        for _ in range(60):  # the NVIDIA driver loads from the initramfs; its nodes may still be coming
            if all(gpu_kernel_nodes(cfg)) and Path("/dev/dri/gamevm-host-card").exists():
                break
            time.sleep(0.25)
        if not all(gpu_kernel_nodes(cfg)):
            missing.append("the graphics card's DRM nodes (NVIDIA driver not loaded?)")
        if not Path("/dev/dri/gamevm-host-card").exists():
            missing.append("/dev/dri/gamevm-host-card (iGPU driver not loaded, or the udev rule is missing)")
    ok = wanted and not missing
    files = {USER_UNIT_DROPIN_DIR / f"{unit}.d" / "50-gamevm.conf": kwin_dropin_text() for unit in KWIN_UNITS}
    files[APP_ENV_FILE] = app_env_text()
    for path, text in files.items():
        if DRY_RUN:
            print(f"[dry-run] {'write' if ok else 'remove'} {path}", file=sys.stderr)
        elif ok:
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            path.write_text(text)
            os.chmod(path, 0o644)
        else:
            path.unlink(missing_ok=True)
    if ok:
        kwin_links(cfg, native=True)
        log("KWin will use the graphics card for the main monitor (iGPU mode)")
        return True
    if not DRY_RUN:
        shutil.rmtree(KWIN_DIR, ignore_errors=True)
    if missing:
        log("gave up waiting for " + " and ".join(missing) + ": KWin keeps its default GPU layout this boot, "
            "so Windows cannot be started until a reboot fixes it", "error")
        return False
    log("KWin GPU management not active (single-GPU mode, dedicated GPU or no iGPU)")
    return True


def step_tuning(cfg: dict, st: HostState) -> None:
    """Latency extras applied only while the VM runs."""
    t, plan = cfg["tuning"], cpu_plan(cfg)
    # ananicy-cpp's rules (CachyOS ships them) renice qemu-system-x86 to nice 9 / ionice 7.
    if t["pause_ananicy"] and unit_active("ananicy-cpp.service"):
        run(["systemctl", "stop", "ananicy-cpp.service"])
        st.push("service_stopped", unit="ananicy-cpp.service")
    # Deep C-states (C3 here: 350 us exit latency) delay guest interrupts on idle vCPU cores.
    if t["block_deep_idle_us"]:
        blocked = []
        for cpu in plan["guest"]:
            for state in sorted(glob.glob(f"/sys/devices/system/cpu/cpu{cpu}/cpuidle/state[0-9]*")):
                try:
                    lat = int(read(f"{state}/latency", "0"))
                except ValueError:
                    continue
                if lat >= t["block_deep_idle_us"] and read(f"{state}/disable") == "0":
                    write_sysfs(f"{state}/disable", "1")
                    blocked.append(state)
        if blocked:
            st.push("idle_blocked", states=blocked)
    # Steer device interrupts to the host cores (managed IRQs, e.g. NVMe queues, refuse: fine).
    if t["irq_affinity"]:
        host = cpuset(plan["host"])
        saved = {}
        for f in sorted(glob.glob("/proc/irq/[0-9]*/smp_affinity_list")):
            prev = read(f)
            if not prev or prev == host:
                continue
            try:
                write_sysfs(f, host)
                saved[f] = prev
            except OSError:
                pass
        prev_default = read("/proc/irq/default_smp_affinity")
        write_sysfs("/proc/irq/default_smp_affinity", cpumask_hex(plan["host"]))
        st.push("irq_affinity", default=prev_default, irqs=saved)


# ---------------------------------------------------------------------------------------------
# preflight: what would otherwise fail only after the desktop has moved
# ---------------------------------------------------------------------------------------------

def kernel_modules_missing(cfg: dict) -> list[str]:
    """Modules this start needs that the running kernel can no longer load from disk: vfio-pci
    (libvirt loads it to take the devices) and the host GPU driver (loaded again when Windows
    stops). pacman removes the running kernel's modules when it updates the kernel, so until the
    reboot the start would fail after the desktop moved, and the GPU could not come back."""
    wanted = [m for m in cfg["gpu"]["host_modules"] if module_loaded(m)]
    if not Path("/sys/bus/pci/drivers/vfio-pci").exists():  # neither loaded nor built in
        wanted.append("vfio-pci")
    missing = []
    for m in wanted:
        try:
            rc = run(["modinfo", "-F", "filename", m], check=False, capture=True, mutate=False, timeout=20).returncode
        except subprocess.TimeoutExpired:
            continue
        if rc not in (0, 127):  # 127: no modinfo, nothing to conclude
            missing.append(m)
    return missing


def network_active(name: str) -> bool:
    """A libvirt network is running: libvirt keeps a status file per active network. (No virsh
    here: the hook must not call it.)"""
    return Path(f"/run/libvirt/network/{name}.xml").exists()


def wanted_networks(cfg: dict) -> list[str]:
    net = cfg["network"]
    return (["default"] if net["mode"] == "nat" else []) + ([net["hostonly_network"]] if net["hostonly"] else [])


def start_problems(cfg: dict, mode: str, xml: str = "") -> list[str]:
    """Why QEMU could not start although the host hand-off would succeed. libvirt takes the PCI
    devices, sets up the networks and starts QEMU only after the hook's prepare has run, so each
    of these would cost a desktop restart there and another one back. xml: the domain definition
    libvirt is starting (the hook gets it); without it the config says what the VM will use."""
    problems = []
    missing = kernel_modules_missing(cfg)
    if missing:
        problems.append(f"the running kernel ({os.uname().release}) can no longer load {', '.join(missing)}: its "
                        "modules were removed from disk (kernel or driver update without a reboot). Reboot first")
    try:
        root = ET.fromstring(xml) if xml else None
    except ET.ParseError:
        root = None
    if root is not None:
        networks = [s.get("network") for s in root.findall("./devices/interface[@type='network']/source")]
        nics = [s.get("dev") for s in root.findall("./devices/interface[@type='direct']/source")]
        paths = [s.get("dev") for kind in ("input[@type='evdev']", "disk[@type='block']")
                 for s in root.findall(f"./devices/{kind}/source")]
        tpm, kvmfr = root.find("./devices/tpm") is not None, "/dev/kvmfr0" in xml
    else:
        networks = wanted_networks(cfg)
        nics = [cfg["network"]["host_nic"]] if cfg["network"]["mode"] == "macvtap" else []
        paths = []  # render_xml leaves out what is missing; gamevm start asks about the keyboard
        problem = boot_disk_problem(cfg)  # ...except the disk Windows starts from
        if problem:
            problems.append(problem)
        tpm, kvmfr = cfg["firmware"]["tpm"], True
    for net in filter(None, networks):
        if not network_active(net):
            problems.append(f"the libvirt network '{net}' is not running (sudo virsh net-start {net})")
    for nic in filter(None, nics):
        if not os.path.exists(f"/sys/class/net/{nic}"):
            problems.append(f"the network interface {nic} does not exist (network.host_nic; see ip -br link)")
    for path in filter(None, paths):
        if not os.path.exists(path):
            problems.append(f"{path} is part of the VM but not there (unplugged, or a wireless receiver asleep?)")
    if tpm and not shutil.which("swtpm"):
        problems.append("swtpm is not installed (pacman -S swtpm)")
    if kvmfr:
        try:
            check_kvmfr(cfg, mode)
        except GameVMError as e:
            problems.append(str(e))
    return problems


def problems_error(problems: list[str]) -> GameVMError:
    text = problems[0] if len(problems) == 1 else "\n  - " + "\n  - ".join(problems)
    return GameVMError(f"Windows cannot start yet, nothing was changed: {text}")


def prepare(cfg: dict, mode: str | None = None) -> HostState:
    mode = mode or resolve_mode(cfg)
    old = HostState.load()
    if old and old.data["steps"]:
        log("stale state from a previous run found, undoing it first", "warn")
        undo(old.data.get("cfg") or cfg, old)
    # The effective config is saved with the journal, so the undo always uses the values the
    # changes were made with, even if /etc/gamevm/gamevm.toml is edited (or broken) meanwhile.
    st = HostState({"version": 1, "mode": mode, "started": time.time(), "cfg": cfg, "steps": []})
    st.save()
    log(f"preparing host for {cfg['vm']['name']} (mode: {mode})")
    try:
        verify_identity(cfg)
        problems = start_problems(cfg, mode, DOMAIN_XML)
        if problems and DRY_RUN:
            log(f"[dry-run] would refuse: {problems_error(problems)}", "warn")
        elif problems:
            raise problems_error(problems)
        # Guest RAM before anything else is touched: this is the step that falls short most often,
        # and gamevm start then starts once more on transparent huge pages. Completed after the unload.
        step_hugepages(cfg, st)
        step_inhibit_sleep(cfg, st)
        step_disk(cfg, st)
        step_gpu_services(cfg, st)
        if mode == "single":
            step_desktop_teardown(cfg, st)
        else:
            step_desktop_to_igpu(cfg, st)
        step_console(cfg, st, mode)
        step_unload_driver(cfg, st, mode)
        if mode == "single":
            restart_user_audio(st)  # the VM's sound goes through PipeWire; the GPU is gone now
        step_audio_access(cfg, st)
        step_hugepages_finish(cfg, st)
        step_isolate(cfg, st)
        step_tuning(cfg, st)
        step_input_watch(cfg, st)
    except Exception as e:
        log(f"start aborted: {e}", "error")
        log("rolling back host changes")
        undo(cfg, st)
        if HUGEPAGES_SHORT not in str(e):  # gamevm start answers that one by starting once more
            notify_user(cfg, "Windows did not start", str(e))
        raise
    for step in st.data["steps"]:  # reopen the apps closed by the desktop restart, now on the iGPU
        if step["kind"] == "desktop_igpu":
            try:
                relaunch_apps(pwd.getpwnam(step["user"]), step["relaunch"])
                restore_windows(cfg, pwd.getpwnam(step["user"]), step.get("windows"))
            except Exception as e:  # noqa: BLE001 - the host is ready: never fail the start for this
                log(f"could not reopen apps: {e}", "warn")
            step["reopened"] = True
            st.save()
    log("host prepared")
    return st


def undo(cfg: dict, st: HostState) -> None:
    """Undo recorded steps in reverse order. Errors are logged, never fatal."""
    for step in reversed(st.data.get("steps", [])):
        kind = step["kind"]
        try:
            if kind == "isolated":
                for unit in step["units"]:
                    run(["systemctl", "set-property", "--runtime", "--", unit, f"AllowedCPUs={step['all']}"])
            elif kind == "workqueue":
                write_sysfs(WORKQUEUE_MASK, step["previous"])
            elif kind == "idle_blocked":
                for state in step["states"]:
                    write_sysfs(f"{state}/disable", "0")
            elif kind == "irq_affinity":
                write_sysfs("/proc/irq/default_smp_affinity", step["default"])
                for f, prev in step["irqs"].items():
                    try:
                        write_sysfs(f, prev)
                    except OSError:
                        pass  # the IRQ went away (e.g. the VM's VFIO vectors)
            elif kind == "hugepages":
                write_sysfs(HUGEPAGES_2M, str(step["previous"]))
            elif kind == "modules_unloaded":
                reattach_gpu(cfg, step["modules"])
            elif kind == "driver_override":
                for bdf in step["devices"]:
                    if pci_exists(bdf):
                        write_sysfs(SYS_PCI / bdf / "driver_override", "\n")
            elif kind == "fb_unbound":
                path = f"/sys/bus/platform/drivers/{step['driver']}/bind"
                try:
                    write_sysfs(path, step["device"])
                except OSError:
                    pass  # normal when a DRM driver owns the framebuffer again
            elif kind == "vtcon_unbound":
                for vt in step["paths"]:
                    write_sysfs(f"{vt}/bind", "1")
            elif kind == "desktop_igpu":
                desktop_to_native(cfg, step, may_stay=bool(st.data.get("release")))
            elif kind == "user_units_stopped":
                user_systemctl(step["user"], "start", *step["units"])
            elif kind == "monitor_paused":
                resume_gpu_monitors([(step["user"], step["unit"])], restart=True)
            elif kind == "indexer_stopped":
                if step["user"] == "root":
                    run(["systemctl", "start", step["unit"]], check=False)
                else:
                    user_systemctl(step["user"], "start", step["unit"])
            elif kind == "dm_stopped":
                run(["systemctl", "start", step["unit"]], check=False)
            elif kind == "service_stopped":
                run(["systemctl", "start", step["unit"]], check=False)
            elif kind == "unmounted":
                if cfg["disk"]["remount_after_stop"]:
                    remount(step)
            elif kind == "automount_stopped":
                run(["udevadm", "settle", "--timeout=10"], check=False)
                path = step.get("path")
                deadline = time.monotonic() + 10
                while (path and not DRY_RUN and not real_mount_at(path) and fuse_server_for(path)
                       and time.monotonic() < deadline):
                    time.sleep(0.5)
                if path and not DRY_RUN and not real_mount_at(path) and fuse_server_for(path):
                    log(f"{path} is not brought back yet: a program still has files open on a copy of it that "
                        "was unmounted meanwhile, and mounting it a second time could damage that file system. "
                        f"Close that program, then: sudo systemctl start {step['unit']}", "error")
                    notify_user(cfg, f"{path} is not available yet", "A program still has files open on it from "
                                f"before. Close it, then run: sudo systemctl start {step['unit']}", critical=True)
                else:
                    run(["systemctl", "start", step["unit"]], check=False)
            elif kind == "pipewire_bound":
                res = run(["umount", step["target"]], check=False, capture=True)
                if res.returncode != 0 and real_mount_at(step["target"]):
                    log(f"could not unmount {step['target']}: {(res.stderr or '').strip()}", "warn")
            elif kind == "acl_added":  # journals written by gamevm before 2026-11
                run(["setfacl", "-x", f"u:{step['user']}", step["path"]], check=False)
            elif kind == "input_watch":
                run(["systemctl", "stop", step["unit"]], check=False, capture=True)
            elif kind == "sleep_inhibited":
                run(["systemctl", "stop", step["unit"]], check=False)
        except Exception as e:  # noqa: BLE001 - keep undoing the rest
            log(f"undo of {kind} failed: {e}", "warn")
    st.clear()


def reattach_gpu(cfg: dict, modules: list[str]) -> None:
    """Give the GPU back to the host driver. Order matters (NVIDIA open-gpu-kernel-modules #1365:
    nvidia must never be active on the GPU while its HDMI-audio function sits on vfio-pci):
    audio function back to its host driver first, NVIDIA driver last."""
    devices = cfg["gpu"]["devices"]
    vga, others = devices[0], devices[1:]
    for bdf in others:  # HDMI audio etc. first
        if not pci_exists(bdf):
            continue
        if pci_driver(bdf) == "vfio-pci":
            write_sysfs("/sys/bus/pci/drivers/vfio-pci/unbind", bdf)
        write_sysfs(SYS_PCI / bdf / "driver_override", "\n")
        if DRY_RUN or pci_driver(bdf) is None:
            write_sysfs("/sys/bus/pci/drivers_probe", bdf)
    if not DRY_RUN and any(pci_exists(b) and pci_driver(b) == "vfio-pci" for b in others):
        log("the GPU's audio function is still on vfio-pci; NOT loading the NVIDIA driver "
            "(NVIDIA open-gpu-kernel-modules #1365 panics in that state)", "error")
        return
    if pci_exists(vga):
        if pci_driver(vga) == "vfio-pci":
            write_sysfs("/sys/bus/pci/drivers/vfio-pci/unbind", vga)
        write_sysfs(SYS_PCI / vga / "driver_override", "\n")
    for m in reversed(modules):
        res = run(["modprobe", m], check=False, capture=True)
        if res.returncode != 0:
            log(f"modprobe {m} failed: {(res.stderr or '').strip()}", "error")
    if pci_exists(vga) and (DRY_RUN or pci_driver(vga) is None):
        write_sysfs("/sys/bus/pci/drivers_probe", vga)


def remount(step: dict) -> None:
    """Remount from fstab once the disk is back on the host (it reappears asynchronously).
    Paths with an fstab automount are left to the automount (mounted again on first access)."""
    automount, _ = mount_units(step["mountpoint"])
    if subprocess.run(["systemctl", "cat", automount], capture_output=True).returncode == 0:
        run(["udevadm", "settle", "--timeout=10"], check=False)
        run(["systemctl", "start", automount], check=False)  # no-op when already active
        return
    dev = f"/dev/{step['dev']}"
    for _ in range(40):
        if DRY_RUN or os.path.exists(dev):
            break
        time.sleep(0.25)
    run(["udevadm", "settle", "--timeout=10"], check=False)
    res = run(["mount", step["mountpoint"]], check=False, capture=True)
    if res.returncode != 0:
        log(f"could not remount {step['mountpoint']}: {(res.stderr or '').strip()}", "warn")


def restart_user_audio(st: HostState) -> None:
    for step in st.data["steps"]:
        if step["kind"] == "user_units_stopped":
            user_systemctl(step["user"], "start", *step["units"])


def gpu_reinit_ok(cfg: dict, since: float) -> bool:
    """After the NVIDIA driver is reloaded, make the GPU initialise (nvidia-smi) and look for
    the known GSP failure that only a host reboot fixes (seen after unclean guest stops)."""
    vga = cfg["gpu"]["devices"][0]
    if DRY_RUN or pci_attr(vga, "vendor") != "0x10de":
        return True
    if not module_loaded("nvidia") or pci_driver(vga) != "nvidia":
        return False
    ok = True
    if shutil.which("nvidia-smi"):
        try:
            ok = run(["nvidia-smi", "-L"], check=False, capture=True, mutate=False, timeout=30).returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
    since_arg = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    klog = run(["journalctl", "-k", "-q", "--no-pager", "--since", since_arg, "-g", GSP_FAILURE_RE],
               check=False, capture=True, mutate=False).stdout.strip()
    return ok and not klog


def release(cfg: dict, st: HostState | None = None) -> None:
    st = st or HostState.load()
    if not st:
        return
    if host_going_down() and not DRY_RUN:
        # Everything prepare changed lives in /run, sysfs or kernel memory. Restarting KWin, loading
        # the NVIDIA driver and mounting disks now would only hold the shutdown up.
        log(f"{cfg['vm']['name']} has stopped and the host is shutting down: the host is left as it is")
        st.clear()
        return
    log(f"restoring host after {cfg['vm']['name']}")
    mode = st.data.get("mode")
    started = st.data.get("started", time.time() - 600)
    reloads_gpu = any(s["kind"] == "modules_unloaded" for s in st.data["steps"])
    st.data["release"] = True  # Windows stopped by itself: desktop.return = "ask" may apply
    undo(cfg, st)
    if mode == "igpu" and cfg["display"]["switch_inputs"] and cfg["display"]["monitor_model"] \
            and shutil.which("ddcutil"):
        # Also when Windows was shut down from inside. Detached: DDC can take seconds.
        run(["systemd-run", "--no-block", "--quiet", "--collect", "ddcutil", "--model",
             cfg["display"]["monitor_model"], "--noverify", "setvcp", "60", cfg["display"]["host_input"]],
            check=False, capture=True)
    if reloads_gpu and not gpu_reinit_ok(cfg, started):
        log("the NVIDIA GPU did not come back after the VM (driver failed to load, or GSP firmware "
            "stuck). Only a host reboot fixes this; shut Windows down from inside Windows to avoid it.", "error")
        if not DRY_RUN:
            REBOOT_MARKER.touch()
        if mode == "single" and cfg["single_gpu"]["reboot_on_gpu_failure"]:
            log("single-GPU mode: no display possible without the GPU, rebooting the host", "error")
            run(["systemctl", "reboot"], check=False)
            return
        notify_user(cfg, "The graphics card did not come back",
                    "Windows has shut down, but the NVIDIA driver could not take the card back. "
                    + ("The Linux desktop stays on the side monitor. " if mode == "igpu" else "")
                    + "Reboot the PC to get it back.", critical=True)
        return
    log("host restored")


def cmd_hook(args: argparse.Namespace) -> int:
    """libvirt: /etc/libvirt/hooks/qemu <domain> <operation> <sub-operation> <extra>."""
    argv = args.hookargs
    global DOMAIN_XML
    try:
        DOMAIN_XML = sys.stdin.read()  # libvirt writes the domain XML to stdin
    except (OSError, ValueError):
        pass
    if len(argv) < 3:
        return 0
    op = (argv[1], argv[2])
    try:
        return hook_dispatch(argv, op)
    except BaseException as e:  # noqa: BLE001 - only a failed prepare of OUR VM may return non-zero
        log(f"unexpected error in hook {op}: {e!r}", "error")
        ours = re.search(r'^\s*name\s*=\s*"([^"]+)"', read(ETC_CONFIG), re.M)
        return 1 if op == ("prepare", "begin") and ours and ours.group(1) == argv[0] else 0


def hook_dispatch(argv: list[str], op: tuple[str, str]) -> int:
    if op == ("release", "end"):
        st = HostState.load()
        snap = st.data.get("cfg") if st else None
        if snap and snap.get("vm", {}).get("name") == argv[0]:
            release(snap, st)
            return 0
    if not ETC_CONFIG.exists():
        return 0
    try:
        cfg = load_config(ETC_CONFIG, trusted=True)
    except Exception as e:  # noqa: BLE001
        # Only block the start of OUR VM (best-effort name lookup); never other VMs or other ops.
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', read(ETC_CONFIG), re.M)
        if m and m.group(1) == argv[0]:
            log(str(e), "error")
            return 1 if argv[1] == "prepare" else 0
        return 0
    if argv[0] != cfg["vm"]["name"]:
        return 0
    try:
        if op == ("prepare", "begin"):
            prepare(cfg)
        elif op == ("release", "end"):
            release(cfg)
    except GameVMError as e:
        print(f"gamevm: {e}", file=sys.stderr)
        return 1 if op[0] == "prepare" else 0
    except Exception as e:  # noqa: BLE001 - libvirt only shows stderr, make it useful
        log(f"unexpected error in hook {op}: {e!r}", "error")
        return 1 if op[0] == "prepare" else 0
    return 0


# ---------------------------------------------------------------------------------------------
# libvirt domain XML
# ---------------------------------------------------------------------------------------------

SMBIOS_KEYS = {
    "bios": ("vendor", "version", "date", "release"),
    "system": ("manufacturer", "product", "version", "serial", "uuid", "sku", "family"),
    "baseBoard": ("manufacturer", "product", "version", "serial", "asset", "location"),
    "chassis": ("manufacturer", "version", "serial", "asset", "sku"),
}


def load_smbios() -> dict | None:
    """Host SMBIOS identity captured by the installer (dmidecode needs root, the file is 0640
    root:libvirt). Copying it, including the baseboard and the system UUID, lets Windows keep its
    digital licence when the same install boots in the VM."""
    try:
        with ETC_SMBIOS.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None
    except PermissionError:
        raise GameVMError(f"cannot read {ETC_SMBIOS} (0640 root:libvirt): this session is not in the "
                          "libvirt group yet. Reboot once after setup (with lingering, logging out is "
                          "not enough), or run with sudo") from None
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise GameVMError(f"{ETC_SMBIOS}: {e}") from None


def sysinfo_xml(smb: dict, uuid: str) -> list[str]:
    out = ["  <sysinfo type='smbios'>"]
    for block, keys in SMBIOS_KEYS.items():
        vals = smb.get(block, {})
        entries = []
        for k in keys:
            v = uuid if (block, k) == ("system", "uuid") else str(vals.get(k, "")).strip()
            if v:
                entries.append(f"      <entry name='{k}'>{escape(v)}</entry>")
        if entries:
            out += [f"    <{block}>", *entries, f"    </{block}>"]
    out.append("  </sysinfo>")
    return out


def pci_addr_xml(bdf: str) -> str:
    dom, bus, rest = bdf.split(":")
    slot, fn = rest.split(".")
    return f"<address domain='0x{dom}' bus='0x{bus}' slot='0x{slot}' function='0x{fn}'/>"


def render_xml(cfg: dict, mode: str | None = None) -> str:
    mode = mode or resolve_mode(cfg)
    plan = cpu_plan(cfg)
    vm, gpu, fw, net, lg = cfg["vm"], cfg["gpu"], cfg["firmware"], cfg["network"], cfg["looking_glass"]
    vcpus = len(plan["guest"])
    uuid = vm["uuid"] or derived_uuid(cfg)
    use_lg = lg["enabled"] and mode == "igpu"
    x: list[str] = []
    a = x.append

    a("<domain type='kvm' xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>")
    a(f"  <name>{escape(vm['name'])}</name>")
    a(f"  <uuid>{uuid}</uuid>")
    a(f"  <title>{escape(vm['title'])}</title>")
    a(f"  <description>Generated by gamevm {VERSION} (mode={mode}) from {escape(cfg['_path'])}. "
      "Edit that file and run `gamevm define` instead of editing this XML.</description>")
    a(f"  <memory unit='MiB'>{vm['memory_mib']}</memory>")
    a(f"  <currentMemory unit='MiB'>{vm['memory_mib']}</currentMemory>")
    a("  <memoryBacking>")
    if vm["hugepages"]:
        a("    <hugepages><page size='2048' unit='KiB'/></hugepages>")
    a("    <nosharepages/>")
    a(f"    <allocation mode='immediate' threads='{min(4, len(plan['emulator']))}'/>")
    a("  </memoryBacking>")
    a(f"  <vcpu placement='static'>{vcpus}</vcpu>")
    a("  <cputune>")
    for i, cpu in enumerate(plan["guest"]):
        a(f"    <vcpupin vcpu='{i}' cpuset='{cpu}'/>")
    a(f"    <emulatorpin cpuset='{cpuset(plan['emulator'])}'/>")
    a("  </cputune>")

    smbios = load_smbios() if fw["smbios_host"] else None
    if smbios:
        x.extend(sysinfo_xml(smbios, uuid))

    # Firmware: OVMF picked by libvirt from the /usr/share/qemu/firmware (and /etc/qemu/firmware)
    # descriptors. Secure Boot needs the Microsoft-keyed descriptor the installer creates.
    a("  <os firmware='efi'>")
    a("    <type arch='x86_64' machine='q35'>hvm</type>")
    a("    <firmware>")
    a(f"      <feature enabled='{'yes' if fw['secure_boot'] else 'no'}' name='secure-boot'/>")
    if fw["secure_boot"]:
        a("      <feature enabled='yes' name='enrolled-keys'/>")
    a("    </firmware>")
    if smbios:
        a("    <smbios mode='sysinfo'/>")
    elif fw["smbios_host"]:
        a("    <smbios mode='host'/>")  # fallback: copies BIOS + system blocks (not the UUID/baseboard)
    a("    <bootmenu enable='no'/>")
    a("  </os>")

    a("  <features>")
    a("    <acpi/>")
    a("    <apic/>")
    a("    <hyperv mode='custom'>")
    # Explicit list. Left out on purpose: evmcs (Intel only), reenlightenment / tlbflush-direct /
    # emsr_bitmap (nested only), reset, vendor_id and <kvm><hidden> (not needed since NVIDIA R465).
    hv = ["<relaxed state='on'/>", "<vapic state='on'/>", "<spinlocks state='on' retries='8191'/>",
          "<vpindex state='on'/>", "<runtime state='on'/>", "<synic state='on'/>",
          "<stimer state='on'><direct state='on'/></stimer>", "<frequencies state='on'/>",
          "<tlbflush state='on'><extended state='on'/></tlbflush>", "<ipi state='on'/>",
          "<xmm_input state='on'/>"]
    if cfg["tuning"]["avic"]:
        hv.append("<avic state='on'/>")  # stops Windows using SynIC AutoEOI, which inhibits AVIC
    for line in hv:
        a(f"      {line}")
    a("    </hyperv>")
    a("    <vmport state='off'/>")
    if fw["secure_boot"]:
        a("    <smm state='on'/>")
    a("    <ioapic driver='kvm'/>")
    a("  </features>")

    a("  <cpu mode='host-passthrough' check='none' migratable='off'>")
    a(f"    <topology sockets='1' dies='1' clusters='1' cores='{plan['cores']}' threads='{plan['threads']}'/>")
    a("    <cache mode='passthrough'/>")
    a("    <maxphysaddr mode='passthrough'/>")
    a("    <feature policy='require' name='topoext'/>")
    a("    <feature policy='require' name='invtsc'/>")
    # No nested virtualisation: stops Windows from starting Hyper-V/VBS inside the VM.
    a("    <feature policy='disable' name='svm'/>")
    if cfg["tuning"]["avic"]:
        # This CPU only offers xAVIC (x2AVIC is hidden), so keep the guest in xAPIC mode.
        a("    <feature policy='disable' name='x2apic'/>")
    a("  </cpu>")

    a(f"  <clock offset='{vm['clock']}'>")
    a("    <timer name='rtc' tickpolicy='catchup'/>")
    a("    <timer name='pit' tickpolicy='discard'/>")  # 'delay' keeps PIT reinjection on, which inhibits AVIC
    a("    <timer name='hpet' present='no'/>")
    a("    <timer name='hypervclock' present='yes'/>")
    a("  </clock>")
    a("  <on_poweroff>destroy</on_poweroff>")
    a("  <on_reboot>restart</on_reboot>")
    a("  <on_crash>destroy</on_crash>")
    a("  <pm>")
    a("    <suspend-to-mem enabled='no'/>")
    a("    <suspend-to-disk enabled='no'/>")
    a("  </pm>")

    a("  <devices>")
    a("    <emulator>/usr/bin/qemu-system-x86_64</emulator>")
    # Passed-through PCI devices sit on their own root ports with hot-plug off, so Windows does
    # not offer to "safely remove" the GPU. GPU functions share one slot, like on real hardware.
    def root_port(index: int) -> None:
        a(f"    <controller type='pci' model='pcie-root-port' index='{index}'><target hotplug='off'/></controller>")

    def port_addr(index: int) -> str:
        return f"<address type='pci' domain='0x0000' bus='0x{index:02x}' slot='0x00' function='0x0'/>"

    gpu_bus = 1
    root_port(gpu_bus)
    for i, bdf in enumerate(gpu["devices"]):
        fn = int(bdf.split(".")[1])
        multi = " multifunction='on'" if i == 0 and len(gpu["devices"]) > 1 else ""
        a("    <hostdev mode='subsystem' type='pci' managed='yes'>")
        a(f"      <source>{pci_addr_xml(bdf)}</source>")
        a(f"      <address type='pci' domain='0x0000' bus='0x{gpu_bus:02x}' slot='0x00' function='0x{fn}'{multi}/>")
        a("    </hostdev>")
    # Windows NVMe: boots first.
    port = gpu_bus + 1
    if cfg["disk"]["nvme"]:
        root_port(port)
        a("    <hostdev mode='subsystem' type='pci' managed='yes'>")
        a(f"      <source>{pci_addr_xml(cfg['disk']['nvme'])}</source>")
        a("      <boot order='1'/>")
        a("      <rom bar='off'/>")  # OVMF has its own NVMe driver
        a(f"      {port_addr(port)}")
        a("    </hostdev>")
        port += 1
    for bdf in cfg["usb"]["controllers"]:
        root_port(port)
        a("    <hostdev mode='subsystem' type='pci' managed='yes'>")
        a(f"      <source>{pci_addr_xml(bdf)}</source>")
        a(f"      {port_addr(port)}")
        a("    </hostdev>")
        port += 1

    # virtio-win driver CD, attached only while the ISO exists (remove the file to detach it).
    # startupPolicy='optional': starts started outside gamevm (virt-manager) survive a deleted ISO.
    for iso, target, boot in ((vm["virtio_iso"], "sdb", ""), (vm["install_iso"], "sdc", "<boot order='2'/>")):
        if iso and os.path.isfile(iso):
            a("    <disk type='file' device='cdrom'>")
            a("      <driver name='qemu' type='raw'/>")
            a(f"      <source file={quoteattr(iso)} startupPolicy='optional'/>")
            a(f"      <target dev='{target}' bus='sata'/>")
            a("      <readonly/>")
            if boot:
                a(f"      {boot}")
            a("    </disk>")
    # disk.boot_disk: Windows on a SATA disk. The whole host disk, raw, on the machine's emulated
    # SATA (AHCI) controller: Windows starts from that with the driver every installation has
    # (storahci), where VirtIO would need a driver that is not there before the first start. It is
    # always part of the definition, also when missing: a VM without its system disk must not start.
    boot = cfg["disk"]["boot_disk"]
    if boot:
        f = whole_disk_facts(boot) or {"rotating": False, "physical": "512", "serial": ""}
        a("    <disk type='block' device='disk'>")
        a("      <driver name='qemu' type='raw' cache='none' io='native'" + ("" if f["rotating"] else " discard='unmap'") + "/>")
        a(f"      <source dev={quoteattr(boot)}/>")
        a(f"      <target dev='sda' bus='sata' rotation_rate='{7200 if f['rotating'] else 1}'/>")
        a(f"      <blockio logical_block_size='512' physical_block_size='{f['physical']}'/>")
        if f["serial"]:
            a(f"      <serial>{escape(f['serial'][:20])}</serial>")
        a("      <boot order='1'/>")
        a("    </disk>")
    # disk.extra: whole host disks (e.g. the shared games HDD), raw on VirtIO SCSI, presented like
    # the real drive (serial, sector sizes, spinning or not) so Windows keeps its drive letter.
    extras = [p for p in cfg["disk"]["extra"] if os.path.exists(p)]
    if extras:
        a("    <controller type='scsi' index='0' model='virtio-scsi'/>")
    for i, path in enumerate(extras):
        name = os.path.basename(os.path.realpath(path))
        q = Path(f"/sys/block/{name}/queue")
        rotating = read(q / "rotational") == "1"
        serial = disk_serial(path)
        a("    <disk type='block' device='disk'>")
        a("      <driver name='qemu' type='raw' cache='none' io='native'" + ("" if rotating else " discard='unmap'") + "/>")
        a(f"      <source dev={quoteattr(path)}/>")
        a(f"      <target dev='sd{chr(ord('d') + i)}' bus='scsi' rotation_rate='{7200 if rotating else 1}'/>")
        a(f"      <blockio logical_block_size='{read(q / 'logical_block_size') or 512}' "
          f"physical_block_size='{read(q / 'physical_block_size') or 512}'/>")
        if serial:
            a(f"      <serial>{escape(serial[:20])}</serial>")
        a("    </disk>")
    a("    <controller type='usb' model='qemu-xhci' ports='15'/>")
    for dev in cfg["usb"]["devices"]:
        vid, pid = dev.lower().split(":")
        a("    <hostdev mode='subsystem' type='usb' managed='yes'>")
        a(f"      <source startupPolicy='optional'><vendor id='0x{vid}'/><product id='0x{pid}'/></source>")
        a("    </hostdev>")

    # Network
    mac = net["mac"] or derived_mac(cfg, 0)
    if net["mode"] == "macvtap":
        # trustGuestRxFilters: sync Windows' multicast list onto the macvtap (IPv6 neighbour
        # discovery, mDNS, SSDP); without it that LAN multicast is filtered out.
        a("    <interface type='direct' trustGuestRxFilters='yes'>")
        a(f"      <mac address='{mac}'/>")
        a(f"      <source dev={quoteattr(net['host_nic'])} mode='bridge'/>")
    else:
        a("    <interface type='network'>")
        a(f"      <mac address='{mac}'/>")
        a("      <source network='default'/>")
    a("      <model type='virtio'/>")
    a(f"      <driver name='vhost' queues='{net['queues']}'/>")
    a("    </interface>")
    if net["hostonly"]:
        a("    <interface type='network'>")
        a(f"      <mac address='{net['hostonly_mac'] or derived_mac(cfg, 1)}'/>")
        a(f"      <source network={quoteattr(net['hostonly_network'])}/>")
        a("      <model type='virtio'/>")
        a("    </interface>")

    # Input
    # A missing node would make the start fail (e.g. a wireless receiver that has not woken up
    # yet), so it is left out with a warning (and plugged in later by the input watcher).
    for _role, dev, xml in evdev_inputs(cfg):
        if os.path.exists(dev):
            a(f"    {xml}")
        else:
            log(f"input device {dev} not found, starting without it", "warn")
    a("    <input type='keyboard' bus='virtio'/>")
    a("    <input type='mouse' bus='virtio'/>")

    # Audio
    if cfg["audio"]["backend"] == "pipewire":
        u = audio_user(cfg)
        # QEMU as its own system user gets just the PipeWire socket, in PIPEWIRE_DIR
        # (step_audio_access). Running as the desktop user it uses the session's own directory.
        runtime = f"/run/user/{u.pw_uid}" if u and qemu_process_user() == u.pw_name else str(PIPEWIRE_DIR)
        lat = cfg["audio"]["latency_us"]
        a("    <sound model='ich9'><audio id='1'/></sound>")  # Windows has no virtio-sound driver
        a(f"    <audio id='1' type='pipewire' runtimeDir='{runtime}'>")
        a(f"      <input streamName='{escape(vm['name'])} microphone' latency='{lat}'/>")
        a(f"      <output streamName='{escape(vm['name'])}' latency='{lat}'/>")
        a("    </audio>")

    # Looking Glass: SPICE for input/clipboard, no emulated video so the graphics card is the only display.
    if use_lg:
        a(f"    <graphics type='spice' port='{lg['spice_port']}' autoport='no'>")
        a("      <listen type='address' address='127.0.0.1'/>")
        a("      <image compression='off'/>")
        a("      <gl enable='no'/>")
        a("    </graphics>")
        a("    <channel type='spicevmc'><target type='virtio' name='com.redhat.spice.0'/></channel>")
        if not lg["kvmfr"]:
            a("    <shmem name='looking-glass'>")
            a("      <model type='ivshmem-plain'/>")
            a(f"      <size unit='M'>{lg['size_mib']}</size>")
            a("    </shmem>")
    # No emulated display adapter by default: the graphics card (and the Looking Glass IDD) are Windows'
    # only displays. rescue_vga adds a QEMU VGA for virt-manager's console (never virtio-gpu:
    # the Looking Glass IDD could pick it as its render adapter).
    if vm["rescue_vga"]:
        if not use_lg:  # something to show it with (virt-manager / any VNC viewer on 127.0.0.1)
            a("    <graphics type='vnc' autoport='yes'><listen type='address' address='127.0.0.1'/></graphics>")
        a("    <video><model type='vga' vram='16384' heads='1' primary='yes'/></video>")
    else:
        a("    <video><model type='none'/></video>")
    a("    <channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0'/></channel>")
    if fw["tpm"]:
        a("    <tpm model='tpm-crb'><backend type='emulator' version='2.0'/></tpm>")
    a("    <memballoon model='none'/>")
    a("  </devices>")

    qargs: list[str] = []
    if cfg["tuning"]["guest_idle_poll"]:
        # Guest idles without exiting: lowest wake-up latency, but its cores show 100 % busy on the host.
        qargs += ["-overcommit", "cpu-pm=on"]
    if gpu["mmio64_mib"]:
        qargs += ["-fw_cfg", f"name=opt/ovmf/X-PciMmio64Mb,string={gpu['mmio64_mib']}"]
    if use_lg and lg["kvmfr"]:
        size = lg["size_mib"] * 1024 * 1024
        qargs += ["-device", '{"driver":"ivshmem-plain","id":"shmem0","memdev":"looking-glass"}',
                  "-object", '{"qom-type":"memory-backend-file","id":"looking-glass",'
                             f'"mem-path":"/dev/kvmfr0","size":{size},"share":true}}']
    if qargs:
        a("  <qemu:commandline>")
        for q in qargs:
            a(f"    <qemu:arg value={quoteattr(q)}/>")
        a("  </qemu:commandline>")
    a("</domain>")
    return "\n".join(x) + "\n"


def check_kvmfr(cfg: dict, mode: str) -> None:
    lg = cfg["looking_glass"]
    if not (lg["enabled"] and lg["kvmfr"] and mode == "igpu"):
        return
    import stat as _stat
    try:
        st = os.stat("/dev/kvmfr0")
    except FileNotFoundError:
        raise GameVMError("/dev/kvmfr0 does not exist: the kvmfr module is not loaded "
                          "(run looking-glass/install.sh, or set looking_glass.enabled = false)") from None
    if not _stat.S_ISCHR(st.st_mode):
        raise GameVMError("/dev/kvmfr0 is a regular file (QEMU created it because kvmfr was not loaded). "
                          "Fix: sudo rm /dev/kvmfr0 && sudo modprobe kvmfr")
    size = read("/sys/module/kvmfr/parameters/static_size_mb").split(",")[0]
    if size and int(size) != lg["size_mib"]:
        raise GameVMError(f"/dev/kvmfr0 is {size} MiB but looking_glass.size_mib is {lg['size_mib']}: "
                          "edit /etc/modprobe.d/kvmfr.conf to match, then reload kvmfr")


def define(cfg: dict, mode: str | None = None) -> None:
    mode = mode or resolve_mode(cfg)
    check_kvmfr(cfg, mode)
    xml = render_xml(cfg, mode)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
        f.write(xml)
        path = f.name
    try:
        virsh("define", path)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------------------------
# user commands
# ---------------------------------------------------------------------------------------------

def print_holders(holders: list[dict]) -> None:
    for h in holders:
        note = "  (logind's copy of the desktop's DRM device: released when KWin lets go)" if session_device_copy(h) else ""
        print(f"  {h['pid']:>7}  {h['user']:<12} {h['comm']:<18} {', '.join(h['nodes'])}{note}")
        print(f"           {h['cmdline']}")


def close_holders(holders: list[dict], timeout: float = 15.0) -> None:
    if DRY_RUN:
        for h in holders:
            print(f"[dry-run] kill -TERM {h['pid']} ({h['comm']})", file=sys.stderr)
        return
    if any(h["comm"] == "steam" for h in holders) and shutil.which("steam"):
        subprocess.run(["steam", "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for h in holders:
        if h["comm"] != "steam":
            try:
                os.kill(h["pid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not find_holders({n for h in holders for n in h["nodes"]}):
            return
        time.sleep(0.5)


def switch_monitor(cfg: dict, target: str) -> bool:
    """Switch the monitor wired to both GPUs to the VM's or the host's input over DDC/CI.
    Best effort: many monitors ignore DDC commands sent to an input they are not showing."""
    d = cfg["display"]
    if not d["monitor_model"] or not shutil.which("ddcutil"):
        return False
    value = d["vm_input"] if target == "vm" else d["host_input"]
    res = run(["ddcutil", "--model", d["monitor_model"], "--noverify", "setvcp", "60", value],
              check=False, capture=True, mutate=True, timeout=20)
    return res.returncode == 0


def cmd_view(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if switch_monitor(cfg, args.target):
        print(f"switched {cfg['display']['monitor_model']} to the {args.target} input")
        return 0
    print("DDC/CI input switch failed (is ddcutil installed, and does the monitor accept DDC on this input?). "
          "Use the monitor's OSD button instead.", file=sys.stderr)
    return 1


def closing_apps(cfg: dict, u: pwd.struct_passwd) -> list[str]:
    """For the confirmation prompt, with the classification the hook uses: the user's apps that
    a KWin restart closes (GUI programs that are not Qt 6 Wayland clients, apps that keep the
    graphics card open) and programs started in a terminal that use it (the terminal stays)."""
    names, conns = set(), display_conns()
    for unit, info in app_units(u).items():
        if any(dies_with_kwin(pid, conns) for pid in info["pids"]):
            names.add(info["app"])
    stop, single = classify_app_holders(u, nvidia_holders(cfg))
    names.update(unit_app(unit)[0] or unit for unit in stop)
    names.update(f"{h['comm']} (started in a terminal)" for h in single)
    return sorted(names)


def launch_vm(start_args: list[str], detached: bool) -> tuple[bool, str]:
    """virsh start. detached: in its own session, output to a file, because the hook is about to
    close this desktop (single) or restart KWin, which closes non-KDE terminals."""
    if not detached:
        res = virsh(*start_args, check=False, timeout=None)
        return res.returncode == 0, (res.stderr or "").strip()
    logf = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "gamevm-start.log"
    with open(logf, "w") as lf:
        p = subprocess.Popen(["env", "LC_ALL=C", "virsh", "-q", "-c", LIBVIRT_URI, *start_args],
                             start_new_session=True, stdout=lf, stderr=subprocess.STDOUT)
    return p.wait() == 0, read(logf)


def cmd_start(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    name = cfg["vm"]["name"]
    mode = resolve_mode(cfg)
    state = domain_state(name)
    if state == "running":
        print(f"{name} is already running")
        return 0
    if not domain_off(state):
        raise GameVMError(f"{name} is {state}: it still owns the graphics card and the Windows disk. `gamevm stop` shuts "
                          "it down (a paused Windows is resumed first); `gamevm kill` forces it off.")
    if REBOOT_MARKER.exists():
        raise GameVMError("the NVIDIA GPU failed to re-initialise after the last VM run (GSP firmware stuck): "
                          "reboot the host before starting the VM again")
    # Everything that would fail only after the desktop has moved is checked now (the hook checks
    # again, for starts from virt-manager).
    for net in wanted_networks(cfg):
        if not network_active(net):
            print(f"starting the libvirt network {net} ...")
            virsh("net-start", net, check=False)
    problems = start_problems(cfg, mode)
    if problems and DRY_RUN:
        print(f"[dry-run] would refuse: {problems_error(problems)}", file=sys.stderr)
    elif problems:
        raise problems_error(problems)
    inp = cfg["input"]
    if inp["evdev"] and inp["keyboard"] and not os.path.exists(inp["keyboard"]):
        print(f"The keyboard that goes to Windows is not connected:\n  {inp['keyboard']}\n"
              f"Without it Windows gets no keyboard, and {inp['toggle'].replace('-', ' + ')} cannot switch the keyboard\n"
              "and mouse between Windows and Linux. Plug it in (or wake it) and start again. If you use another\n"
              "keyboard now, `sudo gamevm probe-input` finds the node for input.keyboard.")
        if not confirm("Start Windows without a keyboard anyway?", False):  # never implied by --yes
            return 1
    handoff = mode == "igpu" and kwin_managed() and kwin_layout() == "native"
    if handoff:
        u = audio_user(cfg)
        blockers = blocking_holders(cfg, u) if u else []
        if blockers:  # only the user's own are visible here; the hook checks everyone's
            raise blocking_error(blockers)
        closing = closing_apps(cfg, u) if u else []
        reopen = sorted(set(wanted_apps(cfg, u)) & set(closing)) if u else []
        print("Starting Windows hands the graphics card and the main monitor to Windows. The Linux desktop\n"
              "restarts on the side monitor: most KDE apps (Konsole, Dolphin, ...) stay open, other apps\n"
              "close. The same happens again when Windows shuts down and the desktop comes back.")
        if closing:
            print("  Will close:  " + ", ".join(closing))
        if reopen:
            print("  Will reopen: " + ", ".join(reopen))
        if not confirm("Continue?", args.yes or args.close_apps):
            return 1
    elif mode == "igpu":
        # (GPU sensor services are held off by the hook itself: pause_gpu_monitors)
        holders = [h for h in find_holders(gpu_device_nodes(cfg)) if proc_unit(h["pid"]) not in GPU_MONITOR_UNITS]
        compositors = [h for h in holders if h["comm"] in COMPOSITORS[:5]]
        if compositors:
            raise GameVMError(
                "your desktop compositor is running on the NVIDIA GPU and gamevm cannot move it (KWin was "
                "started without gamevm's GPU layout, which gamevm-kwin.service sets up at boot). "
                "Check `sudo gamevm doctor`, then reboot.")
        if os.geteuid() != 0 and module_loaded("nvidia_drm"):
            # KWin and other users' processes are invisible to us; the driver's refcount is not.
            seen = sum(len([n for n in h["nodes"] if "nvidia" in n or "/dri/" in n]) for h in holders)
            if module_refcount("nvidia_drm") > seen + 1 and not holders:
                print("Note: the NVIDIA driver is in use by processes you cannot see (KWin, a system service,"
                      " another user). If the start fails, run: sudo gamevm holders")
        if holders:
            print("These programs are using the NVIDIA GPU and must close first:")
            print_holders(holders)
            if not (args.close_apps or confirm("Close them now?", args.yes)):
                return 1
            close_holders(holders)
            left = [h for h in find_holders(gpu_device_nodes(cfg)) if proc_unit(h["pid"]) not in GPU_MONITOR_UNITS]
            if left:
                print("Still running (close them manually):")
                print_holders(left)
                return 1
    else:
        print("Single-GPU mode: starting Windows closes your Linux desktop session.\n"
              "Unsaved work in Linux apps will be lost. The host keeps running in the background;\n"
              "the desktop comes back when Windows shuts down.")
        if not confirm("Continue?", args.yes):
            return 1
    start_args = ["start", name] + (["--reset-nvram"] if args.reset_nvram else [])
    for fallback in (False, True):
        run_cfg = cfg
        if fallback:
            run_cfg = copy.deepcopy(cfg)
            run_cfg["vm"]["hugepages"] = False
        define(run_cfg, mode)
        print(f"starting {name} (mode: {mode}) ...")
        if DRY_RUN:
            virsh(*start_args, check=False, timeout=None)
            return 0
        ok, out = launch_vm(start_args, detached=mode == "single" or handoff)
        if ok:
            break
        if (not fallback and HUGEPAGES_SHORT in out and cfg["vm"]["hugepages"]
                and cfg["vm"]["hugepages_fallback"]):
            print("Not enough contiguous RAM to reserve Windows' memory as huge pages right now (nothing\n"
                  "was changed): starting it with transparent huge pages instead, slightly slower.")
            continue
        print(out.replace(f" {HUGEPAGES_SHORT}", ""), file=sys.stderr)
        print("Nothing was changed on the host, or it was rolled back. Details: journalctl -t gamevm -b",
              file=sys.stderr)
        return 1
    if mode == "single":
        return 0
    print("Windows is booting on the graphics card." + (" The Linux desktop is now on the side monitor."
                                                   if kwin_managed() else ""))
    if cfg["display"]["switch_inputs"]:
        if switch_monitor(cfg, "vm"):
            print(f"  - {cfg['display']['monitor_model']} switched to the graphics card's input (gamevm view host = back to Linux).")
        else:
            print(f"  - Could not switch {cfg['display']['monitor_model']} over DDC/CI: switch it to the graphics card's "
                  "input by hand (or: gamevm view vm).")
    else:
        print("  - Windows appears on the monitor cabled to the graphics card.")
    print(f"  - Keyboard/mouse: press {cfg['input']['toggle'].replace('-', ' + ')} to switch between Linux and Windows.")
    if cfg["looking_glass"]["enabled"]:
        print("  - Or view it in a window: gamevm lg")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    name = cfg["vm"]["name"]
    state = domain_state(name)
    if domain_off(state):
        print(f"{name} is not running")
        return 0
    if state == "paused":
        print(f"{name} is paused: resuming it so that Windows can shut down")
    print(f"shutting down {name} ...")
    told = []

    def tick(elapsed: float) -> None:
        if elapsed >= 60 and not told:
            told.append(True)
            print("still shutting down (if its screen shows Windows installing updates, that can take a while) ...")

    ok, err = shutdown_guest(name, args.timeout, tick)
    if not ok:
        print(f"{name} has not shut down after {args.timeout}s" + (f" (libvirt: {err})" if err else "") + ".\n"
              "  - If its screen shows Windows installing updates or still closing programs, leave it alone and run\n"
              "    `gamevm stop` again to keep waiting: asking again interrupts nothing.\n"
              "  - Only if Windows is frozen: `gamevm kill`. That is like pulling the plug: unsaved data in Windows\n"
              "    is lost, and the graphics card may need a host reboot afterwards.", file=sys.stderr)
        return 1
    return 0 if wait_gpu_back(cfg) else 1  # the release hook also switches the monitor back to Linux


def shutdown_guest(name: str, timeout: float, tick=None) -> tuple[bool, str]:
    """Ask Windows to shut down and wait until the domain is off: (it is off, libvirt's last
    refusal). A paused domain cannot react, so it is resumed first. The request is repeated every
    30 s: Windows ignores it while it is still booting, and ignores extra ones once it is shutting
    down or installing updates."""
    start = time.monotonic()
    asked, err = None, ""
    while True:
        state = domain_state(name)
        if domain_off(state):
            return True, ""
        now = time.monotonic()
        if now - start >= timeout:
            return False, err
        if asked is None or now - asked >= 30:
            if state == "paused":
                virsh("resume", name, check=False)
            res = virsh("shutdown", name, check=False)
            err = "" if res.returncode == 0 else " ".join((res.stderr or "").split())
            asked = now
            if DRY_RUN:
                return True, ""
        if tick:
            tick(now - start)
        time.sleep(1)


def cmd_host_shutdown(args: argparse.Namespace) -> int:
    """ExecStop of gamevm-shutdown.service: the host shuts down or reboots while Windows runs, so
    Windows is shut down first and waited for. systemd stops that unit before libvirt and before
    the VM's scope (see the unit file), so nothing kills QEMU meanwhile. Does nothing when the
    unit is merely stopped by hand."""
    require_root("host-shutdown")
    cfg = load_config(ETC_CONFIG if not DRY_RUN else args.config, trusted=not DRY_RUN)
    name, wait = cfg["vm"]["name"], cfg["vm"]["host_shutdown_wait"]
    # Look for the QEMU process before talking to libvirt: without a VM virtqemud is usually not
    # running, and a connection would ask systemd to start it in the middle of the shutdown.
    if qemu_pid(name) is None or not host_going_down():
        return 0
    log(f"the host is shutting down: shutting {name} down first (waiting up to {wait}s)")
    try:
        ok, err = shutdown_guest(name, wait)
    except subprocess.TimeoutExpired:
        ok, err = False, "libvirt does not answer"
    if ok:
        log(f"{name} has shut down")
        return 0
    log(f"{name} did not shut down within {wait}s" + (f" (libvirt: {err})" if err else "")
        + ": the host's shutdown powers it off now", "error")
    return 1


def wait_gpu_back(cfg: dict, timeout: float = 20) -> bool:
    """Wait for the release hook after the VM stopped. False: the host did not get the GPU back."""
    vga = cfg["gpu"]["devices"][0]
    if gpu_dedicated():
        print("VM stopped; the GPU stays on vfio-pci (dedicated to the VM).")
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drv = pci_driver(vga)
        if drv not in (None, "vfio-pci"):
            # The release hook goes on checking the GPU and moving the desktop back: wait for it.
            end = time.monotonic() + 60
            while STATE_FILE.exists() and time.monotonic() < end:
                time.sleep(0.5)
            if PENDING_FILE.exists() and not REBOOT_MARKER.exists() and kwin_layout() == "igpu":
                print('VM stopped; the graphics card is back on the host. The Linux desktop stays on the side monitor so '
                      'that your apps keep running (desktop.return = "ask"): `gamevm back` brings the main monitor back.')
                return True
            if REBOOT_MARKER.exists() or kwin_layout() == "igpu":
                print("VM stopped, but the graphics card did not come back healthy: the desktop stays on the "
                      "side monitor. Reboot the PC (details: /var/log/gamevm.log).", file=sys.stderr)
                return False
            print(f"VM stopped; GPU is back on the host ({drv}).")
            return True
        time.sleep(0.5)
    print(f"VM stopped, but {vga} is on driver '{pci_driver(vga)}'. Run: sudo gamevm recover", file=sys.stderr)
    return False


def cmd_kill(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if not confirm("Force power-off Windows (unsaved data in Windows is lost)?", args.yes):
        return 1
    virsh("destroy", cfg["vm"]["name"], check=False)
    return 0 if wait_gpu_back(cfg) else 1


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    mode = resolve_mode(cfg)
    print(f"config      {cfg['_path']}")
    print(f"mode        {mode} (configured: {cfg['vm']['mode']})")
    print(f"domain      {cfg['vm']['name']}: {domain_state(cfg['vm']['name'])}")
    if mode == "igpu":
        print("desktop     " + {"native": "main monitor through the graphics card + side monitor (iGPU)",
                                 "igpu": "side monitor only (iGPU); the graphics card is free for Windows",
                                 "unmanaged": "not managed by gamevm this boot (journalctl -b -u gamevm-kwin.service)"}[kwin_layout()])
    for bdf in cfg["gpu"]["devices"] + ([cfg["disk"]["nvme"]] if cfg["disk"]["nvme"] else []) + cfg["usb"]["controllers"]:
        print(f"pci         {bdf} -> {pci_driver(bdf) or '(no driver)'}   {pci_desc(bdf)}")
    if cfg["disk"]["boot_disk"]:
        boot = cfg["disk"]["boot_disk"]
        name = os.path.basename(os.path.realpath(boot)) if os.path.exists(boot) else "(missing)"
        print(f"boot disk   {boot} -> {name}   {boot_disk_problem(cfg) or 'SATA disk, Windows starts from it'}")
    meminfo = dict(re.findall(r"^(\w+):\s+(\d+)", read("/proc/meminfo"), re.M))
    print(f"hugepages   total {meminfo.get('HugePages_Total')} free {meminfo.get('HugePages_Free')} (2 MiB)")
    allowed = run(["systemctl", "show", "-p", "AllowedCPUs", "--value", "user.slice"], capture=True,
                  check=False, mutate=False).stdout.strip()
    print(f"user.slice  AllowedCPUs={allowed or '(all)'}")
    st = HostState.load()
    if st and st.data.get("steps"):
        print("host state  " + ", ".join(s["kind"] for s in st.data["steps"]))
    return 0


def cmd_holders(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    holders = gpu_users(cfg)
    if not holders:
        print("Nothing is using the NVIDIA GPU." + ("" if os.geteuid() == 0 else
              " (only your own processes are visible; sudo gamevm holders shows all)"))
        return 0
    print_holders(holders)
    return 1


# --- keyboard and mouse: plugging them into the running VM again ---------------------------------
# QEMU reads the keyboard and mouse straight from their event devices. When one is unplugged (or its
# receiver re-registers), QEMU closes it for good; plugged in again it belongs to Linux only. If it
# was the keyboard, the switch keys are gone with it and the mouse stays captured in Windows. The
# cure is a hot-unplug and hot-plug of the <input> devices, which libvirt does without Windows
# noticing more than a keyboard being plugged in.

def evdev_inputs(cfg: dict) -> list[tuple[str, str, str]]:
    """(role, event device, libvirt <input> XML) of the configured evdev devices. The switch keys
    (grabToggle) live on the keyboard, which also takes the other devices along (grab='all')."""
    inp = cfg["input"]
    if not inp["evdev"]:
        return []
    out = []
    if inp["keyboard"]:
        out.append(("keyboard", inp["keyboard"], f"<input type='evdev'><source dev={quoteattr(inp['keyboard'])} grab='all' "
                                                 f"grabToggle='{inp['toggle']}' repeat='on'/></input>"))
    if inp["mouse"]:
        out.append(("mouse", inp["mouse"], f"<input type='evdev'><source dev={quoteattr(inp['mouse'])}/></input>"))
    out += [("extra input", dev, f"<input type='evdev'><source dev={quoteattr(dev)} repeat='on'/></input>")
            for dev in inp["extra"]]
    return out


def live_evdev(name: str) -> list[str] | None:
    """Event devices the running domain has (one entry per <input>, so a device plugged in twice
    appears twice). None if the domain is not running."""
    res = virsh("dumpxml", name, check=False, mutate=False, timeout=30)
    if res.returncode != 0 or domain_state(name) != "running":
        return None
    try:
        root = ET.fromstring(res.stdout)
    except ET.ParseError:
        return None
    return [s.get("dev") for s in root.findall("./devices/input[@type='evdev']/source")]


def reattach_input(cfg: dict) -> list[str]:
    """Take every configured evdev device out of the running VM and plug the connected ones in
    again; returns their roles. All of them, the keyboard last: a device that is plugged in starts
    captured by the VM, and the keyboard pulls the others to its side, so afterwards keyboard and
    mouse are together, with Windows (as when the VM starts). libvirt does not refuse a device it
    already has (it would deliver every key twice), so each one is taken out first, as often as
    it is in."""
    name = cfg["vm"]["name"]
    live = live_evdev(name)
    if live is None:
        raise GameVMError(f"{name} is not running")
    done = []
    with tempfile.TemporaryDirectory(prefix="gamevm-input-") as tmp:
        os.chmod(tmp, 0o755)
        for n, (role, dev, xml) in enumerate(reversed(evdev_inputs(cfg))):  # keyboard last
            path = os.path.join(tmp, f"{n}.xml")
            Path(path).write_text(xml + "\n")
            for _ in range(live.count(dev)):
                res = virsh("detach-device", name, path, "--live", check=False, timeout=30)
                if res.returncode != 0:
                    said = " ".join((res.stderr or "").split())
                    if not DRY_RUN and not os.path.exists(dev):
                        # gone from the host as well: nothing to double, try again when it is back
                        log(f"could not unplug the absent {role} from {name} yet: {said}", "warn")
                        break
                    raise GameVMError(f"could not unplug the {role} from {name}: {said}")
            deadline = time.monotonic() + 3
            while not DRY_RUN and dev in (live_evdev(name) or []) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not DRY_RUN and not os.path.exists(dev):
                log(f"the {role} is not connected ({dev}): left out")
                continue
            res = virsh("attach-device", name, path, "--live", check=False, timeout=30)
            if res.returncode != 0:
                raise GameVMError(f"could not plug the {role} into {name}: {' '.join((res.stderr or '').split())}")
            done.append(role)
    return done[::-1]


def cmd_input(args: argparse.Namespace) -> int:
    """Plug the keyboard and mouse into the running VM again (after one was unplugged)."""
    cfg = load_config(args.config)
    if not evdev_inputs(cfg):
        raise GameVMError("input.evdev is off, or no keyboard and mouse are set in the config")
    done = reattach_input(cfg)
    if not done:
        print("None of the configured devices is connected (ls /dev/input/by-id).", file=sys.stderr)
        return 1
    print(f"Reconnected to Windows: {', '.join(done)}. They are with Windows now: press "
          f"{cfg['input']['toggle'].replace('-', ' + ')} to use them on Linux.")
    return 0


def device_id(path: str) -> tuple[int, int] | None:
    """Identity of the device node behind path (None: not there). A device that was unplugged and
    plugged in again gets a new node. Not its change time: libvirt changes the owner on hot-plug."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_rdev, st.st_ino


def cmd_input_watch(args: argparse.Namespace) -> int:
    """Internal (its own systemd unit, started by the hook while the VM runs; never part of the
    hook itself, which must not call virsh): when a configured keyboard or mouse appears as a new
    device, plug the input devices into the VM again. Ends when the VM has stopped.

    It also brings the clipboard link back at every Windows start: KDE Connect on Linux looks for
    devices when its own network changes, which a starting VM is not, and Windows' KDE Connect
    announces itself on the LAN only. So once the one in Windows answers on the private link
    (about a minute after sign-in), Linux' is told to look, twice, 15 seconds apart."""
    require_root("input-watch")
    st = HostState.load()
    cfg = (st.data.get("cfg") if st else None) or load_config(ETC_CONFIG, trusted=True)
    name = cfg["vm"]["name"]
    paths = [dev for _role, dev, _xml in evdev_inputs(cfg)] if cfg["input"]["reattach"] else []
    known = {p: device_id(p) for p in paths}
    failures = 0
    started = time.monotonic()
    look_at = started + 20 if cfg["network"]["hostonly"] else None  # when to try the clipboard link next
    told = 0
    while STATE_FILE.exists() and (paths or look_at):  # the journal goes when the VM has stopped
        time.sleep(1)
        if look_at and time.monotonic() >= look_at:
            look_at = time.monotonic() + 15
            if tcp_open(GUEST_LINK_IP, 1716, 0.5):
                u = audio_user(cfg)
                if u and kdeconnect(u, "call", *KDE_DAEMON, "forceOnNetworkChange", mutate=True) is not None and not told:
                    log("KDE Connect in Windows answers: told KDE Connect on Linux to look for it")
                told += 1
            if told >= 2 or time.monotonic() - started > 900:  # done, or no KDE Connect in this Windows
                look_at = None
        if not paths:
            continue
        now = {p: device_id(p) for p in paths}
        for p in paths:
            if now[p] is None and known[p] is not None:
                log(f"{p} was unplugged")
                known[p] = None
        if not any(now[p] is not None and now[p] != known[p] for p in paths):
            continue
        time.sleep(1.5)  # udev is still setting the new device up, and a receiver brings several nodes
        if domain_state(name) != "running":
            continue  # still starting or already stopping: looked at again in a second
        try:
            done = reattach_input(cfg)
        except (GameVMError, subprocess.TimeoutExpired) as e:
            failures += 1
            log(f"could not reconnect the input devices: {e}", "warn")
            if failures >= 5:
                log("giving up on reconnecting input devices for this run (gamevm input does it by hand)", "error")
                return 1
            time.sleep(5)
            continue
        failures = 0
        known = {p: device_id(p) for p in paths}
        if done:
            log(f"reconnected to {name}: {', '.join(done)}")
            notify_user(cfg, "Keyboard and mouse reconnected to Windows",
                        f"{', '.join(done).capitalize()}: with Windows now. Press "
                        f"{cfg['input']['toggle'].replace('-', ' + ')} to use them on Linux.")
    return 0


def step_input_watch(cfg: dict, st: HostState) -> None:
    """Start the watcher that plugs a re-connected keyboard or mouse into the VM again, and that
    brings the clipboard link (KDE Connect) back once Windows is up."""
    if not (cfg["input"]["reattach"] and evdev_inputs(cfg)) and not cfg["network"]["hostonly"]:
        return
    unit = f"gamevm-input-{cfg['vm']['name']}"
    run(["systemctl", "stop", f"{unit}.service"], check=False, capture=True)  # one left over from a crash
    run(["systemctl", "reset-failed", f"{unit}.service"], check=False, capture=True)
    res = run(["systemd-run", f"--unit={unit}", "--collect", "--quiet", "/usr/bin/python3", "-I",
               os.path.realpath(__file__), "input-watch"], check=False, capture=True)
    if res.returncode == 0:
        st.push("input_watch", unit=f"{unit}.service")
    else:
        log(f"could not start the input watcher: {(res.stderr or '').strip()} (gamevm input does it by hand)", "warn")


def cmd_usb(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    dev = args.device.lower()
    if not USB_ID_RE.match(dev):
        raise GameVMError("device must be VID:PID, e.g. 045e:02fe (see lsusb)")
    vid, pid = dev.split(":")
    xml = (f"<hostdev mode='subsystem' type='usb' managed='yes'><source>"
           f"<vendor id='0x{vid}'/><product id='0x{pid}'/></source></hostdev>")
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
        f.write(xml)
        path = f.name
    try:
        verb = "attach-device" if args.action == "attach" else "detach-device"
        virsh(verb, cfg["vm"]["name"], path, "--live")
    finally:
        os.unlink(path)
    print(f"{args.action}ed {dev}")
    return 0


def key_label(name: str) -> str:
    """KEY_RIGHTCTRL -> 'Right Ctrl' (the names Looking Glass uses for its escape key)."""
    known = {"RIGHTCTRL": "Right Ctrl", "LEFTCTRL": "Left Ctrl", "SCROLLLOCK": "Scroll Lock", "PAUSE": "Pause",
             "RIGHTALT": "Right Alt (AltGr)", "RIGHTSHIFT": "Right Shift", "RIGHTMETA": "Right Meta", "COMPOSE": "Menu"}
    key = name.removeprefix("KEY_")
    return known.get(key, key.replace("_", " ").title())


def lg_argv(cfg: dict, client: str, extra: list[str]) -> list[str]:
    """The Looking Glass client's command line. The options that decide whether you can get out
    of its window again are given here, where they win over any client.ini: until 2026-11 gamevm's
    own client.ini opened the window full screen on the only monitor Linux has while Windows runs,
    captured the mouse at once and left Scroll Lock (a key many keyboards lack) as the only exit.
    Now: a maximised window with its title bar unless looking_glass.fullscreen, the mouse free
    until it is captured on purpose, an escape key every keyboard has, and no change of Windows'
    resolution when the window is resized (duplicated displays share one resolution, so that
    would change the main monitor too). `extra` comes last and overrides all of it."""
    lg = cfg["looking_glass"]
    argv = [client]
    if lg["kvmfr"]:
        argv += ["lgmp:shmDevice=/dev/kvmfr0"]
    argv += [f"spice:port={lg['spice_port']}", f"win:fullScreen={'yes' if lg['fullscreen'] else 'no'}",
             f"win:maximize={'no' if lg['fullscreen'] else 'yes'}", "win:setGuestRes=no", "input:autoCapture=no",
             f"input:escapeKey={lg['escape_key']}", *extra]
    return argv


# The Looking Glass window has a picture only while Windows draws on the Looking Glass virtual
# monitor. The driver's helper is meant to keep that monitor in Windows' display layout, but with a
# real monitor next to it that goes wrong in both directions: by default it makes the virtual one the
# ONLY monitor (the real one goes dark), and with that switched off it fails to bring the virtual
# one back after a Windows start ("Failed to restore the saved display topology (87)", build
# B7-829), which leaves the window empty. So gamevm sets the layout itself, through the guest agent.

LG_DISPLAY = "Looking Glass"  # in the name of the driver's display adapter


def guest_display_layout(cfg: dict, mode: str) -> str:
    """Switch Windows' display layout as Win+P does (clone, internal, external, extend; status
    only looks) and put the main display back on its highest refresh rate: windows/
    display-layout.ps1, started in the signed-in user's session by a one-off scheduled task,
    because display layouts belong to that session and the guest agent is a service. Returns the
    script's result line, which ends in active=[monitor|monitor]; '' if nobody is signed in or it
    did not answer within 45 s (it normally takes 2 to 4; the first run after an update of the
    script compiles its helper, which took 16 s while Windows ran its idle maintenance)."""
    script = bench_script().parent / "display-layout.ps1"
    if not script.is_file():
        raise GameVMError("windows/display-layout.ps1 is not installed (re-run setup/install-host.sh)")
    guest_write(cfg, f"{BENCH_GUEST_DIR}\\display-layout.ps1", script.read_bytes())
    tag = f"gamevm-{os.getpid()}-{time.monotonic_ns()}"
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
$user = (Get-CimInstance Win32_ComputerSystem).UserName
if (-not $user) {{ exit }}
$log = '{BENCH_GUEST_DIR}\\display-layout.log'
if ((Get-Item $log).Length -gt 100KB) {{ Remove-Item $log -Force }}
$run = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File {BENCH_GUEST_DIR}\\display-layout.ps1 -Mode {mode} -Tag {tag}'
schtasks.exe /create /tn gamevm-display-layout /tr $run /sc once /st 00:00 /ru $user /it /f | Out-Null
schtasks.exe /run /tn gamevm-display-layout | Out-Null
for ($i = 0; $i -lt 150; $i++) {{
    Start-Sleep -Milliseconds 300
    $line = Select-String -Path $log -SimpleMatch '{tag}' | Select-Object -Last 1
    if ($line) {{ break }}
}}
schtasks.exe /delete /tn gamevm-display-layout /f | Out-Null
if ($line) {{ $line.Line }}
"""
    _, out, _ = guest_powershell(cfg, ps, timeout=90)
    lines = [x.strip() for x in out.splitlines() if tag in x]
    return lines[-1].replace(tag, "").strip() if lines else ""


def active_displays(result: str) -> list[str] | None:
    """The monitors that show Windows' desktop ('DELL U2723QE', 'Looking Glass'), from a
    display-layout.ps1 result line (None: no such report). Read there and not from WMI, whose
    inventory keeps listing a display with its last mode after Windows switched it off."""
    m = re.search(r"active=\[(.*)\]", result)
    return [name for name in m.group(1).split("|") if name] if m else None


def lg_mirror_on(cfg: dict) -> tuple[bool, str]:
    """Before the Looking Glass window opens: make sure Windows draws on the Looking Glass
    monitor. Returns (the mirror is gamevm's and has to be switched off again, what to tell the
    user). A layout in which that monitor already takes part is the user's own and stays as it
    is, unless it is the mirror of an earlier window that did not close cleanly (the Windows
    script keeps a marker for that)."""
    by_hand = "If the window stays empty: in Windows press Win + P and choose Duplicate."
    try:
        agent(cfg, {"execute": "guest-ping"}, timeout=8)
    except (GameVMError, subprocess.TimeoutExpired, ValueError):
        return False, f"The guest agent in Windows does not answer, so gamevm cannot set Windows' display layout. {by_hand}"
    # Never let the driver switch the real monitor off (its default, "Make LG the only monitor").
    _, out, _ = guest_powershell(cfg, "$k = 'HKLM:\\SOFTWARE\\LookingGlass\\IDD'; if ((Test-Path $k) -and (Get-ItemProperty $k)."
                                      "ExclusiveMonitor -ne 0) { Set-ItemProperty $k -Name ExclusiveMonitor -Value 0 -Type DWord }; "
                                      "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }", timeout=30)
    if LG_DISPLAY not in out:
        return False, (f"Windows has no Looking Glass display: its driver is not installed ({BENCH_GUEST_DIR}\\"
                       "2-inside-vm.ps1 -InstallLookingGlass, in Windows as administrator).")
    said = guest_display_layout(cfg, "mirror")
    active = active_displays(said)
    if active is None:
        return False, (f"Nobody is signed in to Windows (or it did not answer in 45 s), so gamevm left its display layout "
                       f"alone. {by_hand}")
    if not any(LG_DISPLAY in name for name in active):
        return False, f"gamevm could not make Windows mirror its screen ({said}). {by_hand}"
    if "layout=kept" in said and "owned=1" not in said:
        return False, ""
    return True, ("Windows mirrors its screen into the Looking Glass window, and goes back to the main monitor alone "
                  "when you close the window.")


def lg_mirror_off(cfg: dict) -> tuple[bool, str]:
    """After the window closed, when the mirror was gamevm's: the real monitor alone again
    (mirroring costs a little and can limit VRR). "PC screen only" is Windows' first display,
    normally the real one; each result is checked, "Second screen only" is tried if the real
    monitor ended up off, and if neither leaves it on alone the mirror goes back on: the real
    monitor is never left dark. Returns (it worked, what to tell the user)."""
    for mode in ("internal", "external"):
        said = guest_display_layout(cfg, mode)
        active = active_displays(said)
        if active is None:
            return False, ("Windows did not answer when gamevm asked it to stop mirroring its screen. To end the mirror: "
                           "in Windows press Win + P and choose PC screen only.")
        if active and not any(LG_DISPLAY in name for name in active):
            rate = re.search(r"refresh=(?:\d+->)?(\d+)", said)
            return True, f"Windows is back on its main monitor alone ({active[0]}" + (f", {rate.group(1)} Hz" if rate else "") + ")."
    guest_display_layout(cfg, "clone")
    return False, ("gamevm could not switch the mirror off again, so it stays on. To end it: in Windows press Win + P and "
                   "choose PC screen only.")


def cmd_lg(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    client = shutil.which("looking-glass-client")
    if not client:
        raise GameVMError("looking-glass-client is not installed (run looking-glass/install.sh)")
    if domain_state(cfg["vm"]["name"]) != "running":
        raise GameVMError("Windows is not running: there is nothing to show (gamevm start)")
    lg = cfg["looking_glass"]
    key = key_label(lg["escape_key"])
    toggle = cfg["input"]["toggle"].replace("-", " + ")
    hint = (f"{key} captures the mouse and lets it go again. {key} + Q closes the window, {key} + F switches "
            f"full screen. Hold {key} to see every shortcut.")
    print(f"Looking Glass shows Windows in a window. {hint}\n"
          f"The keyboard and mouse must be on the Linux side for it ({toggle} switches them).")
    argv = lg_argv(cfg, client, args.extra)
    if DRY_RUN:
        print("[dry-run] " + ("make Windows mirror its screen, then " if lg["auto_mirror"] else "") + " ".join(argv),
              file=sys.stderr)
        return 0
    mirrored = False
    if lg["auto_mirror"]:
        print("Asking Windows to show its screen in the window ...", flush=True)
        try:
            mirrored, said = lg_mirror_on(cfg)
        except (GameVMError, subprocess.TimeoutExpired, ValueError, OSError) as e:
            said = f"gamevm could not set Windows' display layout ({e}). If the window stays empty: Win + P, Duplicate."
        if said:
            print(said)
    notify_user(cfg, "Looking Glass: how to get out", hint)
    if not mirrored:
        os.execv(client, argv)  # nothing to undo afterwards
    # The mirror has to go off again however the window ends: a closed terminal (SIGHUP) or a kill
    # of gamevm ends the client, and gamevm carries on to the clean-up.
    proc = subprocess.Popen(argv)
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, lambda *_: proc.poll() is None and proc.terminate())
    while True:
        try:
            proc.wait()
            break
        except KeyboardInterrupt:  # Ctrl+C reaches the client too: wait for it to close
            pass
    try:
        if domain_state(cfg["vm"]["name"]) != "running":
            return 0  # Windows went off under the window; the next `gamevm lg` takes the mirror over
        ok, said = lg_mirror_off(cfg)
    except (GameVMError, subprocess.TimeoutExpired, ValueError, OSError) as e:
        ok, said = False, (f"gamevm could not switch Windows' mirror off again ({e}). To end it: in Windows press Win + P "
                           "and choose PC screen only.")
    if not ok:
        notify_user(cfg, "Windows still mirrors its screen", said)
    try:
        print(said)
    except OSError:  # the terminal is gone
        pass
    return 0


def cmd_probe_input(args: argparse.Namespace) -> int:
    """Show which configured evdev node emits Ctrl (the grab toggle must sit on that node)."""
    import select
    import struct
    require_root("probe-input")
    cfg = load_config(args.config)
    inp = cfg["input"]
    paths = [p for p in [inp["keyboard"], inp["mouse"], *inp["extra"]] if p]
    paths += [p for p in sorted(glob.glob("/dev/input/by-id/*-event-*")) if p not in paths]
    fds = {}
    for p in paths:
        try:
            fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = p
        except OSError:
            pass
    print(f"Press Left Ctrl and Right Ctrl a few times, move the mouse ({args.seconds}s) ...")
    fmt = "llHHi"
    size = struct.calcsize(fmt)
    ctrl, rel = {}, {}
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select(list(fds), [], [], 0.2)
        for fd in ready:
            try:
                data = os.read(fd, size * 64)
            except OSError:
                continue
            for i in range(0, len(data) - size + 1, size):
                _, _, etype, code, value = struct.unpack(fmt, data[i:i + size])
                if etype == 1 and code in (29, 97) and value == 1:   # EV_KEY, KEY_LEFTCTRL/KEY_RIGHTCTRL
                    ctrl[fds[fd]] = ctrl.get(fds[fd], 0) + 1
                elif etype == 2:                                      # EV_REL (mouse motion)
                    rel[fds[fd]] = rel.get(fds[fd], 0) + 1
    for fd in fds:
        os.close(fd)
    print("\nCtrl presses seen on:" if ctrl else "\nNo Ctrl presses seen.")
    for p, n in sorted(ctrl.items(), key=lambda kv: -kv[1]):
        mark = "  <- input.keyboard" if p == inp["keyboard"] else ""
        print(f"  {n:4}  {p}{mark}")
    print("Mouse motion seen on:" if rel else "No mouse motion seen.")
    for p, n in sorted(rel.items(), key=lambda kv: -kv[1]):
        mark = "  <- input.mouse" if p == inp["mouse"] else ""
        print(f"  {n:4}  {p}{mark}")
    if ctrl and inp["keyboard"] not in ctrl:
        best = max(ctrl, key=ctrl.get)
        print(f"\n=> set input.keyboard = \"{best}\" (the toggle only works on the node that emits Ctrl)")
        return 1
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Dry-run the libvirt hook: print every host change prepare would make, then its undo."""
    if not DRY_RUN:
        raise GameVMError("selftest only runs with --dry-run")
    global SIMULATE_KWIN
    cfg = load_config(args.config)
    SIMULATE_KWIN = (args.mode or resolve_mode(cfg)) == "igpu"
    print(f"=== prepare (mode {args.mode or resolve_mode(cfg)}) ===", file=sys.stderr)
    st = prepare(cfg, args.mode)
    print("=== recorded steps: " + ", ".join(x["kind"] for x in st.data["steps"]), file=sys.stderr)
    print("=== release (undo) ===", file=sys.stderr)
    undo(cfg, st)
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    """Move the Linux desktop by hand (Windows must be off): native = main monitor through the
    graphics card, igpu = side monitor only. Both restart KWin."""
    require_root("desktop")
    cfg = load_config(ETC_CONFIG, trusted=True)
    if not kwin_managed():
        raise GameVMError("KWin GPU management is not active (iGPU mode only; see gamevm doctor)")
    if not domain_off(domain_state(cfg["vm"]["name"])):
        raise GameVMError("Windows is running: it owns the graphics card")
    u = audio_user(cfg)
    if not u:
        raise GameVMError("audio.user (the desktop user) is not set in the config")
    if args.layout == "native":
        if not module_loaded("nvidia"):
            raise GameVMError("the NVIDIA driver is not loaded (sudo gamevm recover)")
        desktop_to_native(cfg, {"user": u.pw_name, "reopened": True, "started": time.time() - 600})
    elif kwin_layout() != "igpu":
        windows = capture_windows(cfg, u)  # with both monitors: kept for the way back
        relaunch_apps(u, desktop_to_igpu(cfg, u))
        restore_windows(cfg, u, windows)
        save_pending(windows)
    print(f"desktop: {kwin_layout()}")
    return 0


def cmd_back(args: argparse.Namespace) -> int:
    """Bring the Linux desktop back to the main monitor (after desktop.return = "ask" or
    `gamevm desktop igpu`). The same as `sudo gamevm desktop native`; asks for the password."""
    cfg = load_config(args.config)
    if kwin_layout() != "igpu":
        print("The Linux desktop already has the main monitor.")
        return 0
    if not domain_off(domain_state(cfg["vm"]["name"])):
        raise GameVMError("Windows is running: it owns the graphics card and the main monitor (gamevm stop first)")
    if os.geteuid() == 0:
        return cmd_desktop(argparse.Namespace(layout="native"))
    me = "/usr/local/bin/gamevm"
    if DRY_RUN or not shutil.which("pkexec"):
        print(f"run: sudo {me} desktop native")
        return 0 if DRY_RUN else 1
    os.execvp("pkexec", ["pkexec", me, "desktop", "native"])
    return 0


def cmd_kwin_boot(args: argparse.Namespace) -> int:
    require_root("kwin-boot")
    ok = kwin_boot(load_config(ETC_CONFIG if not DRY_RUN else args.config, trusted=not DRY_RUN))
    return 0 if ok else 1


# ---------------------------------------------------------------------------------------------
# gamevm bench: the same benchmark natively and in the VM, plus what the VM costs the host
# ---------------------------------------------------------------------------------------------

BENCH_GUEST_DIR = "C:\\gamevm"
BENCH_MARK = "GAMEVM-BENCH-JSON:"
BENCH_METRICS = (  # key, column, unit, higher is better
    ("compress_1t_mbs", "1 thread", "MB/s", True),
    ("compress_all_mbs", "all threads", "MB/s", True),
    ("mem_latency_ns", "mem latency", "ns", False),
    ("mem_copy_gbs", "mem copy", "GB/s", True),
    ("stall_worst_us", "worst stall", "us", False),
    ("stalls_per_s", "stalls>100us", "/s", False),
    ("disk_read_mbs", "disk read", "MB/s", True),
)


def agent(cfg: dict, cmd: dict, timeout: float = 30):
    res = virsh("qemu-agent-command", cfg["vm"]["name"], json.dumps(cmd), check=False, mutate=False, timeout=timeout)
    if res.returncode != 0:
        raise GameVMError("the QEMU guest agent in Windows does not answer (install virtio-win-guest-tools.exe "
                          f"from the virtio-win CD): {(res.stderr or '').strip()}")
    return json.loads(res.stdout).get("return")


def guest_write(cfg: dict, path: str, data: bytes) -> None:
    handle = agent(cfg, {"execute": "guest-file-open", "arguments": {"path": path, "mode": "wb"}})
    try:
        for i in range(0, len(data), 48 * 1024):
            agent(cfg, {"execute": "guest-file-write",
                        "arguments": {"handle": handle, "buf-b64": base64.b64encode(data[i:i + 48 * 1024]).decode()}})
    finally:
        agent(cfg, {"execute": "guest-file-close", "arguments": {"handle": handle}})


def guest_run(cfg: dict, path: str, argv: list[str], timeout: float) -> tuple[int, str, str]:
    pid = agent(cfg, {"execute": "guest-exec", "arguments": {"path": path, "arg": argv, "capture-output": True}})["pid"]
    deadline = time.monotonic() + timeout
    while True:
        st = agent(cfg, {"execute": "guest-exec-status", "arguments": {"pid": pid}})
        if st.get("exited"):
            break
        if time.monotonic() > deadline:
            raise GameVMError(f"{path} did not finish in Windows within {timeout:.0f}s")
        time.sleep(1)

    def text(key: str) -> str:
        return base64.b64decode(st.get(key, "")).decode("utf-8", "replace")

    return st.get("exitcode", -1), text("out-data"), text("err-data")


def guest_powershell(cfg: dict, script: str, timeout: float = 60) -> tuple[int, str, str]:
    return guest_run(cfg, "powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                             "-Command", script], timeout)


def guest_user_script(cfg: dict, body: str, timeout: float = 45) -> str | None:
    """Run PowerShell in the session of the user who is signed in to Windows and return what it
    printed. The guest agent itself is a service (session 0): it has no clipboard and none of the
    user's programs, so the script is written to C:\\gamevm, started by a one-off scheduled task
    as that user and its output read back. None: nobody is signed in, or no answer in time."""
    tag = f"gamevm-run-{os.getpid()}-{time.monotonic_ns()}"
    base = f"{BENCH_GUEST_DIR}\\{tag}"
    script = f"& {{\r\n{body}\r\n}} *> '{base}.out'\r\n'done' | Out-File -Encoding ascii '{base}.done'\r\n"
    guest_write(cfg, f"{base}.ps1", script.encode("ascii"))
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
$user = (Get-CimInstance Win32_ComputerSystem).UserName
if ($user) {{
    $run = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File {base}.ps1'
    schtasks.exe /create /tn {tag} /tr $run /sc once /st 00:00 /ru $user /it /f | Out-Null
    schtasks.exe /run /tn {tag} | Out-Null
    for ($i = 0; $i -lt {int(timeout / 0.3)}; $i++) {{ if (Test-Path '{base}.done') {{ break }}; Start-Sleep -Milliseconds 300 }}
    schtasks.exe /delete /tn {tag} /f | Out-Null
    if (Test-Path '{base}.done') {{ 'GAMEVM-ANSWER'; Get-Content '{base}.out' }}
}}
Remove-Item '{base}.ps1', '{base}.out', '{base}.done' -Force
# what a run left behind that answered only after its caller had stopped waiting
Get-ChildItem '{BENCH_GUEST_DIR}\\gamevm-run-*' | Where-Object {{ $_.LastWriteTime -lt (Get-Date).AddMinutes(-5) }} | Remove-Item -Force
"""
    _, out, _ = guest_powershell(cfg, ps, timeout=timeout + 30)
    return out.split("GAMEVM-ANSWER", 1)[1].strip() if "GAMEVM-ANSWER" in out else None


def install_windows_share(cfg: dict) -> tuple[bool, str]:
    """Windows' half of sending files: put "Send to > Linux (gamevm)" into the right-click menu
    and "Send to Linux" into the Start menu of the signed-in user (windows/send-to-linux.ps1
    -Install, run in that user's session). Returns (it worked, what Windows said)."""
    script = bench_script().parent / "send-to-linux.ps1"
    if not script.is_file():
        return False, "windows/send-to-linux.ps1 is not installed (re-run setup/install-host.sh)"
    guest_write(cfg, f"{BENCH_GUEST_DIR}\\send-to-linux.ps1", script.read_bytes())
    said = guest_user_script(cfg, f"& powershell.exe -NoProfile -ExecutionPolicy Bypass -File {BENCH_GUEST_DIR}"
                                  '\\send-to-linux.ps1 -Install 2>&1 | ForEach-Object { "$_" }', timeout=60)
    if said is None:
        return False, "nobody is signed in to Windows (or it did not answer)"
    return "installed" in said, " ".join(said.split())


def bench_script() -> Path:
    here = Path(__file__).resolve().parent
    for p in (here / "windows" / "3-benchmark.ps1", here.parent / "windows" / "3-benchmark.ps1"):
        if p.is_file():
            return p
    raise GameVMError("windows/3-benchmark.ps1 not found (re-run setup/install-host.sh)")


# ---------------------------------------------------------------------------------------------
# sharing with the running Windows: its drives in the file manager, the clipboard through KDE Connect
# ---------------------------------------------------------------------------------------------
# While Windows runs, its disks are Windows' alone (two systems cannot mount one NTFS volume), so
# Linux reaches them through Windows: 2-inside-vm.ps1 shares the drives and opens the firewall on
# the host-only adapter, for the Linux host only. The clipboard and "send file" are KDE Connect's
# job on both sides; it finds Windows through the same link.

def tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def cmd_scripts(args: argparse.Namespace) -> int:
    """Copy the current Windows scripts into the running VM (C:\\gamevm), through the guest agent."""
    cfg = load_config(args.config)
    if domain_state(cfg["vm"]["name"]) != "running":
        raise GameVMError("Windows is not running. (Natively, take the scripts from the shared disk's gamevm-windows folder.)")
    agent(cfg, {"execute": "guest-ping"})
    guest_powershell(cfg, f"New-Item -ItemType Directory -Force -Path '{BENCH_GUEST_DIR}' | Out-Null")
    for f in sorted(bench_script().parent.glob("*.ps1")):
        guest_write(cfg, f"{BENCH_GUEST_DIR}\\{f.name}", f.read_bytes())
        print(f"copied {f.name} to {BENCH_GUEST_DIR}")
    print(f"In Windows, in an administrator PowerShell:  Set-ExecutionPolicy -Scope Process Bypass -Force ; "
          f"{BENCH_GUEST_DIR}\\2-inside-vm.ps1")
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    """Open Windows' drives in the file manager: through Windows while it runs, directly when it
    is off (then they are Linux' own mounts again)."""
    cfg = load_config(args.config)
    if domain_state(cfg["vm"]["name"]) == "running":
        if not cfg["network"]["hostonly"]:
            raise GameVMError("network.hostonly is off: Linux has no link to the running Windows")
        if not tcp_open(GUEST_LINK_IP, 445):
            raise GameVMError(f"Windows does not answer on {GUEST_LINK_IP} for file sharing. Still booting? Otherwise run "
                              f"{BENCH_GUEST_DIR}\\2-inside-vm.ps1 once in Windows, as administrator (`gamevm scripts` "
                              "puts the current version there).")
        targets = [f"smb://{GUEST_LINK_IP}/"]
        print("Windows is running: its drives through Windows itself (sign in with your Windows user name and password)")
    else:
        targets = [m for m in cfg["disk"]["unmount"] if os.path.isdir(m)]
        if not targets:
            raise GameVMError("Windows is off and disk.unmount names no folder where its drives are mounted")
        print("Windows is off: its drives are mounted on Linux")
    for target in targets:
        print(f"  {target}")
        if not DRY_RUN:
            subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    return 0


def kdeconnect(u: pwd.struct_passwd, *args: str, mutate: bool = False) -> list[str] | None:
    """busctl on the user's KDE Connect daemon; the answer split into words (None: no KDE Connect)."""
    res = run(as_user(u, "busctl", "--user", "--timeout=5", *args), check=False, capture=True, mutate=mutate, timeout=15)
    if res.returncode != 0:
        return None
    try:
        return shlex.split(res.stdout or "")
    except ValueError:
        return None


KDE_DAEMON = ("org.kde.kdeconnect", "/modules/kdeconnect", "org.kde.kdeconnect.daemon")


def guest_clock(cfg: dict) -> tuple[float, int | None] | None:
    """(seconds by which Windows' clock is ahead of Linux', UTC against UTC; Windows' time zone as
    seconds east of UTC, None if unknown). None: the guest agent does not answer."""
    try:
        t0 = time.time()
        ns = agent(cfg, {"execute": "guest-get-time"}, timeout=8)
        t1 = time.time()
        zone = agent(cfg, {"execute": "guest-get-timezone"}, timeout=8)
    except (GameVMError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(ns, int):
        return None
    return ns / 1e9 - (t0 + t1) / 2, zone.get("offset") if isinstance(zone, dict) else None


def clock_trouble(cfg: dict, fix: bool = False) -> str:
    """'' if Windows' clock agrees with Linux' to within two minutes, else what is wrong and what
    to do. KDE Connect (protocol 8) refuses to pair when the clocks differ by more than 30
    minutes, and sign-ins in Windows dislike a wrong clock too. The usual cause is a Windows time
    zone that is not Linux' one: with vm.clock = "localtime" Windows gets Linux' local time at
    every start and reads it in its own zone. `fix` sets Windows' clock from Linux', but only when
    the zones agree; with different zones that would make the time Windows shows wrong instead."""
    clock = guest_clock(cfg)
    if clock is None or abs(clock[0]) <= 120:
        return ""
    off, zone = clock
    here = time.localtime().tm_gmtoff
    utc = lambda sec: "UTC%+03d:%02d" % (sec // 3600, abs(sec) % 3600 // 60)  # noqa: E731
    apart = f"{round(abs(off) / 60)} minutes {'ahead of' if off > 0 else 'behind'} Linux' clock"
    if zone is not None and zone != here and cfg["vm"]["clock"] == "localtime":
        return (f"Windows' clock is {apart}, because Windows is set to another time zone than Linux ({utc(zone)} "
                f"against {utc(here)}) and reads the time Linux hands it at every start in its own zone. KDE Connect "
                "refuses to pair when the clocks differ by more than 30 minutes. Give Windows Linux' time zone (Settings "
                "> Time & language > Date & time > Time zone), then run  gamevm share --pair")
    if fix:
        try:
            agent(cfg, {"execute": "guest-set-time", "arguments": {"time": time.time_ns()}}, timeout=15)
        except (GameVMError, subprocess.TimeoutExpired, ValueError):
            pass
        again = guest_clock(cfg)
        if again is not None and abs(again[0]) <= 120:
            return ""
    return (f"Windows' clock is {apart}; KDE Connect refuses to pair when the clocks differ by more than 30 minutes. "
            + ("gamevm could not set it: in Windows, Settings > Time & language > Date & time > Sync now" if fix else
               "gamevm share --pair  sets it from Linux' clock first"))


def kde_device(u: pwd.struct_passwd, dev: str, *args: str, mutate: bool = False) -> list[str] | None:
    """A call or property of one KDE Connect device on Linux (args: "call", method | "get-property", name)."""
    return kdeconnect(u, args[0], "org.kde.kdeconnect", f"/modules/kdeconnect/devices/{dev}", "org.kde.kdeconnect.device",
                      *args[1:], mutate=mutate)


def kde_windows_device(u: pwd.struct_passwd) -> tuple[str, str] | None:
    """(id, name) of the KDE Connect device that answers at Windows' address on the private link:
    the VM and nothing else (a phone or another PC on the LAN is never paired by gamevm)."""
    out = kdeconnect(u, "call", *KDE_DAEMON, "deviceNames", "bb", "true", "false") or []
    pairs = out[2:] if out[:1] == ["a{ss}"] else []
    for dev, name in zip(pairs[0::2], pairs[1::2]):
        if GUEST_LINK_IP in (kde_device(u, dev, "get-property", "reachableAddresses") or [])[2:]:
            return dev, name
    return None


def pair_windows(cfg: dict, u: pwd.struct_passwd) -> bool:
    """Pair KDE Connect on Linux with KDE Connect in the running Windows. A pairing request is a
    notification with an Accept button on the other side, open for 30 seconds: easy to miss on a
    screen that shows a game. So gamevm does both halves: Windows asks (kdeconnect-cli --pair,
    run through the guest agent in the signed-in user's session; 5 to 25 s) and gamevm accepts
    on Linux at once. In this order only: when Linux asked and Windows answered with its
    command-line tool, the pairing was dropped again within seconds (KDE Connect 26.08, tested).

    KDE Connect's trap: a request and its acceptance are the same message, so Accept clicked on a
    request that has been answered already reaches the other side as a new request from a device
    it trusts, and that side drops the pairing (tested). The answered notification stays on the
    Linux screen for its 30 seconds, so gamevm says to leave it alone, watches that long, and
    pairs once more if the pairing went. Every attempt starts from a clean slate: Linux' unpair
    also takes Windows out of whatever half-paired state an earlier attempt left."""
    found = kde_windows_device(u)
    if not found:
        print(f"  [ -- ] KDE Connect on Linux does not see Windows at {GUEST_LINK_IP}: open KDE Connect in Windows once "
              "(2-inside-vm.ps1 installs it), then run this again")
        return False
    dev, name = found
    yes = ["b", "true"]
    paired = lambda: kde_device(u, dev, "call", "isPaired") == yes  # noqa: E731
    if paired():
        print(f"  [ OK ] already paired with {name}")
        return True
    trouble = clock_trouble(cfg, fix=True)
    if trouble:
        print(f"  [ -- ] {trouble}")
        return False
    me = kdeconnect(u, "call", *KDE_DAEMON, "selfId") or []

    def once() -> bool:
        kde_device(u, dev, "call", "unpair", mutate=True)
        time.sleep(1)
        answered = None
        if len(me) == 2 and re.fullmatch(r"[0-9A-Za-z_-]{8,64}", me[1]):
            try:
                answered = guest_user_script(
                    cfg, f'& "$env:LOCALAPPDATA\\Microsoft\\WindowsApps\\kdeconnect-cli.exe" --pair -d {me[1]} 2>&1 | '
                         'ForEach-Object { "$_" }', timeout=60)
            except (GameVMError, subprocess.TimeoutExpired, ValueError, OSError):
                pass
        if kde_device(u, dev, "call", "isPairRequestedByPeer") == yes:
            kde_device(u, dev, "call", "acceptPairing", mutate=True)
        elif not paired():
            # Windows could not be made to ask (nobody signed in, no KDE Connect there): by hand.
            if kde_device(u, dev, "call", "requestPairing", mutate=True) is None:
                print(f"  [ -- ] KDE Connect on Linux refused to ask {name} for pairing: pair in the KDE Connect window instead")
                return False
            key = (kde_device(u, dev, "get-property", "verificationKey") or ["", ""])[1]
            print("         Accept the request in Windows now: KDE Connect shows it as a notification for 30 seconds"
                  + (f" (key {key} on both sides)." if key else "."), flush=True)
        for _ in range(22):
            if paired():
                return True
            time.sleep(1.5)
        print(f"  [ -- ] not paired with {name}" + (f" (Windows said: {' '.join(answered.split())[:120]})" if answered else "")
              + ". Run this again, or pair in the KDE Connect window.")
        return False

    print(f"  [ .. ] pairing with {name} ...", flush=True)
    for attempt in (1, 2):
        if not once():
            return False
        print("  [ .. ] paired. KDE Connect shows a \"Pairing request\" on this screen for half a minute: leave it alone.\n"
              "         It is answered already, and Accept on it now would undo the pairing. Checking that it holds ...",
              flush=True)
        held = True
        for _ in range(16):
            time.sleep(2)
            if not paired():
                held = False
                break
        if held:
            print(f"  [ OK ] paired with {name}: text you copy is on both clipboards, and files can be sent both ways")
            return True
        if attempt == 1:
            print("  [ .. ] the pairing was dropped again (a click on that request does it): once more ...", flush=True)
            time.sleep(2)
    print(f"  [ -- ] the pairing with {name} does not hold. Wait a minute without touching the pairing requests, then run "
          "this again.")
    return False


def cmd_share(args: argparse.Namespace) -> int:
    """What Linux and the running Windows share, and the Linux half of setting it up: KDE Connect
    is told to look for Windows on the private link (its own search goes out on the LAN only);
    with --pair, the two KDE Connects are paired."""
    cfg = load_config(args.config)
    u = audio_user(cfg) if os.geteuid() == 0 else pwd.getpwuid(os.getuid())
    if not u:
        raise GameVMError("audio.user (the desktop user) is not set in the config")
    running = domain_state(cfg["vm"]["name"]) == "running"
    print("Windows is running." if running else "Windows is off: its drives are Linux' own (gamevm files); "
          "the rest below applies while it runs.")
    if not cfg["network"]["hostonly"]:
        print("  network.hostonly is off: there is no link between Linux and the running Windows.")
        return 1

    print("Files")
    if running and tcp_open(GUEST_LINK_IP, 445):
        print(f"  [ OK ] Windows shares its drives: gamevm files  (smb://{GUEST_LINK_IP}/)")
    elif running:
        print(f"  [ -- ] Windows does not answer on {GUEST_LINK_IP}: run {BENCH_GUEST_DIR}\\2-inside-vm.ps1 in Windows, as "
              "administrator (gamevm scripts copies the current version there)")
    else:
        print(f"  [ .. ] while Windows runs: gamevm files  (smb://{GUEST_LINK_IP}/, set up by 2-inside-vm.ps1)")

    print("Clipboard and sending files (KDE Connect)")
    daemon = KDE_DAEMON
    custom = kdeconnect(u, "get-property", *daemon, "customDevices")
    if custom is None or custom[:1] != ["as"]:
        print("  [ -- ] KDE Connect does not answer on Linux: sudo pacman -S kdeconnect, then log out and in")
        return 1
    known = custom[2:]
    if GUEST_LINK_IP not in known:
        new = [*known, GUEST_LINK_IP]
        if kdeconnect(u, "set-property", *daemon, "customDevices", "as", str(len(new)), *new, mutate=True) is None:
            print(f"  [ -- ] could not add {GUEST_LINK_IP} to KDE Connect's devices: add it under KDE Connect settings > "
                  "Configure > Custom devices")
        else:
            print(f"  [ OK ] KDE Connect now also looks for Windows at {GUEST_LINK_IP} (its custom devices)")
    else:
        print(f"  [ OK ] KDE Connect looks for Windows at {GUEST_LINK_IP}")
    kdeconnect(u, "call", *daemon, "forceOnNetworkChange", mutate=True)  # look now
    if running:
        time.sleep(1.5)

    def names(only_paired: bool) -> list[str]:
        out = kdeconnect(u, "call", *daemon, "deviceNames", "bb", "true", "true" if only_paired else "false") or []
        return out[3::2] if out[:1] == ["a{ss}"] else []

    if running and getattr(args, "pair", False):
        if not pair_windows(cfg, u):
            return 1
        try:  # Windows' side of sending files: a right-click entry and a small window
            ok, said = install_windows_share(cfg)
        except (GameVMError, subprocess.TimeoutExpired, ValueError, OSError) as e:
            ok, said = False, str(e)
        print(f"  [ OK ] in Windows: right-click a file > Send to > Linux (gamevm), or Start menu > Send to Linux" if ok
              else f"  [ .. ] the Send to Linux entry was not added in Windows ({said}); gamevm share --pair tries again")
        return 0
    trouble = clock_trouble(cfg) if running else ""
    if trouble:
        print(f"  [ -- ] {trouble}")
    paired, reachable = names(True), names(False)
    waiting = [n for n in reachable if n not in paired]
    if paired:
        print(f"  [ OK ] connected and paired: {', '.join(paired)}. Text you copy is on both clipboards; send a file with "
              "right-click > Send to device (Dolphin) or from KDE Connect in Windows")
    if waiting:
        print(f"  [ .. ] found, not paired yet: {', '.join(waiting)}. Pair Windows with:  gamevm share --pair")
    if not paired and not waiting:
        print("  [ .. ] no device found" + (": open KDE Connect in Windows once (2-inside-vm.ps1 installs it), then run "
                                              "gamevm share again" if running else " (Windows is off)"))
    return 0


def qemu_pid(name: str) -> int | None:
    for pdir in Path("/proc").iterdir():
        if pdir.name.isdigit() and read(pdir / "comm").startswith("qemu-system"):
            if f"guest={name}," in read(pdir / "cmdline").replace("\0", " "):
                return int(pdir.name)
    return None


def task_cpu_seconds(stat: str) -> float:
    f = stat.rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def host_sample(pid: int) -> dict:
    """CPU time of each QEMU thread and of its vhost kernel threads, per-CPU interrupts and load."""
    threads = {}
    for t in Path(f"/proc/{pid}/task").iterdir():
        stat = read(t / "stat")
        if stat:
            threads[t.name] = (stat[stat.find("(") + 1:stat.rfind(")")], task_cpu_seconds(stat))
    vhost = sum(task_cpu_seconds(read(p / "stat")) for p in Path("/proc").iterdir()
                if p.name.isdigit() and read(p / "comm") == f"vhost-{pid}")
    lines = read("/proc/interrupts").splitlines()
    ncpu = len(lines[0].split()) if lines else 0
    irq, loc = [0] * ncpu, [0] * ncpu
    for line in lines[1:]:
        parts = line.split()
        vals = [int(x) for x in parts[1:1 + ncpu] if x.isdigit()]
        if len(vals) == ncpu:
            irq = [x + y for x, y in zip(irq, vals)]
            if parts[0] == "LOC:":
                loc = vals
    cpu = {}
    for line in read("/proc/stat").splitlines():
        if line.startswith("cpu") and line[3:4].isdigit():
            f = line.split()
            v = [int(x) for x in f[1:9]]
            cpu[int(f[0][3:])] = (sum(v) - v[3] - v[4], sum(v))
    return {"time": time.monotonic(), "threads": threads, "vhost": vhost, "irq": irq, "loc": loc, "cpu": cpu}


def host_overhead(a: dict, b: dict, plan: dict) -> dict:
    """What running the guest cost the host between two samples."""
    secs = b["time"] - a["time"]
    vcpu = other = 0.0
    for tid, (comm, t1) in b["threads"].items():
        d = t1 - a["threads"].get(tid, (comm, 0.0))[1]
        if re.fullmatch(r"CPU \d+/KVM", comm):
            vcpu += d
        else:
            other += d
    vhost = b["vhost"] - a["vhost"]
    guest = [c for c in plan["guest"] if c < len(b["irq"])]

    def per_s(key: str) -> float:
        return sum(b[key][c] - a[key][c] for c in guest) / secs / max(1, len(guest))

    def busy(cpus: list[int]) -> float:
        d = [(b["cpu"][c][0] - a["cpu"][c][0], b["cpu"][c][1] - a["cpu"][c][1]) for c in cpus if c in a["cpu"]]
        return 100.0 * sum(x for x, _ in d) / max(1, sum(y for _, y in d))

    host = [c for c in plan["host"] if c not in plan["guest"]]
    return {"seconds": round(secs, 1), "vcpu_cpu_s": round(vcpu, 1), "qemu_other_cpu_s": round(other, 1),
            "vhost_cpu_s": round(vhost, 1),
            "overhead_pct": round(100.0 * (other + vhost) / vcpu, 2) if vcpu else None,
            "guest_cpu_irqs_per_s": round(per_s("irq"), 0), "guest_cpu_timer_ticks_per_s": round(per_s("loc"), 0),
            "host_cores_busy_pct": round(busy(host), 1) if host else None}


def bench_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "gamevm" / "bench"


def guest_results(cfg: dict) -> list[dict]:
    _, out, _ = guest_powershell(cfg, f"Get-ChildItem '{BENCH_GUEST_DIR}\\bench' -Filter *.json -ErrorAction "
                                 "SilentlyContinue | Sort-Object Name | ForEach-Object { Get-Content $_.FullName -Raw }")
    res = []
    for line in out.splitlines():
        try:
            res.append(json.loads(line))
        except ValueError:
            continue
    return res


def all_results(cfg: dict, running: bool) -> list[dict]:
    """Runs from Windows' C:\\gamevm\\bench (native and VM) and the host's richer copies of VM runs."""
    found: dict[tuple, dict] = {}
    if running:
        found.update({(r.get("where"), r.get("time")): r for r in guest_results(cfg)})
    else:  # Windows is off: its system drive is mounted on Linux (one of disk.unmount)
        for f in sorted(f for mp in cfg["disk"]["unmount"] for f in (Path(mp) / "gamevm" / "bench").glob("*.json")):
            try:
                r = json.loads(f.read_text(encoding="utf-8-sig"))
                found[(r.get("where"), r.get("time"))] = r
            except (OSError, ValueError):
                continue
    for f in sorted(bench_dir().glob("*.json")):
        try:
            r = json.loads(f.read_text())
            found[(r.get("where"), r.get("time"))] = r
        except (OSError, ValueError):
            continue
    return sorted(found.values(), key=lambda r: r.get("time", ""))


def print_bench(results: list[dict]) -> None:
    if not results:
        print("No results yet: `gamevm bench` while Windows runs; natively, run C:\\gamevm\\3-benchmark.ps1.")
        return
    base = next((r for r in reversed(results) if r.get("where") == "native"), None)
    print(f"{'run':<42}{'CPUs':>5}" + "".join(f"{col:>15}" for _, col, _, _ in BENCH_METRICS))
    for r in results:
        lay = r.get("layout") or {}
        what = r.get("where", "?") + (f" {lay['cores']}c" if lay.get("cores") else "") + \
            (" thp" if lay.get("hugepages") is False else "")
        name = f"{what} {r.get('time', '')[:16].replace('T', ' ')} {r.get('label', '')}"[:41]
        row = f"{name:<42}{r.get('logical_cpus', ''):>5}"
        for key, _, _, higher in BENCH_METRICS:
            v = r.get(key)
            if v is None:
                row += f"{'-':>15}"
                continue
            cell = f"{v:g}"
            b = base.get(key) if base and r is not base else None
            if b:
                pct = 100.0 * v / b
                cell += f" ({pct:.0f}%)" if higher else f" ({pct - 100:+.0f}%)"
            row += f"{cell:>15}"
        print(row)
    print(f"units: {', '.join(f'{col} {unit}' for _, col, unit, _ in BENCH_METRICS)}; "
          "(%) = against the latest native run" + ("" if base else " (none yet: run the script natively)"))
    for r in results:
        h = r.get("host")
        if h:
            print(f"  host side of {r.get('where')} {r.get('time', '')[:16].replace('T', ' ')} {r.get('label', '')}: "
                  f"QEMU emulation {h['qemu_other_cpu_s']} CPU-s + vhost {h['vhost_cpu_s']} CPU-s = "
                  f"{h['overhead_pct']}% on top of the vCPUs' {h['vcpu_cpu_s']} CPU-s; on Windows' cores the host "
                  f"took {h['guest_cpu_irqs_per_s']:g} interrupts/s per CPU ({h['guest_cpu_timer_ticks_per_s']:g} "
                  "timer ticks)" + (f"; Linux's own cores {h['host_cores_busy_pct']}% busy"
                                    if h.get("host_cores_busy_pct") is not None else ""))


def cmd_bench(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    name = cfg["vm"]["name"]
    running = domain_state(name) == "running"
    if args.compare:
        print_bench(all_results(cfg, running))
        return 0
    if not running:
        raise GameVMError("Windows is not running: `gamevm start` first. For the native baseline boot Windows "
                          "natively and run C:\\gamevm\\3-benchmark.ps1 in an administrator PowerShell (docs/06).")
    agent(cfg, {"execute": "guest-ping"})
    guest_powershell(cfg, f"New-Item -ItemType Directory -Force -Path '{BENCH_GUEST_DIR}\\bench' | Out-Null")
    guest_write(cfg, f"{BENCH_GUEST_DIR}\\3-benchmark.ps1", bench_script().read_bytes())
    xml = virsh("dumpxml", name, check=False, mutate=False).stdout or ""
    plan = cpu_plan(cfg)
    layout = {"cores": plan["cores"], "vcpus": len(plan["guest"]), "guest_cpus": plan["guest"],
              "host_cpus": plan["host"], "isolated": cfg["cpu"]["isolate_host"],
              "hugepages": bool(re.search(r"<hugepages[\s>/]", xml))}
    pid = qemu_pid(name)
    print(f"running the benchmark in Windows ({layout['vcpus']} vCPUs on {layout['cores']} cores, "
          f"{'huge pages' if layout['hugepages'] else 'transparent huge pages'}); about a minute, "
          "keep other programs idle ...")
    a = host_sample(pid) if pid else None
    argv = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            f"{BENCH_GUEST_DIR}\\3-benchmark.ps1", "-Seconds", str(args.seconds), "-Quiet"]
    if args.label:
        argv += ["-Label", args.label]
    if args.no_disk:
        argv.append("-SkipDisk")
    code, out, err = guest_run(cfg, "powershell.exe", argv, timeout=60 + 12 * args.seconds)
    b = host_sample(pid) if pid else None
    line = next((x for x in out.splitlines() if x.startswith(BENCH_MARK)), None)
    if code != 0 or not line:
        raise GameVMError(f"the benchmark failed in Windows (exit {code}): {(err or out).strip()[-1500:]}")
    result = json.loads(line[len(BENCH_MARK):])
    result["layout"] = layout
    if a and b:
        result["host"] = host_overhead(a, b, plan)
    d = bench_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{result['time'].replace(':', '')}-vm.json").write_text(json.dumps(result, indent=1))
    print_bench(all_results(cfg, True))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    sys.stdout.write(render_xml(cfg, args.mode))
    return 0


def cmd_define(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    define(cfg, args.mode)
    print(f"defined {cfg['vm']['name']}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    require_root("recover")
    st = HostState.load()
    snap = st.data.get("cfg") if st else None
    try:
        cfg = load_config(ETC_CONFIG, trusted=True)
    except GameVMError as e:
        if not snap:
            raise
        log(f"{e}\nusing the configuration saved when the VM was started", "warn")
        cfg = snap
    state = domain_state(cfg["vm"]["name"])
    if not domain_off(state) and not args.force:
        raise GameVMError(f"the VM is {state}; stop it first (gamevm stop / gamevm kill), or --force")
    if st and st.data.get("steps"):
        log("undoing recorded host changes")
        undo(snap or cfg, st)
    plan = cpu_plan(cfg)
    for unit in ISOLATED_UNITS:
        run(["systemctl", "set-property", "--runtime", "--", unit, f"AllowedCPUs={cpuset(plan['all'])}"], check=False)
    if WORKQUEUE_MASK.exists():
        write_sysfs(WORKQUEUE_MASK, cpumask_hex(plan["all"]))
    if gpu_dedicated():
        log("GPU is dedicated to the VM (vfio-pci from boot): leaving it on vfio-pci")
    elif all(pci_driver(b) in (None, "vfio-pci") for b in cfg["gpu"]["devices"] if pci_exists(b)):
        reattach_gpu(cfg, cfg["gpu"]["host_modules"])
    if REBOOT_MARKER.exists() and not DRY_RUN:
        # Set when the GPU did not come back after a VM run; gamevm start refuses while it exists.
        if gpu_dedicated() or gpu_reinit_ok(cfg, time.time()):
            REBOOT_MARKER.unlink(missing_ok=True)
            log("the NVIDIA GPU answers again: Windows can be started without a reboot")
        else:
            log("the NVIDIA GPU still does not initialise: only a reboot of the host fixes that", "error")
    for vt in glob.glob("/sys/class/vtconsole/vtcon*"):
        if "frame buffer" in read(f"{vt}/name") and read(f"{vt}/bind") == "0":
            write_sysfs(f"{vt}/bind", "1")
    dm = cfg["single_gpu"]["display_manager"]
    if not unit_active(dm):
        run(["systemctl", "start", dm], check=False)
    if kwin_managed() and kwin_layout() == "igpu" and module_loaded("nvidia") and not gpu_dedicated():
        u = audio_user(cfg)
        if u:
            desktop_to_native(cfg, {"user": u.pw_name, "reopened": True, "started": time.time() - 600})
    print("recovery done")
    return 0


# ---------------------------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.fails = 0
        self.warns = 0

    def ok(self, msg):
        print(f"  [ OK ] {msg}")

    def warn(self, msg, hint=""):
        self.warns += 1
        print(f"  [WARN] {msg}" + (f"\n         -> {hint}" if hint else ""))

    def fail(self, msg, hint=""):
        self.fails += 1
        print(f"  [FAIL] {msg}" + (f"\n         -> {hint}" if hint else ""))

    def section(self, title):
        print(f"\n{title}")


def cmd_doctor(args: argparse.Namespace) -> int:
    from gamevm_doctor import doctor  # kept separate to keep this file focused
    return doctor(args, sys.modules[__name__])


def cmd_discover(args: argparse.Namespace) -> int:
    from gamevm_discover import main as discover_main  # what the installer finds on this PC: gamevm_discover.py
    return discover_main(args, sys.modules[__name__])


def cmd_ui(args: argparse.Namespace) -> int:
    from gamevm_ui import main as ui_main  # the window and its local service: gamevm_ui.py, ui/*.qml
    return ui_main(args, sys.modules[__name__])


def cmd_config_apply(args: argparse.Namespace) -> int:
    """(root; the window runs it through pkexec) Install a configuration the user prepared. The
    file has to belong to the user who asked (root reads nothing for them that they could not
    read themselves), is checked like any configuration, and replaces /etc/gamevm/gamevm.toml;
    the previous one is kept beside it as gamevm.toml.bak."""
    require_root("config-apply")
    asker = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID") or "0"
    try:
        fd = os.open(args.file, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise GameVMError(f"cannot read {args.file}: {e.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or str(st.st_uid) not in (asker, "0") or st.st_size > 256 * 1024:
            raise GameVMError(f"{args.file} is not a configuration file of the user who asked")
        data = f.read()
    new = ETC_CONFIG.with_name(".gamevm.toml.new")
    new.write_bytes(data)
    os.chmod(new, 0o644)
    try:
        load_config(new)  # raises with the reason
    except GameVMError:
        new.unlink(missing_ok=True)
        raise
    if DRY_RUN:
        new.unlink(missing_ok=True)
        print(f"[dry-run] would replace {ETC_CONFIG}", file=sys.stderr)
        return 0
    if ETC_CONFIG.exists():
        shutil.copy2(ETC_CONFIG, ETC_CONFIG.with_name(ETC_CONFIG.name + ".bak"))
    os.replace(new, ETC_CONFIG)
    log(f"configuration replaced through the gamevm window (previous: {ETC_CONFIG.name}.bak)")
    print("Saved. It applies the next time Windows starts.")
    return 0


# ---------------------------------------------------------------------------------------------

def cmd_mac(args: argparse.Namespace) -> int:
    """MAC address of the VM's LAN or host-only NIC (used by the installer for the DHCP reservation)."""
    cfg = load_config(args.config)
    net = cfg["network"]
    print(net["mac"] or derived_mac(cfg, 0) if args.which == "lan" else net["hostonly_mac"] or derived_mac(cfg, 1))
    return 0


def main(argv: list[str] | None = None) -> int:
    global DRY_RUN
    syslog.openlog("gamevm", syslog.LOG_PID)  # journalctl -t gamevm (the hook runs as gamevm.py)
    p = argparse.ArgumentParser(prog="gamevm", description=__doc__.split("\n")[0])
    p.add_argument("-c", "--config", help=f"config file (default {ETC_CONFIG})")
    p.add_argument("--dry-run", action="store_true", help="print host-changing actions instead of doing them")
    p.add_argument("--hold", action="store_true",
                   help="after an error, wait for Enter (for launchers, whose terminal window closes at once)")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    s = sub.add_parser("start", help="start the VM")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    s.add_argument("--close-apps", action="store_true", help="do not ask before closing apps")
    s.add_argument("--reset-nvram", action="store_true",
                   help="recreate the VM's UEFI variables from the template (after changing secure_boot)")
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("mac")  # internal: not listed
    s.add_argument("which", choices=["lan", "hostonly"])
    s.set_defaults(fn=cmd_mac)
    s = sub.add_parser("stop", help="shut the VM down cleanly")
    s.add_argument("--timeout", type=int, default=180)
    s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("kill", help="force power-off")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_kill)
    sub.add_parser("status", help="show state").set_defaults(fn=cmd_status)
    sub.add_parser("holders", help="processes using the NVIDIA GPU").set_defaults(fn=cmd_holders)
    s = sub.add_parser("usb", help="hot-plug a USB device into the running VM")
    s.add_argument("action", choices=["attach", "detach"])
    s.add_argument("device", help="VID:PID from lsusb")
    s.set_defaults(fn=cmd_usb)
    s = sub.add_parser("view", help="switch the shared monitor to the VM or the host input (DDC/CI)")
    s.add_argument("target", choices=["vm", "host"])
    s.set_defaults(fn=cmd_view)
    s = sub.add_parser("lg", help="open the Looking Glass client")
    s.add_argument("extra", nargs=argparse.REMAINDER, help="extra looking-glass-client options")
    s.set_defaults(fn=cmd_lg)
    for name, fn in (("render", cmd_render), ("define", cmd_define)):
        s = sub.add_parser(name, help=f"{name} the libvirt domain XML")
        s.add_argument("--mode", choices=["igpu", "single"], help="override the detected mode")
        s.set_defaults(fn=fn)
    s = sub.add_parser("bench", help="benchmark Windows in the VM (and compare with native runs)")
    s.add_argument("--label", default="", help="a note stored with the result, e.g. 6-cores")
    s.add_argument("--seconds", type=int, default=10, help="length of each timed test (default 10)")
    s.add_argument("--no-disk", action="store_true", help="skip the disk read test")
    s.add_argument("--compare", action="store_true", help="only show the saved results")
    s.set_defaults(fn=cmd_bench)
    s = sub.add_parser("doctor", help="preflight checks")
    s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("desktop", help="(root) move the Linux desktop: native (the graphics card too) or igpu")
    s.add_argument("layout", choices=["native", "igpu"])
    s.set_defaults(fn=cmd_desktop)
    sub.add_parser("files", help="open Windows' drives in the file manager (through Windows while it runs)"
                   ).set_defaults(fn=cmd_files)
    s = sub.add_parser("discover", help="show the hardware gamevm would use on this PC (what the installer writes)")
    s.add_argument("--json", action="store_true", help="as JSON")
    s.set_defaults(fn=cmd_discover)
    s = sub.add_parser("ui", help="open the gamevm window: start and stop Windows, set it up, choose what it gets")
    s.add_argument("--page", choices=["home", "sharing", "resources", "setup", "settings", "help"], default="home",
                   help="the page to open on")
    s.add_argument("--shot", metavar="FILE", help=argparse.SUPPRESS)  # tests: save a picture of the window and quit
    s.add_argument("--size", metavar="WxH", help=argparse.SUPPRESS)
    s.add_argument("--delay", metavar="MS", help=argparse.SUPPRESS)
    s.add_argument("--open", metavar="WHAT", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_ui)
    s = sub.add_parser("config-apply")  # internal (root, from the window): not listed
    s.add_argument("file")
    s.set_defaults(fn=cmd_config_apply)
    s = sub.add_parser("share", help="clipboard and file sharing with the running Windows: status, setup and pairing")
    s.add_argument("--pair", action="store_true", help="pair KDE Connect on Linux with KDE Connect in the running Windows")
    s.set_defaults(fn=cmd_share)
    sub.add_parser("scripts", help="copy the current Windows scripts into the running VM (C:\\gamevm)"
                   ).set_defaults(fn=cmd_scripts)
    sub.add_parser("input", help="plug the keyboard and mouse into the running VM again (after a re-plug)"
                   ).set_defaults(fn=cmd_input)
    sub.add_parser("input-watch").set_defaults(fn=cmd_input_watch)  # internal: not listed
    sub.add_parser("back", help="bring the Linux desktop back to the main monitor (asks for the password)"
                   ).set_defaults(fn=cmd_back)
    s = sub.add_parser("kwin-boot")  # internal: not listed
    s.set_defaults(fn=cmd_kwin_boot)
    s = sub.add_parser("host-shutdown")  # internal: not listed (ExecStop of gamevm-shutdown.service)
    s.set_defaults(fn=cmd_host_shutdown)
    s = sub.add_parser("probe-input", help="(root) find the keyboard/mouse event nodes to pass through")
    s.add_argument("--seconds", type=int, default=10)
    s.set_defaults(fn=cmd_probe_input)
    s = sub.add_parser("recover", help="(root) restore the host after a crash")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_recover)
    s = sub.add_parser("selftest", help="with --dry-run: show what the hook would do")
    s.add_argument("--mode", choices=["igpu", "single"])
    s.set_defaults(fn=cmd_selftest)
    s = sub.add_parser("hook")  # internal: not listed
    s.add_argument("hookargs", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_hook)

    args = p.parse_args(argv)
    DRY_RUN = args.dry_run
    try:
        rc = args.fn(args)
    except GameVMError as e:
        print(f"gamevm: {e}", file=sys.stderr)
        rc = 1
    except KeyboardInterrupt:
        return 130
    if rc and args.hold and not DECLINED and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return rc


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    sys.exit(main())
