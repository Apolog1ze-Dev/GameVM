#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""Helpers for install-host.sh / uninstall-host.sh: careful edits of system files.

Every edit is either a clearly delimited managed block or a commented-out original line tagged
'#gamevm#', so `remove` restores the file exactly. Run as root.

  setup_helper.py fill-config TEMPLATE DEST --user USER     create /etc/gamevm/gamevm.toml
  setup_helper.py smbios DEST                               capture host SMBIOS identity
  setup_helper.py qemu-conf apply|remove PATH [--user U] [--kvmfr]
  setup_helper.py fstab apply|remove MOUNTPOINT             fstab entry -> on-demand automount
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

BEGIN = "# >>> gamevm managed block (GameMachine/setup/install-host.sh) >>>"
END = "# <<< gamevm managed block <<<"
TAG = "#gamevm# "


def backup_once(path: Path) -> None:
    bak = path.with_name(path.name + ".gamevm.orig")
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)


def strip_block(text: str) -> str:
    """Remove our managed block and restore lines we commented out."""
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if line.rstrip("\n") == BEGIN:
            skip = True
            continue
        if line.rstrip("\n") == END:
            skip = False
            continue
        if skip:
            continue
        out.append(line[len(TAG):] if line.startswith(TAG) else line)
    return "".join(out)


def write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """Write via a temp file that has its final owner/mode before any content, fsync, rename."""
    tmp = path.with_name(path.name + ".gamevm.tmp")
    st = path.stat() if path.exists() else None
    final_mode = mode if mode is not None else (st.st_mode & 0o7777 if st else 0o644)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        if st:
            os.fchown(fd, st.st_uid, st.st_gid)
        os.fchmod(fd, final_mode)
        with os.fdopen(fd, "w") as f:
            fd = -1
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    tmp.replace(path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


# ---------------------------------------------------------------------------------------------

def toml_set(text: str, section: str, key: str, value: str) -> str:
    """Replace `key = ...` inside [section] (value already TOML-encoded)."""
    lines = text.splitlines(keepends=True)
    cur = None
    for i, line in enumerate(lines):
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if m:
            cur = m.group(1).strip()
            continue
        if cur == section and re.match(rf"^\s*{re.escape(key)}\s*=", line):
            lines[i] = f"{key} = {value}\n"
            return "".join(lines)
    raise SystemExit(f"fill-config: key {section}.{key} not found in template")


def toml_list(values) -> str:
    return "[" + ", ".join(str(v) if isinstance(v, int) else json.dumps(str(v)) for v in values) + "]"


def cmd_fill_config(a) -> int:
    """Create /etc/gamevm/gamevm.toml from the template and what this PC has (gamevm_discover.py).
    Exit code 3: the file is there but the graphics card or the Windows disk could not be told
    without doubt, so it has to be completed by hand before anything else is set up."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gamevm"))
    import gamevm_discover as discover

    dest = Path(a.dest)
    if dest.exists():
        have = tomllib.loads(dest.read_text())
        if not have.get("gpu", {}).get("devices"):
            print(f"  {dest} exists, but gpu.devices is still empty: complete it (see `gamevm discover`), then run this again")
            return 3
        print(f"  {dest} exists, left unchanged")
        return 0
    text = Path(a.template).read_text()
    found = discover.discover()
    for line in discover.describe(found):
        print(f"  {line}")

    try:
        uuid = Path("/sys/class/dmi/id/product_uuid").read_text().strip().lower()
    except OSError:  # readable by root only
        uuid = ""
    if re.fullmatch(r"[0-9a-f-]{36}", uuid) and len(set(uuid.replace("-", ""))) > 2:
        text = toml_set(text, "vm", "uuid", f'"{uuid}"')
        print(f"  vm.uuid = host SMBIOS UUID {uuid}")
    else:
        print(f"  host SMBIOS UUID '{uuid}' looks like a placeholder; gamevm will derive one")
    text = toml_set(text, "audio", "user", f'"{a.user}"')
    text = toml_set(text, "vm", "memory_mib", str(found["memory_mib"]))
    text = toml_set(text, "cpu", "guest_cores", toml_list(found["cpu"]["guest_cores"]))
    text = toml_set(text, "cpu", "host_cores", toml_list(found["cpu"]["host_cores"]))
    for key in ("keyboard", "mouse"):
        text = toml_set(text, "input", key, json.dumps(found["input"][key]))
    text = toml_set(text, "input", "extra", toml_list(found["input"]["extra"]))
    if not found["input"]["keyboard"]:
        text = toml_set(text, "input", "evdev", "false")
    text = toml_set(text, "network", "host_nic", json.dumps(found["network"]["host_nic"]))
    text = toml_set(text, "network", "mode", json.dumps(found["network"]["mode"]))

    # The devices that are handed to Windows, with what they are: the hook refuses to start if the
    # device at an address is no longer the one recorded here.
    devices = found["gpu"]["devices"]
    ids = []
    for bdf in devices:
        dev = Path("/sys/bus/pci/devices") / bdf
        ids.append(f'{(dev / "vendor").read_text().strip()[2:]}:{(dev / "device").read_text().strip()[2:]}')
    text = toml_set(text, "gpu", "devices", toml_list(devices))
    text = toml_set(text, "gpu", "expect_ids", toml_list(ids))
    nvme = found["disk"]["nvme"]
    if nvme:
        dev = Path("/sys/bus/pci/devices") / nvme
        vid = (dev / "vendor").read_text().strip()[2:]
        did = (dev / "device").read_text().strip()[2:]
        serials = [Path(c, "serial").read_text().strip() for c in glob.glob(f"{dev}/nvme/nvme*")]
        text = toml_set(text, "disk", "nvme", json.dumps(nvme))
        text = toml_set(text, "disk", "expect_id", f'"{vid}:{did}"')
        if serials:
            text = toml_set(text, "disk", "expect_serial", json.dumps(serials[0]))
        text = toml_set(text, "disk", "boot_disk", '""')
        text = toml_set(text, "disk", "unmount", toml_list(found["disk"]["unmount"]))
    boot = found["disk"]["boot_disk"]
    if boot:  # Windows on a SATA disk: recorded by its stable name and its serial number
        props = subprocess.run(["udevadm", "info", "--query=property", f"--name={boot}"], capture_output=True,
                               text=True, check=False).stdout
        serial = re.search(r"^ID_SERIAL_SHORT=(.+)$", props, re.M)
        text = toml_set(text, "disk", "boot_disk", json.dumps(boot))
        text = toml_set(text, "disk", "nvme", '""')
        text = toml_set(text, "disk", "expect_id", '""')
        text = toml_set(text, "disk", "expect_serial", json.dumps(serial.group(1).strip() if serial else ""))
        text = toml_set(text, "disk", "unmount", toml_list(found["disk"]["unmount"]))

    dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    write_atomic(dest, text, 0o644)
    os.chown(dest, 0, 0)
    print(f"  wrote {dest}")
    if found["problems"]:
        print(f"\n  {dest} is not complete:")
        for problem in found["problems"]:
            print(f"   - {problem}")
        return 3
    return 0


DMI = {
    "bios": {"vendor": "bios-vendor", "version": "bios-version", "date": "bios-release-date",
             "release": "bios-revision"},
    "system": {"manufacturer": "system-manufacturer", "product": "system-product-name",
               "version": "system-version", "serial": "system-serial-number", "sku": "system-sku-number",
               "family": "system-family"},
    "baseBoard": {"manufacturer": "baseboard-manufacturer", "product": "baseboard-product-name",
                  "version": "baseboard-version", "serial": "baseboard-serial-number",
                  "asset": "baseboard-asset-tag"},
    "chassis": {"manufacturer": "chassis-manufacturer", "version": "chassis-version",
                "serial": "chassis-serial-number", "asset": "chassis-asset-tag"},
}


def toml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cmd_smbios(a) -> int:
    dest = Path(a.dest)
    if dest.exists() and not a.refresh:
        try:
            import grp
            os.chown(dest, 0, grp.getgrnam("libvirt").gr_gid)
            os.chmod(dest, 0o640)
        except KeyError:
            pass
        print(f"  {dest} exists, left unchanged (--refresh to recapture)")
        return 0
    lines = ["# Host SMBIOS identity copied into the VM so Windows sees the same machine (activation).",
             "# Captured by gamevm setup with dmidecode. The system UUID comes from vm.uuid.", ""]
    for block, keys in DMI.items():
        lines.append(f"[{block}]")
        for key, kw in keys.items():
            r = subprocess.run(["dmidecode", "-s", kw], text=True, capture_output=True)
            val = r.stdout.strip().splitlines()[0].strip() if r.returncode == 0 and r.stdout.strip() else ""
            if val and not val.startswith("#"):
                lines.append(f"{key} = {toml_str(val)}")
        lines.append("")
    dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    write_atomic(dest, "\n".join(lines), 0o640)
    try:
        import grp
        os.chown(dest, 0, grp.getgrnam("libvirt").gr_gid)
    except KeyError:
        os.chown(dest, 0, 0)
    print(f"  wrote {dest} (0640 root:libvirt)")
    return 0


# ---------------------------------------------------------------------------------------------

def default_acl(text: str) -> list[str]:
    """The user's active cgroup_device_acl list merged with the commented default shipped in
    qemu.conf, so devices added by hand (e.g. evdev nodes) are kept."""
    out: list[str] = []
    active = re.search(r"^\s*cgroup_device_acl\s*=\s*\[(.*?)\]", text, re.S | re.M)
    commented = re.search(r"^#\s*cgroup_device_acl\s*=\s*\[(.*?)\]", text, re.S | re.M)
    for m in (active, commented):
        if m:
            out += [d for d in re.findall(r'"([^"]+)"', m.group(1)) if d not in out]
    return out or ["/dev/null", "/dev/full", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/ptmx",
                   "/dev/kvm", "/dev/userfaultfd"]


def cmd_qemu_conf(a) -> int:
    path = Path(a.path)
    text = strip_block(path.read_text())
    if a.action == "remove":
        write_atomic(path, text)
        print(f"  {path}: gamevm settings removed")
        return 0
    settings: dict[str, str] = {}
    if a.user:
        settings["user"] = f'"{a.user}"'
        settings["group"] = '"kvm"'
    if a.kvmfr:
        acl = default_acl(text)
        for extra in ("/dev/kvm", "/dev/kvmfr0"):
            if extra not in acl:
                acl.append(extra)
        settings["cgroup_device_acl"] = "[ " + ", ".join(f'"{d}"' for d in acl) + " ]"
    # libvirt's auto_shutdown_* keys are deliberately not set (earlier gamevm versions did; their
    # block was stripped above): a host shutdown is handled by gamevm-shutdown.service, and two
    # mechanisms would cut each other's wait short.
    # Comment out active lines that would conflict with ours, including the continuation lines
    # of a multi-line list value.
    lines, cont = [], False
    for line in text.splitlines(keepends=True):
        if cont:
            lines.append(TAG + line)
            cont = "]" not in line
            continue
        m = re.match(r"^\s*([a-z_]+)\s*=(.*)", line)
        if m and m.group(1) in settings:
            lines.append(TAG + line)
            rhs = m.group(2)
            cont = "[" in rhs and "]" not in rhs
        else:
            lines.append(line)
    text = "".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    block = [BEGIN] + [f"{k} = {v}" for k, v in settings.items()] + [END] if settings else []
    backup_once(path)
    write_atomic(path, text + "".join(line + "\n" for line in block))
    for k, v in settings.items():
        print(f"  {path}: {k} = {v}")
    if not settings:
        print(f"  {path}: no gamevm settings needed")
    return 0


def cmd_fstab(a) -> int:
    path = Path("/etc/fstab")
    text = path.read_text()
    if text and not text.endswith("\n"):
        text += "\n"
    mp = os.path.normpath(a.mountpoint)
    out, changed = [], False
    for line in text.splitlines(keepends=True):
        if a.action == "remove":
            if line.startswith(TAG):
                out.append(line[len(TAG):])
                changed = True
            elif "gamevm-automount" in line:
                changed = True
            else:
                out.append(line)
            continue
        f = line.split()
        if len(f) >= 4 and not line.lstrip().startswith("#") and os.path.normpath(f[1].replace("\\040", " ")) == mp:
            opts = [o for o in f[3].split(",") if o not in ("auto", "noauto") and not o.startswith(
                ("x-systemd.automount", "x-systemd.idle-timeout", "x-systemd.device-timeout"))]
            if "x-systemd.automount" in f[3]:
                out.append(line)
                continue
            opts += ["noauto", "x-systemd.automount", "x-systemd.idle-timeout=2min", "x-systemd.device-timeout=10s"]
            f[3] = ",".join(dict.fromkeys(opts))
            out.append(TAG + line)
            out.append("\t".join(f) + "\t# gamevm-automount: mounted on access, unmounted when idle\n")
            changed = True
        else:
            out.append(line)
    if changed:
        backup_once(path)
        write_atomic(path, "".join(out))
        print("  /etc/fstab: original entries restored" if a.action == "remove"
              else f"  /etc/fstab: {mp} is now an on-demand automount")
    else:
        print(f"  /etc/fstab: nothing to change for {mp}")
    return 0


def main() -> int:
    if os.geteuid() != 0:
        print("setup_helper.py must run as root", file=sys.stderr)
        return 1
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("fill-config")
    s.add_argument("template")
    s.add_argument("dest")
    s.add_argument("--user", required=True)
    s.set_defaults(fn=cmd_fill_config)
    s = sub.add_parser("smbios")
    s.add_argument("dest")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(fn=cmd_smbios)
    s = sub.add_parser("qemu-conf")
    s.add_argument("action", choices=["apply", "remove"])
    s.add_argument("path")
    s.add_argument("--user")
    s.add_argument("--kvmfr", action="store_true")
    s.set_defaults(fn=cmd_qemu_conf)
    s = sub.add_parser("fstab")
    s.add_argument("action", choices=["apply", "remove"])
    s.add_argument("mountpoint")
    s.set_defaults(fn=cmd_fstab)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
