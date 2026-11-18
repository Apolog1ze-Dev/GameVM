# Contributing to gamevm

gamevm has run on exactly one PC so far. The most useful thing you can do is try it on yours and
say what happened.

## Reporting how it went on your hardware

Open an issue with:

- what `python3 gamevm/gamevm.py discover` prints,
- what `sudo gamevm doctor` prints (once installed),
- your distribution, desktop (and whether it is Wayland), kernel and NVIDIA driver version,
- for a failed start or stop: the lines of `/var/log/gamevm.log` from that attempt, and
  `journalctl -t gamevm -b`.

**Read those outputs before you post them.** They contain device names, and the names of your
keyboard and disks can include serial numbers. Replace anything you would rather keep to yourself.

## Changing the code

- `./tests/run.sh` must pass. The tests need no VM and no root; they fake Windows, libvirt and
  KDE Connect and use a made-up PC. Add a test for what you change.
- The window can be run with a made-up PC, which is also how the pictures in the guides are
  made: `GAMEVM_UI_DEMO=off|running|fresh python3 gamevm/gamevm.py ui`. Without a display:
  `QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software QT_FORCE_STDERR_LOGGING=1` and
  `--page NAME --shot FILE.png`.
- The hook (`gamevm.py hook`) runs as root inside libvirt. It must never call `virsh`, and every
  change it makes to the host is recorded so that it can be undone in reverse order.
- Nothing may guess about hardware. Where the graphics card or the Windows disk cannot be told
  without doubt, gamevm stops and says why (see `gamevm/gamevm_discover.py`).
- Text for people is plain: say what happened and what to do, name things the way a user sees
  them ("the graphics card", "the main monitor"), and do not put one PC's hardware into messages
  or guides.
- The scripts in `windows/` stay ASCII (Windows PowerShell 5 reads them without a byte order mark).

## Licence

By contributing you agree that your contribution is licensed under the GNU General Public License
v3.0, like the rest of the project.
