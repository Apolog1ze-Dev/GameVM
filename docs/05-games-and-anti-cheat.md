# 5. Which games work in the VM

The Xbox app, Gaming Services, the Microsoft Store and Game Pass licensing have **no VM check**.
Single-player games and multiplayer games without VM-hostile kernel anti-cheat run in the VM.

Some publishers' anti-cheat refuses to run in any virtual machine, or bans for it. gamevm does
**not** try to hide the VM (that breaks those publishers' terms, risks your account, and would also
turn off the Hyper-V enlightenments that make Windows fast in a VM). For those games, **boot the
same Windows natively** (firmware boot menu): the installed games are the same files, so
nothing needs re-downloading.

## Known to block VMs (play these on bare metal) — status October 2026

| Game / anti-cheat | Notes |
|---|---|
| Valorant, League of Legends (Riot Vanguard) | "not supported in virtual machine environments" (error VAN 138) |
| Call of Duty BO6 / BO7 / Warzone (Ricochet) | BO7 requires TPM 2.0 + Secure Boot verified by remote attestation, which a VM's TPM cannot pass |
| Battlefield 6 (EA Javelin) | Secure Boot mandatory; VMs listed as a cheating method |
| Destiny 2 (BattlEye) | VMs against the rules, bans reported |
| FACEIT | requires TPM, Secure Boot and VBS; no VMs |
| Many BattlEye / EAC titles | decided per game: Rainbow Six Siege, PUBG, Apex Legends, Sea of Thieves, Rust, The Finals, Helldivers 2 have been reported blocked |

## Expected to work

Most Microsoft first-party and single-player Game Pass titles have no kernel anti-cheat: Starfield,
Indiana Jones, Doom: The Dark Ages, Avowed, Microsoft Flight Simulator, Minecraft, Forza
single-player, and so on. VMDB (https://vmdb.it) is a small community compatibility database.

Your library includes **Warhammer 40,000: Darktide** (Easy Anti-Cheat): its VM status is
unconfirmed, so test it, and if it complains, play it on bare metal.

## Secure Boot / TPM for bare-metal anti-cheat

BO7, BF6 and Vanguard need **Secure Boot on** even on bare metal. Many Linux installs boot with it
off, because their boot loader is unsigned. Options: sign your boot chain with `sbctl` and leave
Secure Boot on, or switch it on in the BIOS only for those sessions. Inside the VM, gamevm can also
provide Secure Boot with Microsoft keys, but that does not make VM-blocking games work. To switch
it on:

```bash
sudo ./setup/install-host.sh --secure-boot      # creates the Microsoft-keyed variable store
SUDO_EDITOR=nano sudoedit /etc/gamevm/gamevm.toml   # [firmware] secure_boot = true
gamevm start --reset-nvram                      # once: rebuilds the VM's UEFI variables with the keys
```

Windows then boots through `\EFI\Boot\bootx64.efi`, which `1-prepare-windows.ps1` made sure
exists.
