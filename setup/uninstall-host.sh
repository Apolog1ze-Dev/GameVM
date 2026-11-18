#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
# Undo setup/install-host.sh (and looking-glass/install.sh with --looking-glass).
#
#   sudo ./setup/uninstall-host.sh [--purge] [--looking-glass] [--dry-run]
#
#   default          remove gamevm's hook, programs, udev rules, KWin GPU layout unit, shutdown unit, initramfs
#                    drop-in, qemu.conf block, fstab change, host-only network and ufw rules.
#                    Keeps: installed packages, /etc/gamevm (config + SMBIOS), the VM definition,
#                    its NVRAM and TPM state.
#   --purge          also undefine the VM (deleting its NVRAM and TPM state) and remove /etc/gamevm
#   --looking-glass  also remove the Looking Glass client and the kvmfr module
# The Windows install on the NVMe is never touched.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY=0 PURGE=0 LG=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;; --purge) PURGE=1 ;; --looking-glass) LG=1 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
export LC_ALL=C  # virsh output is parsed below
run()  { if ((DRY)); then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
step() { printf '\n\e[1;36m==> %s\e[0m\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
VIRSH=(virsh -q -c qemu:///system)

NAME=win11
[[ -f /etc/gamevm/gamevm.toml ]] && NAME="$(python3 -c 'import tomllib;print(tomllib.load(open("/etc/gamevm/gamevm.toml","rb"))["vm"]["name"])')"
STATE="$("${VIRSH[@]}" domstate "$NAME" 2>/dev/null || true)"
if [[ -n $STATE && $STATE != "shut off" ]]; then
    echo "The VM $NAME is $STATE: shut it down first (gamevm stop)." >&2
    exit 1
fi
if command -v snapper >/dev/null && snapper list-configs 2>/dev/null | grep -q '^root '; then
    run snapper -c root create --type single --description "gamevm: before uninstall-host" --userdata important=yes
fi

step "Host shutdown unit, libvirt hook and gamevm programs"
# First, while the program it calls still exists: stopping the unit runs `gamevm host-shutdown`,
# which only acts while the host itself is shutting down.
run systemctl disable --now gamevm-shutdown.service 2>/dev/null || true
run rm -f /etc/systemd/system/gamevm-shutdown.service
run rm -f /etc/libvirt/hooks/qemu.d/gamevm /usr/local/bin/gamevm /usr/local/bin/gpu-run
run rm -rf /usr/local/lib/gamevm
if [[ -n ${SUDO_USER:-} ]]; then
    UH="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    run rm -f "$UH/.local/share/applications/gamevm-start.desktop" "$UH/.local/share/applications/gamevm-lg.desktop"
    # the window's own files: its "carry on after the restart" entry and what it remembers (USB answers)
    run rm -f "$UH/.config/autostart/gamevm-ui-setup.desktop" "$UH/.config/gamevm/ui.json"
fi
run systemctl try-restart virtqemud.service

step "KWin GPU layout, udev rules, initramfs"
run systemctl disable gamevm-kwin.service 2>/dev/null || true
# The drop-ins go; /run/gamevm/kwin stays until the reboot, because the running KWin still uses it.
run rm -f /etc/systemd/system/gamevm-kwin.service \
          /run/systemd/user/plasma-kwin_wayland.service.d/50-gamevm.conf \
          /run/systemd/user/plasma-login-kwin_wayland.service.d/50-gamevm.conf \
          /run/environment.d/60-gamevm.conf
run systemctl daemon-reload
run rm -f /etc/systemd/user-environment-generators/60-gamevm-host-gpu \
          /etc/udev/rules.d/61-gamevm-host-gpu.rules /etc/udev/rules.d/62-gamevm-hide-windows-disk.rules
run udevadm control --reload
if compgen -G '/etc/mkinitcpio.conf.d/9[01]-gamevm-*.conf' >/dev/null || [[ -e /etc/modprobe.d/gamevm-vfio-dedicated.conf ]]; then
    run rm -f /etc/mkinitcpio.conf.d/90-gamevm-igpu.conf /etc/mkinitcpio.conf.d/91-gamevm-vfio-dedicated.conf \
              /etc/modprobe.d/gamevm-vfio-dedicated.conf
    run mkinitcpio -P
fi
run rm -f /etc/modprobe.d/gamevm-kvm-avic.conf /etc/qemu/firmware/40-gamevm-ovmf-x86_64-secure-enrolled-4m.json \
          /etc/sysctl.d/60-gamevm-memory.conf
[[ -e /proc/sys/vm/defrag_mode ]] && run sysctl -q -w vm.defrag_mode=0

step "qemu.conf and fstab"
if [[ -f /etc/libvirt/qemu.conf ]]; then run python3 "$REPO/setup/setup_helper.py" qemu-conf remove /etc/libvirt/qemu.conf; fi
if grep -q '^#gamevm# ' /etc/fstab; then
    run python3 "$REPO/setup/setup_helper.py" fstab remove /
    run systemctl daemon-reload
fi

step "Network"
if "${VIRSH[@]}" net-info gamevm-host >/dev/null 2>&1; then
    run "${VIRSH[@]}" net-destroy gamevm-host || true
    run "${VIRSH[@]}" net-undefine gamevm-host
fi
if command -v ufw >/dev/null; then
    run ufw delete allow in on virbr-gamevm || true
    run ufw delete allow in on virbr0 to any port 67 proto udp || true
    run ufw delete allow in on virbr0 to any port 53 || true
    run ufw route delete allow in on virbr0 || true
fi
if [[ -n ${SUDO_USER:-} ]]; then
    UH="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    run runuser -u "$SUDO_USER" -- sed -i '/^uri_default = "qemu:\/\/\/system"$/d' "$UH/.config/libvirt/libvirt.conf" 2>/dev/null || true
fi

if ((PURGE)); then
    step "VM definition (NVRAM + TPM state) and /etc/gamevm"
    if "${VIRSH[@]}" dominfo "$NAME" >/dev/null 2>&1; then
        run "${VIRSH[@]}" undefine "$NAME" --nvram --tpm
    fi
    run rm -rf /etc/gamevm /usr/local/share/gamevm
fi

if ((LG)); then
    step "Looking Glass"
    run rm -f /usr/local/bin/looking-glass-client /etc/modprobe.d/kvmfr.conf /etc/modules-load.d/kvmfr.conf \
              /etc/udev/rules.d/70-kvmfr.rules
    for v in $(dkms status kvmfr 2>/dev/null | sed -n 's#^kvmfr/\([^,:]*\).*#\1#p' | sort -u); do
        run dkms remove "kvmfr/$v" --all
        run rm -rf "/usr/src/kvmfr-$v"
    done
    run modprobe -r kvmfr || true
fi

echo
echo "Done. Packages (libvirt, qemu modules, swtpm, ...) were left installed; remove them with pacman if you want."
echo "The 'libvirt' group membership and lingering were left as they are. Reboot to finish."
