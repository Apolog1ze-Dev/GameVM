#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
# gamevm host setup for Arch-based Linux (Arch, CachyOS, EndeavourOS, ...). Idempotent: safe to run again
# (e.g. after enabling the iGPU).
#
#   sudo ./setup/install-host.sh [options]
#
#   --dry-run          show what would be done, change nothing
#   --yes              do not ask questions (accept the defaults below)
#   --no-snapshot      skip the snapper snapshot
#   --virt-manager     also install virt-manager (GUI; costs nothing while closed)
#   --secure-boot      create a Microsoft-keyed OVMF variable store (for firmware.secure_boot = true)
#   --no-fstab         do not turn the Windows C: fstab entry into an on-demand automount
#   --avic             EXPERIMENT: force-enable AVIC in kvm_amd (taints the kernel, see docs/06)
#   --dedicated-gpu    iGPU mode only: bind the graphics card to vfio-pci at boot (Linux never uses it;
#                      the most robust option). --dynamic-gpu switches back to the default hand-off.
#
# Nothing here touches the bootloader or the kernel command line. Every edited file is backed up
# as <file>.gamevm.orig, and setup/uninstall-host.sh undoes the changes.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY=0 YES=0 SNAP=1 VMM=0 SB=0 FSTAB=1 AVIC=0 GPUMODE=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;; --yes) YES=1 ;; --no-snapshot) SNAP=0 ;; --virt-manager) VMM=1 ;;
        --secure-boot) SB=1 ;; --no-fstab) FSTAB=0 ;; --avic) AVIC=1 ;;
        --dedicated-gpu) GPUMODE=dedicated ;; --dynamic-gpu) GPUMODE=dynamic ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
export LC_ALL=C  # virsh/ufw output is parsed below

c_step=$'\e[1;36m' c_ok=$'\e[32m' c_warn=$'\e[33m' c_err=$'\e[31m' c_off=$'\e[0m'
step() { printf '\n%s==> %s%s\n' "$c_step" "$*" "$c_off"; }
ok()   { printf '  %s✔%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '  %s!%s %s\n' "$c_warn" "$c_off" "$*"; }
die()  { printf '%sERROR:%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }
run()  { if ((DRY)); then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
ask()  { ((YES)) && return 0; local a; read -r -p "  $1 [Y/n] " a; [[ -z $a || $a =~ ^[Yy] ]]; }
install_file() {  # mode src dest
    if ((DRY)); then printf '  [dry-run] install -m %s %s %s\n' "$1" "$2" "$3"; return; fi
    install -D -m "$1" -o root -g root "$2" "$3"
}

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0 $*"
USER_NAME="${SUDO_USER:-}"
[[ -n $USER_NAME && $USER_NAME != root ]] || die "run it from your normal account with sudo (SUDO_USER is not set)"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
command -v pacman >/dev/null || die "this script is for Arch-based systems (it installs packages with pacman)"
python3 -c 'import tomllib' 2>/dev/null || die "python >= 3.11 required"
HELPER=(python3 "$REPO/setup/setup_helper.py")

# The graphics chip Linux keeps while Windows has the NVIDIA card: any other one, usually the one
# built into the CPU (AMD or Intel). With it, iGPU mode; without, single-GPU mode.
IGPU="" IGPU_VENDOR="" IGPU_DRIVER=""
for d in /sys/bus/pci/devices/*; do
    if [[ $(cat "$d/class") == 0x03* && $(cat "$d/vendor") != 0x10de ]]; then
        IGPU="${d##*/}"; IGPU_VENDOR="$(cat "$d/vendor")"
        IGPU_DRIVER="$(basename "$(readlink -f "$d/driver" 2>/dev/null)" 2>/dev/null || true)"
    fi
done
case "$IGPU_VENDOR" in 0x8086) [[ -n $IGPU_DRIVER ]] || IGPU_DRIVER=i915 ;; *) [[ -n $IGPU_DRIVER ]] || IGPU_DRIVER=amdgpu ;; esac

echo "gamevm host setup  (user: $USER_NAME, repo: $REPO)"
if [[ -n $IGPU ]]; then
    echo "  iGPU found at $IGPU -> iGPU mode will be configured (Linux keeps the side monitor while Windows runs)"
else
    echo "  no iGPU on the PCI bus -> single-GPU mode (enable it in the BIOS later and re-run this script)"
fi
((DRY)) && echo "  DRY RUN: nothing will be changed"

# ------------------------------------------------------------------------------------------------
if ((SNAP)) && command -v snapper >/dev/null && snapper list-configs 2>/dev/null | grep -q '^root '; then
    step "Snapshot (roll back from the GRUB 'snapshots' menu or btrfs-assistant if anything goes wrong)"
    run snapper -c root create --type single --description "gamevm: before install-host" --userdata important=yes
    ok "snapper snapshot created"
fi

# ------------------------------------------------------------------------------------------------
step "Packages"
PKGS=(libvirt qemu-base qemu-hw-usb-host qemu-hw-usb-redirect qemu-audio-pipewire qemu-audio-spice
      qemu-ui-spice-core qemu-chardev-spice swtpm dnsmasq edk2-ovmf dmidecode ntfs-3g ddcutil
      kdeconnect kio-extras  # these two: clipboard and Windows' drives while Windows runs (gamevm share)
      qt6-declarative)       # the QML runtime that draws the gamevm window (gamevm ui)
# Vulkan for the chip the Linux desktop runs on while Windows has the card.
case "$IGPU_VENDOR" in
    0x1002) PKGS+=(vulkan-radeon lib32-vulkan-radeon) ;;
    0x8086) PKGS+=(vulkan-intel lib32-vulkan-intel) ;;
esac
((VMM)) && PKGS+=(virt-manager)
((SB)) && PKGS+=(virt-firmware)
PAC=(pacman -S --needed)
((YES)) && PAC+=(--noconfirm)
run "${PAC[@]}" "${PKGS[@]}"
ok "installed: ${PKGS[*]}"

# ------------------------------------------------------------------------------------------------
step "libvirt: modular daemons, socket-activated (nothing runs while no VM is up)"
for u in libvirtd.service libvirtd.socket libvirtd-ro.socket libvirtd-admin.socket libvirtd-tcp.socket libvirtd-tls.socket; do
    if systemctl is-enabled --quiet "$u" 2>/dev/null; then run systemctl disable --now "$u"; fi
done
SOCKETS=()
for drv in qemu network nodedev storage; do
    SOCKETS+=("virt${drv}d.socket" "virt${drv}d-ro.socket" "virt${drv}d-admin.socket")
done
run systemctl unmask "${SOCKETS[@]}"
run systemctl enable --now "${SOCKETS[@]}" virtlogd.socket virtlockd.socket
ok "sockets enabled: virtqemud virtnetworkd virtnodedevd virtstoraged (+ virtlogd, virtlockd)"
run usermod -aG libvirt "$USER_NAME"
ok "$USER_NAME added to the 'libvirt' group (reboot once: with lingering, logging out is not enough)"
if ((! DRY)); then
    # As the user, so root never follows symlinks planted in the user's home.
    runuser -u "$USER_NAME" -- sh -c 'mkdir -p "$1" && { grep -qs "^uri_default" "$1/libvirt.conf" ||
        echo "uri_default = \"qemu:///system\"" >> "$1/libvirt.conf"; }' sh "$USER_HOME/.config/libvirt"
fi
ok "virsh/virt-manager default to qemu:///system for $USER_NAME"
for f in "$REPO"/host/desktop/*.desktop; do
    if ((DRY)); then echo "  [dry-run] install launcher ${f##*/}"; else
        runuser -u "$USER_NAME" -- install -D -m 0644 "$f" "$USER_HOME/.local/share/applications/${f##*/}"
    fi
done
ok "app launchers: 'gamevm' (the window: start and stop Windows, setup, what Windows gets) and 'Windows (Looking Glass)'"

# ------------------------------------------------------------------------------------------------
step "gamevm program, hook and helpers"
install_file 0755 "$REPO/gamevm/gamevm.py" /usr/local/lib/gamevm/gamevm.py
install_file 0644 "$REPO/gamevm/gamevm_doctor.py" /usr/local/lib/gamevm/gamevm_doctor.py
# The window (`gamevm ui`): its local service and the pages Qt's QML runtime draws.
install_file 0644 "$REPO/gamevm/gamevm_ui.py" /usr/local/lib/gamevm/gamevm_ui.py
run rm -rf /usr/local/lib/gamevm/ui  # pages that no longer exist go
for f in "$REPO"/gamevm/ui/*; do
    install_file 0644 "$f" "/usr/local/lib/gamevm/ui/${f##*/}"
done
for f in "$REPO"/windows/*.ps1; do  # gamevm bench and gamevm scripts copy them into the running VM
    install_file 0644 "$f" "/usr/local/lib/gamevm/windows/${f##*/}"
done
# Where these files came from, so `gamevm doctor` can tell when the checkout has moved on.
if ((DRY)); then echo "  [dry-run] record $REPO in /usr/local/lib/gamevm/SOURCE"; else
    printf '%s\n' "$REPO" > /usr/local/lib/gamevm/SOURCE
    chmod 0644 /usr/local/lib/gamevm/SOURCE
fi
run ln -sfn /usr/local/lib/gamevm/gamevm.py /usr/local/bin/gamevm
install_file 0755 "$REPO/host/gpu-run" /usr/local/bin/gpu-run
install_file 0755 "$REPO/host/libvirt-hook" /etc/libvirt/hooks/qemu.d/gamevm
if [[ -e /etc/libvirt/hooks/qemu ]]; then
    warn "/etc/libvirt/hooks/qemu exists: libvirt runs it BEFORE gamevm's hook. Make sure it does not"
    warn "touch the GPU or the Windows disk of the gamevm VM."
fi
ok "gamevm -> /usr/local/bin/gamevm, gpu-run -> /usr/local/bin/gpu-run, hook -> /etc/libvirt/hooks/qemu.d/gamevm"

# ------------------------------------------------------------------------------------------------
step "Configuration (/etc/gamevm)"
run install -d -m 0755 -o root -g root /etc/gamevm
if ((DRY)); then
    echo "  [dry-run] create /etc/gamevm/gamevm.toml from config/gamevm.toml (uuid, user, device IDs, serial)"
    echo "  [dry-run] capture host SMBIOS identity to /etc/gamevm/smbios.toml"
else
    # The template plus what this PC has: graphics card, Windows disk, keyboard, mouse, cores, memory.
    "${HELPER[@]}" fill-config "$REPO/config/gamevm.toml" /etc/gamevm/gamevm.toml --user "$USER_NAME" || {
        rc=$?
        ((rc == 3)) && die "gamevm could not tell the graphics card or the Windows disk on this PC without doubt (see above).
       Complete /etc/gamevm/gamevm.toml by hand (docs/01-requirements-and-cabling.md), then run this script again."
        exit "$rc"
    }
    "${HELPER[@]}" smbios /etc/gamevm/smbios.toml
fi
CONF=/etc/gamevm/gamevm.toml
[[ -f $CONF ]] || CONF="$REPO/config/gamevm.toml"
cfg() { python3 - "$CONF" "$1" <<'PY'
import sys, tomllib
d = tomllib.load(open(sys.argv[1], "rb"))
for k in sys.argv[2].split("."):
    d = d[k]
print(" ".join(map(str, d)) if isinstance(d, list) else ("true" if d is True else "false" if d is False else d))
PY
}
AUDIO="$(cfg audio.backend)"; QEMU_AS_USER="$(cfg audio.qemu_as_user)"
LG="$(cfg looking_glass.enabled)"; KVMFR="$(cfg looking_glass.kvmfr)"
GPU_IDS="$(cfg gpu.expect_ids | tr ' ' ',')"
NVME="$(cfg disk.nvme)"; NETMODE="$(cfg network.mode)"; HOSTONLY="$(cfg network.hostonly)"
SERIAL="$(cfg disk.expect_serial)"; UNMOUNT="$(cfg disk.unmount)"; EXTRA="$(cfg disk.extra)"

# ------------------------------------------------------------------------------------------------
step "QEMU settings (/etc/libvirt/qemu.conf)"
QARGS=(qemu-conf apply /etc/libvirt/qemu.conf)
[[ $AUDIO == pipewire && $QEMU_AS_USER == true ]] && QARGS+=(--user "$USER_NAME")
[[ $LG == true && $KVMFR == true ]] && QARGS+=(--kvmfr)
if ((DRY)); then echo "  [dry-run] setup_helper.py ${QARGS[*]}"; else "${HELPER[@]}" "${QARGS[@]}"; fi
if [[ $AUDIO == pipewire && $QEMU_AS_USER == true ]]; then
    ok "QEMU runs as $USER_NAME:kvm so Windows audio reaches your PipeWire"
elif [[ $AUDIO == pipewire ]]; then
    ok "QEMU stays unprivileged; the hook lets it reach your PipeWire socket only while the VM runs"
fi
run loginctl enable-linger "$USER_NAME"
ok "lingering on for $USER_NAME (PipeWire keeps running in single-GPU mode)"

# ------------------------------------------------------------------------------------------------
step "Host shutdown: Windows is shut down first"
# libvirt's own auto_shutdown_* settings (written by earlier gamevm versions, removed just above)
# cannot do this here: logind lets a shutdown wait 5 s and systemd gives virtqemud 10 s to stop,
# then QEMU is killed. gamevm-shutdown.service holds the host's shutdown until Windows is off.
install_file 0644 "$REPO/host/systemd/gamevm-shutdown.service" /etc/systemd/system/gamevm-shutdown.service
run systemctl daemon-reload
run systemctl enable --now gamevm-shutdown.service
ok "shutting down or rebooting the host shuts Windows down first (waits up to vm.host_shutdown_wait seconds)"
if systemctl is-enabled --quiet libvirt-guests.service 2>/dev/null; then
    warn "libvirt-guests.service is enabled: at host shutdown it also acts on the gamevm VM (by default it tries"
    warn "to suspend it, which a VM with a passed-through GPU cannot do). Disable it unless other VMs need it."
fi

# ------------------------------------------------------------------------------------------------
step "Host GPU (iGPU) integration"
install_file 0644 "$REPO/host/udev/61-gamevm-host-gpu.rules" /etc/udev/rules.d/61-gamevm-host-gpu.rules
install_file 0644 "$REPO/host/systemd/gamevm-kwin.service" /etc/systemd/system/gamevm-kwin.service
run systemctl daemon-reload
run systemctl enable gamevm-kwin.service
ok "udev names /dev/dri/gamevm-host-{card,render}; gamevm-kwin.service gives KWin the graphics card for the"
ok "main monitor at boot (iGPU mode only); the hook moves KWin to the iGPU while Windows runs"
# Earlier gamevm versions kept the whole desktop on the iGPU: remove those pieces.
if [[ -e /etc/systemd/user-environment-generators/60-gamevm-host-gpu ]]; then
    run rm -f /etc/systemd/user-environment-generators/60-gamevm-host-gpu
    ok "removed the old session environment generator"
fi
if [[ -e /etc/mkinitcpio.conf.d/90-gamevm-igpu.conf ]]; then
    run rm -f /etc/mkinitcpio.conf.d/90-gamevm-igpu.conf
    run mkinitcpio -P
    ok "removed the old amdgpu-first initramfs drop-in (mkinitcpio -P done)"
fi
[[ -n $IGPU ]] || warn "no iGPU found: iGPU mode needs it enabled in the BIOS (docs/01); single-GPU mode works without"
if [[ $GPUMODE == dedicated ]]; then
    [[ -n $IGPU ]] || die "--dedicated-gpu needs the iGPU: with it, Linux would have no display at all"
    # A dry run on a fresh host reads the template, whose IDs fill-config only fills in for real.
    [[ -n $GPU_IDS ]] || ((DRY)) || die "gpu.expect_ids is empty in $CONF"
    [[ -n $GPU_IDS ]] || GPU_IDS="<vendor:device IDs filled in by the real run>"
    tmp="$(mktemp)"
    cat > "$tmp" <<VFIOCONF
# gamevm --dedicated-gpu: the graphics card belongs to vfio-pci from boot; Linux never loads a driver for
# it. BEFORE disabling the iGPU in the BIOS, run: sudo ./setup/install-host.sh --dynamic-gpu
# Emergency: add  module_blacklist=vfio_pci  to the kernel command line in GRUB (press e).
options vfio-pci ids=$GPU_IDS disable_vga=1
softdep nvidia pre: vfio-pci
softdep nouveau pre: vfio-pci
softdep snd_hda_intel pre: vfio-pci
VFIOCONF
    install_file 0644 "$tmp" /etc/modprobe.d/gamevm-vfio-dedicated.conf; rm -f "$tmp"
    tmp="$(mktemp)"
    cat > "$tmp" <<MKINITCONF
# gamevm --dedicated-gpu: vfio-pci first and no NVIDIA modules in the initramfs (on CachyOS, chwd adds
# them in 10-chwd.conf); the iGPU's driver next for the console.
# Remove with: sudo ./setup/install-host.sh --dynamic-gpu
_gamevm_modules=(vfio_pci vfio vfio_iommu_type1 $IGPU_DRIVER)
for _m in "\${MODULES[@]}"; do
    case "\$_m" in nvidia|nvidia_modeset|nvidia_uvm|nvidia_drm|$IGPU_DRIVER|vfio_pci|vfio|vfio_iommu_type1) ;;
        *) _gamevm_modules+=("\$_m") ;; esac
done
MODULES=("\${_gamevm_modules[@]}")
unset _gamevm_modules _m
MKINITCONF
    install_file 0644 "$tmp" /etc/mkinitcpio.conf.d/91-gamevm-vfio-dedicated.conf; rm -f "$tmp"
    run mkinitcpio -P
    ok "dedicated: the graphics card is bound to vfio-pci at the next boot"
elif [[ $GPUMODE == dynamic ]]; then
    if [[ -e /etc/modprobe.d/gamevm-vfio-dedicated.conf || -e /etc/mkinitcpio.conf.d/91-gamevm-vfio-dedicated.conf ]]; then
        run rm -f /etc/modprobe.d/gamevm-vfio-dedicated.conf /etc/mkinitcpio.conf.d/91-gamevm-vfio-dedicated.conf
        run mkinitcpio -P
    fi
    ok "dynamic: Linux uses the graphics card for the main monitor whenever Windows is off (from the next boot)"
fi

# ------------------------------------------------------------------------------------------------
step "Memory: keep RAM defragmented for the VM's huge pages"
# Windows' RAM is reserved as 2 MiB blocks that must each be physically contiguous. After long
# uptimes free RAM is fragmented (CachyOS turns background compaction off), so the reservation can
# fall short and gamevm falls back to slower transparent huge pages. vm.defrag_mode (Linux 6.15+)
# makes the page allocator keep free memory in whole 2 MiB blocks.
if [[ -e /proc/sys/vm/defrag_mode ]]; then
    tmp="$(mktemp)"
    printf '%s\n' "# gamevm: keep free RAM in whole 2 MiB blocks, so the Windows VM's huge pages can be reserved" \
        "# even after a long uptime. Remove this file to undo." "vm.defrag_mode = 1" > "$tmp"
    install_file 0644 "$tmp" /etc/sysctl.d/60-gamevm-memory.conf
    rm -f "$tmp"
    run sysctl -q -w vm.defrag_mode=1
    ok "vm.defrag_mode = 1 (free RAM stays in 2 MiB blocks; fully effective after the next reboot)"
else
    warn "this kernel has no vm.defrag_mode: after long uptimes gamevm may fall back to transparent huge pages"
fi

step "Windows disk protection"
# Hide the disks Windows gets (the NVMe, disk.extra) from udisks/Dolphin, so nothing on the host
# mounts one by accident: it may belong to the running VM, or be hibernated by Windows.
SERIALS=()
[[ -n $SERIAL ]] && SERIALS+=("$SERIAL")
for disk in $EXTRA; do
    s="$(udevadm info --query=property --name="$disk" 2>/dev/null | sed -n 's/^ID_SERIAL_SHORT=//p')"
    if [[ -n $s ]]; then SERIALS+=("$s"); else warn "disk.extra $disk: no serial found (not hidden from the file manager)"; fi
done
if ((${#SERIALS[@]})); then
    tmp="$(mktemp)"
    {
        echo "# gamevm: hide the disks given to the Windows VM from udisks/Dolphin, so nothing on the host"
        echo "# mounts them by accident (they may belong to the running VM, or be hibernated by Windows)."
        for s in "${SERIALS[@]}"; do
            echo "SUBSYSTEM==\"block\", ENV{ID_SERIAL_SHORT}==\"$s\", ENV{UDISKS_IGNORE}=\"1\""
        done
    } > "$tmp"
    install_file 0644 "$tmp" /etc/udev/rules.d/62-gamevm-hide-windows-disk.rules
    rm -f "$tmp"
    ok "disks hidden from the file manager (fstab mounts still work): ${SERIALS[*]}"
fi
if ((FSTAB)); then
    for mp in $UNMOUNT; do
        if grep -qE "^[^#]\S*\s+$mp\s" /etc/fstab && ! grep -qE "^[^#]\S*\s+$mp\s.*x-systemd\.automount" /etc/fstab \
                && ask "Mount $mp only on access (automount) instead of at boot?"; then
            if ((DRY)); then echo "  [dry-run] setup_helper.py fstab apply $mp"; else "${HELPER[@]}" fstab apply "$mp"; fi
        fi
    done
    run systemctl daemon-reload
    # Not while Windows runs: these are its disks then, and an automount would mount them here too.
    VMSTATE="$(virsh -q -c qemu:///system domstate "$(cfg vm.name)" 2>/dev/null || true)"
    for mp in $UNMOUNT; do
        am="$(systemd-escape -p "$mp").automount"
        if [[ -z $VMSTATE || $VMSTATE == "shut off" ]] && systemctl cat "$am" >/dev/null 2>&1 && ! mountpoint -q "$mp"; then
            run systemctl start "$am"
        fi
    done
fi
run udevadm control --reload
run udevadm trigger --subsystem-match=drm --subsystem-match=block

# ------------------------------------------------------------------------------------------------
step "Network"
run systemctl start virtnetworkd.socket
if [[ $HOSTONLY == true ]]; then
    HMAC="$(python3 "$REPO/gamevm/gamevm.py" -c "$CONF" mac hostonly)"
    HOSTENTRY="<host mac='$HMAC' ip='10.77.0.2'/>"
    if ! virsh -q -c qemu:///system net-info gamevm-host >/dev/null 2>&1; then
        tmp="$(mktemp --suffix=.xml)"
        sed "s|<!-- @HOSTONLY_RESERVATION@ -->|$HOSTENTRY|" "$REPO/libvirt/gamevm-host-network.xml" > "$tmp"
        run virsh -q -c qemu:///system net-define "$tmp"
        rm -f "$tmp"
    elif ! virsh -q -c qemu:///system net-dumpxml gamevm-host | grep -q "ip='10.77.0.2'"; then
        run virsh -q -c qemu:///system net-update gamevm-host add ip-dhcp-host "$HOSTENTRY" --config --live
    fi
    run virsh -q -c qemu:///system net-autostart gamevm-host
    virsh -q -c qemu:///system net-info gamevm-host 2>/dev/null | grep -q 'Active:.*yes' || run virsh -q -c qemu:///system net-start gamevm-host
    ok "host-only network gamevm-host (Linux 10.77.0.1 <-> Windows 10.77.0.2)"
fi
if [[ $NETMODE == nat ]]; then
    run virsh -q -c qemu:///system net-autostart default
    virsh -q -c qemu:///system net-info default 2>/dev/null | grep -q 'Active:.*yes' || run virsh -q -c qemu:///system net-start default
fi
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
    # ufw's DROP policies also drop libvirt's DHCP/forwarding (libvirt cannot open holes in them).
    [[ $HOSTONLY == true ]] && run ufw allow in on virbr-gamevm comment 'gamevm host-only network'
    if [[ $NETMODE == nat ]]; then
        run ufw allow in on virbr0 to any port 67 proto udp comment 'libvirt DHCP'
        run ufw allow in on virbr0 to any port 53 comment 'libvirt DNS'
        run ufw route allow in on virbr0 comment 'libvirt NAT'
    fi
    ok "ufw rules added for the VM networks"
fi

# ------------------------------------------------------------------------------------------------
if ((SB)); then
    step "Secure Boot variable store (Microsoft keys)"
    if [[ ! -f /usr/local/share/gamevm/OVMF_VARS.ms.4m.fd ]]; then
        run install -d /usr/local/share/gamevm
        run virt-fw-vars --input /usr/share/edk2/x64/OVMF_VARS.4m.fd \
            --output /usr/local/share/gamevm/OVMF_VARS.ms.4m.fd --enroll-microsoft --secure-boot
    fi
    tmp="$(mktemp)"
    cat > "$tmp" <<'EOF'
{
  "description": "OVMF x64 4MB, Secure Boot + SMM, Microsoft keys enrolled (gamevm, virt-fw-vars --enroll-microsoft)",
  "interface-types": ["uefi"],
  "mapping": {
    "device": "flash",
    "executable": {"filename": "/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd", "format": "raw"},
    "nvram-template": {"filename": "/usr/local/share/gamevm/OVMF_VARS.ms.4m.fd", "format": "raw"}
  },
  "targets": [{"architecture": "x86_64", "machines": ["pc-q35-*"]}],
  "features": ["acpi-s3", "acpi-s4", "amd-sev", "enrolled-keys", "requires-smm", "secure-boot", "verbose-dynamic"],
  "tags": []
}
EOF
    install_file 0644 "$tmp" /etc/qemu/firmware/40-gamevm-ovmf-x86_64-secure-enrolled-4m.json
    rm -f "$tmp"
    ok "set firmware.secure_boot = true, then once: gamevm start --reset-nvram (docs/05)"
fi

if ((AVIC)); then
    step "EXPERIMENT: AVIC"
    tmp="$(mktemp)"; printf '# gamevm --avic experiment: remove this file to undo\noptions kvm_amd avic=1 force_avic=1\n' > "$tmp"
    install_file 0644 "$tmp" /etc/modprobe.d/gamevm-kvm-avic.conf; rm -f "$tmp"
    warn "takes effect after reboot (or: sudo modprobe -r kvm_amd && sudo modprobe kvm_amd); set tuning.avic = true"
fi

# ------------------------------------------------------------------------------------------------
step "virtio-win drivers and Windows scripts"
ISO=/var/lib/libvirt/images/virtio-win.iso
if [[ ! -f $ISO ]]; then
    found="$(ls -1t "$USER_HOME"/Downloads/virtio-win*.iso 2>/dev/null | head -1 || true)"
    if [[ -n $found ]]; then
        run install -D -m 0644 "$found" "$ISO"
        ok "virtio-win ISO: $found -> $ISO"
    else
        # Not there yet: fetch it. The server's "stable" link redirects to plain http, so the final
        # address is asked for first and the download itself insists on https; the result has to
        # be an ISO image (its "CD001" mark) before it is used.
        VIRTIO_URL="https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
        dest="$USER_HOME/Downloads/virtio-win.iso"
        if ((DRY)); then echo "  [dry-run] download $VIRTIO_URL to $dest (about 880 MB)"; else
            echo "  downloading the Windows driver disc (about 880 MB) ..."
            runuser -u "$USER_NAME" -- mkdir -p "$USER_HOME/Downloads"
            final="$(runuser -u "$USER_NAME" -- curl -fsSIL --max-time 60 -o /dev/null -w '%{url_effective}' "$VIRTIO_URL" || true)"
            final="https://${final#*://}"
            if [[ $final == https://fedorapeople.org/*.iso ]] &&
               runuser -u "$USER_NAME" -- curl -fL --proto '=https' --proto-redir '=https' --retry 3 -C - -o "$dest.part" "$final" &&
               [[ "$(dd if="$dest.part" bs=1 skip=32769 count=5 2>/dev/null)" == CD001 ]]; then
                runuser -u "$USER_NAME" -- mv -f "$dest.part" "$dest"
                run install -D -m 0644 "$dest" "$ISO"
                ok "virtio-win ISO downloaded: $dest -> $ISO"
            else
                warn "the driver disc could not be downloaded. Download virtio-win.iso in your browser from"
                warn "  https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/"
                warn "into ~/Downloads and run this script again."
            fi
        fi
    fi
else
    ok "virtio-win ISO present: $ISO"
fi
# A place Windows can read when it is booted natively: the last of the Windows-side mount points of
# the configuration (disk.unmount) that is mounted right now, a shared data disk if there is one.
SHARE=""
for mp in $UNMOUNT; do
    ls "$mp" >/dev/null 2>&1 || true  # an automount mounts on access (not while Windows has the disk)
    # the last line: an automounted path lists its autofs trigger first
    [[ $(findmnt -no FSTYPE "$mp" 2>/dev/null | tail -n 1) =~ ^(fuseblk|ntfs|ntfs3|vfat|exfat)$ ]] && SHARE="$mp/gamevm-windows"
done
if [[ -n $SHARE ]]; then
    run mkdir -p "$SHARE"
    run cp -f "$REPO"/windows/*.ps1 "$SHARE/"
    if [[ -f $ISO ]] && ((! DRY)); then
        bsdtar -xf "$ISO" -C "$SHARE" virtio-win-guest-tools.exe 2>/dev/null && ok "virtio-win-guest-tools.exe copied to $SHARE"
    fi
    ok "Windows scripts copied to $SHARE: open that folder in Windows when you boot it natively"
else
    warn "none of Windows' partitions is mounted on Linux right now, so the Windows scripts were not copied there."
    warn "For the step in Windows (docs/02-windows.md): download this project in Windows, or copy its windows/"
    warn "folder to a USB stick. Later, while Windows runs in the VM:  gamevm scripts"
fi

# ------------------------------------------------------------------------------------------------
step "Restart libvirt so it picks up the hook and settings"
VMSTATE="$(virsh -q -c qemu:///system domstate "$(cfg vm.name)" 2>/dev/null || true)"
if [[ -n $VMSTATE && $VMSTATE != "shut off" ]]; then
    warn "Windows is $VMSTATE: libvirt is not restarted under it. It reads the new settings the next time it"
    warn "starts (it exits by itself two minutes after Windows has shut down)."
else
    run systemctl try-restart virtqemud.service virtnetworkd.service
    ok "done"
fi

echo
echo "Next steps:"
echo "  1. Reboot (libvirt group membership${IGPU:+, KWin GPU layout})."
[[ $LG == true && -n $IGPU ]] && echo "  2. Looking Glass:          sudo ./looking-glass/install.sh"
echo "  3. Windows (bare metal):  run gamevm-windows\\1-prepare-windows.ps1, then shut Windows down."
echo "  4. Check everything:      sudo gamevm doctor"
echo "  5. Start Windows:         gamevm start"
