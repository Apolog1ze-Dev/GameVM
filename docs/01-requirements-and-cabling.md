# 1. What you need, BIOS settings and cabling

## 1.1 What you need

| | Needed | Notes |
|---|---|---|
| **Linux** | An Arch-based distribution (Arch, CachyOS, EndeavourOS, ...) with systemd | The installer uses `pacman`. Other distributions are not supported yet. |
| **Desktop** | KDE Plasma 6 on Wayland for iGPU mode | The hand-over restarts KWin. Single-GPU mode closes the whole session instead and does not depend on KWin. |
| **Processor and board** | Hardware virtualisation and an IOMMU (AMD-Vi or Intel VT-d), UEFI boot | Switched on in the BIOS, see 1.3. |
| **Graphics card** | An NVIDIA card, used by Linux with NVIDIA's own driver | This is the card Windows gets. gamevm unloads the NVIDIA driver to hand it over; AMD and Intel cards are not supported as the card for Windows yet. |
| **A second graphics chip** | Optional: the one built into most CPUs (AMD or Intel) | With it you get **iGPU mode**: Linux stays on screen while Windows runs. Without it, **single-GPU mode**: the Linux desktop closes while Windows runs. |
| **Windows** | An existing Windows 11 installation on an **NVMe drive of its own** | The whole drive is handed to Windows, so nothing of Linux may be on it. Windows on a SATA disk, or on the same drive as Linux, does not work. |
| **Monitors** | iGPU mode: a monitor on the graphics card and one on the motherboard, or one monitor with a cable to each | See 1.4. |
| **Keyboard and mouse** | USB (wired, or a wireless one with its own USB receiver) | Bluetooth keyboards and mice cannot be handed over this way. |

`gamevm discover` (after cloning the project, no installation needed) shows what gamevm finds on
your PC and says so if something above is missing:

```bash
python3 gamevm/gamevm.py discover
```

gamevm was built and tested on one PC: a Ryzen 7 9800X3D with its built-in Radeon graphics, an
RTX 4080 SUPER, KDE Plasma 6 on Wayland, CachyOS. Everything else is untested, an Intel built-in
graphics chip in particular. Please report what you find.

## 1.2 Why a second graphics chip changes so much

A GeForce card cannot be split between Linux and a VM: it has no SR-IOV, NVIDIA's vGPU is limited
to datacenter and professional cards, and the paravirtual graphics drivers for Windows guests
stop at Direct3D 11 in alpha quality. So Windows gets the whole card, and Linux needs something
else to draw with meanwhile.

| | **iGPU mode** | **single-GPU mode** |
|---|---|---|
| Needs | a second graphics chip, usually the one in the CPU | nothing |
| Linux desktop while Windows runs | stays on screen, on the monitor cabled to the motherboard | closes; Linux keeps running without a screen and the desktop returns when Windows shuts down |
| Where Windows shows | on the monitor cabled to the graphics card, at its full refresh rate with G-SYNC/VRR and HDR | on every monitor of the graphics card |
| The graphics card while Windows is off | Linux uses it normally, also for Linux games | Linux uses it normally |

`vm.mode = "auto"` picks iGPU mode whenever a second graphics chip is present.

## 1.3 BIOS settings

Names differ between board makers; look for these.

| Setting | Usual names | Value |
|---|---|---|
| Hardware virtualisation | SVM Mode (AMD), Intel Virtualization Technology / VT-x | Enabled |
| IOMMU | IOMMU (AMD), VT-d (Intel) | Enabled |
| Built-in graphics (iGPU mode) | Integrated Graphics, IGD Multi-Monitor, iGPU Multi-Monitor | **Enabled / Forced**. "Auto" usually switches it off as soon as a graphics card is installed. |
| First display | Initial Display Output, Primary Display | The graphics card (PCIe / PEG), so the BIOS and boot screens show on your main monitor |
| Above 4G Decoding | Above 4G Decoding | Enabled |
| Resizable BAR | Re-Size BAR Support | Enabled or Auto |
| CSM | CSM Support, Launch CSM | Disabled (UEFI boot) |
| Secure Boot | Secure Boot | As your Linux needs it. Most Arch-based installs boot with it off. |

After changing them, `python3 gamevm/gamevm.py discover` should list both graphics chips, and
`sudo gamevm doctor` (once installed) checks the IOMMU groups.

Enabling the built-in graphics can renumber PCI addresses. gamevm records what each device is
(vendor and device ID, the disk's serial number) and refuses to start if an address no longer
points at the expected hardware; `doctor` then says which address to correct in
`/etc/gamevm/gamevm.toml`.

## 1.4 Cabling (iGPU mode)

**Two monitors.** Leave your main monitor on the graphics card. Plug the second monitor into the
**motherboard's** video output (HDMI, DisplayPort or USB-C), not into the graphics card.

```
graphics card   ─────► main monitor   (Linux while Windows is off, Windows while it runs)
motherboard     ─────► side monitor   (Linux, always)
```

| | Main monitor | Side monitor |
|---|---|---|
| Windows off | Linux, drawn by the graphics card: full refresh rate, VRR, Linux games | Linux |
| Windows running | Windows, drawn by the graphics card: full refresh rate, G-SYNC/VRR, HDR | Linux: your chat, browser, a Looking Glass window |

- **The switch is automatic.** Starting Windows moves the Linux desktop to the side monitor and
  gives the main monitor to Windows; shutting Windows down brings it back. Each switch restarts
  KWin, the desktop's compositor: you stay logged in and KDE's own apps (Konsole, Dolphin, Kate)
  stay open, while other apps (browsers, Electron apps, Steam) close and are reopened for you
  (docs/03).
- **Windows in a window.** While Windows runs, Looking Glass can show it in a window on the side
  monitor. The picture is then drawn by the built-in graphics chip, which is fine for the
  desktop and menus; play on the main monitor.

**One monitor.** Connect it twice: one cable from the graphics card, one from the motherboard.
Linux stays on the motherboard input; to see Windows you either use the Looking Glass window, or
switch the monitor's input to the graphics card's cable for full speed. gamevm can switch the
input for you over DDC/CI (`display.switch_inputs`, `gamevm view vm|host`); monitors that ignore
those commands are switched with their own button. This arrangement has not been tested.

**No second graphics chip.** Nothing to cable: that is single-GPU mode.

## 1.5 Checks after setup

```bash
python3 gamevm/gamevm.py discover     # what gamevm found: graphics card, Windows disk, keyboard, mouse
gamevm status                         # who has what right now
sudo gamevm doctor                    # IOMMU groups, device IDs, the desktop's GPU layout, the Windows disk
```

## 1.6 Notes from the PC this was built on

Kept here because they may save you time on similar hardware (Gigabyte AM5 board, Ryzen 9000).

- A BIOS update resets every setting, and newer BIOS versions switch Secure Boot on by default:
  switch it off again (or sign your boot chain) before booting Linux.
- A firmware-TPM reset can invalidate the Windows Hello PIN of the natively booted Windows. Know
  your password.
- Newer firmware can break passthrough with `Firmware has requested this device have a 1:1 IOMMU
  mapping`. If `sudo gamevm doctor` or `journalctl -k | grep -i "1:1"` shows it, set *Kernel DMA
  Protection Indicator* to Disabled in the BIOS. That also lowers DMA protection for the natively
  booted Windows.
- With the built-in graphics forced on, give them a modest frame buffer (512 MB is enough for a
  desktop).
- If the side monitor shows graphics hangs on a new kernel, try the distribution's LTS kernel.
