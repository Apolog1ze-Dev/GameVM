#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Karim Dagher
# Build and install the Looking Glass client + the kvmfr kernel module (DKMS) for iGPU mode.
#
#   sudo ./looking-glass/install.sh [BUILD]
#
# BUILD defaults to looking_glass.build in /etc/gamevm/gamevm.toml (e.g. B7-829-cdba29f2). The
# Windows side (Looking Glass IDD) MUST be the same build: client and IDD speak a versioned protocol
# and refuse to talk otherwise. To upgrade, change looking_glass.build, re-run this script, and
# install the matching IDD in Windows (C:\gamevm\2-inside-vm.ps1 -LookingGlassBuild <BUILD>).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
die()  { printf '\e[31mERROR:\e[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\e[1;36m==> %s\e[0m\n' "$*"; }
ok()   { printf '  \e[32m✔\e[0m %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run with sudo"
USER_NAME="${SUDO_USER:-}"
[[ -n $USER_NAME && $USER_NAME != root ]] || die "run it from your normal account with sudo"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
CONF=/etc/gamevm/gamevm.toml
[[ -f $CONF ]] || CONF="$REPO/config/gamevm.toml"
read -r CFG_BUILD SIZE < <(python3 - "$CONF" <<'PY'
import sys, tomllib
lg = tomllib.load(open(sys.argv[1], "rb"))["looking_glass"]
print(lg["build"], lg["size_mib"])
PY
)
BUILD="${1:-$CFG_BUILD}"
[[ $BUILD =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid build id '$BUILD'"
SRC="$USER_HOME/.cache/gamevm/looking-glass-$BUILD"
as_user() { runuser -u "$USER_NAME" -- "$@"; }

step "Build dependencies (on CachyOS, dkms brings clang/lld for its clang-built kernel)"
pacman -S --needed --noconfirm base-devel cmake pkgconf binutils dkms fontconfig fuse3 gmp libglvnd \
    libxkbcommon nettle spice-protocol wayland wayland-protocols libpipewire libpulse libsamplerate \
    usbredir libdecor libunwind elfutils libx11 libxcursor libxfixes libxi libxinerama libxss \
    libxpresent libxrandr ttf-dejavu
for k in /usr/lib/modules/*/build; do [[ -d $k ]] || die "kernel headers missing for ${k%/build}"; done
ok "dependencies installed"

step "Source $BUILD"
as_user mkdir -p "$USER_HOME/.cache/gamevm"
TARBALL="$USER_HOME/.cache/gamevm/looking-glass-$BUILD.tar.gz"
if [[ -s $TARBALL ]] && ! gzip -t "$TARBALL" 2>/dev/null; then
    echo "  cached download is damaged, fetching it again"
    rm -f "$TARBALL"
fi
if [[ ! -s $TARBALL ]]; then
    as_user curl -fL --proto '=https' -o "$TARBALL.part" "https://looking-glass.io/artifact/$BUILD/source"
    as_user mv "$TARBALL.part" "$TARBALL"
fi
echo "  sha256: $(sha256sum "$TARBALL" | cut -d' ' -f1)"
rm -rf "$SRC"
as_user mkdir -p "$SRC"
as_user tar -xzf "$TARBALL" -C "$SRC" --strip-components=1
[[ -d $SRC/client && -d $SRC/module ]] || die "unexpected tarball layout in $SRC"
ok "unpacked to $SRC"

step "Client"
as_user cmake -S "$SRC/client" -B "$SRC/client/build" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/usr/local -DENABLE_PULSEAUDIO=no -DENABLE_X11=no
as_user cmake --build "$SRC/client/build" -j"$(nproc)"
cmake --install "$SRC/client/build"
[[ -x /usr/local/bin/looking-glass-client ]] || die "client was not installed"
ok "looking-glass-client $BUILD installed to /usr/local/bin"

step "kvmfr kernel module (DKMS, rebuilt automatically for new kernels)"
VER="$(sed -n 's/^PACKAGE_VERSION="\{0,1\}\([^"]*\)"\{0,1\}/\1/p' "$SRC/module/dkms.conf")"
[[ -n $VER ]] || die "cannot read PACKAGE_VERSION from module/dkms.conf"
if dkms status "kvmfr/$VER" 2>/dev/null | grep -q .; then
    dkms remove "kvmfr/$VER" --all || true
fi
rm -rf "/usr/src/kvmfr-$VER"
# Root-owned copy: DKMS sources dkms.conf and runs make as root on every kernel update.
install -d -m 0755 -o root -g root "/usr/src/kvmfr-$VER"
cp -rT --no-preserve=ownership "$SRC/module" "/usr/src/kvmfr-$VER"
chown -R root:root "/usr/src/kvmfr-$VER"
chmod -R u=rwX,go=rX "/usr/src/kvmfr-$VER"
dkms add "kvmfr/$VER"
for b in /usr/lib/modules/*/build; do      # every installed kernel (e.g. also linux-cachyos-lts)
    kv="${b%/build}"; kv="${kv##*/}"
    dkms install "kvmfr/$VER" -k "$kv"
done
cat > /etc/modprobe.d/kvmfr.conf <<EOF
# gamevm / Looking Glass: one static shared-memory device, must equal looking_glass.size_mib
options kvmfr static_size_mb=$SIZE
EOF
printf '# gamevm / Looking Glass: load before any VM starts (else QEMU creates a plain file)\nkvmfr\n' > /etc/modules-load.d/kvmfr.conf
# uaccess gives the logged-in user (Looking Glass client) access; group kvm covers QEMU.
# The file must sort before 73-seat-late.rules for uaccess to apply.
printf 'SUBSYSTEM=="kvmfr", GROUP="kvm", MODE="0660", TAG+="uaccess"\n' > /etc/udev/rules.d/70-kvmfr.rules
udevadm control --reload
[[ -f /dev/kvmfr0 && ! -c /dev/kvmfr0 ]] && rm -f /dev/kvmfr0  # left behind by QEMU without the module
modprobe -r kvmfr 2>/dev/null || true
modprobe kvmfr
udevadm trigger --subsystem-match=kvmfr
udevadm settle
[[ -c /dev/kvmfr0 ]] || die "/dev/kvmfr0 was not created (dmesg | grep kvmfr)"
ok "/dev/kvmfr0 ready ($SIZE MiB), loaded at every boot"

step "Client configuration"
CI="$USER_HOME/.config/looking-glass/client.ini"
if [[ ! -f $CI ]]; then
    as_user install -D -m 0644 "$REPO/looking-glass/client.ini" "$CI"
    ok "wrote $CI"
else
    # gamevm's client.ini until 2026-11 opened the window full screen with the mouse captured and
    # Scroll Lock as the only way out. A file that still is that template, unedited, is replaced.
    case "$(sha256sum < "$CI" | cut -d' ' -f1)" in
        51d12f1e697c953b06bf2c117eaf7731c3179ce8bea143674581da091ab5c954)
            as_user cp -p "$CI" "$CI.gamevm.orig"
            as_user install -m 0644 "$REPO/looking-glass/client.ini" "$CI"
            ok "$CI was gamevm's old template: replaced (a window you can leave; the old file is $CI.gamevm.orig)" ;;
        *)  ok "$CI exists, left unchanged (template: looking-glass/client.ini)" ;;
    esac
fi

cat <<EOF

Looking Glass $BUILD is installed.
In Windows (inside the VM) install the IDD of the SAME build:
    C:\\gamevm\\2-inside-vm.ps1 -LookingGlassBuild $BUILD
  or download "Windows IDD" for $BUILD from https://looking-glass.io/downloads
Then on Linux:  gamevm lg
EOF
