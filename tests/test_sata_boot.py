# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""Windows on a SATA disk (disk.boot_disk): what the configuration accepts, what stops a start, how
the disk is taken from Linux and what the VM definition says, with a made-up disk.

    python3 tests/test_sata_boot.py            what tests/run.sh runs
    python3 tests/test_sata_boot.py firmware   also starts the VM firmware (needs qemu, OVMF, mtools,
                                               sfdisk and systemd-boot's EFI file; no root, no KVM
                                               needed) and lets it start a made-up disk attached the
                                               same way
"""
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import check, finish, g, ui  # noqa: E402

BOOT = "/dev/disk/by-id/ata-Sample_SSD_S0000001"
EXTRA = "/dev/disk/by-id/ata-Sample_HDD_H0000002"
SATA = {**common.SAMPLE, ("disk", "nvme"): "", ("disk", "boot_disk"): BOOT, ("disk", "expect_serial"): "S0000001"}
TEMPLATE = (common.REPO / "config" / "gamevm.toml").read_text()


def config(changes=None):
    """The made-up PC with Windows on a SATA disk, as a loaded configuration (or the refusal)."""
    path = Path(tempfile.mkdtemp(prefix="gamevm-test-")) / "gamevm.toml"
    path.write_text(ui.set_keys(TEMPLATE, {**SATA, **(changes or {})}))
    try:
        return g.load_config(path)
    except g.GameVMError as e:
        return str(e)
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def facts(**changes):
    """What the kernel would say about the made-up disk."""
    return {"name": "sdb", "whole": True, "serial": "S0000001", "sector": "512", "physical": "4096", "rotating": False,
            "mounts": [{"dev": "sdb3", "mountpoint": "/mnt/windows", "fstype": "fuseblk"}], **changes}


def with_disk(found):
    g.whole_disk_facts = lambda path: found if path == BOOT else None


# Nothing here may depend on the machine the tests run on: a made-up processor (8 cores, 2 threads
# each), the sample's graphics card where the configuration says it is and no other PCI device,
# and everything else a start looks at reported as fine.
CARD = dict(zip(common.SAMPLE[("gpu", "devices")], common.SAMPLE[("gpu", "expect_ids")]))
g.log = lambda *a, **k: None
g.cpu_cores = lambda: {core: [core, core + 8] for core in range(8)}
g.pci_exists = lambda bdf: bdf in CARD
g.pci_id = lambda bdf: CARD.get(bdf, "")
g.kernel_modules_missing = lambda cfg: []
g.network_active = lambda net: True
g.check_kvmfr = lambda cfg, mode: None


print("the configuration")
cfg = config()
check("a SATA disk by its /dev/disk/by-id/ name is accepted", isinstance(cfg, dict) and cfg["disk"]["boot_disk"] == BOOT, cfg)
check("a name that changes between boots (/dev/sdb) is refused", "by-id" in str(config({("disk", "boot_disk"): "/dev/sdb"})))
check("a partition instead of the whole disk is refused", "whole disk" in str(config({("disk", "boot_disk"): BOOT + "-part3"})))
check("an NVMe drive and a SATA disk together are refused: Windows starts from one",
      "not both" in str(config({("disk", "nvme"): "0000:02:00.0"})))
check("the same disk as boot disk and as extra disk is refused", "also listed" in str(config({("disk", "extra"): [BOOT]})))

print("what stops a start")
with_disk(facts())
check("the expected disk, 512-byte sectors, nothing of Linux on it: fine", g.boot_disk_problem(cfg) == "", g.boot_disk_problem(cfg))
with_disk(None)
check("the disk is not there: said plainly", "is not there" in g.boot_disk_problem(cfg))
try:
    g.verify_identity(cfg)
    refused = ""
except g.GameVMError as e:
    refused = str(e)
check("...and the hook's identity check refuses the start with it", "is not there" in refused, refused)
check("...and so do the checks `gamevm start` makes before the desktop moves",
      any("is not there" in p for p in g.start_problems(cfg, "igpu")), g.start_problems(cfg, "igpu"))
with_disk(facts())
check("...which pass once the disk is back", not any("boot_disk" in p for p in g.start_problems(cfg, "igpu")), g.start_problems(cfg, "igpu"))
with_disk(facts(serial="OTHER999"))
check("another disk under that name (serial differs): refused", "not the Windows disk" in g.boot_disk_problem(cfg))
with_disk(facts(serial=""))
check("a disk without a serial where one is expected: refused", "not the Windows disk" in g.boot_disk_problem(cfg))
with_disk(facts(whole=False, name="sdb3"))
check("a name that leads to a partition: refused", "not a whole disk" in g.boot_disk_problem(cfg))
with_disk(facts(sector="4096"))
check("a disk with 4096-byte sectors: refused (a SATA disk in the VM has 512)", "4096-byte sectors" in g.boot_disk_problem(cfg))
for mp in ("/", "/home", "/boot", "[swap]"):
    with_disk(facts(mounts=[{"dev": "sdb2", "mountpoint": mp, "fstype": "ext4"}]))
    cfg_bad = config({("disk", "unmount"): [mp]}) if mp.startswith("/") else cfg
    check(f"the disk Linux runs from ({mp}): refused, also when listed in disk.unmount",
          "Linux runs from" in g.boot_disk_problem(cfg_bad), g.boot_disk_problem(cfg_bad))
nvme_cfg = g.load_config(common.sample_config_file())
with_disk(None)
check("a PC with an NVMe drive is not asked about any of this", g.boot_disk_problem(nvme_cfg) == "")

print("taking the disk from Linux")


class Journal:
    def __init__(self):
        self.data = {"steps": []}

    def push(self, kind, **info):
        self.data["steps"].append({"kind": kind, **info})


released = []
real_release = g.release_block_devices
g.release_block_devices = lambda cfg, st, what, devs: released.append((what, devs))
g.disk_devices = lambda path: {BOOT: ["sdb", "sdb1", "sdb2", "sdb3"], EXTRA: ["sdc", "sdc1"]}[path]
g.unit_active = lambda unit: False
g.extra_disks = lambda cfg: cfg["disk"]["extra"]
both = config({("disk", "extra"): [EXTRA]})
g.step_disk(both, Journal())
check("the whole disk with every partition is freed, before the extra disks",
      released == [(BOOT, ["sdb", "sdb1", "sdb2", "sdb3"]), (EXTRA, ["sdc", "sdc1"])], released)
g.release_block_devices = real_release
g.holders_of_block = lambda devs: []
g.mounts_of = lambda devs: [{"dev": "sdb3", "mountpoint": "/mnt/windows", "fstype": "fuseblk"},
                            {"dev": "sdb4", "mountpoint": "/data", "fstype": "ext4"}]
try:
    g.release_block_devices(cfg, Journal(), BOOT, ["sdb", "sdb3", "sdb4"])
    refused = ""
except g.GameVMError as e:
    refused = str(e)
check("a partition Linux has mounted somewhere else stops the start, nothing is unmounted",
      "the host is using it" in refused and "/data" in refused, refused)
g.holders_of_block = lambda devs: ["sdb2 -> dm-0"]
try:
    g.release_block_devices(cfg, Journal(), BOOT, ["sdb", "sdb2"])
    refused = ""
except g.GameVMError as e:
    refused = str(e)
check("a disk in use by LVM / encryption / RAID stops the start", "device-mapper" in refused, refused)

print("the VM definition")
g.disk_serial = lambda path: "H0000002"
with_disk(facts())
try:
    xml = g.render_xml(cfg, "igpu")
    root = ET.fromstring(xml)
except Exception as e:  # noqa: BLE001
    xml, root = f"{type(e).__name__}: {e}", None
check("the definition is built", root is not None, xml[:300])
if root is not None:
    disks = root.findall("./devices/disk[@device='disk']")
    d = disks[0] if disks else ET.Element("none")
    check("one disk: the SATA disk, as a block device", len(disks) == 1 and d.get("type") == "block" and d.find("source").get("dev") == BOOT)
    check("on the SATA bus, first port (the CD-ROMs keep the second and third)",
          d.find("target").get("bus") == "sata" and d.find("target").get("dev") == "sda"
          and sorted(t.get("dev") for t in root.findall("./devices/disk/target")) == ["sda", "sdb", "sdc"][:len(root.findall("./devices/disk"))])
    check("the firmware starts from it first", d.find("boot").get("order") == "1"
          and [b.get("order") for b in root.findall("./devices/hostdev/boot")] == [])
    check("raw, past Linux' file cache, with TRIM for an SSD",
          d.find("driver").attrib == {"name": "qemu", "type": "raw", "cache": "none", "io": "native", "discard": "unmap"}, d.find("driver").attrib)
    check("512-byte sectors, the real physical size, shown as an SSD, with its own serial",
          d.find("blockio").attrib == {"logical_block_size": "512", "physical_block_size": "4096"}
          and d.find("target").get("rotation_rate") == "1" and d.find("serial").text == "S0000001")
    check("no NVMe drive is handed over as hardware beside it",
          all("bus='0x02'" not in ET.tostring(h, encoding="unicode") for h in root.findall("./devices/hostdev[@type='pci']")))
    with_disk(facts(rotating=True, physical="512"))
    hdd = ET.fromstring(g.render_xml(cfg, "igpu")).find("./devices/disk[@device='disk']")
    check("a spinning disk: shown as one, no TRIM", hdd.find("target").get("rotation_rate") == "7200" and "discard" not in hdd.find("driver").attrib)
    with_disk(None)
    gone = ET.fromstring(g.render_xml(cfg, "igpu")).find("./devices/disk[@device='disk']")
    check("the disk stays in the definition when it is missing: Windows must not start without it",
          gone is not None and gone.find("source").get("dev") == BOOT)
    with_disk(facts())
    g.extra_disks = lambda cfg: cfg["disk"]["extra"]
    old_exists = os.path.exists
    os.path.exists = lambda p: True if p == EXTRA else old_exists(p)
    try:
        two = ET.fromstring(g.render_xml(both, "igpu"))
    finally:
        os.path.exists = old_exists
    targets = [(t.get("dev"), t.get("bus")) for t in two.findall("./devices/disk[@device='disk']/target")]
    check("an extra disk beside it keeps its own place (VirtIO SCSI)", targets == [("sda", "sata"), ("sdd", "scsi")], targets)
    nv = ET.fromstring(g.render_xml(nvme_cfg, "igpu"))
    check("a PC with an NVMe drive: unchanged, the drive is handed over and starts first",
          nv.find("./devices/disk[@device='disk']") is None and [b.get("order") for b in nv.findall("./devices/hostdev/boot")] == ["1"])
    if shutil.which("virt-xml-validate"):
        f = Path(tempfile.mkdtemp(prefix="gamevm-test-")) / "domain.xml"
        f.write_text(xml)
        res = subprocess.run(["virt-xml-validate", str(f), "domain"], capture_output=True, text=True)
        check("libvirt's own schema accepts the definition", res.returncode == 0, (res.stdout + res.stderr)[-300:])
        shutil.rmtree(f.parent, ignore_errors=True)
    else:
        print("  skip  libvirt's schema check (virt-xml-validate is not installed)")

print("the installer writes it down")
sys.path.insert(0, str(common.REPO / "setup"))
import argparse  # noqa: E402
import gamevm_discover  # noqa: E402
import setup_helper  # noqa: E402

tools = Path(tempfile.mkdtemp(prefix="gamevm-test-"))
common.fake_tools(tools / "bin", {"udevadm": 'echo "ID_MODEL=Sample_SSD"; echo "ID_SERIAL_SHORT=S0000001"\n'})
found = {"gpu": {"devices": [], "name": ""}, "host_gpu": [], "mode": "single",
         "disk": {"nvme": "", "boot_disk": BOOT, "bus": "sata", "name": "Sample SSD", "unmount": ["/mnt/windows"]},
         "input": {"keyboard": "", "mouse": "", "extra": []}, "cpu": {"guest_cores": [1, 2, 3], "host_cores": [0], "threads": 2},
         "memory_mib": 8192, "network": {"host_nic": "eth0", "mode": "macvtap"}, "problems": [], "notes": []}
gamevm_discover.discover = lambda: found
real_chown, os.chown = os.chown, lambda *a, **k: None
dest = tools / "etc" / "gamevm.toml"
try:
    with open(os.devnull, "w") as quiet:
        real_out, sys.stdout = sys.stdout, quiet
        try:
            rc = setup_helper.cmd_fill_config(argparse.Namespace(dest=str(dest), template=str(common.REPO / "config" / "gamevm.toml"), user="someone"))
        finally:
            sys.stdout = real_out
finally:
    os.chown = real_chown
import tomllib  # noqa: E402
wrote = tomllib.loads(dest.read_text())["disk"] if dest.exists() else {}
check("a SATA Windows disk: its stable name, its serial and its Linux mount points, and no NVMe drive",
      rc == 0 and (wrote.get("boot_disk"), wrote.get("expect_serial"), wrote.get("unmount"), wrote.get("nvme"), wrote.get("expect_id"))
      == (BOOT, "S0000001", ["/mnt/windows"], "", ""), (rc, wrote))
check("the describing line says how Windows will see the disk",
      any("SATA" in line and BOOT in line for line in gamevm_discover.describe(found)), gamevm_discover.describe(found))
shutil.rmtree(tools, ignore_errors=True)

if sys.argv[1:] == ["firmware"] and root is not None:
    print("the VM firmware starts a disk attached this way")
    ovmf_code = next((p for p in ("/usr/share/edk2/x64/OVMF_CODE.4m.fd", "/usr/share/OVMF/OVMF_CODE_4M.fd",
                                  "/usr/share/edk2/ovmf/OVMF_CODE.fd") if os.path.exists(p)), "")
    ovmf_vars = next((p for p in ("/usr/share/edk2/x64/OVMF_VARS.4m.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd",
                                  "/usr/share/edk2/ovmf/OVMF_VARS.fd") if os.path.exists(p)), "")
    loader = "/usr/lib/systemd/boot/efi/systemd-bootx64.efi"
    need = [t for t in ("qemu-system-x86_64", "sfdisk", "mkfs.fat", "mmd", "mcopy") if not shutil.which(t)]
    if need or not ovmf_code or not ovmf_vars or not os.path.exists(loader):
        print(f"  skip  missing: {', '.join(need) or 'OVMF or ' + loader}")
    else:
        # Not under /tmp where that is a RAM disk: cache='none' opens the disk past the file cache,
        # which a RAM disk cannot do.
        work = Path(tempfile.mkdtemp(prefix="gamevm-sata-", dir=os.environ.get("GAMEVM_TEST_DIR") or Path.home() / ".cache"))
        try:
            img, esp, nvram = work / "disk.img", work / "esp.img", work / "vars.fd"
            shutil.copy(ovmf_vars, nvram)  # a fresh firmware memory, as on a first start
            with open(img, "wb") as fh:
                fh.truncate(160 * 1024 * 1024)
            # the layout of a Windows system disk: EFI partition (100 MiB, as Windows makes it),
            # Microsoft reserved, data
            subprocess.run(["sfdisk", "--quiet", str(img)], check=True, capture_output=True, text=True, input=(
                "label: gpt\nstart=2048, size=204800, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B\n"
                "size=32768, type=E3C9E316-0B5C-4DB8-817D-F92DF00215AE\ntype=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n"))
            with open(esp, "wb") as fh:
                fh.truncate(204800 * 512)
            subprocess.run(["mkfs.fat", "-F", "32", str(esp)], check=True, capture_output=True)
            mark = "gamevm-sata-proof-" + os.urandom(4).hex()
            (work / "loader.conf").write_text("timeout 20\nconsole-mode keep\n")
            (work / "proof.conf").write_text(f"title {mark}\nefi /EFI/proof/proof.efi\n")
            for folder in ("::/EFI", "::/EFI/BOOT", "::/EFI/proof", "::/loader", "::/loader/entries"):
                subprocess.run(["mmd", "-i", str(esp), folder], check=True, capture_output=True)
            # where the firmware of a new PC (or VM) looks first: \EFI\BOOT\BOOTX64.EFI, the copy of
            # its boot manager Windows keeps there. A boot loader stands in for it, with a menu
            # entry whose name exists only on this disk.
            for src, dst in ((loader, "::/EFI/BOOT/BOOTX64.EFI"), (loader, "::/EFI/proof/proof.efi"),
                             (work / "loader.conf", "::/loader/loader.conf"), (work / "proof.conf", "::/loader/entries/proof.conf")):
                subprocess.run(["mcopy", "-i", str(esp), str(src), dst], check=True, capture_output=True)
            with open(img, "r+b") as out, open(esp, "rb") as src:
                out.seek(2048 * 512)
                shutil.copyfileobj(src, out)

            # 1. What libvirt makes of the <disk> gamevm writes (asked from libvirt itself).
            disk_xml = xml[xml.index("    <disk type='block' device='disk'>"):]
            disk_xml = disk_xml[:disk_xml.index("</disk>") + len("</disk>")]
            (work / "domain.xml").write_text(
                "<domain type='kvm'><name>gamevm-sata-check</name><memory unit='MiB'>512</memory><vcpu>1</vcpu>"
                f"<os><type arch='x86_64' machine='q35'>hvm</type></os><devices>\n{disk_xml}\n</devices></domain>\n")
            res = subprocess.run(["virsh", "-c", "qemu:///session", "domxml-to-native", "qemu-argv", str(work / "domain.xml")],
                                 capture_output=True, text=True) if shutil.which("virsh") else None
            dev = {}
            if res is not None and res.returncode == 0:
                import json
                for m in re.finditer(r"'(\{[^']*\})'", res.stdout):
                    j = json.loads(m.group(1))
                    if j.get("driver") == "ide-hd":
                        dev = j
                check("libvirt turns that disk into a SATA disk on the first port that the firmware starts first, "
                      "with 512-byte sectors and its serial",
                      (dev.get("bus"), dev.get("bootindex"), dev.get("logical_block_size"), dev.get("serial")) == ("ide.0", 1, 512, "S0000001"), dev)
            else:
                print("  skip  asking libvirt (no libvirt session here); using the arguments it is known to produce")
            dev = dev or {"driver": "ide-hd", "bus": "ide.0", "id": "sata0-0-0", "bootindex": 1, "logical_block_size": 512,
                          "physical_block_size": 4096, "rotation_rate": 1, "write-cache": "on", "serial": "S0000001"}
            dev["drive"] = "windows-disk"

            # 2. The firmware with a disk attached by exactly those arguments (a file stands in for
            #    the disk, opened the same way: past the file cache, native asynchronous I/O, TRIM).
            kvm = ["-accel", "kvm"] if os.access("/dev/kvm", os.R_OK | os.W_OK) else ["-accel", "tcg"]
            cmd = ["qemu-system-x86_64", "-machine", "q35", *kvm, "-m", "512", "-nodefaults", "-display", "none",
                   "-serial", "stdio", "-monitor", "none", "-no-reboot",
                   "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code}",
                   "-drive", f"if=pflash,format=raw,file={nvram}",
                   "-blockdev", f"driver=file,filename={img},node-name=windows-disk,aio=native,discard=unmap,"
                                "cache.direct=on,cache.no-flush=off",
                   "-device", ",".join(f"{k}={v}" for k, v in dev.items())]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, deadline = b"", time.monotonic() + 120
            os.set_blocking(proc.stdout.fileno(), False)
            while time.monotonic() < deadline and proc.poll() is None and mark.encode() not in out:
                if select.select([proc.stdout], [], [], 0.5)[0]:
                    out += os.read(proc.stdout.fileno(), 65536) or b""
            proc.kill()
            err = proc.communicate()[1].decode(errors="replace")
            plain = re.sub(r"\x1b\[[0-9;?=]*[A-Za-z]", "", out.decode(errors="replace"))
            started = re.search(r'BdsDxe: starting \S+ "[^"]*" from (\S+)', plain)
            check("the firmware starts from the disk on the SATA controller's first port",
                  bool(started) and "/Sata(0x0," in started.group(1), (plain[-300:] + err[-300:]))
            check(f"and runs the boot loader in its EFI partition (it showed '{mark}', which exists only on that disk)",
                  mark in plain, re.sub(r" {3,}", " | ", plain)[-400:])
        finally:
            shutil.rmtree(work, ignore_errors=True)

finish()
