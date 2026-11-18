# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""Keeping apps across the desktop restart (which apps reopen, asking them to quit, putting windows
back, bringing the desktop back later), with every system call replaced: no signal is sent and
nothing on the host changes."""
import argparse
import tempfile
import importlib.util
import os
import pwd
import signal
import subprocess
import sys
import types
from pathlib import Path

REPO = Path(sys.argv[1])
sys.path.insert(0, str(REPO / 'tests'))
import common
SAMPLE_CFG = common.sample_config_file()
scratch = Path(tempfile.mkdtemp(prefix="gamevm-test-"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "gamevm" / "gamevm.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    m.STATE_DIR = scratch / "run"
    m.STATE_DIR.mkdir(exist_ok=True)
    m.STATE_FILE = m.STATE_DIR / "state.json"
    m.PENDING_FILE = m.STATE_DIR / "return-pending.json"
    m.REBOOT_MARKER = m.STATE_DIR / "gpu-needs-reboot"
    return m


fails = 0


def check(name, cond, detail=""):
    global fails
    print(("  ok    " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


g = load("gamevm")
cfg = g.load_config(SAMPLE_CFG)
u = pwd.getpwuid(os.getuid())

print("config")
check("a configuration without the newer keys gets their defaults", cfg["desktop"]["relaunch_all"] is True and cfg["desktop"]["return"] == "auto"
      and cfg["desktop"]["quit_timeout"] == 5 and cfg["desktop"]["restore_windows"] is True)
for key, bad in (("return", "later"), ("quit_timeout", 99)):
    try:
        g.validate_config(dict(cfg, desktop=dict(cfg["desktop"], **{key: bad})))
        check(f"desktop.{key} = {bad!r} rejected", False)
    except g.GameVMError:
        check(f"desktop.{key} = {bad!r} rejected", True)
tmpl = g.load_config(SAMPLE_CFG)
check("config template loads with the new keys", tmpl["desktop"]["relaunch_exclude"] == [] and tmpl["desktop"]["return"] == "auto")

print("which apps are reopened")
real_running, real_unit_app = g.running_apps, g.unit_app
g.running_apps = lambda u: {"org.example.Notes": "app-org.example.Notes-1.scope", "browser": "app-browser-2.scope",
                            "chat": "app-chat@autostart.service"}
g.unit_app = lambda unit: (unit.split("-", 1)[1].split("@")[0].rsplit("-", 1)[0] if "@" not in unit else unit[4:].split("@")[0],
                           "@autostart" in unit)
now = sorted(g.wanted_apps(cfg, u))
check("every open app is on the list, not only the listed ones", now == ["browser", "chat", "org.example.Notes"], str(now))
only = sorted(g.wanted_apps(dict(cfg, desktop=dict(cfg["desktop"], relaunch_all=False)), u))
check("relaunch_all = false: only apps started at login and those in desktop.relaunch", only == ["chat"], str(only))
listed = sorted(g.wanted_apps(dict(cfg, desktop=dict(cfg["desktop"], relaunch_all=False, relaunch=["browser"])), u))
check("...plus the ones listed in desktop.relaunch", listed == ["browser", "chat"], str(listed))
excl = sorted(g.wanted_apps(dict(cfg, desktop=dict(cfg["desktop"], relaunch_exclude=["browser"])), u))
check("relaunch_exclude is honoured", excl == ["chat", "org.example.Notes"], str(excl))
g.running_apps, g.unit_app = real_running, real_unit_app
check("Chromium browsers get --restore-last-session, others nothing",
      g.SESSION_RESTORE_ARGS.get("chromium") == ["--restore-last-session"] and "firefox" not in g.SESSION_RESTORE_ARGS)

print("quit_apps (signals are recorded, not sent)")
sent = []
g.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
g.os.kill = lambda pid, sig: sent.append((pid, sig))
logs = []
g.log = lambda msg, level="info": logs.append(msg)
g.wait_gone = lambda pids, seconds: sent.append(("wait", len(pids), seconds))
units = {"app-chat@autostart.service": {"app": "chat", "autostart": True, "pids": [100, 101]},
         "app-firefox@x.service": {"app": "firefox", "autostart": False, "pids": [200, 201]},
         "app-kitty-300.scope": {"app": "kitty", "autostart": False, "pids": [300, 301]},
         "app-org.kde.dolphin@y.service": {"app": "org.kde.dolphin", "autostart": False, "pids": [400]},
         "app-code-500.scope": {"app": "code", "autostart": False, "pids": [500, 501]},
         "app-gone@z.service": {"app": "gone", "autostart": False, "pids": [600]}}
g.app_units = lambda u: units
g.display_conns = lambda: set()
g.dies_with_kwin = lambda pid, conns: pid not in (400,)            # Dolphin is Qt: survives
g.proc_maps = lambda pid: b"/usr/lib/firefox/libxul.so" if pid in (200, 201) else b""
g.unit_main_pids = lambda u, us: {"app-chat@autostart.service": 100, "app-firefox@x.service": 200, "app-kitty-300.scope": 300,
                                  "app-code-500.scope": 500, "app-gone@z.service": 0}
g.read = lambda path, default="": {"/proc/300/comm": "kitty", "/proc/100/comm": "chat", "/proc/500/comm": "code"}.get(str(path), default)
g.proc_unit = lambda pid: "app-code-500.scope" if pid in (os.getpid(), os.getppid()) else ""   # gamevm started from VS Code's terminal
g.quit_apps(cfg, u)
check("only the main process of an app that will close is asked (SIGTERM)", [s for s in sent if s[0] != "wait"] == [(100, signal.SIGTERM)], str(sent))
check("not asked: Firefox family, a terminal, a Qt app, an app without main process, the app gamevm runs in",
      all(p not in [s[0] for s in sent] for p in (200, 300, 400, 500, 600)))
check("then waits desktop.quit_timeout for its processes", sent[-1] == ("wait", 2, 5), str(sent))
sent.clear()
g.quit_apps(dict(cfg, desktop=dict(cfg["desktop"], quit_timeout=0)), u)
check("quit_timeout = 0: nobody is asked", sent == [])

print("back to the main monitor: normal, and with desktop.return = \"ask\"")
h = load("gamevm_b")
calls = []
h.log = lambda msg, level="info": calls.append("log:" + msg[:40])
h.kwin_layout = lambda: "igpu"
h.wanted_apps = lambda cfg, u: {"chat": "app-chat@autostart.service"}
h.gpu_kernel_nodes = lambda cfg: ("/dev/dri/card1", "/dev/dri/renderD128")
h.gpu_reinit_ok = lambda cfg, since: True
h.doomed_procs = lambda u: {100, 101}
h.app_units = lambda u: {"app-chat@autostart.service": {"app": "chat", "autostart": True, "pids": [100, 101]}}
h.kwin_pids = lambda u: [1]
h.quit_apps = lambda cfg, u: calls.append("quit")
h.kwin_links = lambda cfg, native: calls.append(f"links native={native}")
h.kwin_restart = lambda cfg, u: calls.append("kwin restart")
h.wait_gone = lambda pids, s: None
h.relaunch_apps = lambda u, apps: calls.append("relaunch " + ",".join(apps))
h.restore_windows = lambda cfg, u, w: calls.append(f"restore {len(w or [])} windows")
h.capture_windows = lambda cfg, u: calls.append("capture") or [{"key": "now"}]
h.ask_return = lambda u, closing: calls.append("ask: " + ",".join(closing))
step = {"kind": "desktop_igpu", "user": u.pw_name, "reopened": True, "started": 0, "windows": [{"key": "a"}, {"key": "b"}]}
h.desktop_to_native(cfg, step, may_stay=True)
check("auto: quit, restart on the graphics card, reopen, put the 2 remembered windows back",
      [c for c in calls if not c.startswith("log:")] == ["quit", "links native=True", "kwin restart", "relaunch chat", "restore 2 windows"], str(calls))
calls.clear()
ask = dict(cfg, desktop=dict(cfg["desktop"], **{"return": "ask"}))
h.desktop_to_native(ask, step, may_stay=True)
check("ask: no restart, no app closed; the question is put and the window places are kept for later",
      [c for c in calls if not c.startswith("log:")] == ["ask: chat"] and h.pending_windows() == step["windows"], str(calls))
calls.clear()
h.desktop_to_native(ask, {"user": u.pw_name, "reopened": True, "started": 0})        # what `gamevm back` runs
check("gamevm back afterwards: restart, reopen, the kept places are used, then forgotten",
      [c for c in calls if not c.startswith("log:")] == ["quit", "links native=True", "kwin restart", "relaunch chat", "restore 2 windows"]
      and h.pending_windows() is None, str(calls))
calls.clear()
h.doomed_procs = lambda u: set()
h.desktop_to_native(ask, step, may_stay=True)
check("ask, but nothing would close (only KDE apps open): moves back at once", "kwin restart" in calls and not any(c.startswith("ask") for c in calls), str(calls))
calls.clear()
h.doomed_procs = lambda u: {100}
h.desktop_to_native(ask, dict(step, reopened=False), may_stay=False)
check("ask does not apply to a failed start: the rollback always moves back", "kwin restart" in calls, str(calls))
calls.clear()
h.desktop_to_native(ask, {"user": u.pw_name, "reopened": True, "started": 0})
check("manual move without kept places: the current ones are read first", calls[0] == "capture" or "capture" in calls, str(calls))

print("gamevm stop's last line and gamevm back")
out = []
h.print = lambda *a, **k: out.append(" ".join(map(str, a)))
h.gpu_dedicated = lambda: False
h.pci_driver = lambda bdf: "nvidia"
h.save_pending([{"key": "a"}])
check("stop in ask mode: says Linux stays on the side monitor and how to get back", h.wait_gpu_back(cfg) is True and "gamevm back" in out[-1], str(out))
h.PENDING_FILE.unlink()
check("stop when the card did not come back: still reported as a failure", h.wait_gpu_back(cfg) is False and "did not come back healthy" in out[-1], str(out))
h.kwin_layout = lambda: "native"
check("back when Linux already has the main monitor: nothing to do", h.cmd_back(argparse.Namespace(config=str(SAMPLE_CFG))) == 0 and "already" in out[-1])
h.kwin_layout = lambda: "igpu"
h.domain_state = lambda name: "running"
try:
    h.cmd_back(argparse.Namespace(config=str(SAMPLE_CFG)))
    check("back while Windows runs: refused", False)
except h.GameVMError as e:
    check("back while Windows runs: refused", "Windows is running" in str(e))
h.domain_state = lambda name: "shut off"
h.DRY_RUN = True
check("back (dry-run): shows the command instead of running pkexec", h.cmd_back(argparse.Namespace(config=str(SAMPLE_CFG))) == 0 and "desktop native" in out[-1])

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
