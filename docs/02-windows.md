# 2. Preparing your existing Windows

gamevm boots the Windows 11 that is already installed on your PC. The VM gets the whole NVMe
drive, so Windows runs on its own NVMe driver at native speed, DirectStorage keeps its fast path,
your games are already there, and you can still boot the same Windows natively from the firmware
boot menu at any time.

The same installation now runs on two "machines": your real board and the VM. That is fine, with
these rules:

| Rule | Why |
|---|---|
| **Fast Startup and hibernation stay off** | A hibernated Windows resumed on different (virtual) hardware can corrupt the disk. gamevm refuses to start if the Windows volume is hibernated. |
| **Sign in with your password in the VM** | The Windows Hello PIN is sealed in the motherboard's TPM; the VM has its own (software) TPM. Never use "Set up my PIN" / "I forgot my PIN" in the VM: that wipes the PIN of the natively booted Windows too. |
| **No BitLocker / device encryption** | It would bind the disk to one of the two TPMs and ask for the recovery key in the other. |
| **Never both at once** | Linux must not mount the Windows drive while the VM runs. gamevm unmounts it and hides the disk from the file manager. |
| **Shut Windows down, don't force it off** | A forced power-off can leave the graphics card needing a restart of the PC (an NVIDIA driver firmware issue), and leaves the file system unclean. |

## 2.1 In Windows, booted natively (once)

The gamevm window shows this as the step *Run one script in Windows*.

1. Boot Windows the way you always did (firmware boot menu, or your boot loader's Windows entry).
2. Get the project's `windows` folder into Windows: download the project there, or use the copy
   the installer placed in a `gamevm-windows` folder on a Windows drive that Linux has mounted.
3. In an **administrator** PowerShell:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass -Force
   .\1-prepare-windows.ps1            # add -CreateRescueAdmin for a spare local administrator
   ```
   It turns off Fast Startup and hibernation, re-enables password sign-in, blocks automatic device
   encryption, checks the EFI fallback loader the VM boots, reports activation and the clock mode,
   installs the virtio drivers if their installer lies next to the script, and copies the scripts
   to `C:\gamevm`.
4. If it says the clock is kept in UTC, set `clock = "utc"` under `[vm]` in
   `/etc/gamevm/gamevm.toml`.
5. **Shut down** normally.

Back in Linux, `sudo gamevm doctor` must now report the Windows volume as clean.

Windows and Linux should be set to the **same time zone**: Windows gets Linux' local time at every
start and reads it in its own zone. A different zone makes its clock wrong by whole hours, which
breaks the clipboard link and can break sign-ins. Windows' *Set time zone automatically* can move
the zone when a VPN is on; switch it off if that happens.

## 2.2 First start in the VM

Start Windows from the gamevm window, or:

```bash
gamevm start
```

- iGPU mode: the Linux desktop moves to the side monitor (KWin restarts; see docs/03) and Windows
  appears on the main monitor. Press **both Ctrl keys** to move the keyboard and mouse between
  Linux and Windows.
- The first start is slower: Windows installs the new (virtual) chipset devices and may restart
  once.
- Sign in with your **password**.
- In an administrator PowerShell inside Windows:
  ```powershell
  Set-ExecutionPolicy -Scope Process Bypass -Force
  C:\gamevm\2-inside-vm.ps1
  ```
  It installs the virtio drivers and the guest agent from the driver disc gamevm attaches, offers
  the Looking Glass display driver (iGPU mode; it asks first), gives the private network adapter
  its fixed address, sets up sharing with Linux over it (drive shares, firewall rules, KDE
  Connect, the *Send to Linux* entry: docs/03), turns on MSI interrupts for the graphics card,
  sets the High performance power plan, and checks the graphics driver, activation, the TPM and
  Gaming Services.
- **Activation:** gamevm copies your board's identity (SMBIOS data and UUID) into the VM, so
  Windows usually stays activated. If it does not: *Settings → System → Activation → Troubleshoot
  → "I changed hardware on this device recently"*, with the Microsoft account your licence is
  linked to.
- **Xbox app:** sign in again if asked. If a game says it is not available, use *Xbox app →
  profile → Support → Gaming Services Repair*.

What gamevm changes in Windows also applies when you boot it natively: Fast Startup off, the
High performance power plan, the drive shares (reachable only where Windows' file sharing is on,
and only with your password), the firewall rules for the private link.

## 2.3 More disks for Windows

Windows only sees the drive it boots from. A second disk with your game libraries has to be
handed over too, as a whole, and Linux cannot use it meanwhile:

```toml
[disk]
extra = ["/dev/disk/by-id/ata-YOUR_DISK"]      # ls -l /dev/disk/by-id/
unmount = ["/mnt/windows", "/mnt/games"]        # where Linux mounts partitions of these disks
```

`python3 gamevm/gamevm.py discover` lists disks formatted by Windows that it left alone.

## 2.4 A different Windows

gamevm runs whatever Windows is on the drive it hands over. For games in 2026:

- **Windows 11 Home or Pro, a current release.**
- Not Windows 10: NVIDIA's Game Ready drivers for it end in October 2026.
- Not LTSC / IoT LTSC: no Microsoft Store, so no Xbox app or Gaming Services.
- Not "debloated" images: they remove Xbox components and come from an untrusted source.

To install a **separate** fresh Windows instead of using your current one:

1. Put another NVMe drive in. Never use the drive of your current Windows for this: installing
   erases it.
2. In `/etc/gamevm/gamevm.toml`, set `disk.nvme` to the new drive's PCI address, and
   `disk.expect_id` and `disk.expect_serial` to its values (`lspci -nn -s <address>`,
   `cat /sys/class/nvme/nvme*/serial`). The hook refuses any disk that does not match them.
3. Download the Windows image from Microsoft and set `vm.install_iso` to its path. Windows 11
   Setup wants Secure Boot: set `firmware.secure_boot = true` after
   `sudo ./setup/install-host.sh --secure-boot`.
4. `gamevm start --reset-nvram`, install Windows (load drivers from the driver disc if Setup asks
   for a network driver), then empty `vm.install_iso` again.

Microsoft's licence terms count this as a second installation that needs its own licence.
