# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""Finding the hardware (gamevm_discover.py), fed with made-up machines."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import check, finish  # noqa: E402
import gamevm_discover as d

EFI, MSR, DATA, WINRE, LINUX = d.PART_EFI, d.PART_MS_RESERVED, d.PART_MS_DATA, d.PART_MS_RECOVERY, "0fc63daf-8483-4772-8e79-3d69d8477de4"


def disk(name, tran, model, parts):
    return {"name": name, "type": "disk", "tran": tran, "model": model, "children": [
        {"name": f"{name}p{i + 1}", "parttype": pt, "fstype": fs, "mountpoints": mp, "uuid": f"U-{name}-{i + 1}"}
        for i, (pt, fs, mp) in enumerate(parts)]}


windows = disk("nvme0n1", "nvme", "Fast SSD", [(EFI, "vfat", []), (MSR, None, []), (DATA, "ntfs", ["/mnt/windows"]), (WINRE, "ntfs", [])])
linux = disk("nvme1n1", "nvme", "Linux SSD", [(EFI, "vfat", ["/boot/efi"]), (LINUX, "btrfs", ["/", "/home"])])
games = disk("sda", "sata", "Big HDD", [(MSR, None, []), (DATA, "ntfs", [None])])
ctrl = {"nvme0n1": "0000:02:00.0", "nvme1n1": "0000:03:00.0"}.get

print("graphics")
pci = ["0000:00:02.0", "0000:01:00.0", "0000:01:00.1", "0000:02:00.0"]
nv = {"bdf": "0000:01:00.0", "vendor": "0x10de", "boot_vga": True, "driver": "nvidia"}
intel = {"bdf": "0000:00:02.0", "vendor": "0x8086", "boot_vga": False, "driver": "i915"}
r = d.choose_gpus([intel, nv], pci)
check("the NVIDIA card with all its functions goes to Windows, the other chip stays", r["devices"] == ["0000:01:00.0", "0000:01:00.1"] and r["host"] == ["0000:00:02.0"] and not r["problem"])
check("no NVIDIA card: nothing is chosen, and it says why", d.choose_gpus([intel], pci)["devices"] == [] and "NVIDIA" in d.choose_gpus([intel], pci)["problem"])
two = d.choose_gpus([nv, dict(nv, bdf="0000:04:00.0")], pci + ["0000:04:00.0"])
check("two NVIDIA cards: it does not guess", two["devices"] == [] and "more than one" in two["problem"])

print("the Windows disk")
r = d.choose_windows_disk([windows, linux, games], ctrl)
check("the NVMe drive with a Windows system layout", r["nvme"] == "0000:02:00.0" and r["disk"] == "nvme0n1" and not r["problem"], r)
check("a data disk formatted by Windows is mentioned, not taken", r["others"] == ["Big HDD (sda)"], r["others"])
both = disk("nvme0n1", "nvme", "One SSD", [(EFI, "vfat", ["/boot/efi"]), (MSR, None, []), (DATA, "ntfs", []), (LINUX, "ext4", ["/"])])
r = d.choose_windows_disk([both], ctrl)
check("Windows and Linux on one drive: refused, with the reason", not r["nvme"] and "same drive" in r["problem"], r)
sata = disk("sdb", "sata", "SATA SSD", [(EFI, "vfat", []), (MSR, None, []), (DATA, "ntfs", ["/mnt/windows"])])
by_id = {"sdb": "/dev/disk/by-id/ata-SATA_SSD_S0000001"}.get
r = d.choose_windows_disk([sata, linux, games], ctrl, by_id)
check("Windows on a SATA disk: taken by its stable name, not as a PCI device",
      r["boot_disk"] == "/dev/disk/by-id/ata-SATA_SSD_S0000001" and r["bus"] == "sata" and r["disk"] == "sdb" and not r["nvme"] and not r["problem"], r)
check("...and the data disk beside it is still only mentioned", r["others"] == ["Big HDD (sda)"], r["others"])
check("...its Linux mount points are found like an NVMe drive's", d.mount_points_of("sdb", [sata, linux], "") == ["/mnt/windows"])
r = d.choose_windows_disk([sata, linux], ctrl, lambda name: "")
check("a SATA disk without a stable name: refused, with the reason", not r["boot_disk"] and "stable name" in r["problem"], r)
r = d.choose_windows_disk([dict(sata, **{"log-sec": 4096}), linux], ctrl, by_id)
check("a SATA disk with 4096-byte sectors: refused, with the reason", not r["boot_disk"] and "4096-byte sectors" in r["problem"], r)
r = d.choose_windows_disk([dict(sata, **{"log-sec": 512}), linux], ctrl, by_id)
check("512-byte sectors are fine", r["boot_disk"] and not r["problem"], r)
r = d.choose_windows_disk([dict(sata, tran="usb"), linux], ctrl, by_id)
check("Windows on a USB disk: refused, with the reason", not r["boot_disk"] and not r["nvme"] and "usb" in r["problem"], r)
r = d.choose_windows_disk([windows, sata, linux], ctrl, by_id)
check("Windows on an NVMe drive and on a SATA disk: it does not guess", not r["nvme"] and not r["boot_disk"] and "more than one" in r["problem"], r)
sata_both = disk("sdb", "sata", "One SATA SSD", [(EFI, "vfat", ["/boot/efi"]), (MSR, None, []), (DATA, "ntfs", []), (LINUX, "ext4", ["/"])])
r = d.choose_windows_disk([sata_both], ctrl, by_id)
check("Windows and Linux on one SATA disk: refused, with the reason", not r["boot_disk"] and "same drive" in r["problem"], r)
import tempfile  # noqa: E402
ids = Path(tempfile.mkdtemp(prefix="gamevm-test-"))
(ids / "sdb").touch()
(ids / "sdb3").touch()
for link, target in (("wwn-0x5000000000000001", "sdb"), ("ata-SATA_SSD_S0000001", "sdb"), ("ata-SATA_SSD_S0000001-part3", "sdb3"), ("usb-Stick_1", "sdc")):
    (ids / link).symlink_to(ids / target)
check("the stable name: the one with model and serial, never a partition's", d.stable_disk_path("sdb", ids) == str(ids / "ata-SATA_SSD_S0000001"), d.stable_disk_path("sdb", ids))
(ids / "ata-SATA_SSD_S0000001").unlink()
check("...the bare identifier where there is no other", d.stable_disk_path("sdb", ids) == str(ids / "wwn-0x5000000000000001"))
check("...nothing for a disk without one", d.stable_disk_path("sdz", ids) == "")
r = d.choose_windows_disk([windows, disk("nvme2n1", "nvme", "Other", [(EFI, "vfat", []), (MSR, None, []), (DATA, "ntfs", [])]), linux], ctrl)
check("two Windows drives: it does not guess", not r["nvme"] and "more than one" in r["problem"], r)
r = d.choose_windows_disk([linux], ctrl)
check("no Windows at all: says so", not r["nvme"] and "no drive" in r["problem"])
shared = d.choose_windows_disk([windows, linux], {"nvme0n1": "0000:02:00.0", "nvme1n1": "0000:02:00.0"}.get)
check("a Windows drive behind the same controller as the Linux drive is refused", not shared["nvme"] and "shares" in shared["problem"], shared)
fstab = "UUID=U-nvme0n1-3 /mnt/win\\040c ntfs-3g defaults,x-systemd.automount 0 0\nUUID=other /data ext4 defaults 0 0\n# UUID=U-nvme0n1-4 /x ntfs 0 0\n"
check("mount points: mounted now, or by an fstab line (automounts too)", d.mount_points_of("nvme0n1", [windows, linux], fstab) == ["/mnt/win c", "/mnt/windows"],
      d.mount_points_of("nvme0n1", [windows, linux], fstab))

print("keyboard and mouse")
names = ["usb-KB_Maker_Keyboard-event-kbd", "usb-KB_Maker_Keyboard-if02-event-kbd", "usb-KB_Maker_Keyboard-if02-event-mouse",
         "usb-Mouse_Maker_Receiver-if02-event-mouse", "usb-Cam-event-if00", "usb-KB_Maker_Keyboard-hidraw"]
r = d.choose_input(names)
check("the keyboard's main node, its media-key node as extra", r["keyboard"].endswith("Keyboard-event-kbd") and [x.rsplit("/", 1)[1] for x in r["extra"]] == ["usb-KB_Maker_Keyboard-if02-event-kbd"], r)
check("the mouse is the real mouse, not the mouse a keyboard announces", r["mouse"].endswith("Receiver-if02-event-mouse"), r)
r = d.choose_input(["usb-Combo_Receiver-event-kbd", "usb-Combo_Receiver-if01-event-mouse"])
check("a combined receiver gives both", r["keyboard"].endswith("event-kbd") and r["mouse"].endswith("if01-event-mouse"), r)
r = d.choose_input(["usb-A-event-kbd", "usb-B-event-kbd", "usb-M-event-mouse"])
check("two keyboards: the first, with a note to check", r["keyboard"].endswith("usb-A-event-kbd") and "several keyboards" in r["note"], r)
check("nothing connected: empty, with notes", d.choose_input([])["keyboard"] == "" and "no keyboard" in d.choose_input([])["note"])

print("processor and memory")
even = {i: [i, i + 8] for i in range(8)}
check("8 cores: Linux keeps the first and the last", d.split_cores(even)["host_cores"] == [0, 7] and d.split_cores(even)["guest_cores"] == [1, 2, 3, 4, 5, 6])
four = {i: [i, i + 4] for i in range(4)}
check("4 cores: Linux keeps the first", d.split_cores(four)["host_cores"] == [0] and d.split_cores(four)["guest_cores"] == [1, 2, 3])
hybrid = {**{i: [2 * i, 2 * i + 1] for i in range(8)}, **{8 + i: [16 + i] for i in range(8)}}
r = d.split_cores(hybrid)
check("two kinds of cores: Windows gets the ones with two threads, and it is said", r["guest_cores"] == [1, 2, 3, 4, 5, 6, 7] and set(range(8, 16)) <= set(r["host_cores"]) and "two kinds" in r["note"], r)
check("one core: shared", d.split_cores({0: [0]})["guest_cores"] == [0])
mem = lambda gib: d.memory_for_windows(f"MemTotal:       {int(gib * 1024 * 1024)} kB\n") // 1024  # noqa: E731
check("memory: half, in steps of 2 GiB, Linux keeps 6 where it can", [mem(x) for x in (7.6, 15.5, 30.8, 62.5, 125)] == [4, 8, 16, 32, 62], [mem(x) for x in (7.6, 15.5, 30.8, 62.5, 125)])

print("this machine (reads only, shown for information)")
for line in d.describe(d.discover()):
    print("        " + line)
finish()
