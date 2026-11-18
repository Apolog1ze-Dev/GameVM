# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""The gamevm window's service (gamevm_ui.py): configuration editing, checks, USB and pairing logic,
and root's half of Save. Nothing on the machine is changed; Windows and KDE Connect are faked."""
import argparse
import os
import pathlib
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO, check, fake_tools, finish, g, sample_config_text, ui  # noqa: E402

print("configuration as text")
t = "# c\n[a]\n# about x\nx = 1   # one\ny = [1,\n     2]\nz = \"q # not a comment\"\n\n[b]\nk = true\n"
out = ui.set_keys(t, {("a", "x"): 5, ("a", "y"): [3], ("a", "z"): "new", ("b", "k"): False, ("b", "n"): ["u"], ("c", "m"): 2})
check("a value changes, its comment stays", "x = 5   # one" in out)
check("an array over several lines becomes one line", "y = [3]\nz" in out)
check("a '#' inside a string is not taken for a comment", 'z = "new"\n' in out)
check("a missing key is added under its section", '[b]\nn = ["u"]\nk = false' in out)
check("a missing section is added", out.rstrip().endswith("[c]\nm = 2"))
import tomllib
check("the result parses to the wanted values", tomllib.loads(out) == {"a": {"x": 5, "y": [3], "z": "new"}, "b": {"n": ["u"], "k": False}, "c": {"m": 2}})
template = (REPO / "config" / "gamevm.toml").read_text()
live = sample_config_text()
same = ui.set_keys(template, {})
check("no change: the file is untouched", same == template)
try:
    g.load_config(REPO / "config" / "gamevm.toml")
    refused = False
except g.GameVMError as e:
    refused = "not filled in" in str(e)
check("the bare template is refused as a configuration, with the reason", refused)
filled = pathlib.Path(tempfile.mkdtemp()) / "gamevm.toml"; filled.write_text(live)
check("filled in for a PC it is valid", g.load_config(filled)["gpu"]["devices"] == ["0000:01:00.0", "0000:01:00.1"])

print("the doctor's report")
d = ui.parse_doctor("gamevm 1.0.0 doctor - config x\n\nPlatform\n  [ OK ] fine\n  [WARN] hmm\n         -> do this\n\nDisk\n  [FAIL] broken\n\n1 problem(s), 1 warning(s)\n")
check("sections, levels and hints", [s["title"] for s in d["sections"]] == ["Platform", "Disk"] and d["fails"] == 1 and d["warns"] == 1
      and d["sections"][0]["items"][1] == {"level": "warn", "msg": "hmm", "hint": "do this"}, d)

print("names")
check("graphics card name", ui.short_gpu_name("VGA compatible controller [0300]: NVIDIA Corporation AD103 [GeForce RTX 4080 SUPER] [10de:2702] (rev a1)") == "GeForce RTX 4080 SUPER")

print("the service with a made-up PC")
svc = ui.Service(g, demo="off"); svc.build(); s = svc.snap
check("a snapshot with every part", all(k in s for k in ("vm", "busy", "needs", "done", "sharing", "resources", "setup", "settings", "help")))
check("cores: Windows' and Linux' add up", len(s["resources"]["cores"]) == 8 and sum(c["windows"] for c in s["resources"]["cores"]) == 6)
fresh = ui.Service(g, demo="fresh"); fresh.build()
steps = {x["id"]: x for x in fresh.snap["setup"]["steps"]}
check("nothing installed: the install step is the current one, and Home says setup is left",
      steps["install"]["state"] == "current" and steps["look"]["done"] and any(n["id"] == "setup" for n in fresh.snap["needs"]))
check("revision only moves when something changed", (svc.build(), svc.rev)[1] == s["rev"])

print("while Windows runs: USB devices and the clipboard link (faked)")
svc = ui.Service(g, demo="off"); svc.build()
svc.windows_info = lambda: None
svc.prefs.update(new_usb="ask", usb_rules={"aaaa:0002": "windows"}, paired_once=False); svc.save_prefs = lambda: None
spawned = []; svc.spawn = lambda argv: spawned.append(argv[-3:])
devices = {"1111:0001": "Pad"}
svc.usb_now = lambda: dict(devices); svc.input_usb_ids = lambda: set()
g_kde = (g.kde_windows_device, g.kde_device)
g.kde_windows_device = lambda u: None
svc.watch_running()
check("devices present at the start are not asked about", svc.usb_asks == {} and spawned == [])
devices["2222:0003"] = "Stick"; devices["aaaa:0002"] = "Wheel"
svc.watch_running()
check("a new device with no rule is asked about", svc.usb_asks == {"2222:0003": "Stick"}, svc.usb_asks)
check("a new device with a remembered answer goes to Windows without asking", spawned == [["usb", "attach", "aaaa:0002"]], spawned)
needs = svc.needs("running", "igpu", [])
check("the question is on the Home page", any(n.get("device") == "2222:0003" and n["remember"] for n in needs))
ok, msg, _ = svc.act("usb-give", {"device": "2222:0003", "remember": True})
check("answering gives it to Windows and remembers", ok and spawned[-1] == ["usb", "attach", "2222:0003"] and svc.prefs["usb_rules"]["2222:0003"] == "windows" and not svc.usb_asks)
del devices["2222:0003"]; devices["3333:0004"] = "Cam"; svc.watch_running(); del devices["3333:0004"]; svc.watch_running()
check("an unplugged device's question goes away", svc.usb_asks == {})
jobs = []
svc.start_job = lambda kind, title, argv, after=None: (jobs.append(argv[-2:]), (True, ""))[1]
g.kde_windows_device = lambda u: ("dev", "WINPC"); g.kde_device = lambda u, dev, *a, **k: ["b", "false"]
svc.watch_running(); svc.watch_running()
check("an unpaired Windows is paired by itself, once per run", jobs == [["share", "--pair"]], jobs)
g.kde_device = lambda u, dev, *a, **k: ["b", "true"]; svc.watch_running()
check("paired: seen", svc.paired is True)
g.kde_windows_device, g.kde_device = g_kde

print("config-apply (root's half of Save), with /etc faked")
tmp = pathlib.Path(tempfile.mkdtemp())
etc = tmp / "gamevm.toml"; etc.write_text(live)
old = (g.ETC_CONFIG, g.require_root, g.log); g.ETC_CONFIG = etc; g.require_root = lambda _w: None; g.log = lambda *a, **k: None
os.environ["PKEXEC_UID"] = str(os.getuid())
new = tmp / "new.toml"; new.write_text(ui.set_keys(live, {("vm", "memory_mib"): 12288}))
rc = g.cmd_config_apply(argparse.Namespace(file=str(new)))
check("a valid file replaces the configuration and the old one is kept", rc == 0 and "memory_mib = 12288" in etc.read_text()
      and (tmp / "gamevm.toml.bak").read_text() == live)
bad = tmp / "bad.toml"; bad.write_text(live.replace("[vm]", "[vm]\nmemory_mib = \"lots\"", 1).replace("memory_mib = 16384", "", 1))
try:
    g.cmd_config_apply(argparse.Namespace(file=str(bad))); refused = False
except g.GameVMError:
    refused = True
check("an invalid file is refused and nothing changes", refused and "memory_mib = 12288" in etc.read_text() and not (tmp / ".gamevm.toml.new").exists())
link = tmp / "link.toml"; link.symlink_to("/etc/shadow")
try:
    g.cmd_config_apply(argparse.Namespace(file=str(link))); refused = False
except g.GameVMError:
    refused = True
check("a symlink (to a file root could read for the user) is refused", refused)
os.environ["PKEXEC_UID"] = "12345"
try:
    g.cmd_config_apply(argparse.Namespace(file=str(new))); refused = False
except g.GameVMError:
    refused = True
check("a file of another user is refused", refused)
g.ETC_CONFIG, g.require_root, g.log = old

finish()
