#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
# gamevm's tests. They need no VM, no root and change nothing on the machine: Windows, libvirt,
# KDE Connect and the file manager are faked, and the configuration is a made-up PC.
#   GAMEVM_TEST_NETWORK=1 ./tests/run.sh   also fetches 64 KiB of the driver disc from the real server
#   python3 -I tests/test_sata_boot.py firmware   also starts the VM firmware from a made-up SATA disk
set -u
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
failed=0
run() {
    echo "== $*"
    if ! python3 -I "$@" > /tmp/gamevm-test.$$ 2>&1; then
        failed=1
        cat /tmp/gamevm-test.$$
    else
        tail -n 1 /tmp/gamevm-test.$$
    fi
    rm -f /tmp/gamevm-test.$$
}
run tests/test_discover.py
run tests/test_sata_boot.py
run tests/test_window.py
run tests/test_sharing.py
run tests/test_host_steps.py "$PWD" mock
run tests/test_apps.py "$PWD"
bash -n setup/install-host.sh setup/uninstall-host.sh looking-glass/install.sh && echo "== shell scripts parse" || failed=1
python3 -m py_compile gamevm/*.py setup/setup_helper.py && echo "== python files compile" || failed=1
find . -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
exit $failed
