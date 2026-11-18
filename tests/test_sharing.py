# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""The sharing half of the window's service: sending, watching files arrive, received files, drives,
the Send to Linux entry. Windows, KDE Connect and the file manager are faked. The download of the
driver disc is only tried with GAMEVM_TEST_NETWORK=1 (it fetches 64 KiB from the real server)."""
import json
import os
import pathlib
import tempfile
import threading
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO, check, fake_tools, finish, g, sample_config_text, ui  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp())
bindir = tmp / "bin"
fake_tools(bindir, {
    "kdeconnect-cli": f'echo "$@" >> "{tmp}/cli.log"\ncase "$*" in *refuse*) exit 1;; esac\nexit 0\n',
    "dbus-monitor": """cat <<'EOF'
   string ":1.99"
signal time=1.0 sender=:1.5 -> destination=(null destination) serial=7 path=/modules/kdeconnect/devices/WINDEV/share; interface=org.kde.kdeconnect.device.share; member=shareReceived
   string "file://%s/from%%20windows.bin"
signal time=2.0 sender=:1.5 -> destination=(null destination) serial=8 path=/modules/kdeconnect/devices/PHONE/share; interface=org.kde.kdeconnect.device.share; member=shareReceived
   string "file://%s/from-phone.jpg"
EOF
sleep 30
""" % (tmp, tmp),
    "xdg-open": f'echo "xdg-open $@" >> "{tmp}/open.log"\n',
    "dolphin": f'echo "dolphin $@" >> "{tmp}/open.log"\n',
})
(tmp / "from windows.bin").write_bytes(b"x" * 2048)

svc = ui.Service(g, demo="off"); svc.load_cfg(); svc.hw = svc.inventory()
svc.demo = ""; svc.load_cfg = lambda: None   # a made-up PC, but the real code paths from here on
svc.save_prefs = lambda: None; svc.transfers = []; svc.prefs["win_tool"] = ""
svc.build = lambda: None
state = {"vm": "running"}
svc.vm_state = lambda: state["vm"]
real = (g.kde_windows_device, g.guest_powershell, g.guest_write, g.guest_user_script)
g.kde_windows_device = lambda u: ("WINDEV", "WINPC")
svc.paired = True; svc.win.update(dev="WINDEV", name="WINPC", downloads="C:\\Users\\Me\\Downloads", shares=["C", "D"], info=True)

print("sending to Windows")
a = tmp / "report final.pdf"; a.write_bytes(b"p" * 5000)
folder = tmp / "a folder"; folder.mkdir()
arrived = {"size": 0}
def fake_ps(cfg, script, timeout=60):
    fake_ps.scripts.append(script)
    return 0, f"{arrived['size']}|C:\\Users\\Me\\Downloads\\report final.pdf\n" if arrived["size"] else "", ""
fake_ps.scripts = []
g.guest_powershell = fake_ps
ok, msg = svc.send_files([f"file://{str(a).replace(' ', '%20')}", str(folder), str(tmp / "missing.txt"), str(tmp / "refuse-me")])
t = svc.transfers[-1] if svc.transfers else {}
check("a file is handed to KDE Connect for the Windows device", ok and "-d WINDEV --share " + str(a) in (tmp / "cli.log").read_text(), msg)
check("folders and missing files are named, not sent", "a folder is a folder" in msg and "missing.txt was not found" in msg, msg)
check("it is listed as sending, with its size", t.get("state") == "sending" and t.get("size") == 5000 and t.get("dir") == "to", t)
time.sleep(2.5); arrived["size"] = 2500; time.sleep(2.5)
check("half way: the list shows how far it is", t["state"] == "sending" and abs(t["progress"] - 0.5) < 0.01, t)
arrived["size"] = 5000; time.sleep(3)
check("arrived: with where it is in Windows", t["state"] == "arrived" and t["where"].endswith("report final.pdf"), t)
check("the question to Windows quotes the folder and the name's stem", "-LiteralPath 'C:\\Users\\Me\\Downloads'" in fake_ps.scripts[-1] and "StartsWith('report final')" in fake_ps.scripts[-1])
state["vm"] = "off"
ok, msg = svc.send_files([str(a)]); check("Windows off: nothing is sent, the drives are pointed to", not ok and "drives" in msg, msg)
state["vm"] = "running"; svc.paired = False
ok, msg = svc.send_files([str(a)]); check("not linked yet: says it links by itself", not ok and "by itself" in msg, msg)
svc.paired = True

print("received from Windows")
threading.Thread(target=svc.watch_received, daemon=True).start(); time.sleep(1.5)
got = [x for x in svc.transfers if x["dir"] == "from"]
check("a file from the Windows device is listed, name decoded, with its size", len(got) == 1 and got[0]["name"] == "from windows.bin" and got[0]["size"] == 2048, got)
check("a file from another device (a phone) is not this page's business", not any("phone" in x["name"] for x in svc.transfers))
ok, msg, _ = svc.act("show-transfer", {"id": got[0]["id"]}); time.sleep(0.5)
check("Show opens the folder with the file selected", ok and "dolphin --select" in (tmp / "open.log").read_text())

print("drives and the list")
sh = svc.sharing("running")
check("running: the shared drives, by letter, over the private link", [d["url"] for d in sh["drives"]] == ["smb://10.77.0.2/C/", "smb://10.77.0.2/D/"] and sh["canSend"])
ok, msg, _ = svc.act("open-drive", {"url": "smb://10.77.0.2/C/"}); check("a listed drive opens", ok)
ok, msg, _ = svc.act("open-drive", {"url": "smb://evil.example/x"}); check("an address that is not a listed drive is refused", not ok)
off = svc.sharing("off"); check("off: Windows' drives are Linux folders", all(d["url"].startswith("/") for d in off["drives"]) and not off["canSend"], off["drives"])
svc.act("clear-transfers", {}); check("clearing empties the list", svc.transfers == [])

print("Windows' side: the Send to Linux entry")
wrote, ran = [], []
g.guest_write = lambda cfg, path, data: wrote.append((path, len(data)))
g.guest_user_script = lambda cfg, body, timeout=45: (ran.append(body), "installed: Send to > Linux (gamevm), Start menu > Send to Linux")[1]
ok, said = g.install_windows_share(svc.cfg)
check("the script is copied to C:\\gamevm and run with -Install in the user's session", ok and wrote and wrote[0][0] == "C:\\gamevm\\send-to-linux.ps1" and "-Install" in ran[0], (wrote, ran))
g.guest_user_script = lambda cfg, body, timeout=45: None
check("nobody signed in: reported, not claimed", g.install_windows_share(svc.cfg)[0] is False)
g.install_windows_share_real = g.install_windows_share
calls = []
g.install_windows_share = lambda cfg: (calls.append(1), (True, "installed"))[1]
svc.reset_win(); svc.ensure_windows_tool(); time.sleep(0.5); svc.ensure_windows_tool()
check("the window adds it once per Windows run", len(calls) == 1 and svc.prefs["win_tool"] != "")
svc.reset_win(); svc.ensure_windows_tool(); time.sleep(0.3)
check("...and not again while the script is unchanged", len(calls) == 1)
g.install_windows_share = g.install_windows_share_real

svc2 = None
if os.environ.get("GAMEVM_TEST_NETWORK") == "1":
    print("the driver disc is fetched by itself (first 64 KiB here)")
    home = tmp / "home"; home.mkdir(); os.environ["HOME"] = str(home)
    svc2 = ui.Service(g, demo="off"); svc2.load_cfg(); svc2.demo = ""; svc2.save_prefs = lambda: None; svc2.build = lambda: None
    svc2.cfg = json.loads(json.dumps(svc2.cfg)); svc2.cfg["vm"]["virtio_iso"] = str(tmp / "nope.iso")
    svc2.load_cfg = lambda: None
    os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]  # the real curl and tools again
    svc2.iso_range = "0-65535"
    svc2.ensure_driver_disc()
    for _ in range(60):
        if svc2.iso["state"] in ("done", "failed"): break
        time.sleep(1)
    iso = home / "Downloads" / "virtio-win.iso"
    check("downloaded over https into Downloads and recognised as a disc image", svc2.iso["state"] == "done" and iso.stat().st_size == 65536, svc2.iso)
    check("the total size was learnt for the progress figure", svc2.iso["total"] > 800_000_000, svc2.iso)
    steps = {s["id"]: s for s in svc2.setup_steps("igpu")}
    check("the setup step is gamevm's and is done", steps["iso"]["by"] == "gamevm" and steps["iso"]["done"])
else:
    print("the driver disc download: skipped (set GAMEVM_TEST_NETWORK=1 to try it)")
g.kde_windows_device, g.guest_powershell, g.guest_write, g.guest_user_script = real
for p in svc.monitors + (svc2.monitors if svc2 else []):
    if p.poll() is None: p.terminate()
finish()
