# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
"""Shared by the tests: load gamevm from this checkout, a filled-in sample configuration, check()."""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO / "gamevm"))

import gamevm as g  # noqa: E402
import gamevm_ui as ui  # noqa: E402

fails = 0


def check(label, cond, extra=""):
    global fails
    fails += not cond
    print(("  ok    " if cond else "  FAIL  ") + label + (f"  [{extra}]" if extra and not cond else ""))


def finish():
    print(f"\n{fails} failure(s)")
    sys.exit(1 if fails else 0)


# The template as the installer would fill it in for a made-up PC (the template itself has no
# devices and is not a valid configuration on purpose).
SAMPLE = {
    ("gpu", "devices"): ["0000:01:00.0", "0000:01:00.1"], ("gpu", "expect_ids"): ["10de:2786", "10de:22bc"],
    ("disk", "nvme"): "0000:02:00.0", ("disk", "unmount"): ["/mnt/windows"],
    ("input", "keyboard"): "/dev/input/by-id/usb-Sample_Keyboard-event-kbd",
    ("input", "mouse"): "/dev/input/by-id/usb-Sample_Mouse-event-mouse",
    ("cpu", "guest_cores"): [1, 2, 3, 4, 5, 6], ("cpu", "host_cores"): [0, 7], ("vm", "memory_mib"): 16384,
    ("network", "host_nic"): "eth0", ("audio", "user"): "someone",
}


def sample_config_text() -> str:
    return ui.set_keys((REPO / "config" / "gamevm.toml").read_text(), SAMPLE)


def sample_config_file() -> Path:
    path = Path(tempfile.mkdtemp(prefix="gamevm-test-")) / "gamevm.toml"
    path.write_text(sample_config_text())
    return path


def fake_tools(folder: Path, tools: dict) -> None:
    """Put shell scripts named like real programs first in PATH."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in tools.items():
        f = folder / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(0o755)
    os.environ["PATH"] = f"{folder}:{os.environ['PATH']}"
