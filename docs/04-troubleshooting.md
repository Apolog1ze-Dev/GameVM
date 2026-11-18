# 4. Troubleshooting and recovery

Start with:

```bash
sudo gamevm doctor
gamevm status
journalctl -t gamevm -b                  # what the hook did / refused (also: /var/log/gamevm.log)
journalctl -u virtqemud -b               # libvirt's side (hook errors, QEMU start errors)
sudo cat /var/log/libvirt/qemu/win11.log # QEMU's own log
```

## The VM refuses to start

| Message | Fix |
|---|---|
| `Windows cannot start yet, nothing was changed: ...` | A check before the hand-off found something that would have failed halfway. *The running kernel can no longer load ...*: the kernel or the NVIDIA driver was updated and its old modules are gone from disk; reboot. *The libvirt network ... is not running*: `gamevm start` starts it by itself; from virt-manager run `sudo virsh net-start gamevm-host`. *... is part of the VM but not there*: plug the keyboard, mouse or disk in (or wake the wireless receiver) and start again. |
| `win11 is paused: it still owns the graphics card ...` | The VM was paused (virt-manager, `virsh suspend`). `gamevm stop` resumes it and shuts it down. |
| `cannot check whether Windows left ... hibernated` | `ntfs-3g.probe` is missing: `sudo pacman -S ntfs-3g`. gamevm no longer skips this check silently. |
| `... is hibernated` | Windows really hibernated (Fast Startup is back on, e.g. after a feature update). Boot it natively, run `1-prepare-windows.ps1` again, shut down. A Windows that was merely forced off is detected and allowed. |
| `<mount point> is busy: <program> uses it` | Close that program: a file manager window or terminal open on the Windows drive, Steam with a library there. KDE File Search (Baloo) is paused automatically while Windows runs, but better stop it indexing C: (`balooctl6 config remove includeFolders <mount point>/`; `gamevm doctor` warns about it). `sudo fuser -vm <mount point>` lists everything. If it says *an unknown program* and nothing has a file open there, check that the installed gamevm is current (`sudo gamevm doctor`): versions before 2026-11-18 lost a race against KDE, which looks at every mount point the instant one is unmounted and so mounted C: again through its automount. |
| `the GPU is still in use by: <program>(<pid>: sync_file (...))` | That program still holds a synchronisation handle (fence) created by the NVIDIA driver, often inherited from an app that has closed. Unloading the driver now would crash the kernel later, when the handle is released, so gamevm refuses. Close the program (or log out and in) and start again. |
| `the GPU is still in use by: ...` (after the desktop restarted) | Something reopened the graphics card after gamevm's check, e.g. a service that came back. `sudo gamevm holders` shows it; stop it and retry. |
| `your desktop compositor is running on the NVIDIA GPU and gamevm cannot move it` | KWin started without gamevm's GPU layout, which `gamevm-kwin.service` sets up at boot: setup was installed during this boot, or the service failed. **Reboot** (logging out is not enough). If it repeats: `sudo gamevm doctor` and `journalctl -b -u gamevm-kwin.service`. |
| `the graphics card is in use by ...` (before the desktop restarts) | Something gamevm does not close for you uses the graphics card: another user's program, a system service, one of your services using CUDA or `nvidia-smi`, or a program in an SSH/tmux session. Stop it and start again. |
| `the login screen is using the graphics card` | Log in to the desktop first, then start Windows. |
| `KWin did not come back within 25s` | Rare. The session is still logged in; log out and back in, then `sudo gamevm recover`. |
| `refusing to pass ... through: the host is using it` | Something mounted the Windows disk. Unmount it, or add the mount point to `disk.unmount`. |
| `... is <id>, expected <id>: PCI numbering changed` | A BIOS/hardware change moved devices. Find the new addresses with `lspci -nn` and fix `gpu.devices` / `disk.nvme`. |
| `disk.boot_disk ... is not there` | The SATA disk Windows starts from is unplugged, or its name changed. `ls -l /dev/disk/by-id/` shows the names; `gamevm discover` shows what gamevm would pick. |
| `... has serial ..., expected ...: this is not the Windows disk` | Another disk sits where the Windows disk was (disks swapped or replaced). Fix `disk.boot_disk` / `disk.nvme` and `disk.expect_serial`. |
| `disk.boot_disk ... is the disk Linux runs from` | `disk.boot_disk` points at the Linux disk. Windows needs a disk of its own; set it to the Windows disk. |
| `... has 4096-byte sectors: Windows cannot start from it in the VM` | A SATA disk in the VM always has 512-byte sectors, so the partitions of a disk with real 4096-byte sectors cannot be read there. Windows on such a disk can only be booted natively (docs/02, 2.5). |
| `only N of M hugepages could be allocated even after closing apps` | Windows' RAM is reserved as 2 MiB blocks that must each be physically contiguous, so free RAM can be too fragmented even when there is plenty of it. gamevm already reserves what it can before the desktop moves, then drops the file cache, compacts memory and fills the rest once apps are closed and the NVIDIA driver is unloaded. Close more programs (browsers, Steam) and start again, or set `vm.hugepages = false`: Windows then uses transparent huge pages, slightly slower but never refused. With `vm.hugepages_fallback = true` (default) gamevm does this by itself for one start when the huge pages cannot be reserved before the desktop moves. `vm.defrag_mode = 1` (set by the installer) keeps this rare. |
| `/dev/kvmfr0 is a regular file` | QEMU created it before kvmfr was loaded: `sudo rm /dev/kvmfr0 && sudo modprobe kvmfr`. |
| `Failed to open ... evdev` / keyboard missing | The receiver/keyboard was unplugged. Move the mouse to wake the receiver; check `ls /dev/input/by-id`. |
| `firmware has requested this device have a 1:1 IOMMU mapping` | Newer BIOS DMA protection: set *Kernel DMA Protection Indicator = Disabled* (docs/01). |

When any step fails, everything done so far is rolled back automatically.

## The host did not come back after the VM

```bash
sudo gamevm recover      # undoes recorded steps, reloads NVIDIA, resets CPU/IRQ isolation, restarts the login manager
```

- **"NVIDIA GPU did not re-initialise" / `RmInitAdapter failed` / `WPR2 already up`**: the GPU's
  firmware is stuck. This is a known nvidia-open issue after unclean guest stops, and only a host
  reboot fixes it. In single-GPU mode gamevm reboots automatically
  (`single_gpu.reboot_on_gpu_failure`). Avoid it by shutting Windows down from inside Windows or
  with `gamevm stop`, never `gamevm kill`.
- **iGPU mode, the main monitor stays black after Windows shut down** (the side monitor still shows Linux): gamevm
  keeps the desktop on the side monitor when the graphics card did not come back healthy, and tells you to reboot
  (`journalctl -t gamevm -b`). If the graphics card is fine (`nvidia-smi` works), bring the desktop back
  with `sudo gamevm desktop native`.
- **iGPU mode, the login screen is on the side monitor only:** you logged out while Windows ran.
  Log in: your session starts with the main monitor again.
- **iGPU mode, both monitors black:** press Ctrl+Alt+F3 for a text console, log in and run
  `sudo gamevm recover`; or log out and in. If KWin cannot start at all, the session ends and
  the login screen comes back.
- **Single-GPU mode, black screen after Windows shut down:** press Ctrl+Alt+F3 for a text console
  (if available) and run `sudo gamevm recover`, or SSH in from another device. If neither works,
  press the reset button: nothing is lost on the host side.
- **Never write to `/sys/bus/pci/drivers/nvidia/unbind` by hand.** On nvidia-open 595–615 that
  can deadlock until reboot (NVIDIA issue #1119). gamevm only ever *unloads* the module.

## Shutting down the host while Windows runs

`gamevm-shutdown.service` holds the host's shutdown until Windows has shut down. To check that it
did, look at the end of `/var/log/gamevm.log` after the next boot: it must show *the host is
shutting down: shutting win11 down first* followed by *win11 has shut down*, and Windows must not
report an unexpected shutdown (Event Viewer → System, event 41 or 6008).

- *did not shut down within 300s*: Windows needed longer, usually for updates. Raise
  `vm.host_shutdown_wait` (up to 840).
- Nothing in the log: the unit was not running. `sudo gamevm doctor` reports it; re-run
  `setup/install-host.sh`.
- `libvirt-guests.service` must stay disabled: it would try to suspend the VM at the same time.

## Windows drive problems

- `Unknown PCI header type 127` / the NVMe vanishes after a VM run: set `disk.d3cold_off = true`.
  If that does not help, boot `linux-cachyos-lts` (a 7.2.2+ regression was reported on AM5).
- Windows Boot Manager disappeared from GRUB: grub-mkconfig ran while the disk was in the VM
  (os-prober could not see it). Run `sudo grub-mkconfig -o /boot/grub/grub.cfg` with the VM off.
  The firmware boot menu (F8, F11 or F12 at power-on, depending on the board) always works.
- Windows on a SATA disk stops with the blue screen `INACCESSIBLE_BOOT_DEVICE` in the VM: its
  standard SATA driver is switched off for booting (Windows was installed with the SATA ports in
  RAID / Intel RST mode). Nothing is damaged. Boot Windows natively, run
  `1-prepare-windows.ps1` again and shut down (docs/02, 2.5).
- Windows on a SATA disk: the VM's firmware says *No bootable option or device was found*. The
  disk's EFI partition has no fallback loader (`\EFI\Boot\bootx64.efi`). Boot Windows natively
  and run `1-prepare-windows.ps1`: it checks for the file and offers to recreate it.

## Windows-side problems

- **The main monitor is black while Windows runs (iGPU mode):** the Looking Glass IDD made itself Windows'
  only monitor. Run `gamevm lg` on Linux: it switches that setting off, shows Windows in a window
  on the side monitor and mirrors it back onto the main monitor; closing the window leaves the main monitor as the only
  screen. By hand in Windows: *Win + P → PC screen only* (or *Second screen only*).
- **The Looking Glass window is empty** (grey, or only the Looking Glass logo) while the main monitor shows
  Windows: Windows is not drawing on the virtual monitor. `gamevm lg` normally switches the
  mirror on by itself and says why if it could not: nobody signed in to Windows, the guest agent
  not answering, the driver not installed (`C:\gamevm\2-inside-vm.ps1 -InstallLookingGlass`), or
  `looking_glass.auto_mirror = false`. By hand in Windows: *Win + P → Duplicate*. What gamevm
  asked Windows to do and what came of it is in `C:\gamevm\display-layout.log`.
- **Windows still duplicates its screen after the Looking Glass window was closed:** gamevm could
  not switch the mirror off (it says so in a notification). *Win + P → PC screen only*; or open
  and close `gamevm lg` once more, which takes a mirror it made itself over and ends it.
- **Linux on the main monitor with Windows in a window is not possible** with the main monitor cabled to the graphics card
  alone: while Windows runs, only Windows can drive that cable. It would take a second cable from
  the motherboard to the main monitor and `display.switch_inputs = true` (the `[display]` section of the
  config); everything Linux then shows on the main monitor, the Looking Glass window included, is drawn by
  the small iGPU.
- **Code 43 / no Resizable BAR in GPU-Z:** set `gpu.mmio64_mib = 65536`, then `gamevm define`.
- **No network the first time:** install `virtio-win-guest-tools.exe` from the virtio-win CD (E:).
- **Keyboard or mouse dead in Windows after it was unplugged (or the mouse stuck in Windows with
  no way to switch):** QEMU drops an input device that disappears. gamevm plugs it in again by
  itself a few seconds after it is back (`/var/log/gamevm.log`: *reconnected to win11*). If it did
  not: `gamevm input`. If the device has a different name now (another USB mode of the keyboard, a
  different receiver), set `input.keyboard` / `input.mouse` to the new name from
  `ls /dev/input/by-id` (`sudo gamevm probe-input` shows which node sends the keys).
- **`gamevm files`: "Windows does not answer on 10.77.0.2":** the sharing step of
  `2-inside-vm.ps1` has not run (`gamevm scripts`, then run it in Windows), or the host-only
  adapter has no `10.77.0.2` address (`ipconfig` in Windows; `sudo gamevm doctor` on Linux).
- **Dolphin keeps asking for the password of `smb://10.77.0.2`:** use the Windows user name the
  script printed (for a Microsoft account its e-mail address) and the account's password, not the
  PIN. An account without a password cannot sign in over the network.
- **KDE Connect does not find the other side:** it must be running on both; `gamevm share` makes
  Linux look for Windows on the private link and shows what it sees. In Windows, the firewall rules
  named *gamevm: KDE Connect ...* must exist (`2-inside-vm.ps1` creates them).
- **`gamevm share --pair` says Windows' clock is some minutes off:** the time zone set in Windows
  is not Linux' one (docs/03). Set it in Windows (*Settings → Time & language → Date & time*),
  then run `gamevm share --pair` again: it corrects the clock and pairs.
- **The clipboard stopped working, `gamevm share` says "not paired yet":** something answered a
  pairing request a second time (*Accept* on a request that was answered already drops the
  pairing). `gamevm share --pair` pairs again; leave the request it shows alone.
- **Mouse feels laggy:** make sure the vioinput driver is installed (Device Manager → Human
  Interface Devices → VirtIO Input), otherwise Windows uses an emulated PS/2 mouse.
- **Crackling audio:** raise `audio.latency_us` (e.g. 30000), then `gamevm define`.
- **No sound at all:** `findmnt /run/gamevm/pipewire/pipewire-0` must show a mount while Windows
  runs (the hook hands QEMU your PipeWire socket there), and `/var/log/gamevm.log` must not say
  *the VM will have no sound*. If you restarted PipeWire while Windows was running, Windows keeps
  the old socket: restart Windows. As a fallback, `audio.qemu_as_user = true` (then re-run
  `setup/install-host.sh`) runs QEMU as you, which needs none of this.
- **Looking Glass shows "waiting for host":** the client and the Windows IDD must be the **same
  build** (`looking_glass.build`). Check the tray icon in Windows. Also make sure the graphics card is not
  showing a physical monitor that Windows picked as its main display.

## Rolling back the whole setup

- `sudo ./setup/uninstall-host.sh` removes gamevm's hook, programs, udev rules, KWin GPU layout
  unit, initramfs drop-ins, qemu.conf block, fstab change, host-only network and ufw rules.
  Add `--looking-glass` to also remove the Looking Glass client and kvmfr module, and `--purge` to
  also delete the VM definition (with its UEFI variables and TPM state) and `/etc/gamevm`.
  Packages stay installed.
- The installer took a **snapper snapshot** first ("gamevm: before install-host"). Boot it from
  GRUB's snapshots menu, or restore it with btrfs-assistant. Note that this also rolls back
  `/var/lib/libvirt` (the VM's UEFI variables and TPM).

## Reaching Linux from Windows (single-GPU mode)

```bash
sudo pacman -S --needed openssh
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' | sudo tee /etc/ssh/sshd_config.d/20-gamevm.conf
sudo systemctl enable --now sshd
```

Then put your Windows OpenSSH public key in `~/.ssh/authorized_keys`, and from Windows run
`ssh <your Linux user>@10.77.0.1`. Don't bind sshd to 10.77.0.1 with `ListenAddress`: that address
only exists once libvirt has started the network, so sshd would fail at boot. ufw already blocks
SSH from everywhere except the VM's link, because the installer only opened `virbr-gamevm`.
