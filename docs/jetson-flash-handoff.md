# Handoff: flash the Jetson Orin Nano Super from Windows + WSL

For an agent helping on a teammate's Windows laptop. Team: Knight Hacks IX, project Farsight
(repo https://github.com/azizu06/farsight). Time matters: Devpost is due Sun Oct 11, 10:00 AM EDT.

## Goal
Get the Jetson Orin Nano Super Developer Kit booting **JetPack 7.2.1** from its **microSD card**, by
flashing it with **NVIDIA SDK Manager running in WSL2 Ubuntu**. Done = the Jetson boots to an Ubuntu
login on its own, with SSH installed. Then hand it back (last section).

## What already happened (do not retry these)
- Firmware (UEFI/QSPI) on the board is already **39.2.x**. A JetPack 6 SD image will not fit it.
- The **JetPack 7.2.1 USB ISO installer fails** at ~67%: late command
  `apt-get install --reinstall nvidia-l4t-bootloader` exits 100. Cause: the firmware reports the board
  as `jetson-orin-nx-devkit-16gb` (TNSPEC `3767-300-0005-R.1-1-1-...`) although the module is an
  Orin Nano 8GB (3767-0005). Known NVIDIA bug, no official fix:
  https://forums.developer.nvidia.com/t/jetpack-7-2-installation-exit-100-for-apt-get-install-nvidia-l4t-bootloader/383607
- A forum workaround (edit `/target/etc/nv_boot_control.conf` to `jetson-orin-nano-devkit`, then
  `dpkg --configure -a`) got the package installed, but the installer had already aborted, so the
  boot setup is incomplete: the UEFI Boot Manager's "SD Device" and "ubuntu" entries both bounce back,
  and the UEFI shell found nothing bootable. The SD card contents are therefore junk: **overwrite them**.
- A Mac (Apple Silicon) cannot run SDK Manager, which is why this is on Windows.

## Constraints
- SDK Manager supports native Ubuntu x86_64 hosts. **WSL2 is unofficial**; NVIDIA's own WSL guide
  exists (https://docs.nvidia.com/sdk-manager/2.0.0/wsl-systems/index.html) but forum reports show USB
  disconnects mid-flash. If it fails twice, stop: the fallback is the NVIDIA team at the event.
- Target storage: **SD card** only (WSL cannot flash NVMe).
- Install **Jetson Linux only**. Untick runtime components and SDKs; the Mac side installs them later.
- Laptop must be Intel/AMD (not Snapdragon), on mains power, sleep off, about 40 GB free.
- Never put passwords or account details in files, logs or chat.

## Phases (stop at any failed Check and look at the screen before continuing)

1. **WSL ready.** Admin PowerShell: `wsl --update` then `wsl -l -v`.
   Check: Ubuntu 22.04 or 24.04, VERSION 2 (else `wsl --set-version <distro> 2`).
2. **USB passthrough tool.** Admin PowerShell: `winget install usbipd`, reopen PowerShell as admin.
   Check: `usbipd --version` prints a version.
3. **SDK Manager.** Download the `.deb` from https://developer.nvidia.com/sdk-manager (free NVIDIA
   developer login, done by a human). In WSL:
   `sudo apt update && sudo apt install -y /mnt/c/Users/<windows-user>/Downloads/sdkmanager_*_amd64.deb`
   Check: `sdkmanager --ver` prints a version.
4. **Recovery mode (human, physical).** Jetson power off, USB installer stick removed, SD card in.
   Jumper the header pins labelled **FC REC** and **GND** (under the module edge). Power on (screen
   stays black). USB-C data cable from the Jetson's USB-C port to the laptop.
5. **Attach to WSL.** Admin PowerShell: `usbipd list`, find **0955:7523** (APX), note BUSID, then
   `usbipd bind --busid <BUSID>` and `usbipd attach --wsl --busid <BUSID> --auto-attach`.
   Keep that window open for the whole flash (it re-attaches when the Jetson re-enumerates).
   Check: `lsusb` in WSL shows `NVIDIA Corp. APX`.
6. **Flash.** In WSL run `sdkmanager` (GUI via WSLg on Windows 11; on Windows 10 with no GUI use
   `sdkmanager --cli` and follow its prompts). Select: Jetson, **Jetson Orin Nano Developer Kit
   (Super)**, **JetPack 7.2.1**, components **Jetson Linux** only. Flash dialog: OEM Configuration
   **Pre-Config** (the human types the username and password), storage **SD Card**. ~45-60 min.
   Check: "Installation completed successfully".
7. **First boot.** Remove the jumper, power-cycle. Check: Ubuntu login screen. Log in, join Wi-Fi,
   terminal: `sudo apt install -y openssh-server`.

## Troubleshooting
- Device not in `usbipd list`: not in recovery mode (re-seat jumper, power-cycle) or charge-only cable.
- Flash stalls or "device not found" mid-way: confirm the auto-attach window is still running and the
  BUSID is unchanged; retry the flash once.
- `bonding_masters` or other host networking errors: known WSL weakness; retry once, then stop.
- Use the systematic-debugging approach: read the exact error and SDK Manager's log
  (`~/.nvsdkm/logs/`) before changing anything.

## Hand back
Move the USB-C cable to the owner's Mac. The owner runs `ssh-copy-id <user>@192.168.55.1` once and
their own agent takes over (repo setup: `tools/bootstrap_jetson.sh`, `jetson/setup.sh` on branch
`1-jetson-stream`, PR https://github.com/azizu06/farsight/pull/20). Report back: success or the exact
failing step and error text.

## Suggested skills
- `superpowers:systematic-debugging` (or `diagnosing-bugs`) if any phase fails.
- `superpowers:verification-before-completion` before declaring the flash done.
