# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""Find the hardware gamevm needs on the PC it runs on: `gamevm discover`.

The installer writes the result into /etc/gamevm/gamevm.toml, so nobody has to look up PCI
addresses. It decides only what can be read off the machine without doubt, and says so when it
cannot: the wrong graphics card is a broken start, the wrong disk is a destroyed system. Rules:

  graphics card for Windows  the one NVIDIA card (gamevm unloads the NVIDIA driver to hand it over)
  graphics for Linux         any other graphics chip, e.g. the one built into the CPU -> iGPU mode
  Windows disk               an NVMe drive with a Windows system layout (EFI + Microsoft reserved
                             partition) that carries nothing of the running Linux. The whole drive
                             is handed over, so Windows and Linux on ONE drive cannot work.
  keyboard, mouse            the /dev/input/by-id nodes of a real keyboard and a real mouse
  processor, memory          Linux keeps the first core (and the last on 6 cores or more), Windows
                             gets the rest; half the memory
  network                    the card with the default route; Wi-Fi cannot give Windows its own
                             address on the LAN, so it goes through Linux then

Everything here only reads. The parsing functions take their input as arguments (tests feed them).
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from pathlib import Path

SYS_PCI = Path("/sys/bus/pci/devices")
NVIDIA = "0x10de"
PART_EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
PART_MS_RESERVED = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
PART_MS_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
PART_MS_RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
LINUX_MOUNTS = ("/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[SWAP]")


def read(path, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return default


def pci_name(bdf: str) -> str:
    """A readable name from lspci ('GeForce RTX 4070', 'Radeon Graphics'), else the PCI address."""
    try:
        out = subprocess.run(["lspci", "-s", bdf], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return bdf
    text = out.split(": ", 1)[-1] if ": " in out else out
    text = re.sub(r"\s*\(rev [0-9a-f]+\)$", "", text)
    m = re.findall(r"\[([^\[\]]+)\]", text)
    return (m[-1] if m else text) or bdf


# ---------------------------------------------------------------------------------------------
# graphics
# ---------------------------------------------------------------------------------------------

def pci_display_devices(sys_pci: Path = SYS_PCI) -> list[dict]:
    out = []
    for dev in sorted(sys_pci.iterdir()) if sys_pci.is_dir() else []:
        if read(dev / "class").startswith("0x03"):
            out.append({"bdf": dev.name, "vendor": read(dev / "vendor"), "boot_vga": read(dev / "boot_vga") == "1",
                        "driver": os.path.basename(os.path.realpath(dev / "driver")) if (dev / "driver").exists() else ""})
    return out


def choose_gpus(displays: list[dict], all_pci: list[str]) -> dict:
    """{'devices': every function of the card for Windows, 'host': the other graphics chips,
    'problem': why nothing could be chosen}. `all_pci` are all PCI addresses of the machine."""
    nvidia = [d for d in displays if d["vendor"] == NVIDIA]
    others = [d["bdf"] for d in displays if d["vendor"] != NVIDIA]
    if not nvidia:
        return {"devices": [], "host": others, "problem":
                "no NVIDIA graphics card found. gamevm hands an NVIDIA card to Windows; other cards are not supported yet."}
    if len(nvidia) > 1:
        return {"devices": [], "host": others, "candidates": [d["bdf"] for d in nvidia], "problem":
                "more than one NVIDIA graphics card found: put the PCI addresses of the one for Windows (all its "
                "functions) into gpu.devices yourself."}
    slot = nvidia[0]["bdf"].rsplit(".", 1)[0]
    functions = sorted(b for b in all_pci if b.rsplit(".", 1)[0] == slot)
    return {"devices": functions, "host": others, "problem": ""}


# ---------------------------------------------------------------------------------------------
# disks
# ---------------------------------------------------------------------------------------------

def lsblk() -> list[dict]:
    try:
        out = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,TYPE,TRAN,MODEL,SIZE,LOG-SEC,PARTTYPE,FSTYPE,MOUNTPOINTS,"
                              "UUID,PARTUUID,LABEL,PARTLABEL"], capture_output=True, text=True, timeout=20).stdout
        return json.loads(out or "{}").get("blockdevices", [])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def descendants(node: dict) -> list[dict]:
    out = []
    for child in node.get("children") or []:
        out.append(child)
        out += descendants(child)
    return out


def nvme_controller(disk_name: str, sys_block: Path = Path("/sys/block")) -> str:
    """The PCI address of the NVMe controller a disk (nvme0n1) hangs on, '' if it cannot be told."""
    m = re.findall(r"/([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])/nvme/", os.path.realpath(sys_block / disk_name / "device") + "/")
    return m[-1] if m else ""


def stable_disk_path(disk_name: str, by_id: Path = Path("/dev/disk/by-id")) -> str:
    """The name of a whole disk that stays the same across boots (sda can become sdb): the one with
    model and serial number (ata-...) before the bare identifier (wwn-...); '' if it has none."""
    try:
        names = sorted(n for n in os.listdir(by_id) if "-part" not in n and os.path.basename(os.path.realpath(by_id / n)) == disk_name)
    except OSError:
        return ""
    for prefix in ("ata-", "wwn-"):
        for n in names:
            if n.startswith(prefix):
                return str(by_id / n)
    return ""


def choose_windows_disk(blockdevices: list[dict], controller_of=nvme_controller, path_of=stable_disk_path) -> dict:
    """{'nvme': PCI address of the drive's controller (an NVMe drive is handed over as hardware),
    'boot_disk': /dev/disk/by-id/ path (a SATA disk is opened by the VM as a whole), 'bus': 'nvme'
    or 'sata', 'disk': its name, 'model', 'others': NTFS data disks worth mentioning, 'problem'}.
    A Windows system disk has an EFI partition and a Microsoft reserved (or recovery) partition; a
    data disk formatted by Windows has no EFI partition."""
    disks = [b for b in blockdevices if b.get("type") == "disk" and not b["name"].startswith(("zram", "loop"))]

    def kinds(disk):
        return {(p.get("parttype") or "").lower() for p in descendants(disk)}

    def has_linux(disk):
        return any(m in LINUX_MOUNTS for p in [disk, *descendants(disk)] for m in (p.get("mountpoints") or []) if m)

    def is_windows_system(disk):
        k = kinds(disk)
        return PART_EFI in k and (PART_MS_RESERVED in k or PART_MS_RECOVERY in k)

    def label(disk):
        return f"{(disk.get('model') or disk['name']).strip()} ({disk['name']})"

    def bus(disk):
        tran = (disk.get("tran") or "").lower()
        return "nvme" if tran == "nvme" else "sata" if tran in ("sata", "ata") else tran

    systems = [d for d in disks if is_windows_system(d)]
    data = [label(d) for d in disks if d not in systems and not has_linux(d)
            and any((p.get("fstype") or "") == "ntfs" for p in descendants(d))]
    usable = [d for d in systems if bus(d) in ("nvme", "sata") and not has_linux(d)]
    res = {"nvme": "", "boot_disk": "", "bus": "", "disk": "", "model": "", "others": data, "problem": ""}
    if len(usable) == 1 and bus(usable[0]) == "nvme":
        res.update(disk=usable[0]["name"], model=(usable[0].get("model") or "").strip(), bus="nvme",
                   nvme=controller_of(usable[0]["name"]))
        linux_on_same_controller = [d["name"] for d in disks if d is not usable[0] and has_linux(d)
                                    and controller_of(d["name"]) == res["nvme"] and res["nvme"]]
        if not res["nvme"]:
            res["problem"] = f"the NVMe controller of {label(usable[0])} could not be found"
        elif linux_on_same_controller:
            res.update(nvme="", problem=f"{label(usable[0])} shares its NVMe controller with the Linux disk")
    elif len(usable) == 1:
        # A SATA disk has no controller of its own to hand over (the board's SATA controller also
        # carries other disks), so the VM opens the disk itself and shows it to Windows as SATA.
        d = usable[0]
        res.update(disk=d["name"], model=(d.get("model") or "").strip(), bus="sata", boot_disk=path_of(d["name"]))
        sector = str(d.get("log-sec") or 512)
        if not res["boot_disk"]:
            res["problem"] = (f"{label(d)} has no stable name under /dev/disk/by-id/ (ata-... or wwn-...), so it cannot be "
                              "told apart from other disks after a reboot")
        elif sector != "512":
            res.update(boot_disk="", problem=f"{label(d)} has {sector}-byte sectors. A SATA disk in the VM always has "
                       "512-byte sectors, so Windows could not read its partitions there.")
    elif len(usable) > 1:
        res.update(candidates=[label(d) for d in usable], problem=
                   "more than one drive with a Windows installation: " + ", ".join(label(d) for d in usable)
                   + ". Put the right one into the configuration yourself: disk.nvme (the PCI address of an NVMe drive) "
                   "or disk.boot_disk (the /dev/disk/by-id/ path of a SATA disk).")
    elif any(has_linux(d) for d in systems):
        res["problem"] = ("Windows and Linux are installed on the same drive. gamevm gives the whole Windows drive to the VM, "
                          "so Windows has to be on a drive of its own.")
    elif systems:
        res["problem"] = (f"Windows was found on {label(systems[0])}, which is connected through "
                          f"{bus(systems[0]) or 'an unknown bus'}. gamevm starts Windows from an internal NVMe or SATA drive.")
    else:
        res["problem"] = "no drive with an installed Windows was found (an EFI partition plus a Microsoft reserved partition)."
    return res


def mount_points_of(disk_name: str, blockdevices: list[dict], fstab: str) -> list[str]:
    """Where Linux mounts partitions of that disk: now, or by an /etc/fstab line (an automount that
    is not mounted right now counts). These are unmounted before Windows starts."""
    disk = next((b for b in blockdevices if b["name"] == disk_name), None)
    if not disk:
        return []
    parts = descendants(disk)
    found = [m for p in parts for m in (p.get("mountpoints") or []) if m and m.startswith("/")]
    keys = {}
    for p in parts:
        keys[f"/dev/{p['name']}"] = p
        for tag, col in (("UUID", "uuid"), ("PARTUUID", "partuuid"), ("LABEL", "label"), ("PARTLABEL", "partlabel")):
            if p.get(col):
                keys[f"{tag}={p[col]}"] = p
    for line in fstab.splitlines():
        f = line.split("#", 1)[0].split()
        if len(f) >= 2 and f[0].replace('"', "") in keys and f[1].startswith("/"):
            found.append(f[1].replace("\\040", " "))
    return sorted(set(found))


# ---------------------------------------------------------------------------------------------
# keyboard and mouse
# ---------------------------------------------------------------------------------------------

def choose_input(names: list[str], by_id: str = "/dev/input/by-id") -> dict:
    """{'keyboard', 'mouse', 'extra', 'note'} from the names in /dev/input/by-id. A keyboard is a
    device with an -event-kbd node; its further keyboard nodes (media keys) follow as `extra`. A
    mouse is an -event-mouse node of a device that is not a keyboard (many keyboards also announce
    a mouse); only when there is no such device, a keyboard's own mouse node is taken."""
    nodes = []
    for n in sorted(names):
        m = re.fullmatch(r"(.+?)(-if\d+)?-event-(kbd|mouse)", n)
        if m:
            nodes.append({"name": n, "device": m.group(1), "sub": bool(m.group(2)), "kind": m.group(3)})
    keyboards = [x for x in nodes if x["kind"] == "kbd"]
    main = [x for x in keyboards if not x["sub"]] or keyboards
    res = {"keyboard": "", "mouse": "", "extra": [], "note": ""}
    notes = []
    if main:
        kb = main[0]
        res["keyboard"] = f"{by_id}/{kb['name']}"
        res["extra"] = [f"{by_id}/{x['name']}" for x in keyboards if x["device"] == kb["device"] and x is not kb]
        if len({x["device"] for x in main}) > 1:
            notes.append("several keyboards are connected: `sudo gamevm probe-input` shows which one you type on")
    else:
        notes.append("no keyboard found under /dev/input/by-id (a Bluetooth keyboard cannot be handed to Windows this way)")
    kb_devices = {x["device"] for x in keyboards}
    mice = [x for x in nodes if x["kind"] == "mouse"]
    real = [x for x in mice if x["device"] not in kb_devices] or mice
    if real:
        res["mouse"] = f"{by_id}/{real[0]['name']}"
        if len({x["device"] for x in real}) > 1:
            notes.append("several mice are connected: check input.mouse")
    else:
        notes.append("no mouse found under /dev/input/by-id")
    res["note"] = "; ".join(notes)
    return res


# ---------------------------------------------------------------------------------------------
# processor, memory, network
# ---------------------------------------------------------------------------------------------

def cpu_cores(sys_cpu: Path = Path("/sys/devices/system/cpu")) -> dict[int, list[int]]:
    """{core id: [logical CPUs]} of the first processor package (online CPUs)."""
    cores: dict[tuple[int, int], list[int]] = {}
    for d in glob.glob(str(sys_cpu / "cpu[0-9]*")):
        topo = Path(d) / "topology"
        if not (topo / "core_id").exists():
            continue
        key = (int(read(topo / "physical_package_id", "0") or 0), int(read(topo / "core_id")))
        cores.setdefault(key, []).append(int(Path(d).name[3:]))
    first = min((k[0] for k in cores), default=0)
    return {core: sorted(cpus) for (pkg, core), cpus in sorted(cores.items()) if pkg == first}


def split_cores(cores: dict[int, list[int]]) -> dict:
    """{'guest_cores', 'host_cores', 'note'}. Linux keeps the first core, and the last one too from
    6 cores up. On processors with two kinds of cores (some with two threads, some with one) Windows
    gets only the cores with the most threads: the VM's processor layout needs them alike."""
    ids = sorted(cores)
    if len(ids) < 2:
        return {"guest_cores": ids, "host_cores": ids, "note": "a single core: Windows and Linux have to share it"}
    most = max(len(v) for v in cores.values())
    alike = [c for c in ids if len(cores[c]) == most]
    note = ""
    if len(alike) != len(ids):
        note = "this processor has two kinds of cores: Windows gets the ones with the most threads, Linux keeps the others"
        keep = {alike[0]} if len(alike) > 1 else set()
        guest = [c for c in alike if c not in keep]
        return {"guest_cores": guest, "host_cores": [c for c in ids if c not in guest], "note": note}
    keep = {ids[0], ids[-1]} if len(ids) >= 6 else {ids[0]}
    return {"guest_cores": [c for c in ids if c not in keep], "host_cores": sorted(keep), "note": note}


def memory_for_windows(meminfo: str) -> int:
    """MiB for Windows: half of the memory in steps of 2 GiB, at least 4 GiB, leaving Linux 6 GiB
    where there is that much."""
    m = re.search(r"^MemTotal:\s+(\d+) kB", meminfo, re.M)
    total = int(m.group(1)) // (1024 * 1024) if m else 16
    half = (total + 2) // 4 * 2
    return max(4, min(half, max(4, total - 6))) * 1024


def default_nic() -> tuple[str, bool]:
    """(the network card with the default route, whether it is Wi-Fi)."""
    try:
        out = subprocess.run(["ip", "-o", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    m = re.search(r"\bdev (\S+)", out)
    nic = m.group(1) if m else ""
    return nic, bool(nic) and Path(f"/sys/class/net/{nic}/wireless").exists()


# ---------------------------------------------------------------------------------------------

def discover() -> dict:
    """Everything above for this machine, with readable names and the problems found."""
    displays = pci_display_devices()
    all_pci = sorted(p.name for p in SYS_PCI.iterdir()) if SYS_PCI.is_dir() else []
    gpu = choose_gpus(displays, all_pci)
    blocks = lsblk()
    disk = choose_windows_disk(blocks)
    cores = cpu_cores()
    split = split_cores(cores)
    nic, wifi = default_nic()
    by_id = "/dev/input/by-id"
    inp = choose_input(os.listdir(by_id) if os.path.isdir(by_id) else [])
    problems = [p for p in (gpu["problem"], disk["problem"]) if p]
    notes = [n for n in (inp["note"], split["note"]) if n]
    if wifi:
        notes.append(f"{nic} is Wi-Fi: Windows reaches the network through Linux (network.mode = nat)")
    if disk["others"]:
        notes.append("other disks formatted by Windows: " + ", ".join(disk["others"]) + ". Windows only sees one of them "
                     "while it runs if you add it to disk.extra (docs/02-windows.md)")
    return {
        "gpu": {"devices": gpu["devices"], "name": pci_name(gpu["devices"][0]) if gpu["devices"] else ""},
        "host_gpu": [{"bdf": b, "name": pci_name(b)} for b in gpu["host"]],
        "mode": "igpu" if gpu["host"] else "single",
        "disk": {"nvme": disk["nvme"], "boot_disk": disk["boot_disk"], "bus": disk["bus"],
                 "name": disk["model"] or disk["disk"],
                 "unmount": mount_points_of(disk["disk"], blocks, read("/etc/fstab")) if disk["disk"] else []},
        "input": {k: inp[k] for k in ("keyboard", "mouse", "extra")},
        "cpu": {"guest_cores": split["guest_cores"], "host_cores": split["host_cores"],
                "threads": max((len(v) for v in cores.values()), default=1)},
        "memory_mib": memory_for_windows(read("/proc/meminfo")),
        "network": {"host_nic": nic, "mode": "nat" if wifi or not nic else "macvtap"},
        "problems": problems,
        "notes": notes,
    }


def describe(d: dict) -> list[str]:
    """What was found, as lines for a person."""
    lines = []
    lines.append("graphics card for Windows: " + (f"{d['gpu']['name']} ({', '.join(d['gpu']['devices'])})" if d["gpu"]["devices"] else "not found"))
    if d["host_gpu"]:
        lines.append("graphics for Linux meanwhile: " + ", ".join(f"{h['name']} ({h['bdf']})" for h in d["host_gpu"])
                     + "  -> iGPU mode: the Linux desktop stays on screen while Windows runs")
    else:
        lines.append("no second graphics chip -> single-GPU mode: the Linux desktop closes while Windows runs")
    if d["disk"]["nvme"]:
        lines.append(f"Windows disk: {d['disk']['name']} (NVMe controller {d['disk']['nvme']}, handed to Windows as hardware)")
    elif d["disk"]["boot_disk"]:
        lines.append(f"Windows disk: {d['disk']['name']} (SATA, {d['disk']['boot_disk']}; Windows sees it as a SATA disk)")
    else:
        lines.append("Windows disk: not found")
    if d["disk"]["unmount"]:
        lines.append("  mounted on Linux at: " + ", ".join(d["disk"]["unmount"]) + " (unmounted while Windows runs)")
    lines.append(f"keyboard: {d['input']['keyboard'] or 'not found'}")
    lines.append(f"mouse: {d['input']['mouse'] or 'not found'}")
    lines.append(f"processor: cores {d['cpu']['guest_cores']} for Windows, {d['cpu']['host_cores']} for Linux")
    lines.append(f"memory for Windows: {d['memory_mib'] // 1024} GiB")
    lines.append(f"network: {d['network']['host_nic'] or 'no default route'} ({d['network']['mode']})")
    lines += [f"note: {n}" for n in d["notes"]]
    lines += [f"PROBLEM: {p}" for p in d["problems"]]
    return lines


def main(args, _g=None) -> int:
    d = discover()
    if getattr(args, "json", False):
        print(json.dumps(d, indent=2))
    else:
        print("\n".join(describe(d)))
    return 1 if d["problems"] else 0
