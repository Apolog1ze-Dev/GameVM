# 3. Daily use

## The gamevm window

*gamevm* in the application launcher, or `gamevm ui`. It needs nothing beyond Plasma itself (it
is drawn by Qt's QML runtime, `qml6`), and as a Qt program it stays open when the desktop moves
between the graphics cards.

| Page | What it is for |
|---|---|
| **Home** | Whether Windows runs, and the one button for it (*Start Windows*, *Shut down Windows*, *Show Windows in a window*). **Needs you** holds only what is yours to decide or needs your password: a setup step that is left, an update of the installed gamevm, a newly plugged USB device (*Give it to Windows / Keep it on Linux*, with *Remember this*). **Taken care of** lists what gamevm did without asking: the checks, keyboard and mouse, the clipboard link (paired by itself the first time Windows runs), your apps. |
| **Sharing** | Files between the two. *Send to Windows*: drop files on the page; *Activity* shows each one going and where it arrived, and what came from Windows. *Windows' drives* opens them in the file manager (through Windows while it runs, as plain folders while it is off). The clipboard link's state. |
| **Resources** | What Windows gets: processor cores (select a core to move it), memory, which USB devices go to Windows, what happens to devices plugged in later, the network mode. *Save* asks for your password and applies at the next start of Windows; *Back to recommended* restores gamevm's choice for this hardware. Disks and graphics are shown, and are chosen when gamevm is installed. |
| **Set up** | Every setup step with who does it. gamevm's steps happen or are noticed by themselves (the monitor on the motherboard, the driver disc in Downloads, the restart); yours wait with one button. After *Restart now* the window opens again by itself and carries on. |
| **Settings** | How the switch behaves: the question before a start, reopening apps, bringing the desktop back, the keys that switch keyboard and mouse. |
| **Help** | The checks in full (they run by themselves when the window opens and every 15 minutes while Windows is off), the guides, the log, *Repair after a crash*. |

The window does the watching: a USB device plugged in while Windows runs is only asked about, and
the clipboard is only paired by itself, while the window is open. Starting and stopping Windows
does not need it. The installers still run in a terminal (they ask questions and the password).

## Commands

```bash
gamevm ui               # the window described above (--page setup opens on that page)
gamevm start            # start Windows (iGPU mode: lists the apps that will close/reopen, asks)
gamevm stop             # clean shutdown; the graphics card returns to Linux afterwards
gamevm status           # VM state, which driver owns the GPU/NVMe, hugepages, CPU isolation
gamevm lg               # Windows in a window (Looking Glass, iGPU mode)
gamevm holders          # what keeps the NVIDIA GPU busy (sudo for all users' processes)
gamevm bench            # benchmark Windows in the VM, compared with native runs (docs/06)
sudo gamevm desktop native|igpu   # move the Linux desktop by hand (Windows off): main and side monitor / side monitor only
gamevm usb attach 045e:02fe   # hot-plug a USB device (VID:PID from lsusb) into Windows
gamevm input            # plug the keyboard and mouse into Windows again (normally automatic)
gamevm files            # Windows' drives in the file manager (through Windows while it runs)
gamevm share            # clipboard and file sharing with the running Windows: status, setup (--pair)
gamevm scripts          # copy the current Windows scripts to C:\gamevm in the running VM
gamevm back             # bring the Linux desktop back to the main monitor (desktop.return = "ask")
gamevm kill             # force power-off (last resort)
sudo gamevm doctor      # full check
sudo gamevm recover     # restore the host if something went wrong (VM must be off)
```

## What happens when you start the VM

The libvirt hook (`/etc/libvirt/hooks/qemu.d/gamevm`, running as root) does this. It records each
step in `/run/gamevm/state.json` and undoes them in reverse order when Windows stops, or right away
if a step fails:

1. Checks that `gpu.devices` and `disk.nvme` are still the expected devices (IDs, serial), and
   that nothing would fail later, once the desktop has moved: the running kernel can still load
   vfio-pci and the NVIDIA driver (not after a kernel update without a reboot), the VM's networks
   run, and its keyboard, mouse and extra disk are there. `gamevm start` makes the same checks
   before it asks anything, and asks before it starts Windows without a keyboard.
2. Reserves the memory for Windows as 2 MiB hugepages. This is the step most likely to fall short, so it comes
   before anything else is touched: `gamevm start` then simply starts once more on transparent
   huge pages. If it falls short at every start on your PC, set `vm.hugepages = false` to skip
   the attempt.
3. Blocks host sleep while the VM runs.
4. Unmounts the Windows drive from Linux and refuses if anything else on it is mounted or busy, or if
   Windows left it hibernated. (A Windows that was forced off is not "hibernated": it just replays
   its journal on the next boot.)
5. *iGPU mode:* first checks that nothing it cannot close or restart uses the graphics card: another
   user's program (or the login screen), a system service not listed in `gpu.host_services`, one
   of your user services that uses CUDA, `nvidia-smi` or the GPU's I2C bus, or one of your
   programs outside an app (an SSH or tmux session, a script). If something does, the start is
   refused with its name before the desktop is touched. (A CUDA job or a monitor such as nvtop
   that you started in a terminal is closed instead, and listed under *Will close*.) Then it moves
   the Linux desktop to the side monitor: KWin restarts on the iGPU alone (your session stays logged in),
   background services of your session that still use the graphics card are restarted on the iGPU, apps
   that still use it are closed, and your chosen apps reopen on the side monitor (see below).
   *Single-GPU mode:* stops the login manager and your graphical session instead (the host keeps
   running headless; your PipeWire is restarted so Windows has sound).
6. Unloads the NVIDIA driver with `modprobe -r`. It never force-unbinds it, which can deadlock
   nvidia-open. In iGPU mode, if something still uses the GPU at this point, the start is refused
   and everything is rolled back (the desktop restarts back onto the graphics card). In single-GPU mode,
   remaining programs that hold the GPU are terminated, system services included (never the core
   ones: systemd, logind, D-Bus, polkit, libvirt, sshd). List GPU services in `gpu.host_services`
   to have them stopped and started again cleanly instead.
7. Hands QEMU your PipeWire socket for sound: only the socket, bind-mounted into
   `/run/gamevm/pipewire`, so your session's own directory stays private.
8. Confines all host processes, kernel workqueues and IRQs to cores 0 and 7. Windows gets cores
   1–6 for itself, without deep idle states, and ananicy-cpp is paused.

Then libvirt binds the GPU and NVMe to vfio-pci and boots Windows. On shutdown, whether from
Linux (`gamevm stop`) or from inside Windows, everything returns: the HDMI audio function first,
then the NVIDIA driver, then the desktop. In iGPU mode gamevm checks that the graphics card came back
healthy, then restarts KWin on it: the main monitor shows Linux again and your chosen apps reopen. If the
card did not come back (a rare NVIDIA driver firmware issue), the desktop stays on the side monitor, and
`gamevm stop` and the journal tell you to reboot. In single-GPU mode the login manager starts again (with autologin you are
back at your desktop). The configuration in effect at start is
saved with the journal, so editing `/etc/gamevm/gamevm.toml` while Windows runs cannot break the
restore.

## Stopping Windows, and shutting down the PC

- **`gamevm stop`** and *Start → Power → Shut down* inside Windows end the same way: the host gets
  everything back. `gamevm stop` also resumes a paused VM first, and repeats its request every 30
  seconds in case Windows was still booting.
- **If `gamevm stop` gives up after 3 minutes**, look at Windows' screen. If it is installing
  updates or still closing programs, leave it and run `gamevm stop` again to keep waiting: asking
  again interrupts nothing. Use `gamevm kill` only when Windows is frozen. It is like pulling the
  plug, and the graphics card may need a host reboot afterwards.
- **Shutting down or rebooting Linux while Windows runs** (from the menu, with `systemctl
  poweroff`, or with the PC's power button) shuts Windows down first. The host waits for it, up to
  `vm.host_shutdown_wait` seconds (300 by default), and only then goes on. The Linux desktop is
  already gone meanwhile; the main monitor shows Windows shutting down. This is also the safe way out if
  the keyboard and mouse are stuck in a frozen Windows: press the power button once and let KDE's
  shutdown prompt on the side monitor run out.
- **Notifications.** If a start is refused, or the graphics card did not come back after Windows shut
  down, a desktop notification says so, also when no terminal is open (Windows shut down from
  inside, the launcher). The details are in `/var/log/gamevm.log`.

## Seeing Windows: the main monitor and Looking Glass (iGPU mode)

While Windows runs, **the main monitor always shows Windows**: its only cable comes from the graphics card, and
the graphics card belongs to Windows until Windows shuts down. Looking Glass does not change that and
cannot give the main monitor back to Linux. What it adds is a second view: `gamevm lg` shows the same
Windows desktop in a window on the side monitor, the monitor Linux keeps. Use it to look at or click
something in Windows without turning to the main monitor; play on the main monitor.

How the picture gets into the window: the Looking Glass driver in Windows (the "IDD") adds a
virtual monitor, and the window shows whatever Windows draws on it.

- **`gamevm lg` sets this up by itself** (`looking_glass.auto_mirror`). When the window opens it
  makes Windows mirror its screen onto the virtual monitor (what *Win + P → Duplicate* does) and
  puts the main monitor back on its highest refresh rate; when the window closes, Windows goes back to the
  main monitor alone, which is best for G-SYNC/VRR and costs nothing. This takes a few seconds each way
  and the main monitor may blink once.
- If the virtual monitor is already part of a layout **you** chose in Windows (Duplicate or
  Extend), gamevm leaves it alone, also when the window closes.
- Someone has to be signed in to Windows for it (the layout belongs to the signed-in user).
- The driver's own *Make LG the only monitor* setting would turn the main monitor black; `2-inside-vm.ps1`
  and `gamevm lg` switch it off.
- For a sharp window, the virtual monitor needs a mode with the main monitor's resolution and exact
  refresh rate (2560×1440 at 239.970 Hz, for example): Looking Glass tray icon in Windows → *Open
  configuration*.

`gamevm lg` opens a **maximised window with a title bar** on the side monitor, not a full-screen one: the
side monitor is the only monitor Linux has while Windows runs, so the Linux desktop stays reachable.

| To | Do |
|---|---|
| Use Windows in the window | Have the keyboard and mouse on the Linux side (both Ctrl), then move the mouse into the window and click. |
| Capture the mouse for a game, and let it go again | Tap **Right Ctrl**. |
| Close the window | **Right Ctrl + Q**, or its close button. Windows keeps running on the main monitor. |
| Full screen and back | **Right Ctrl + F**. |
| See every shortcut | Hold **Right Ctrl**. |

`gamevm lg` shows these keys in a notification each time. The key is `looking_glass.escape_key`
(`KEY_RIGHTCTRL`; Looking Glass' own default, Scroll Lock, is missing on many keyboards), and
`looking_glass.fullscreen = true` opens full screen. These settings win over
`~/.config/looking-glass/client.ini`. Resizing the window does not change Windows' resolution.

## Keyboard, mouse, controllers, sound

- **Both Ctrl keys** switch the keyboard and mouse between Windows and Linux. When the VM starts,
  input goes to Windows. If the toggle does nothing, run `sudo gamevm probe-input` to find the
  event node that emits Ctrl, and set `input.keyboard` to it.
- **Unplugged the keyboard or mouse while Windows runs?** Plug it in again: within about three
  seconds gamevm connects it to Windows again by itself and says so in a notification
  (`input.reattach`). Keyboard and mouse are with Windows afterwards, also if you were on the
  Linux side: press both Ctrl to come back. By hand: `gamevm input`, or *Reconnect keyboard and
  mouse to Windows* in the launcher (reachable with the keyboard alone: Alt+Space, type
  "reconnect").
- **With Looking Glass:** press both Ctrl once so the keyboard and mouse are on the Linux side,
  then use the Looking Glass window (Right Ctrl captures the mouse and lets it go; see above).
- **Gamepads and other USB devices:** hand them to Windows one by one (the *Resources* page, or
  `usb.devices`; `gamevm usb attach 045e:02fe` for one session, with the ID from `lsusb`), or
  give Windows a whole USB controller with everything plugged into it (`usb.controllers`, a PCI
  address; `lsusb -t` shows which ports belong to which controller). The controller must be alone
  in its IOMMU group, which `sudo gamevm doctor` checks.
- **Sound** goes through Linux's PipeWire, with about 20 ms of buffering. In Windows the emulated
  card is called **"Speakers (High Definition Audio Device)"** (output) and **"Line In (High
  Definition Audio Device)"** (microphone); make them Windows' default devices. On Linux the VM
  shows up as a `qemu-system-x86_64` stream that you can route and mix like any other program.
- **A USB headset or sound card natively in Windows** (for its own Windows software): hand the
  device to Windows as above. Linux has no access to it meanwhile. If Windows then needs no sound
  through Linux at all, set `audio.backend = "none"`.

## Linux apps when Windows starts and stops (iGPU mode)

While Windows is off, Linux runs on the graphics card as it always did: games, Steam and Proton go straight
to the main monitor at its full refresh rate (with VRR if *Adaptive sync* is *Automatic*). Nothing needs `gpu-run`.

Each start and each stop of Windows restarts KWin, the compositor (KWin cannot let go of the GPU
it draws with while it runs). Your session stays logged in. What happens to apps:

| App | When KWin restarts |
|---|---|
| Konsole, Dolphin, Kate and other widget-based KDE apps | stay open (Qt reconnects) |
| Plasma panel, wallpaper, KRunner | gone for a few seconds, then back (open notifications are lost) |
| Background services of your session that used the graphics card (kded6, plasmazones, ...) | restarted by gamevm on the right GPU |
| Windowless helpers of an app (an audio tool's PipeWire filters, say) | closed and reopened with the app |
| GPU sensors of KDE System Monitor widgets (ksystemstats, which runs `nvidia-smi`) | paused for a moment while the NVIDIA driver unloads; show no graphics card while Windows runs, and get it back afterwards |
| Qt Quick KDE apps that draw with the graphics card (System Settings, Discover, System Monitor) | closed by gamevm |
| Electron and Chromium apps (Discord, VS Code, Chrome), Firefox, GTK apps, Steam, games, any X11 app | close |
| A program using the graphics card that you started in a terminal (nvtop, a game) | closed by gamevm; the terminal stays |
| PipeWire, D-Bus, system services, other users' programs, your programs outside an app (SSH, tmux, scripts) | unaffected, unless they use the graphics card: then the start is refused and names them (gamevm never kills them behind your back). Services listed in `gpu.host_services` (nvidia-persistenced, nvidia-powerd) are stopped while Windows runs and started again afterwards |

A restart cannot keep an app that closes alive. gamevm keeps what a restart allows:

- **Every app that closes is reopened** (`desktop.relaunch_all = true`), except the desktop file
  IDs in `desktop.relaunch_exclude`. The ID is the `.desktop` file name without the extension
  (`ls /usr/share/applications ~/.local/share/applications`). With `relaunch_all = false` only
  the apps started at login and those in `desktop.relaunch` come back.
- **Apps are asked to quit first** and get `desktop.quit_timeout` seconds (5), so they leave as
  when you quit them: their session and settings are written, and they do not start with a "did
  not shut down correctly" message. Firefox-based browsers are not asked: they restore their tabs
  after an unexpected end, but not after a normal one, unless *Open previous windows and tabs* is
  on. Chromium browsers are reopened with `--restore-last-session`.
- **Windows go back** to the monitor, place and size they had before Windows started, also the
  KDE apps that stayed open and were moved to the side monitor meanwhile (`desktop.restore_windows`).
- **One restart instead of two** with `desktop.return = "ask"`: when Windows stops, Linux stays on
  the side monitor and a notification offers to bring the main monitor back; `gamevm back` (or the launcher's
  *Bring the Linux desktop back to the main monitor*) does it later, and asks for your password.
  Until then Linux does not use the graphics card, and starting Windows again needs no restart. The
  default, `"auto"`, moves back at once, so Linux has the graphics card whenever Windows is off.

What no restart can keep is whatever an app does not save itself: a text you were typing, a
download, a voice call (rejoin once the app is back), a task running in an app. Browsers set to
forget their tabs forget them. `gamevm start` lists what will close and
reopen before it asks. Apps you quit yourself while Windows runs are not reopened when it stops.

While Windows runs, apps on the side monitor use the iGPU. Linux games still run there, slowly.
`gamevm stop` brings the graphics card back.

> **Plasma 6.8** (due 2026-10-14) reworks how KWin handles several GPUs. After that update, run
> `sudo gamevm doctor` and try one `gamevm start` / `gamevm stop` before you rely on it.

## Clipboard and files between Linux and Windows

While Windows runs, its disks belong to Windows alone: Linux cannot mount C: or the shared HDD at
the same time (two systems on one NTFS volume would destroy it), and KDE's file search only knows
what was on them when it last looked. What connects the two is the private link (Linux
`10.77.0.1`, Windows `10.77.0.2`). Set up once:

1. Get the current scripts into Windows: `gamevm scripts` copies them to `C:\gamevm` while Windows
   runs.
2. In Windows, in an administrator PowerShell: `C:\gamevm\2-inside-vm.ps1`. Its sharing step
   shares every NTFS drive, opens the Windows firewall for file sharing and KDE Connect on the
   private adapter and for the Linux host only, and installs KDE Connect.
3. Open KDE Connect in Windows once (afterwards it starts with Windows). On Linux run
   `gamevm share --pair`: it pairs the two by itself, over the private link and with this
   Windows only. For half a minute KDE Connect shows a *Pairing request* on the Linux screen:
   leave it alone. It is answered already, and *Accept* on it now would undo the pairing (a quirk
   of KDE Connect; gamevm watches for it and pairs once more).

Both clocks have to agree for the pairing (KDE Connect refuses when they differ by more than 30
minutes). Windows gets Linux' local time at every start and reads it in its own time zone, so
Windows needs **the same time zone as Linux**; `gamevm share` says so if it does not. Windows'
*Set time zone automatically* can move the zone when a VPN is on: switch it off if that happens.
After every Windows start the two find each other again by themselves, about a minute after you
sign in.

Afterwards:

| What | How |
|---|---|
| Windows' drives from Linux | `gamevm files`, or *Open Windows' drives* in the launcher. Dolphin opens `smb://10.77.0.2/`; sign in with your Windows user name and password (Dolphin can remember them). When Windows is off, the same command opens the folders Linux mounts those drives at. |
| Text clipboard, both ways | Copy on one side, paste on the other (KDE Connect; it has to run on both). |
| Send a file to the other side | **Linux:** the *Sharing* page of the gamevm window: drop files on it (or *Choose files*), and it lists them as they arrive in Windows' Downloads folder. Dolphin's right-click > *Send to device via KDE Connect* does the same without the list. **Windows:** right-click a file > *Send to* > *Linux (gamevm)*, or *Send to Linux* in the Start menu (a small window to drop files on). gamevm adds both to Windows by itself; they arrive in the Downloads folder on Linux and show up on the *Sharing* page. |
| Check | `gamevm share` shows what is reachable and paired, and makes Linux look for Windows again. |

Limits: files cannot be copied *as clipboard content* between the two, only sent or fetched as
above. A Windows account without a password cannot be used for the shared drives. Linux reaching
into a Windows that is busy with a game costs that game some disk speed, like any copy job.

## A second disk for Windows

Windows only gets the drive it boots from. If your game libraries are on another disk, hand that
disk over too (`disk.extra`, docs/02): Windows then has it under the drive letter it has natively,
and Linux unmounts it first and mounts it again when Windows has shut down. If a Linux program has
a file open on it, the start is refused and names the program. The disk is hidden from the file
manager's device list meanwhile, so it cannot be mounted by accident.

Windows remembers drive letters per partition. A virtual CD drive that grabbed a letter while the
disk was missing would push the disk to another one, so `2-inside-vm.ps1` moves virtual CD drives
to the end of the alphabet.

## Networking

- Windows has its own address on your LAN (`network.mode = "macvtap"`, on the network card with
  the default route): good for Xbox party chat and NAT type. Over Wi-Fi that is not possible, and
  the installer chooses `"nat"` (through Linux) instead.
- Linux ↔ Windows: a private link, Linux `10.77.0.1` ↔ Windows `10.77.0.2` (SSH, SMB, RDP,
  Moonlight). Through macvtap the host cannot reach the guest, so use these addresses.
  `2-inside-vm.ps1` marks that link as a private network. Shares and Remote Desktop (Windows Pro
  only) still have to be switched on in Windows itself.

## Single-GPU mode

`gamevm start` warns first. Then your Linux desktop closes: save your work. Linux keeps running
headless (downloads, servers, and so on). Shut Windows down to get the desktop back. To reach Linux
from Windows meanwhile, use SSH to `10.77.0.1` (enable `sshd`, see docs/04-troubleshooting.md).

## Editing the configuration

```bash
SUDO_EDITOR=nano sudoedit /etc/gamevm/gamevm.toml   # or: kate /etc/gamevm/gamevm.toml (asks for your password on save)
gamevm doctor && gamevm define      # gamevm start also re-defines the VM from the config
```

Do not edit the VM in virt-manager: `gamevm start` regenerates the definition from the config.
Never change `vm.uuid`: it is Windows' machine identity and the key of its TPM state.
