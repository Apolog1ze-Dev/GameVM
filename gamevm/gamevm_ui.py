# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""The gamevm window: `gamevm ui`.

Two halves. This file is a small service on 127.0.0.1 that knows the state of things (through
gamevm itself, imported as `g`) and carries out what the window asks for. The window is ui/*.qml,
drawn by Qt's own QML runtime (qml6, part of every Plasma 6 install), so nothing extra has to be
installed, and as a Qt 6 Wayland program it stays open when the desktop moves between the
graphics cards. The window polls GET /state and sends POST /action; both need the token from the
session file that only the user can read.

The rule the pages follow: gamevm does by itself whatever it can (checks, pairing the clipboard,
reconnecting input devices, noticing that a setup step is done). Only choices that are the
user's, and steps that need hands or the password, are put in front of the user ("Needs you").
"""

from __future__ import annotations

import collections
import grp
import hashlib
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
UI_DIR = HERE / "ui"
INSTALL_DIR = Path("/usr/local/lib/gamevm")
INSTALLED_CLI = Path("/usr/local/bin/gamevm")
HOOK = Path("/etc/libvirt/hooks/qemu.d/gamevm")
DRIVER_DISC_URL = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/"
DRIVER_DISC_ISO = DRIVER_DISC_URL + "virtio-win.iso"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
PREFS_FILE = CONFIG_HOME / "gamevm" / "ui.json"
AUTOSTART_FILE = CONFIG_HOME / "autostart" / "gamevm-ui-setup.desktop"
PREF_DEFAULTS = {"confirm_start": True, "new_usb": "ask", "usb_rules": {}, "winprep_done": False,
                 "first_start_done": False, "paired_once": False, "transfers": [], "win_tool": ""}
TOGGLE_WORDS = {"ctrl-ctrl": "Both Ctrl keys", "alt-alt": "Both Alt keys", "shift-shift": "Both Shift keys",
                "meta-meta": "Both Meta keys", "scrolllock": "Scroll Lock", "ctrl-scrolllock": "Ctrl + Scroll Lock"}


# ---------------------------------------------------------------------------------------------
# the configuration file as text: change single keys, keep every comment
# ---------------------------------------------------------------------------------------------

def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def set_keys(text: str, changes: dict[tuple[str, str], object]) -> str:
    """`text` with section.key = value for every change. The rest of the file, comments included,
    stays as it is; a key the file does not have yet is added under its section's heading."""
    lines = text.split("\n")
    header = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
    for (sec, key), val in changes.items():
        start = next((i for i, line in enumerate(lines) if (m := header.match(line)) and m.group(1).strip() == sec), None)
        new = f"{key} = {toml_value(val)}"
        if start is None:
            lines += ["", f"[{sec}]", new]
            continue
        end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
        at = next((i for i in range(start + 1, end) if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i])), None)
        if at is None:
            lines.insert(start + 1, new)
            continue
        last = at
        value = lines[at].split("=", 1)[1]
        if value.strip().startswith("[") and value.count("[") > value.count("]"):  # an array over several lines
            depth = value.count("[") - value.count("]")
            while depth > 0 and last + 1 < end:
                last += 1
                depth += lines[last].count("[") - lines[last].count("]")
        comment = ""
        if last == at:
            m = re.search(r"\s+#.*$", lines[at])
            if m and lines[at][:m.start()].count('"') % 2 == 0:
                comment = m.group(0)
        lines[at:last + 1] = [new + comment]
    return "\n".join(lines)


def parse_doctor(text: str) -> dict:
    """`gamevm doctor`'s report as sections of {level, msg, hint}."""
    sections, fails, warns = [], 0, 0
    for line in text.splitlines():
        if line.startswith("  [") and len(line) > 8 and line[7] == "]":
            level = line[3:7].strip().lower()
            if not sections:
                sections.append({"title": "Checks", "items": []})
            sections[-1]["items"].append({"level": level, "msg": line[9:].strip(), "hint": ""})
            fails += level == "fail"
            warns += level == "warn"
        elif line.startswith("         -> ") and sections and sections[-1]["items"]:
            sections[-1]["items"][-1]["hint"] = line[12:].strip()
        elif line and not line.startswith((" ", "gamevm ")) and not re.match(r"^\d+ problem", line):
            sections.append({"title": line.strip(), "items": []})
    return {"sections": [s for s in sections if s["items"]], "fails": fails, "warns": warns}


def read(path, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return default


def short_gpu_name(desc: str) -> str:
    """'VGA ...: NVIDIA Corporation AD103 [GeForce graphics card] [10de:2702] (rev a1)' -> 'GeForce graphics card'."""
    m = re.search(r"\[([^\[\]]+)\]\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", desc)
    if m:
        return m.group(1)
    return re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\].*$", "", desc.split(": ", 1)[-1]).strip()


def app_name(app_id: str) -> str:
    """The name a desktop file gives an app ('org.mozilla.firefox' -> 'Firefox'), else the id's last part."""
    for base in [Path.home() / ".local/share", *map(Path, (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":"))]:
        m = re.search(r"^Name=(.+)$", read(base / "applications" / f"{app_id}.desktop"), re.M)
        if m:
            return m.group(1).strip()
    return app_id.rsplit(".", 1)[-1]


# ---------------------------------------------------------------------------------------------
# sample data: GAMEVM_UI_DEMO=off|running|fresh draws the window with a made-up PC (pictures for the
# guides, tests). Nothing of the real machine is read or changed then.
# ---------------------------------------------------------------------------------------------
SAMPLE_HW = {
    "cores": [{"id": i, "threads": [i, i + 8], "windows": 1 <= i <= 6} for i in range(8)],
    "memTotal": 32,
    "disks": [{"name": "Samsung SSD 990 PRO", "size": "2 TB", "role": "windows"},
              {"name": "WD Blue HDD", "size": "4 TB", "role": "shared"},
              {"name": "Crucial P5 Plus", "size": "1 TB", "role": "linux"}],
    "gpus": [{"name": "GeForce RTX 4070", "role": "windows"}, {"name": "Radeon Graphics", "role": "linux"}],
    "usb": [{"id": "046d:c33f", "name": "USB Keyboard", "owner": "both"}, {"id": "046d:c08b", "name": "Gaming Mouse", "owner": "both"},
            {"id": "045e:02fe", "name": "Xbox Wireless Adapter", "owner": "windows"},
            {"id": "0d8c:0014", "name": "USB Headset", "owner": "linux"}, {"id": "046d:085e", "name": "Webcam", "owner": "linux"}],
}
SAMPLE_CFG = {"gpu": {"devices": ["0000:01:00.0", "0000:01:00.1"]}, "disk": {"nvme": "0000:02:00.0", "unmount": ["/mnt/windows"]},
              "cpu": {"guest_cores": [1, 2, 3, 4, 5, 6], "host_cores": [0, 7]}, "vm": {"memory_mib": 16384},
              "input": {"keyboard": "/dev/input/by-id/usb-Keyboard-event-kbd", "mouse": "/dev/input/by-id/usb-Mouse-event-mouse"},
              "network": {"host_nic": "enp5s0"}, "usb": {"devices": ["045e:02fe"]}}
SAMPLE_DOCTOR = {"sections": [{"title": t, "items": [{"level": "ok", "msg": m, "hint": ""} for m in ms]} for t, ms in (
    ("Platform", ["/dev/kvm is usable", "IOMMU enabled"]), ("Mode", ["mode = igpu", "desktop: main monitor through the graphics card"]),
    ("GPU passthrough", ["GeForce RTX 4070 in a group of its own"]), ("Windows disk", ["Samsung SSD 990 PRO, file system clean"]),
    ("USB and input", ["keyboard and mouse found"]), ("Network", ["private link active"]))],
    "fails": 0, "warns": 0, "when": "17:40", "running": False, "ran": True}
SAMPLE_LOG = ["2026-01-10 17:02:11 [info] preparing host for win11", "2026-01-10 17:02:14 [info] restarting KWin (the session stays; non-KDE apps close)",
              "2026-01-10 17:02:16 [info] host prepared", "2026-01-10 19:48:03 [info] win11 stopped", "2026-01-10 19:48:09 [info] host restored"]


# ---------------------------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------------------------

class Service:
    def __init__(self, g, config: str | None = None, demo: str = ""):
        self.g = g
        self.config_path = config
        self.demo = demo  # "running" | "off": show that state without a VM (screens for tests and docs)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.rev = 0
        self.snap: dict = {"rev": 0}
        self.snap_key = ""
        self.job: dict | None = None
        self.last_error = ""
        self.doctor = {"sections": [], "fails": 0, "warns": 0, "when": "", "running": False, "ran": False}
        self.doctor_at = 0.0
        self.hw: dict = {}
        self.cfg: dict = {}
        self.cfg_error = ""
        self.cfg_mtime = -1.0
        self.was_running = False
        self.usb_seen: set[str] | None = None
        self.usb_asks: dict[str, str] = {}
        self.paired: bool | None = None
        self.pair_tried = False
        self.pair_failed = ""
        self.shutdown_unit: bool | None = None
        self.children: list[subprocess.Popen] = []
        self.monitors: list[subprocess.Popen] = []   # helpers that end with the window
        self.win: dict = {}                          # what is known about the running Windows; see reset_win()
        self.win_asked = 0.0
        self.iso = {"state": "", "done": 0, "total": 0, "error": ""}
        self.iso_range: str | None = None            # tests: fetch only this byte range
        self._downloads: Path | None = None
        self.prefs = dict(PREF_DEFAULTS)
        try:
            self.prefs.update({k: v for k, v in json.loads(read(PREFS_FILE) or "{}").items() if k in PREF_DEFAULTS})
        except ValueError:
            pass
        self.transfers: list[dict] = [x for x in self.prefs["transfers"] if isinstance(x, dict)][-30:]
        for old in self.transfers:  # a transfer the last window was still watching
            if old.get("state") == "sending":
                old["state"] = "sent"
        self.reset_win()
        self.found: dict = {}  # what gamevm_discover found, while nothing is installed yet
        if demo:
            self.prefs = dict(PREF_DEFAULTS, winprep_done=demo != "fresh", first_start_done=demo != "fresh", paired_once=True)
            self.transfers = []
            self.doctor = dict(SAMPLE_DOCTOR)
            self.shutdown_unit = True
            self._downloads = Path("/home/you/Downloads")
        if demo == "running":  # a linked Windows with a few transfers
            self.paired = True
            self.win.update(name="WINDOWS-PC", downloads="C:\\Users\\You\\Downloads", shares=["C", "D"], info=True)
            self.prefs["win_tool"] = "demo"
            self.transfers = [
                {"id": "1", "dir": "to", "name": "screenshots.zip", "size": 48_300_000, "state": "arrived", "progress": 1.0,
                 "when": "17:02", "where": "C:\\Users\\You\\Downloads\\screenshots.zip"},
                {"id": "2", "dir": "from", "name": "savegame-backup.7z", "size": 912_000_000, "state": "received", "progress": 1.0,
                 "when": "17:20", "where": "/home/you/Downloads/savegame-backup.7z"},
                {"id": "3", "dir": "to", "name": "mod-pack-2.1.zip", "size": 1_740_000_000, "state": "sending", "progress": 0.42,
                 "when": "17:31", "where": ""}]

    # ------------------------------------------------------------------ small helpers
    def save_prefs(self) -> None:
        if self.demo:
            return
        try:
            PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
            PREFS_FILE.write_text(json.dumps(self.prefs, indent=2) + "\n")
        except OSError:
            pass

    def cli(self, *args: str) -> list[str]:
        return [sys.executable, "-I", str(HERE / "gamevm.py"), *(["-c", self.config_path] if self.config_path else []), *args]

    def repo(self) -> Path | None:
        """The project folder with the installer scripts: this checkout, or the one that was installed from."""
        for p in (HERE.parent, Path(read(INSTALL_DIR / "SOURCE").strip() or "/nonexistent")):
            if (p / "setup" / "install-host.sh").is_file():
                return p
        return None

    def user(self) -> pwd.struct_passwd:
        return pwd.getpwuid(os.getuid())

    def installed(self) -> bool:
        return self.demo != "fresh" and (bool(self.demo) or (self.g.ETC_CONFIG.exists() and INSTALLED_CLI.exists() and HOOK.exists()))

    def load_cfg(self) -> None:
        g = self.g
        if self.demo:
            if not self.cfg:
                self.cfg = g.deep_merge(g.DEFAULTS, SAMPLE_CFG)
            return
        if not self.config_path and not g.ETC_CONFIG.exists():
            # Nothing installed yet: show what the installer would write for this PC.
            if not self.cfg:
                import gamevm_discover
                d = self.found = gamevm_discover.discover()
                self.cfg = g.deep_merge(g.DEFAULTS, {
                    "gpu": {"devices": d["gpu"]["devices"]}, "disk": {"nvme": d["disk"]["nvme"], "boot_disk": d["disk"]["boot_disk"], "unmount": d["disk"]["unmount"]},
                    "cpu": {"guest_cores": d["cpu"]["guest_cores"] or [1], "host_cores": d["cpu"]["host_cores"] or [0]},
                    "vm": {"memory_mib": d["memory_mib"]}, "input": d["input"],
                    "network": d["network"], "audio": {"user": self.user().pw_name}})
                self.cfg_error = ""
            return
        path = Path(self.config_path) if self.config_path else (g.ETC_CONFIG if g.ETC_CONFIG.exists() else g.REPO_CONFIG)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime == self.cfg_mtime and self.cfg:
            return
        self.cfg_mtime = mtime
        try:
            self.cfg, self.cfg_error = g.load_config(self.config_path), ""
        except g.GameVMError as e:
            self.cfg, self.cfg_error = g.deep_merge(g.DEFAULTS, {}), str(e)
        self.found = {}
        self.hw = {}

    def in_terminal(self, command: str) -> tuple[bool, str]:
        """Run a command the user has to see and answer (the installers ask questions and the password)."""
        script = f"{command}; echo; read -r -p 'Press Enter to close this window ' _"
        for argv in (["konsole", "-e", "bash", "-c", script], ["xdg-terminal-exec", "bash", "-c", script],
                     ["kitty", "bash", "-c", script], ["xterm", "-e", "bash", "-c", script]):
            if shutil.which(argv[0]):
                self.children.append(subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True))
                return True, "A terminal window opened: follow it there."
        return False, f"No terminal program found. Run this yourself: {command}"

    def spawn(self, argv: list[str]) -> None:
        self.children.append(subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, start_new_session=True))

    def busy(self) -> bool:
        return bool(self.job and not self.job["done"])

    def start_job(self, kind: str, title: str, argv: list[str], after=None) -> tuple[bool, str]:
        with self.lock:
            if self.busy():
                return False, f"Still busy: {self.job['title']}"
            job = {"kind": kind, "title": title, "lines": collections.deque(maxlen=200), "done": False, "rc": None}
            self.job = job

        def work():
            try:
                p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, errors="replace")
                for line in p.stdout:
                    job["lines"].append(line.rstrip("\n"))
                    self.build()
                rc = p.wait()
            except OSError as e:
                job["lines"].append(str(e))
                rc = 1
            job["rc"] = rc
            if after:
                try:
                    after(rc, list(job["lines"]))
                except Exception as e:  # noqa: BLE001 - a follow-up must never take the service down
                    job["lines"].append(f"({e})")
            job["done"] = True
            self.build()

        threading.Thread(target=work, daemon=True).start()
        self.build()
        return True, ""

    # ------------------------------------------------------------------ sharing files
    # Text goes over the clipboard link by itself. Files: Linux -> Windows through KDE Connect, and
    # the service watches them arrive through the guest agent; Windows -> Linux through the "Send
    # to > Linux (gamevm)" entry gamevm puts into Windows, which KDE Connect on Linux announces.
    def reset_win(self) -> None:
        self.win = {"dev": "", "name": "", "user": "", "downloads": "", "shares": [], "info": False, "tool_tried": False}

    def linux_downloads(self) -> Path:
        if self._downloads is None:
            try:
                out = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            self._downloads = Path(out) if out and out != str(Path.home()) else Path.home() / "Downloads"
        return self._downloads

    def add_transfer(self, **fields) -> dict:
        t = {"id": str(time.time_ns()), "when": time.strftime("%H:%M"), "day": time.strftime("%Y-%m-%d"), "progress": 0.0,
             "where": "", **fields}
        with self.lock:
            self.transfers.append(t)
            del self.transfers[:-30]
        self.save_transfers()
        self.build()
        return t

    def save_transfers(self) -> None:
        if self.demo:
            return
        self.prefs["transfers"] = [dict(x) for x in self.transfers]
        self.save_prefs()

    def windows_info(self) -> None:
        """Once per Windows run: who is signed in, their Downloads folder, the drives Windows shares."""
        if self.win["info"] or time.monotonic() - self.win_asked < 15:
            return
        self.win_asked = time.monotonic()
        ps = r"""$ErrorActionPreference = 'SilentlyContinue'
$u = (Get-CimInstance Win32_ComputerSystem).UserName
$dl = ''
if ($u) {
    $sid = ([System.Security.Principal.NTAccount]$u).Translate([System.Security.Principal.SecurityIdentifier]).Value
    $prof = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid").ProfileImagePath
    $dl = (Get-ItemProperty "Registry::HKEY_USERS\$sid\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders").'{374DE290-123F-4565-9164-39C4925E467B}'
    if ($dl) { $dl = $dl.Replace('%USERPROFILE%', $prof) } else { $dl = "$prof\Downloads" }
}
@{ user = "$u"; downloads = "$dl"; shares = @(Get-SmbShare | Where-Object { -not $_.Special } | ForEach-Object { $_.Name }) } | ConvertTo-Json -Compress"""
        try:
            _, out, _ = self.g.guest_powershell(self.cfg, ps, timeout=30)
            info = json.loads(out.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001 - no guest agent (yet): asked again later
            return
        self.win.update(info=True, user=str(info.get("user") or ""), downloads=str(info.get("downloads") or ""),
                        shares=[str(s) for s in info.get("shares") or [] if re.fullmatch(r"[\w$ .-]{1,80}", str(s))])

    def follow_sent(self, t: dict) -> None:
        """Watch a file that was sent arrive in Windows' Downloads folder, through the guest agent."""
        quote = lambda s: s.replace("'", "''")  # noqa: E731 - inside PowerShell's single quotes
        stem = Path(t["name"]).stem
        waited = 0
        while waited < 900 and not self.stop_event.is_set() and self.vm_state() == "running":
            time.sleep(2)
            waited += 2
            folder = self.win["downloads"]
            if not folder:
                if waited >= 40:
                    break  # no guest agent: it was handed to KDE Connect, more is not known
                continue
            ps = (f"Get-ChildItem -LiteralPath '{quote(folder)}' -File | Where-Object {{ $_.Name.StartsWith('{quote(stem)}') -and "
                  "$_.CreationTime -gt (Get-Date).AddMinutes(-30) } | Sort-Object CreationTime -Descending | "
                  "Select-Object -First 1 | ForEach-Object { '{0}|{1}' -f $_.Length, $_.FullName }")
            try:
                _, out, _ = self.g.guest_powershell(self.cfg, ps, timeout=20)
            except Exception:  # noqa: BLE001
                continue
            line = out.strip().splitlines()[-1] if out.strip() else ""
            size, _, where = line.partition("|")
            if not size.isdigit() or not where:
                continue
            t["progress"] = min(1.0, int(size) / max(1, t["size"]))
            if int(size) >= t["size"] and not where.lower().endswith(".part"):
                t.update(state="arrived", where=where, progress=1.0)
                break
            self.build()
        if t["state"] == "sending":
            t["state"] = "sent"
        self.save_transfers()
        self.build()

    def send_files(self, paths: list) -> tuple[bool, str]:
        g = self.g
        if self.vm_state() != "running":
            return False, "Windows is off. Its drives are open on Linux instead: see Windows' drives."
        try:
            found = g.kde_windows_device(self.user())
        except Exception:  # noqa: BLE001
            found = None
        if not found or not self.paired:
            return False, "Windows is not linked yet. gamevm links it by itself once you are signed in there; try again in a moment."
        sent, skipped = [], []
        for raw in paths[:100]:
            path = urllib.parse.unquote(str(raw).removeprefix("file://"))
            name = os.path.basename(path.rstrip("/")) or path
            if os.path.isdir(path):
                skipped.append(f"{name} is a folder: send its files, or a zip of it")
                continue
            if not os.path.isfile(path):
                skipped.append(f"{name} was not found")
                continue
            try:
                res = subprocess.run(["kdeconnect-cli", "-d", found[0], "--share", path], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as e:
                skipped.append(f"{name}: {e}")
                continue
            if res.returncode != 0:
                skipped.append(f"{name}: KDE Connect refused it")
                continue
            t = self.add_transfer(dir="to", name=name, size=os.path.getsize(path), state="sending")
            threading.Thread(target=self.follow_sent, args=(t,), daemon=True).start()
            sent.append(name)
        said = (f"Sending {len(sent)} file(s) to Windows' Downloads folder." if sent else "") + (
            (" Not sent: " + "; ".join(skipped) + ".") if skipped else "")
        return bool(sent), said.strip() or "Nothing to send."

    def watch_received(self) -> None:
        """Files that arrive from another device: KDE Connect on Linux announces each on D-Bus."""
        try:
            p = subprocess.Popen(["dbus-monitor", "--session",
                                  "type='signal',sender='org.kde.kdeconnect',interface='org.kde.kdeconnect.device.share',"
                                  "member='shareReceived'"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 errors="replace")
        except OSError:
            return
        self.monitors.append(p)
        dev = ""
        for line in p.stdout:
            m = re.search(r"path=/modules/kdeconnect/devices/([^/;]+)/share", line)
            if m:
                dev = m.group(1)
                continue
            m = re.match(r'\s+string "(.*)"', line)
            if not m or not dev:
                continue
            sender, dev = dev, ""
            if self.win["dev"] and sender != self.win["dev"]:
                continue  # a phone or another PC: not this page's business
            path = urllib.parse.unquote(m.group(1).removeprefix("file://"))
            self.add_transfer(dir="from", name=os.path.basename(path), state="received", where=path,
                              size=os.path.getsize(path) if os.path.isfile(path) else 0, progress=1.0)

    def ensure_windows_tool(self) -> None:
        """Windows' half of sending files ("Send to > Linux (gamevm)"): added without asking, once
        per version of the script, when Windows is paired."""
        if self.win["tool_tried"]:
            return
        self.win["tool_tried"] = True
        try:
            script = self.g.bench_script().parent / "send-to-linux.ps1"
            digest = hashlib.sha256(script.read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001
            return
        if digest == self.prefs["win_tool"]:
            return

        def work():
            try:
                ok, _said = self.g.install_windows_share(self.cfg)
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                self.prefs["win_tool"] = digest
                self.save_prefs()
                self.build()

        threading.Thread(target=work, daemon=True).start()

    def ensure_driver_disc(self) -> None:
        """The Windows driver disc (virtio-win.iso): fetched into Downloads without asking when it
        is nowhere yet. https only, and it has to be a disc image before it counts."""
        cfg = self.cfg
        if self.iso["state"] or Path(cfg["vm"]["virtio_iso"]).exists() or list((Path.home() / "Downloads").glob("virtio-win*.iso")):
            return
        self.iso.update(state="downloading", done=0, total=0, error="")

        def work():
            dest = Path.home() / "Downloads" / "virtio-win.iso"
            part = dest.with_name(dest.name + ".part")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                head = subprocess.run(["curl", "-fsSIL", "--max-time", "60", "-o", "/dev/null", "-w",
                                       "%{url_effective} %header{content-length}", DRIVER_DISC_ISO],
                                      capture_output=True, text=True, timeout=90)
                final, _, length = head.stdout.strip().partition(" ")
                final = "https://" + final.split("://", 1)[-1]  # the server redirects to plain http: not taken
                if head.returncode != 0 or not re.fullmatch(r"https://fedorapeople\.org/[\w./-]+\.iso", final):
                    raise OSError("the download address could not be found")
                self.iso["total"] = int(length) if length.isdigit() else 0
                argv = ["curl", "-fsL", "--proto", "=https", "--proto-redir", "=https", "--retry", "3", "-o", str(part),
                        *(["-r", self.iso_range] if self.iso_range else ["-C", "-"]), final]  # -C -: carry on a part
                p = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
                self.monitors.append(p)
                while p.poll() is None:
                    time.sleep(1)
                    self.iso["done"] = part.stat().st_size if part.exists() else 0
                    self.build()
                if p.returncode != 0 or not part.exists():
                    raise OSError("the download did not finish")
                with open(part, "rb") as f:
                    f.seek(32769)
                    mark = f.read(5)
                if mark != b"CD001":
                    part.unlink()
                    raise OSError("what arrived is not a disc image")
                part.rename(dest)
                self.iso["state"] = "done"
            except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                self.iso.update(state="failed", error=str(e))
            self.build()

        threading.Thread(target=work, daemon=True).start()

    def sharing(self, state: str) -> dict:
        g, cfg = self.g, self.cfg
        if state == "running":
            drives = [{"label": f"{s}:" if len(s) == 1 else s, "detail": "", "url": f"smb://{g.GUEST_LINK_IP}/{s}/"}
                      for s in self.win["shares"]] or [{"label": "Windows' drives", "detail": "", "url": f"smb://{g.GUEST_LINK_IP}/"}]
        else:
            drives = [{"label": Path(p).name, "detail": p, "url": p} for p in cfg["disk"]["unmount"]]
        can = state == "running" and bool(self.paired)
        why = "" if can else ("Windows is off. Its drives are open on Linux instead, below." if state == "off"
                              else "Windows is starting or shutting down." if state != "running"
                              else "Linking with Windows. This happens by itself once you are signed in there.")
        return {"state": state, "canSend": can, "why": why, "linked": bool(self.paired), "device": self.win["name"],
                "link": cfg["network"]["hostonly"], "drives": drives, "transfers": list(reversed(self.transfers))[:12],
                "downloads": str(self.linux_downloads()), "winDownloads": self.win["downloads"],
                "winTool": bool(self.prefs["win_tool"])}

    # ------------------------------------------------------------------ what is true right now
    def vm_state(self) -> str:
        if self.demo:
            return "off" if self.demo == "fresh" else self.demo
        g = self.g
        if self.job and not self.job["done"] and self.job["kind"] in ("start", "stop"):
            return "starting" if self.job["kind"] == "start" else "stopping"
        try:
            st = g.domain_state(self.cfg["vm"]["name"])
        except Exception:  # noqa: BLE001 - libvirt down, domain not defined yet
            st = ""
        if st in ("running", "paused"):
            return "running"
        if st and not g.domain_off(st):
            return "stopping"
        if g.STATE_FILE.exists():  # the hook is preparing or restoring the host
            return "stopping" if self.was_running else "starting"
        return "off"

    def mode(self) -> str:
        if self.demo:
            return "igpu"
        if self.found:
            return self.found["mode"]
        try:
            return self.g.resolve_mode(self.cfg)
        except Exception:  # noqa: BLE001
            return "single"

    def inventory(self) -> dict:
        """The hardware the Resources page shows, with who has what according to the config."""
        g, cfg = self.g, self.cfg
        if self.demo:
            return SAMPLE_HW
        cores = {}
        try:
            cores = g.cpu_cores()
        except Exception:  # noqa: BLE001
            pass
        guest = set(cfg["cpu"]["guest_cores"])
        mem_total = 0
        m = re.search(r"^MemTotal:\s+(\d+) kB", read("/proc/meminfo"), re.M)
        if m:
            mem_total = int(m.group(1)) // (1024 * 1024)
        # disks
        win_disks, shared = set(), set()
        if cfg["disk"]["nvme"]:
            win_disks = {p.name for p in Path(f"/sys/bus/pci/devices/{cfg['disk']['nvme']}").glob("nvme/nvme*/nvme*n*")}
        if cfg["disk"]["boot_disk"]:  # Windows on a SATA disk
            win_disks.add(Path(os.path.realpath(cfg["disk"]["boot_disk"])).name)
        for extra in cfg["disk"]["extra"]:
            shared.add(Path(os.path.realpath(extra)).name)
        disks = []
        try:
            out = subprocess.run(["lsblk", "-J", "-d", "-b", "-o", "NAME,SIZE,MODEL,TYPE"], capture_output=True, text=True, timeout=10).stdout
            for d in json.loads(out or "{}").get("blockdevices", []):
                if d.get("type") != "disk" or d["name"].startswith(("zram", "loop")):
                    continue
                role = "windows" if d["name"] in win_disks else "shared" if d["name"] in shared else "linux"
                gb = int(d.get("size") or 0) / 1e9  # as on the box: decimal units
                size = f"{gb / 1000:.1f}".rstrip("0").rstrip(".") + " TB" if gb >= 950 else f"{gb:.0f} GB"
                disks.append({"name": (d.get("model") or d["name"]).strip(), "size": size, "role": role})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        disks.sort(key=lambda d: ("windows", "shared", "linux").index(d["role"]))
        # graphics
        gpus = []
        for bdf in cfg["gpu"]["devices"][:1]:
            gpus.append({"name": short_gpu_name(g.pci_desc(bdf)), "role": "windows"})
        try:
            for bdf in g.host_display_gpus(cfg):
                gpus.append({"name": short_gpu_name(g.pci_desc(bdf)), "role": "linux"})
        except Exception:  # noqa: BLE001
            pass
        return {"cores": [{"id": c, "threads": cores[c], "windows": c in guest} for c in sorted(cores)],
                "memTotal": mem_total, "disks": disks, "gpus": gpus, "usb": self.usb_devices()}

    def input_usb_ids(self) -> set[str]:
        ids = set()
        inp = self.cfg["input"]
        for path in [inp["keyboard"], inp["mouse"], *inp["extra"]]:
            if not path or not os.path.exists(path):
                continue
            p = (Path("/sys/class/input") / Path(os.path.realpath(path)).name / "device").resolve()
            while p != p.parent:
                if (p / "idVendor").exists():
                    ids.add(f"{read(p / 'idVendor').strip()}:{read(p / 'idProduct').strip()}")
                    break
                p = p.parent
        return ids

    def usb_now(self) -> dict[str, str]:
        """{vid:pid: name} of the USB devices that are plugged in (hubs left out)."""
        out = {}
        for d in sorted(Path("/sys/bus/usb/devices").glob("*")):
            if ":" in d.name or not (d / "idVendor").exists() or read(d / "bDeviceClass").strip() == "09":
                continue
            maker, prod = read(d / "manufacturer").strip(), read(d / "product").strip()
            name = prod if not maker or maker.lower() in prod.lower() else f"{maker} {prod}"
            dev_id = f"{read(d / 'idVendor').strip()}:{read(d / 'idProduct').strip()}"
            out.setdefault(dev_id, name or f"USB device {dev_id}")
        return out

    def usb_devices(self) -> list[dict]:
        both, win = self.input_usb_ids(), set(self.cfg["usb"]["devices"])
        return [{"id": i, "name": n, "owner": "both" if i in both else "windows" if i in win else "linux"}
                for i, n in self.usb_now().items()]

    # ------------------------------------------------------------------ checks
    def run_doctor(self) -> None:
        if self.doctor["running"] or self.demo:
            return
        self.doctor["running"] = True

        def work():
            try:
                out = subprocess.run(self.cli("doctor"), capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
                parsed = parse_doctor(out.stdout)
            except (OSError, subprocess.TimeoutExpired) as e:
                parsed = {"sections": [{"title": "Checks", "items": [{"level": "fail", "msg": f"the checks did not run: {e}",
                                                                       "hint": ""}]}], "fails": 1, "warns": 0}
            self.doctor = {**parsed, "when": time.strftime("%H:%M"), "running": False, "ran": True}
            self.doctor_at = time.monotonic()
            self.build()

        threading.Thread(target=work, daemon=True).start()

    def stale_install(self) -> list[str]:
        """Files of the project folder whose installed copy differs (the doctor's own comparison)."""
        for sec in self.doctor["sections"]:
            for item in sec["items"]:
                m = re.search(r"the installed gamevm is not the one in .*\(differs: (.+)\)", item["msg"])
                if m:
                    return [x.strip() for x in m.group(1).split(",")]
        return []

    # ------------------------------------------------------------------ setup
    def setup_steps(self, mode: str) -> list[dict]:
        g, cfg, hw = self.g, self.cfg, self.hw
        installed = self.installed()
        if not self.demo and not self.prefs["first_start_done"] and "host prepared" in read(g.LOG_FILE):
            self.prefs.update(first_start_done=True, winprep_done=True)  # Windows has run here before this window existed
            self.save_prefs()
        steps = []

        def add(step_id, title, by, done, text_done, text_todo, action=None, note=""):
            steps.append({"id": step_id, "title": title, "by": by, "done": bool(done),
                          "text": text_done if done else text_todo, "action": None if done else action, "note": note})

        win = next((x["name"] for x in hw.get("gpus", []) if x["role"] == "windows"), "")
        lin = next((x["name"] for x in hw.get("gpus", []) if x["role"] == "linux"), "")
        disk = next((x["name"] for x in hw.get("disks", []) if x["role"] == "windows"), "")
        found = ", ".join(x for x in (f"{win} for Windows" if win else "", f"{lin} for Linux" if lin else "",
                                      f"Windows on the {disk}" if disk else "") if x)
        trouble = " ".join(self.found.get("problems", []))
        add("look", "Looked at this PC", "gamevm", bool(win) and not trouble, found + ".",
            trouble or "No graphics card for Windows was found.",
            {"id": "open-requirements", "label": "What gamevm needs"} if trouble or not win else None)
        if mode == "igpu":
            igpus = g.host_display_gpus(cfg)
            on_board = bool(self.demo) or any(
                read(c / "status").strip() == "connected" for c in Path("/sys/class/drm").glob("card*-*")
                if (c / "device" / "device").resolve().name in igpus)
            add("cable", "Plug the side monitor into the motherboard", "you", on_board,
                "Noticed: a monitor is on the motherboard's output.",
                "Move the side monitor's cable from the graphics card to the motherboard (HDMI or DisplayPort). "
                "gamevm notices when it is there.")
        iso = bool(self.demo) or Path(cfg["vm"]["virtio_iso"]).exists() or bool(list((Path.home() / "Downloads").glob("virtio-win*.iso")))
        if self.iso["state"] == "failed":
            add("iso", "Download the Windows driver disc", "you", iso, "The driver disc is here.",
                f"gamevm could not download it ({self.iso['error']}). Download virtio-win.iso into your Downloads folder; "
                "gamevm notices when it has arrived.", {"id": "open-driver-page", "label": "Open the download page"})
        else:
            pct = f" {100 * self.iso['done'] // self.iso['total']} %" if self.iso["total"] else ""
            add("iso", "Download the Windows driver disc", "gamevm", iso, "The driver disc is here.",
                f"Downloading it into your Downloads folder (about 880 MB) ...{pct}")
        add("install", "Install on this PC", "gamevm", installed, "Installed. A snapshot was taken first.",
            "Sets up the virtual machine software, the hand-over of the graphics card and the private link.",
            {"id": "install", "label": "Install"}, "Opens a terminal and asks for your password.")
        try:
            in_group = grp.getgrnam("libvirt").gr_gid in os.getgroups()
        except KeyError:
            in_group = False
        restarted = installed and (bool(self.demo) or (in_group and (mode != "igpu" or g.kwin_managed())))
        add("restart", "Restart the PC", "you", restarted, "Done: the new permissions and the graphics layout are active.",
            "The new permissions and the graphics layout apply after a restart. Setup carries on by itself when you log "
            "in again.", {"id": "restart", "label": "Restart now"})
        if mode == "igpu" and cfg["looking_glass"]["enabled"]:
            add("lg", "Add Windows in a window", "gamevm", installed and (bool(self.demo) or bool(shutil.which("looking-glass-client"))),
                "Installed: Looking Glass can show Windows on the side monitor.",
                "Builds Looking Glass, which shows Windows in a window on the side monitor.",
                {"id": "install-lg", "label": "Install"}, "Opens a terminal and asks for your password.")
        add("winprep", "Run one script in Windows", "you", self.prefs["winprep_done"], "Done.",
            "Start Windows the way you always did, get this project's windows folder there (download the project in "
            "Windows), run 1-prepare-windows.ps1 as administrator, then shut Windows down.",
            {"id": "winprep-done", "label": "I have done this"})
        checked = self.doctor["ran"] and self.doctor["fails"] == 0
        add("check", "Check everything", "gamevm", checked,
            "No problems found." if not self.doctor["warns"] else f"No problems found, {self.doctor['warns']} notes.",
            "Checking ..." if self.doctor["running"] or not self.doctor["ran"]
            else f"{self.doctor['fails']} problem(s) found: see Needs you on the Home page.",
            None if self.doctor["running"] or not self.doctor["ran"] else {"id": "run-checks", "label": "Check again"})
        add("first", "Start Windows for the first time", "you", self.prefs["first_start_done"],
            "Done: Windows has run here.",
            "Start Windows from the Home page, then run C:\\gamevm\\2-inside-vm.ps1 inside it once (drivers, sharing).",
            {"id": "goto-home", "label": "Go to Home"})
        current = next((s for s in steps if not s["done"]), None)
        for s in steps:
            s["state"] = "done" if s["done"] else "current" if s is current else "waiting"
        if restarted and not self.demo and AUTOSTART_FILE.exists():
            AUTOSTART_FILE.unlink(missing_ok=True)
        return steps

    # ------------------------------------------------------------------ while Windows runs
    def watch_running(self) -> None:
        """Clipboard link and USB devices, looked after without asking where the answer is known."""
        g, cfg = self.g, self.cfg
        self.windows_info()
        now = self.usb_now()
        if self.usb_seen is None:
            self.usb_seen = set(now)
        fresh = set(now) - self.usb_seen
        self.usb_seen = set(now)
        skip = self.input_usb_ids()
        for dev in fresh - skip:
            rule = "windows" if dev in cfg["usb"]["devices"] else self.prefs["usb_rules"].get(dev) or self.prefs["new_usb"]
            if rule == "windows":
                self.spawn(self.cli("usb", "attach", dev))
            elif rule == "ask":
                self.usb_asks[dev] = now[dev]
        for dev in list(self.usb_asks):
            if dev not in now:
                del self.usb_asks[dev]
        if cfg["network"]["hostonly"] and not self.busy():
            try:
                u = self.user()
                found = g.kde_windows_device(u)
                self.paired = bool(found) and g.kde_device(u, found[0], "call", "isPaired") == ["b", "true"]
            except Exception:  # noqa: BLE001 - no KDE Connect on this side
                self.paired = None
                found = None
            if found:
                self.win.update(dev=found[0], name=found[1])
            if self.paired and not self.prefs["paired_once"]:
                self.prefs["paired_once"] = True
                self.save_prefs()
            if self.paired:
                self.ensure_windows_tool()
            if found and self.paired is False and not self.pair_tried:
                self.pair_tried = True  # once per Windows run; `gamevm share --pair` does both halves itself

                def paired(rc, lines):
                    self.pair_failed = "" if rc == 0 else next((x.strip() for x in reversed(lines) if "[ -- ]" in x), "pairing failed")
                self.start_job("pair", "Linking the clipboard with Windows", self.cli("share", "--pair"), paired)

    # ------------------------------------------------------------------ the snapshot the window draws
    def needs(self, state: str, mode: str, steps: list[dict]) -> list[dict]:
        g, out = self.g, []
        todo = [s for s in steps if not s["done"] and (s["id"] != "check" or self.doctor["ran"])]
        if todo:
            out.append({"id": "setup", "title": "Finish setting up", "text": f"{len(todo)} of {len(steps)} steps are left. Next: "
                        f"{todo[0]['title'].lower()}.", "primary": {"id": "goto-setup", "label": "Open setup"}})
        if self.last_error:
            out.append({"id": "error", "title": "That did not work", "text": self.last_error,
                        "primary": {"id": "dismiss-error", "label": "OK"}})
        if self.cfg_error:
            out.append({"id": "config", "title": "The settings file has a mistake", "text": self.cfg_error,
                        "primary": {"id": "open-config", "label": "Open the file"}})
        stale = self.stale_install()
        if stale:
            out.append({"id": "update", "title": "Update the installed gamevm",
                        "text": "The project folder is newer than what is installed ("
                                + ("1 file differs" if len(stale) == 1 else f"{len(stale)} files differ")
                                + "). Until you update, Windows starts with the older version.",
                        "primary": {"id": "install", "label": "Update now"}, "note": "Opens a terminal and asks for your password."})
        for sec in self.doctor["sections"]:
            for item in sec["items"]:
                if item["level"] == "fail":
                    out.append({"id": "check-" + str(len(out)), "title": item["msg"][:1].upper() + item["msg"][1:],
                                "text": item["hint"], "primary": {"id": "goto-help", "label": "See all checks"}})
        if state == "off" and g.PENDING_FILE.exists():
            out.append({"id": "back", "title": "Bring the Linux desktop back to the main monitor?",
                        "text": "Windows is off, and the desktop is still on the side monitor. Moving it back closes and "
                                "reopens the apps that cannot move.",
                        "primary": {"id": "back", "label": "Bring it back"}, "note": "Asks for your password."})
        if state == "running":
            for dev, name in self.usb_asks.items():
                out.append({"id": "usb-" + dev, "kind": "usb", "device": dev, "title": f"Give the {name} to Windows?",
                            "text": "It was just plugged in. On Linux it stays usable here; given to Windows, only Windows "
                                    "sees it until Windows shuts down.",
                            "primary": {"id": "usb-keep", "label": "Keep it on Linux"},
                            "secondary": {"id": "usb-give", "label": "Give it to Windows"}, "remember": True})
            if self.pair_failed:
                out.append({"id": "pair", "title": "The clipboard link did not come up", "text": self.pair_failed,
                            "primary": {"id": "pair", "label": "Try again"}})
        return out

    def taken_care(self, state: str, mode: str) -> list[dict]:
        cfg, d, out = self.cfg, self.doctor, []
        toggle = TOGGLE_WORDS.get(cfg["input"]["toggle"], cfg["input"]["toggle"])
        if d["running"] or not d["ran"]:
            out.append({"title": "Checks", "text": "Running now ...", "ok": False})
        elif d["fails"] == 0:
            out.append({"title": "Checks", "ok": True, "text": f"All passed at {d['when']}"
                        + (f", with {d['warns']} notes under Help." if d["warns"] else ".")})
        if cfg["input"]["evdev"] and cfg["input"]["keyboard"]:
            tail = " Unplugging and plugging back in reconnects them." if cfg["input"]["reattach"] else ""
            out.append({"title": "Keyboard and mouse", "ok": True,
                        "text": (f"With Windows. {toggle} switch sides." if state == "running"
                                 else f"Go to Windows when it starts. {toggle} switch sides.") + tail})
        if cfg["network"]["hostonly"]:
            if state == "running" and self.paired:
                text, ok = "Linked. Copy on one side, paste on the other.", True
            elif state == "running":
                text, ok = "Waiting for Windows to answer, then linked without asking.", False
            elif self.prefs["paired_once"]:
                text, ok = "Linked with this Windows. Ready again when it runs.", True
            else:
                text, ok = "Links itself the first time Windows runs.", True
            out.append({"title": "Clipboard and files", "text": text, "ok": ok})
        if mode == "igpu" and cfg["desktop"]["relaunch_all"]:
            out.append({"title": "Your apps", "ok": True,
                        "text": "Moved to the side monitor. The ones that had to close were reopened." if state == "running"
                        else "Move with the desktop. The ones that have to close reopen by themselves."})
        if self.shutdown_unit:
            out.append({"title": "Turning the PC off", "ok": True, "text": "Windows is shut down first, so nothing is lost."})
        return out

    def resources(self) -> dict:
        cfg, hw = self.cfg, self.hw
        total = hw.get("memTotal", 0)
        n = len(hw.get("cores", []))
        keep = {0} if n < 6 else {0, n - 1}  # what Linux keeps by default: the first core, and the last on bigger CPUs
        rec_mem = max(4, min(total - 8, (total + 2) // 4 * 2)) if total else 16  # half of it, in steps of 2 GiB
        return {"cores": hw.get("cores", []), "isolate": cfg["cpu"]["isolate_host"],
                "memTotal": total, "mem": cfg["vm"]["memory_mib"] // 1024, "memMin": 4, "memMax": max(4, total - 4),
                "hugepages": cfg["vm"]["hugepages"], "disks": hw.get("disks", []), "gpus": hw.get("gpus", []),
                "lookingGlass": cfg["looking_glass"]["enabled"], "usb": hw.get("usb", []), "newUsb": self.prefs["new_usb"],
                "network": cfg["network"]["mode"], "privateLink": cfg["network"]["hostonly"],
                "recommended": {"windowsCores": [c["id"] for c in hw.get("cores", []) if c["id"] not in keep],
                                "mem": rec_mem, "isolate": True, "hugepages": True, "network": "macvtap", "privateLink": True}}

    def settings(self) -> dict:
        cfg = self.cfg
        return {"confirmStart": self.prefs["confirm_start"], "reopenApps": cfg["desktop"]["relaunch_all"],
                "restoreWindows": cfg["desktop"]["restore_windows"], "returnAsk": cfg["desktop"]["return"] == "ask",
                "shutdownWait": cfg["vm"]["host_shutdown_wait"], "lgFullscreen": cfg["looking_glass"]["fullscreen"],
                "reattach": cfg["input"]["reattach"], "toggle": cfg["input"]["toggle"],
                "toggles": [{"id": k, "label": v} for k, v in TOGGLE_WORDS.items()]}

    def build(self) -> None:
        with self.lock:
            g = self.g
            self.load_cfg()
            if not self.hw:
                self.hw = self.inventory()
            state, mode = self.vm_state(), self.mode()
            if state == "running" and not self.was_running and not self.prefs["first_start_done"]:
                self.prefs["first_start_done"] = True
                self.save_prefs()
            if state == "off" and self.was_running:
                self.pair_tried, self.pair_failed, self.usb_asks, self.paired = False, "", {}, None
                self.reset_win()
            if state in ("running", "off"):
                self.was_running = state == "running"
            steps = self.setup_steps(mode)
            job = self.job
            snap = {
                "version": g.VERSION,
                "vm": {"name": self.cfg["vm"]["name"], "state": state, "mode": mode,
                       "desktop": "native" if self.demo else g.kwin_layout() if mode == "igpu" and g.kwin_managed() else "",
                       "lookingGlass": mode == "igpu" and self.cfg["looking_glass"]["enabled"]
                       and (bool(self.demo) or bool(shutil.which("looking-glass-client"))),
                       "toggle": TOGGLE_WORDS.get(self.cfg["input"]["toggle"], self.cfg["input"]["toggle"])},
                "busy": {"active": bool(job and not job["done"]), "kind": job["kind"] if job else "",
                         "title": job["title"] if job else "", "lines": list(job["lines"])[-8:] if job else []},
                "needs": self.needs(state, mode, steps),
                "done": self.taken_care(state, mode),
                "sharing": self.sharing(state),
                "resources": self.resources(),
                "setup": {"steps": steps,
                          "left": sum(not s["done"] and (s["id"] != "check" or self.doctor["ran"]) for s in steps),
                          "yours": sum(s["by"] == "you" for s in steps)},
                "settings": self.settings(),
                "help": {"doctor": self.doctor, "log": SAMPLE_LOG if self.demo else read(g.LOG_FILE).splitlines()[-60:],
                         "installed": self.installed(), "canSave": self.installed()},
            }
            key = json.dumps(snap, sort_keys=True)
            if key != self.snap_key:
                self.rev += 1
                self.snap_key = key
                self.snap = {"rev": self.rev, **snap}

    def loop(self) -> None:
        n = 0
        while not self.stop_event.is_set():
            try:
                if n % 40 == 0 and not self.demo:  # once a minute
                    res = subprocess.run(["systemctl", "is-enabled", "gamevm-shutdown.service"], capture_output=True, text=True)
                    self.shutdown_unit = res.stdout.strip() == "enabled"
                    self.hw = {}
                if n == 0 or (not self.busy() and self.vm_state() == "off" and time.monotonic() - self.doctor_at > 900):
                    self.run_doctor()
                if n == 0 and not self.demo:
                    self.ensure_driver_disc()
                if n % 3 == 0 and not self.demo and self.vm_state() == "running":
                    self.watch_running()
                self.build()
            except Exception as e:  # noqa: BLE001 - keep serving; the window shows the last good state
                print(f"gamevm ui: {type(e).__name__}: {e}", file=sys.stderr)
            n += 1
            self.stop_event.wait(1.5)

    # ------------------------------------------------------------------ what the window can ask for
    def save_config(self, changes: dict, what: str) -> tuple[bool, str]:
        """Write changed keys into the configuration: checked here first, installed by root (pkexec)."""
        g = self.g
        changes = {k: v for k, v in changes.items() if self.cfg[k[0]][k[1]] != v}
        if not changes:
            return True, "Nothing to change."
        if not g.ETC_CONFIG.exists() or not INSTALLED_CLI.exists():
            return False, "gamevm is not installed yet: finish the setup first."
        if "config-apply" not in read(INSTALL_DIR / "gamevm.py"):
            return False, "The installed gamevm is too old to save from here. Update it first (Home, Needs you)."
        text = set_keys(read(g.ETC_CONFIG), changes)
        run_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "gamevm-ui"
        run_dir.mkdir(mode=0o700, exist_ok=True)
        new = run_dir / "gamevm.toml.new"
        new.write_text(text)
        try:
            g.load_config(new)
        except g.GameVMError as e:
            return False, f"That would not be a valid configuration: {e}"

        def done(rc, lines):
            self.cfg_mtime = -1.0
            self.last_error = "" if rc == 0 else (
                "Saving was cancelled at the password prompt." if rc in (126, 127)
                else "Saving failed: " + " ".join(lines[-3:]))
            if rc == 0:
                self.run_doctor()

        ok, why = self.start_job("save", f"Saving {what}", ["pkexec", str(INSTALLED_CLI), "config-apply", str(new)], done)
        return ok, why or "Asking for your password ..."

    def act(self, name: str, a: dict) -> tuple[bool, str, dict]:
        g, cfg = self.g, self.cfg
        state = self.vm_state()
        ok, msg, data = True, "", {}
        if name == "start-preview":
            mode = self.mode()
            try:
                problems = [str(p) for p in g.start_problems(cfg, mode)]
            except Exception as e:  # noqa: BLE001
                problems = [str(e)]
            closing, reopen = [], []
            if mode == "igpu" and g.kwin_managed() and g.kwin_layout() == "native":
                try:
                    u = g.audio_user(cfg) or self.user()
                    closing = g.closing_apps(cfg, u)
                    reopen = sorted(set(g.wanted_apps(cfg, u)) & set(closing))
                except Exception:  # noqa: BLE001
                    pass
            keyboard = cfg["input"]["keyboard"]
            if cfg["input"]["evdev"] and keyboard and not os.path.exists(keyboard):
                problems.append("The keyboard that goes to Windows is not connected. Plug it in (or wake it), then start.")
            data = {"mode": mode, "problems": problems, "closing": [app_name(x) for x in closing],
                    "reopen": [app_name(x) for x in reopen], "confirm": self.prefs["confirm_start"]}
        elif name == "start":
            if "confirm" in a:
                self.prefs["confirm_start"] = bool(a["confirm"])
                self.save_prefs()
            self.last_error = ""

            def started(rc, lines):
                if rc != 0:
                    self.last_error = "Windows did not start. " + " ".join(x.strip() for x in lines[-4:] if x.strip())
            ok, msg = self.start_job("start", "Starting Windows", self.cli("start", "--yes"), started)
        elif name == "stop":
            self.last_error = ""

            def stopped(rc, lines):
                if rc != 0:
                    self.last_error = "Windows did not shut down cleanly. " + " ".join(x.strip() for x in lines[-3:] if x.strip())
                self.doctor_at = 0.0
            ok, msg = self.start_job("stop", "Shutting Windows down", self.cli("stop"), stopped)
        elif name == "kill":
            ok, msg = self.start_job("stop", "Switching Windows off", self.cli("kill", "--yes"))
        elif name == "lg":
            self.spawn(self.cli("lg"))
            msg = "Opening Windows in a window ..."
        elif name == "files":
            self.spawn(self.cli("files"))
        elif name == "send-files":
            ok, msg = self.send_files(list(a.get("paths") or []))
        elif name == "open-drive":
            url = str(a.get("url", ""))
            if url not in [d["url"] for d in self.sharing(state)["drives"]]:
                return False, "That drive is not there right now.", {}
            self.spawn(["xdg-open", url])
        elif name == "open-downloads":
            self.spawn(["xdg-open", str(self.linux_downloads())])
        elif name == "show-transfer":
            t = next((x for x in self.transfers if x["id"] == str(a.get("id", ""))), None)
            if not t or t["dir"] != "from" or not os.path.exists(t["where"]):
                return False, "That file is no longer where it arrived.", {}
            self.spawn(["dolphin", "--select", t["where"]] if shutil.which("dolphin") else ["xdg-open", os.path.dirname(t["where"])])
        elif name == "clear-transfers":
            with self.lock:
                self.transfers = [x for x in self.transfers if x["state"] == "sending"]
            self.save_transfers()
        elif name == "back":
            self.spawn(self.cli("back"))
        elif name == "pair":
            self.pair_failed, self.pair_tried = "", False
        elif name in ("usb-give", "usb-keep"):
            dev = str(a.get("device", ""))
            if not g.USB_ID_RE.match(dev):
                return False, "Unknown device.", {}
            if a.get("remember"):
                self.prefs["usb_rules"][dev] = "windows" if name == "usb-give" else "linux"
                self.save_prefs()
            self.usb_asks.pop(dev, None)
            if name == "usb-give":
                self.spawn(self.cli("usb", "attach", dev))
                msg = "Given to Windows."
        elif name == "dismiss-error":
            self.last_error = ""
        elif name == "run-checks":
            self.run_doctor()
        elif name == "save-resources":
            cores = [c["id"] for c in self.hw.get("cores", [])]
            win = sorted({int(c) for c in a.get("windowsCores", []) if int(c) in cores})
            host = [c for c in cores if c not in win]
            if not win or not host:
                return False, "Windows needs at least one core, and so does Linux.", {}
            present = {d["id"] for d in self.hw.get("usb", [])}
            usb = [d for d in cfg["usb"]["devices"] if d not in present] + sorted(
                str(d) for d in a.get("usbWindows", []) if g.USB_ID_RE.match(str(d)) and str(d) in present)
            if a.get("newUsb") in ("ask", "linux", "windows"):
                self.prefs["new_usb"] = a["newUsb"]
                self.save_prefs()
            mem = max(4, min(int(a.get("mem", 16)), max(4, self.hw.get("memTotal", 16) - 4)))
            ok, msg = self.save_config({
                ("cpu", "guest_cores"): win, ("cpu", "host_cores"): host, ("cpu", "isolate_host"): bool(a.get("isolate")),
                ("vm", "memory_mib"): mem * 1024, ("vm", "hugepages"): bool(a.get("hugepages")),
                ("looking_glass", "enabled"): bool(a.get("lookingGlass")), ("usb", "devices"): usb,
                ("network", "mode"): "nat" if a.get("network") == "nat" else "macvtap",
                ("network", "hostonly"): bool(a.get("privateLink"))}, "what Windows gets")
        elif name == "save-settings":
            self.prefs["confirm_start"] = bool(a.get("confirmStart", True))
            self.save_prefs()
            toggle = a.get("toggle") if a.get("toggle") in TOGGLE_WORDS else cfg["input"]["toggle"]
            ok, msg = self.save_config({
                ("desktop", "relaunch_all"): bool(a.get("reopenApps")), ("desktop", "restore_windows"): bool(a.get("restoreWindows")),
                ("desktop", "return"): "ask" if a.get("returnAsk") else "auto",
                ("vm", "host_shutdown_wait"): max(30, min(int(a.get("shutdownWait", 300)), 840)),
                ("looking_glass", "fullscreen"): bool(a.get("lgFullscreen")), ("input", "reattach"): bool(a.get("reattach")),
                ("input", "toggle"): toggle}, "the settings")
        elif name == "open-driver-page":
            self.spawn(["xdg-open", DRIVER_DISC_URL])
        elif name in ("install", "install-lg", "uninstall"):
            repo = self.repo()
            if not repo:
                return False, "The project folder with the installer was not found.", {}
            script = {"install": "setup/install-host.sh", "install-lg": "looking-glass/install.sh",
                      "uninstall": "setup/uninstall-host.sh"}[name]
            ok, msg = self.in_terminal(f"sudo {shlex.quote(str(repo / script))}")
            self.doctor_at = 0.0
        elif name == "restart":
            if state != "off":
                return False, "Shut Windows down first.", {}
            AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTOSTART_FILE.write_text("[Desktop Entry]\nType=Application\nName=gamevm setup\n"
                                      "Comment=Carries on with the gamevm setup after the restart\nExec=gamevm ui --page setup\n")
            self.spawn(["systemctl", "reboot"])
            msg = "Restarting ..."
        elif name == "winprep-done":
            self.prefs["winprep_done"] = True
            self.save_prefs()
        elif name == "recover":
            ok, msg = self.start_job("recover", "Repairing after a crash", ["pkexec", str(INSTALLED_CLI), "recover"])
        elif name == "open-config":
            self.spawn(["xdg-open", str(g.ETC_CONFIG)])
        elif name == "open-log":
            self.spawn(["xdg-open", str(g.LOG_FILE)])
        elif name in ("open-doc", "open-requirements"):
            repo = self.repo()
            doc = "01-requirements-and-cabling.md" if name == "open-requirements" else re.sub(r"[^A-Za-z0-9._-]", "", str(a.get("name", "")))
            if repo and (repo / "docs" / doc).is_file():
                self.spawn(["xdg-open", str(repo / "docs" / doc)])
            else:
                ok, msg = False, "The guide was not found."
        elif name in ("goto-setup", "goto-help", "goto-home"):
            data = {"page": name.removeprefix("goto-")}
        else:
            return False, f"unknown action: {name}", {}
        self.build()
        return ok, msg, data


def handler_for(svc: Service, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def allowed(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost") and secrets.compare_digest(self.headers.get("X-Gamevm-Token") or "", token)

        def reply(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if not self.allowed():
                return self.reply(403, {"error": "forbidden"})
            if self.path.split("?")[0] == "/state":
                return self.reply(200, svc.snap)
            self.reply(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self.allowed():
                return self.reply(403, {"error": "forbidden"})
            try:
                size = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(min(size, 1 << 20)) or b"{}")
                ok, msg, data = svc.act(str(req.get("id", "")), req.get("args") or {})
            except Exception as e:  # noqa: BLE001 - the window gets the reason instead of a hang
                ok, msg, data = False, f"{type(e).__name__}: {e}", {}
            self.reply(200, {"ok": ok, "message": msg, "data": data})

    return Handler


def main(args, g) -> int:
    qml = shutil.which("qml6") or shutil.which("qml") or ("/usr/lib/qt6/bin/qml" if os.path.exists("/usr/lib/qt6/bin/qml") else "")
    if not qml:
        raise g.GameVMError("Qt's QML runtime is missing (the qml6 program, package qt6-declarative)")
    if not (UI_DIR / "Main.qml").is_file():
        raise g.GameVMError(f"{UI_DIR} is missing (re-run setup/install-host.sh)")
    svc = Service(g, args.config, demo=os.environ.get("GAMEVM_UI_DEMO", ""))
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(svc, token))
    server.daemon_threads = True
    run_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "gamevm-ui"
    run_dir.mkdir(mode=0o700, exist_ok=True)
    session = run_dir / f"session-{os.getpid()}.json"
    fd = os.open(session, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": server.server_address[1], "token": token}, f)
    svc.build()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=svc.loop, daemon=True).start()
    threading.Thread(target=svc.watch_received, daemon=True).start()
    env = dict(os.environ, QML_XHR_ALLOW_FILE_READ="1", QT_QUICK_CONTROLS_STYLE="Basic")
    try:
        window = ["--link", str(session), "--page", args.page]  # (--session is taken: Qt reads it itself)
        for name in ("shot", "size", "delay", "open"):
            if getattr(args, name, None):
                window += [f"--{name}", getattr(args, name)]
        return subprocess.call([qml, str(UI_DIR / "Main.qml"), "--", *window], env=env)
    except KeyboardInterrupt:
        return 0
    finally:
        svc.stop_event.set()
        for helper in svc.monitors:
            if helper.poll() is None:
                helper.terminate()
        server.shutdown()
        session.unlink(missing_ok=True)
