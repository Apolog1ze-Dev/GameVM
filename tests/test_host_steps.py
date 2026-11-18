# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""Host-side steps with every system call replaced: the sound hand-off, stopping Windows, shutting
the PC down with Windows running, and the doctor's check of the installed copy.

    python3 tests/test_host_steps.py <repo> mock     what tests/run.sh runs
    ... <repo> ns     the REAL sound hand-off as fake root in a private user and mount namespace
                      (needs unshare and a running PipeWire; nothing outside the namespace changes)
"""
import argparse
import pwd
import tempfile
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

REPO, PART = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(REPO / 'tests'))
import common
SAMPLE_CFG = common.sample_config_file()


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


g = load("gamevm", "gamevm/gamevm.py")
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("  ok    " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))
    fails += 0 if cond else 1


class St:  # journal stand-in
    def __init__(self):
        self.data = {"steps": []}

    def push(self, kind, **info):
        self.data["steps"].append({"kind": kind, **info})

    def clear(self):
        pass


cfg = g.load_config(SAMPLE_CFG)
logs = []
g.log = lambda msg, level="info": logs.append((level, msg))

if PART == "ns":
    # fake root in a private namespace; /mnt is a tmpfs only this namespace sees
    assert os.geteuid() == 0
    g.PIPEWIRE_DIR = Path("/mnt/gamevm/pipewire")
    st = St()
    g.step_audio_access(cfg, st)
    target = "/mnt/gamevm/pipewire/pipewire-0"
    check("real code: socket bind-mounted and journaled", st.data["steps"] == [{"kind": "pipewire_bound", "target": target}]
          and g.real_mount_at(target) and Path(target).is_socket(), str((st.data, logs)))
    env = {k: v for k, v in os.environ.items() if k not in ("XDG_RUNTIME_DIR", "PIPEWIRE_REMOTE")}
    env["PIPEWIRE_RUNTIME_DIR"] = str(g.PIPEWIRE_DIR)          # what libvirt gives QEMU
    out = subprocess.run(["pw-cli", "info", "0"], env=env, text=True, capture_output=True).stdout
    check("a PipeWire client given only that directory reaches the server", "core.name" in out, out[:200])
    check("the session directory keeps mode 0700 (no ACL)", oct(os.stat(f"/run/user/{os.getuid()}").st_mode & 0o777) == "0o700")
    st2 = St()
    g.step_audio_access(cfg, st2)                               # as after a crash: the old mount is still there
    n = sum(1 for line in open("/proc/self/mountinfo") if f" {target} " in line)
    check("run again over a leftover mount: replaced, not stacked", n == 1 and len(st2.data["steps"]) == 1, f"{n} mounts")
    g.undo(cfg, st2)
    check("undo: unmounted", not g.real_mount_at(target), str(logs))
    logs.clear()
    g.DOMAIN_XML = "<domain><devices><audio id='1' type='pipewire' runtimeDir='/run/user/1000'/></devices></domain>"
    st3 = St()
    g.step_audio_access(cfg, st3)
    check("VM definition from the older gamevm: nothing mounted, and the log says how to fix it",
          st3.data["steps"] == [] and not g.real_mount_at(target) and any("gamevm define" in m for _, m in logs), str(logs))
    g.DOMAIN_XML = ""
    g.qemu_process_user = lambda: pwd.getpwuid(os.getuid()).pw_name
    st4 = St()
    g.step_audio_access(cfg, st4)
    check("QEMU running as the desktop user: nothing to do", st4.data["steps"] == [] and not g.real_mount_at(target))

else:
    d = load("gamevm_doctor", "gamevm/gamevm_doctor.py")
    h = load("setup_helper", "setup/setup_helper.py")
    scratch = Path(tempfile.mkdtemp(prefix="gamevm-test-"))

    print("prepare(): order of the steps")
    order = []
    for name in ("verify_identity", "step_hugepages", "step_inhibit_sleep", "step_disk", "step_gpu_services",
                 "step_desktop_to_igpu", "step_console", "step_unload_driver", "step_audio_access",
                 "step_hugepages_finish", "step_isolate", "step_tuning", "step_input_watch"):
        setattr(g, name, (lambda n: lambda *a, **k: order.append(n))(name))
    g.start_problems = lambda *a, **k: []
    g.STATE_DIR = scratch / "run"
    g.STATE_FILE = g.STATE_DIR / "state.json"
    g.prepare(cfg, "igpu")
    check("huge pages right after the checks, before sleep block and disks; sound after the driver unload",
          order == ["verify_identity", "step_hugepages", "step_inhibit_sleep", "step_disk", "step_gpu_services",
                    "step_desktop_to_igpu", "step_console", "step_unload_driver", "step_audio_access",
                    "step_hugepages_finish", "step_isolate", "step_tuning", "step_input_watch"], str(order))
    order.clear()
    undone = []
    g.undo = lambda cfg, st: undone.append([s["kind"] for s in st.data["steps"]])
    g.notify_user = lambda *a, **k: None

    def short(cfg, st):
        st.push("hugepages", previous=0, target=8192)
        raise g.GameVMError(f"only 7495 of 8192 hugepages available (RAM too fragmented) {g.HUGEPAGES_SHORT}")
    g.step_hugepages = short
    try:
        g.prepare(cfg, "igpu")
    except g.GameVMError:
        pass
    check("huge pages short: nothing but the reservation is rolled back (no disk, no sleep block touched)",
          order == ["verify_identity"] and undone == [["hugepages"]], str((order, undone)))

    print("hibernation check without ntfs-3g.probe")
    g2 = load("gamevm2", "gamevm/gamevm.py")
    g2.log = lambda *a, **k: None
    g2.holders_of_block = lambda devs: []
    g2.mounts_of = lambda devs: []
    g2.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
    g2.os.open, g2.os.close = (lambda *a, **k: 3), (lambda fd: None)
    fstype = {"/dev/x1": "ntfs", "/dev/x2": "vfat"}
    g2.run = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, fstype.get(cmd[-1], "") + "\n", "")
    g2.shutil = types.SimpleNamespace(which=lambda name: None)
    try:
        g2.release_block_devices(cfg, St(), "the test disk", ["x1", "x2"])
        check("NTFS partition + tool missing: refused", False)
    except g2.GameVMError as e:
        check("NTFS partition + tool missing: refused, with the package to install", "ntfs-3g.probe is not installed" in str(e), str(e))
    fstype["/dev/x1"] = "ext4"
    try:
        g2.release_block_devices(cfg, St(), "the test disk", ["x1", "x2"])
        check("no NTFS partition: the missing tool does not matter", True)
    except g2.GameVMError as e:
        check("no NTFS partition: the missing tool does not matter", False, str(e))

    print("gamevm recover and the reboot marker")
    g3 = load("gamevm3", "gamevm/gamevm.py")
    g3.REBOOT_MARKER = scratch / "run" / "gpu-needs-reboot"
    g3.REBOOT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    g3.require_root = lambda what: None
    g3.HostState = types.SimpleNamespace(load=lambda: None)
    g3.load_config = lambda *a, **k: cfg
    g3.domain_state = lambda name: "shut off"
    g3.run = lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", "")
    g3.write_sysfs = lambda *a, **k: None
    g3.WORKQUEUE_MASK = scratch / "nope"
    g3.pci_driver = lambda bdf: "nvidia"
    g3.unit_active = lambda unit: True
    g3.kwin_managed = lambda: False
    real_glob = g3.glob.glob
    g3.glob = types.SimpleNamespace(glob=lambda pattern: [] if "vtconsole" in pattern else real_glob(pattern))
    g3.print = lambda *a, **k: None
    said = []
    g3.log = lambda msg, level="info": said.append((level, msg))
    ns = argparse.Namespace(force=False)
    g3.REBOOT_MARKER.touch()
    g3.gpu_reinit_ok = lambda cfg, since: False
    g3.cmd_recover(ns)
    check("GPU still dead: marker stays, says reboot", g3.REBOOT_MARKER.exists() and said[-1][0] == "error", str(said))
    g3.gpu_reinit_ok = lambda cfg, since: True
    g3.cmd_recover(ns)
    check("GPU answers again: marker cleared", not g3.REBOOT_MARKER.exists() and "without a reboot" in said[-1][1], str(said))

    print("doctor: installed files vs the checkout")
    d.INSTALL_DIR = scratch / "inst"
    (d.INSTALL_DIR / "windows").mkdir(parents=True, exist_ok=True)
    fake = types.SimpleNamespace(__file__=str(d.INSTALL_DIR / "gamevm.py"), read=g.read)
    d.INSTALLED_FROM = {"gamevm/gamevm.py": d.INSTALL_DIR / "gamevm.py", "gamevm/gamevm_doctor.py": d.INSTALL_DIR / "gamevm_doctor.py"}
    (d.INSTALL_DIR / "gamevm.py").write_bytes((REPO / "gamevm/gamevm.py").read_bytes())
    (d.INSTALL_DIR / "gamevm_doctor.py").write_bytes(b"older\n")
    # the window's files are compared too (gamevm_ui.py, ui/*): installed and current here
    (d.INSTALL_DIR / "ui").mkdir(exist_ok=True)
    (d.INSTALL_DIR / "gamevm_ui.py").write_bytes((REPO / "gamevm/gamevm_ui.py").read_bytes())
    for page in (REPO / "gamevm/ui").glob("*"):
        (d.INSTALL_DIR / "ui" / page.name).write_bytes(page.read_bytes())
    check("installed doctor, no SOURCE recorded: cannot tell, says nothing", d.stale_files(fake) == (None, []))
    (d.INSTALL_DIR / "SOURCE").write_text(str(REPO) + "\n")
    check("installed doctor with SOURCE: names the file that differs", d.stale_files(fake) == (REPO, ["gamevm/gamevm_doctor.py"]), str(d.stale_files(fake)))
    (d.INSTALL_DIR / "gamevm_doctor.py").write_bytes((REPO / "gamevm/gamevm_doctor.py").read_bytes())
    check("...and nothing once they match", d.stale_files(fake) == (REPO, []))
    (d.INSTALL_DIR / "ui" / "Main.qml").write_bytes(b"older\n")
    check("an outdated page of the window is named too", d.stale_files(fake) == (REPO, ["gamevm/ui/Main.qml"]), str(d.stale_files(fake)))
    (d.INSTALL_DIR / "SOURCE").write_text("/nonexistent/checkout\n")
    check("checkout moved away: says nothing", d.stale_files(fake) == (None, []))

    print("setup_helper: fstab conversion keeps one device timeout")
    line = "UUID=X /mnt/t ntfs-3g defaults,nofail,x-systemd.device-timeout=30 0 0\n"
    f = line.split()
    opts = [o for o in f[3].split(",") if o not in ("auto", "noauto") and not o.startswith(
        ("x-systemd.automount", "x-systemd.idle-timeout", "x-systemd.device-timeout"))]
    src = (REPO / "setup/setup_helper.py").read_text()
    check("the filter in the helper drops an existing device timeout", '"x-systemd.device-timeout"))]' in src and opts == ["defaults", "nofail"], str(opts))

print(f"\n{fails} failure(s)")
sys.exit(1 if fails else 0)
