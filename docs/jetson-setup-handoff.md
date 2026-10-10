# Jetson Orin Nano setup handoff

Updated: October 10, 2026, America/New_York.

This is the latest session handoff and supersedes the setup instructions in `docs/jetson-flash-handoff.md`. That earlier document remains useful as reported history.

## Goal and current blocker

Finish setting up the Jetson Orin Nano 8GB developer kit with JetPack 7.2.1 on its microSD card. Success means it boots Ubuntu from the SD card, has a usable account and networking, and accepts SSH connections.

**Setup is not complete. Two Flash clicks have failed during prechecks; no actual storage flash progress or completion has been reported.** Recovery detection and APX driver setup previously worked. First click stopped after initial Windows USB binding. The retry passed that stage but the board-information transfer failed with **“ERROR: might be timeout in USB write”**, followed by **“Reading board information failed”**, **“Parsing boardid failed”**, and **“Bootrom status check failed.”** Most recently, after the user dismissed the error, APX disappeared from the connected Windows device list and from WSL. The last-known USB port is 2-10.

**Latest physical state reported by the user:** Jetson powered, display black, USB-C connected. Fresh host checks after a USB-only reconnect still found no connected APX device. Earlier, SDK Manager confirmed recovery mode and the user selected the developer-kit model **P3767-0005 module / P3768-0000 carrier**. The precise software command used in that successful recovery-entry attempt was not explicitly reported. A black display alone does not establish the current recovery state.

## Hardware and authorization

- Jetson Orin Nano 8GB developer kit; target storage is microSD.
- Barrel-jack power supply, USB-C cable to the Windows laptop, external display, and keyboard are available. Other USB peripherals are connected.
- A SanDisk USB stick containing the Jetson ISO is inserted into the Jetson.
- The user has no jumper wire and reported having no paper clip. No recovery pins have been connected.
- The user explicitly authorized erasing the SD card. Do not ask for that permission again unless the target changes.
- Desired preconfigured username in SDK Manager: `farsight`. The user entered a password locally; its value is unknown and must not be requested in chat or logged.

## Verified laptop / SDK Manager state

- Windows 11 Pro, x86_64.
- WSL2 distribution: `Ubuntu-22.04`, Ubuntu 22.04.5 LTS.
- Initially approximately 731 GB free on C: and 933 GB free inside WSL; sufficient space was available.
- USBIPD-Win is installed at `C:\Program Files\usbipd-win\usbipd.exe`.
- NVIDIA SDK Manager 2.4.1.13536 is installed at:
  - `C:\Program Files\NVIDIA Corporation\SDK Manager\NvSDKManagerGUI.exe`
  - `C:\Program Files\NVIDIA Corporation\SDK Manager\NvSDKManager.exe`
- Installer and installed GUI Authenticode signatures were verified as valid and signed by NVIDIA Corporation.
- The user signed in and reached SDK Manager's flash dialog.

### Selected configuration

- Product: Jetson.
- Target hardware: Jetson Orin Nano modules / Orin Nano 8GB in the flash dialog.
- JetPack: 7.2.1.
- Installation method: Direct Flash.
- Host development and additional SDKs: unchecked.
- Components: Jetson Linux Image and Flash Jetson Linux only. Runtime and SDK component groups were deselected.
- Downloads completed. SDK Manager built the OS image and reached the flash preparation dialog.
- Recovery setup: **Manual Setup – Jetson Orin Nano 8GB**.
- OEM Configuration: **Pre-Config**.
- New username: `farsight`.
- Storage Device: **SD Card**.
- Earlier detection status: **Could not detect a board**, then **APX Driver Not Found**. Before the failed prechecks, the correct developer-kit model was selected on port **2-10**, and Flash was enabled. Current detection is absent.

Download folder shown by SDK Manager: `C:\Users\Mehdi\Downloads\nvidia\sdkm_downloads`.

SDK install folder shown: `C:\ProgramData\NVIDIA Corporation\SDKs`.

### Host launch problem already fixed

Initially SDK Manager reported that PowerShell was unavailable. PowerShell was installed; SDK Manager's log showed that its process could not resolve `powershell`. An inspected process environment had `PATHEXT=.CPL`, excluding executable extensions. The app was relaunched with the Windows machine/user PATH and machine PATHEXT, after which the new log no longer showed the PowerShell error and successfully obtained the system boot time.

If that error recurs, close SDK Manager normally, then run this in Windows PowerShell:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
$env:PATHEXT = [Environment]::GetEnvironmentVariable('PATHEXT', 'Machine')
Start-Process -FilePath 'C:\Program Files\NVIDIA Corporation\SDK Manager\NvSDKManagerGUI.exe' -WorkingDirectory 'C:\Program Files\NVIDIA Corporation\SDK Manager'
```

These changes affect the launched process, not persistent system settings. The screen-resolution warning is advisory; clicking Yes allowed the GUI to continue.

For part of the session, this agent's WSL environment could not execute Windows `.exe` files (`Exec format error`); `/proc/sys/fs/binfmt_misc` lacked WSLInterop. That temporary host execution limitation subsequently resolved. The final Windows USB checks ran successfully and independently confirmed APX was absent.

SDK Manager host logs are under `C:\ProgramData\NVIDIA Corporation\SDK Manager`. Inspect only relevant diagnostics; logs may contain account-related information.

## What we observed on the Jetson

1. Initially the board was off. After the user powered it, it reached UEFI.
2. Early Windows `usbipd list` checks showed no NVIDIA device. These checks happened before anyone entered physical recovery mode.
3. The Jetson's UEFI Boot Manager listed:
   - `UEFI USB SanDisk 3.2Gen1`;
   - `Ubuntu`;
   - `UEFI SD Device`;
   - network boot options and UEFI Shell.
4. The user selected both `Ubuntu` and `UEFI SD Device`. Both returned to Boot Manager without starting Linux.
5. The user booted the SanDisk USB. A firmware-update confirmation appeared, and the user pressed **Y**. The next reported screen was GRUB; actual firmware version/update completion was not independently read afterward.
6. Top-level GRUB choices were:
   - `Install Jetson ISO r39.2.1`;
   - `Boot from next volume`;
   - `UEFI Firmware Settings`.
7. Pressing **e** on the first item revealed that it contains a submenu with:
   - Install on NVMe;
   - Install on USB;
   - Install on eMMC (for AGX Orin);
   - Install on microSD (for AGX Orin, Orin Nano/NX);
   - **Boot Into Rescue Shell**;
   - Check Disc for Defects.
8. No boot text edits were requested or reported. The user returned to the menu and selected Boot Into Rescue Shell.
9. Rescue showed **“Press Enter for maintenance, or Ctrl+D to continue.”** The user pressed **Ctrl+D**, continued booting, and the screen went black. They never reached a shell and never ran `reboot --force forced-recovery`.

The rescue entry's photographed kernel command included `boot-live-env=yes systemd.unit=rescue.target`. Preserve these if inspecting or editing that entry later.

## Immediate next steps: resume here — APX no longer enumerates

The user questioned whether another full rescue/recovery cycle would work; there is no guarantee. After dismissing the second error popup, they reported the board was no longer detected. Fresh diagnostics confirmed:

- Windows `usbipd list`: **no connected NVIDIA/APX device**; only a persisted APX binding remains.
- Windows `Get-PnpDevice -PresentOnly` filtered for vendor `0955`: no device.
- WSL `lsusb`: only the Linux USB root hubs, no APX.
- SDK Manager log records removal of **USBIP Shared Device**, with no later NVIDIA addition in the inspected region.

This is not merely a stale SDK Manager display. The reason for disappearance is not established. Do not claim a recovery command or further reset is guaranteed to resolve the transfer timeout.

The user unplugged/reconnected USB-C at the laptop with barrel power left connected. Fresh checks afterward still show **no connected APX/NVIDIA device** in Windows PnP, Windows `usbipd list`, or WSL `lsusb`. This USB-only reconnect experiment is complete and did not restore detection.

The next attempt requires a board reset and a fresh Force Recovery entry before a flash host can communicate. Given the preceding WSL transfer timeout and the user's concern about repeated rescue cycles, the recommended handoff is a **native Ubuntu flash host**, a known USB data cable, and a suitable jumper for physical recovery. A native host removes the Windows/WSL USB passthrough layer, but does not guarantee that any separate cable/device issue is resolved. No further reset or flash attempt has been performed after the failed USB-only reconnect.

## Previous USB-write timeout (history)

The proposed single retry was performed. It passed the first-bind branch, then `nvsdkmanager_flash.sh --check-all` attempted to read the module EEPROM through the Tegra recovery protocol. The initial RAM/bootstrap USB transfer timed out after approximately 10 seconds. No board-information read succeeded, and the actual storage flash has not been reported to start.

Sanitized diagnostic messages from the latest SDK Manager log:

```text
ERROR: might be timeout in USB write.
Error: Return value 3
--- Error: Reading board information failed.
Error: Parsing boardid failed.
Bootrom status check failed.
```

Checks at the time of this timeout showed **2-10 / 0955:7523 / APX / Attached** in `usbipd list` and NVIDIA APX in WSL `lsusb`. Enumeration worked then; sustained recovery-protocol transfer failed. These errors do not establish a damaged EEPROM or faulty cable. APX subsequently disappeared, as described in the current-state section above.

SDK Manager requested a complete reset and fresh recovery entry after the first bind. That full sequence has not been confirmed. The USB-only reconnect was tried later and failed to restore detection. The current recommendation is to hand off to a native Ubuntu flash host with a known data cable and physical recovery jumper, rather than repeat identical WSL attempts.

No complete power/reset/re-entry cycle after the initial host USB binding has yet been confirmed. The software rescue entry previously made recovery detection possible, but the user did not explicitly report every command entered.

## Previous first-bind precheck failure (history)

The first Flash click ran the initial Windows USB binding/attachment, then SDK Manager deliberately returned the first-bind reset error. Fresh diagnostics after that popup:

- Windows `usbipd list`: bus **2-10**, **0955:7523 APX**, state **Attached**.
- Windows PnP: status **OK**, **USBIP Shared Device**.
- WSL `lsusb`: **0955:7523 NVIDIA Corp. APX**.
- SDK Manager created WSL distro **Nvidia_SDKM_Ubuntu_22.04_JetPack_7.2.1**, running as WSL2.
- The log explicitly showed the attachment script succeeded and APX appeared, followed by **Detected first bind, need to reset the device and then continue**. No actual flash progress was reported.
- WSLInterop is now registered again; the agent can run Windows executables from its WSL session. This supersedes the earlier host execution limitation.

The single immediate retry was subsequently performed: it passed this initial-binding stage and failed with the USB-write timeout recorded above. Do not repeat that completed experiment as though it were still pending.

## Verified flash settings before the failed precheck

The screenshot before the failed prechecks confirmed:

- Selected device: **Jetson Orin Nano [8GB developer kit version]**, port **2-10**.
- Module **P3767-0005**, carrier **P3768-0000**, selected in the preceding hardware-identification dialog.
- OEM Configuration: **Pre-Config**.
- New username: `farsight`; password entered locally and masked.
- Storage Device: **SD Card**.
- **Flash** button was enabled. The first click failed at the first-bind precheck; no flash progress or completion has been reported.

These are the intended settings once the next host has restored and verified recovery detection. SD overwrite permission is already granted. Keep Jetson power and USB-C connected during an actual flash, inspect progress/errors, and verify completion before reporting a successful installation.

## Previous APX / WinUSB driver setup (detection worked afterward)

NVIDIA's official APX guide instructs installing WinUSB using Zadig. The two linked files have been downloaded together into:

`C:\Users\Mehdi\Documents\Codex\2026-10-10\ive-j\outputs\apx-driver`

- `zadig-2.9.exe`, downloaded from NVIDIA's linked upstream release: `https://github.com/pbatard/libwdi/releases/download/v1.5.1/zadig-2.9.exe`.
- `zadig.ini`, downloaded from `https://developer.download.nvidia.com/sdkmanager/resources/help/resources/zadig.ini`.
- The config defaults to WinUSB and exits after successful installation. Executable signature validation was not performed here; the download source was the upstream release linked by NVIDIA.

The driver setup instructions given to the user were:

1. Keep Jetson power and USB-C connected; leave it in its current recovery state.
2. Open the folder above in Explorer and run `zadig-2.9.exe`; complete the Windows administrator prompt.
3. Select **APX** in Zadig's device list. Verify the selected device is the Jetson/NVIDIA device; do not change the driver of unrelated devices.
4. Set the destination driver to **WinUSB**, then click **Install Driver**.
5. After installation completes, dismiss the SDK Manager error and click **refresh**.
6. Verify detection of Orin Nano 8GB, confirm the existing SD Card / Manual Setup / Pre-Config settings, then proceed with Flash. SD overwrite is already authorized.

Official guide: https://developer.nvidia.com/w/sdkmanager/resources/help/index.html

The subsequent successful APX detection established that driver setup worked. Reinstalling WinUSB has not been established as a fix for the later transfer timeout or absent device. These historical Windows GUI instructions require the human operator.

## Previous rescue-shell sequence (history)

The instructions used before APX detection were:

1. On the Jetson keyboard, press **Ctrl+Alt+Delete** once to request a restart.
2. If it does not restart within about 30 seconds, disconnect barrel power, wait 5 seconds, then reconnect it. Keep USB-C connected. Do not interrupt an active firmware update or flash if the screen now shows one.
3. Tap **Esc** when the NVIDIA logo appears.
4. Select **Boot Manager → UEFI USB SanDisk**.
5. Select **Install Jetson ISO r39.2.1 → Boot Into Rescue Shell**. Enter on the top-level Install item opens the submenu.
6. At the maintenance prompt, press **Enter**, not Ctrl+D.
7. Inspect/photograph the resulting prompt. If the NVIDIA logo does not return after restarting, troubleshoot display/power before continuing.

### Software recovery command used in the proposed sequence

Run this **on the Jetson's Linux shell**, not in GRUB, UEFI Shell, Windows, or WSL:

```bash
reboot --force forced-recovery
```

NVIDIA documents software recovery from a Linux terminal. A later screenshot established APX was visible at that time, but the precise command sequence in the user's attempt was not explicitly confirmed. APX is currently absent. Verify actual host enumeration after any new recovery entry.

The expected result is a restart into Force Recovery Mode with a black display. Keep power and USB-C connected. Then refresh SDK Manager and check the Windows USB device list:

```powershell
& 'C:\Program Files\usbipd-win\usbipd.exe' list
```

Look for an NVIDIA/APX device (vendor ID `0955`). Confirm actual detection; a black display alone does not establish recovery mode. If SDK Manager reports a missing APX driver, follow its NVIDIA driver instructions.

If the rescue prompt asks for a password, says the root account is locked, or the reboot command errors, record the exact screen rather than assuming the command worked. No debug-shell boot modification has been applied yet. A temporary `systemd.debug_shell=1` boot option is a possible follow-up to investigate if ordinary rescue cannot provide a console; do not treat that as already tested.

### Once SDK Manager detects the correct board

- Confirm Manual Setup, Orin Nano 8GB, Pre-Config, desired account details, and **SD Card**.
- The SD erase permission is already granted.
- Click Flash and watch SDK Manager's Terminal/logs for the exact result.
- Follow the current flash dialog for any USB-stick requirement. The Windows/JetPack 7.x documentation lists an extra USB stick; the existing stick has been left inserted, but its suitability for every Direct Flash step is not independently verified.
- Record success or the exact failing component and error. Do not claim that reaching the flash dialog or building an image means the board was flashed.

## Alternative physical recovery

If software recovery is unavailable, obtain a jumper cap or suitable conductor and use NVIDIA's documented physical procedure:

1. Disconnect barrel power; keep the USB-C data connection and SD card in place.
2. Bridge **J14 pins 9 (GND) and 10 (FORCE_RECOVERY / FC REC)**.
3. Reconnect barrel power while the pair is bridged.
4. Remove the bridge once the host detects recovery mode.

J14 is the **single row of 12 pins at the rear edge opposite the USB/Ethernet connectors**, below the module/heatsink overhang. It is separate from the long 40-pin GPIO header. The user eventually photographed the correct header, but no pins have been bridged.

Identify the labeled recovery/GND pair and pin-1 orientation using the carrier specification; do not rely on “left/right” instructions from a different photo orientation. NVIDIA allows a bare metal paper clip; contact must be limited to the intended two pins.

## Prior handoff: reported history, not independently reproduced

The original document supplied by the user was `jetson-flash-handoff.md` in Downloads. It reported:

- QSPI/UEFI firmware was already 39.2.x.
- A prior JetPack 7.2.1 ISO install failed around 67% while reinstalling `nvidia-l4t-bootloader`, exit 100.
- Board identity was reported as `jetson-orin-nx-devkit-16gb` despite the Orin Nano 8GB module.
- Editing `/target/etc/nv_boot_control.conf` and running `dpkg --configure -a` installed a package but left an interrupted, unbootable installation.
- SD contents could be replaced.
- Suggested stopping after two failed WSL flash attempts and seeking NVIDIA event support.

In this session we **confirmed the two SD boot entries fail**, but did not read the board-spec strings, package status, installer crash log, or firmware version ourselves. The claimed bootloader failure comes from that earlier handoff.

The old handoff's instruction to run a Linux SDK Manager `.deb` inside WSL for JetPack 7.2.1 is outdated: NVIDIA's current documentation says JetPack 7.1+ is unavailable in SDK Manager running directly inside WSL; use **Windows SDK Manager**, which orchestrates the required environment.

## Known ISO issue and boundaries

NVIDIA's forum confirms a board-identification mismatch affecting the ISO bootloader installation flow. Its published configuration workaround requires an **already bootable installed Linux system**. Forum staff explicitly distinguish editing a failed install through `chroot /target` from having a bootable system or updated EFI board-spec.

Do not repeat the earlier package/config edit and assume it finishes the installer. No verified fix for this user's failed ISO state was established in this session. Direct Flash is a recovery attempt, not a guaranteed fix for every issue.

The display + USB installer path is supported and does not require host USB detection. Recovery mode is required for **SDK Manager Direct Flash**. Earlier rescue-shell work sought a software recovery entry because the user lacked a jumper; it is historical, not an active installation still running.

## After a successful flash

1. Follow SDK Manager's completion/reboot instructions and remove any recovery jumper.
2. Ensure the Jetson boots from the SD card rather than the installer USB; remove the installer when instructed.
3. Log in using the preconfigured account, or complete first-boot setup if Runtime was selected instead.
4. Establish Wi-Fi or Ethernet connectivity.
5. On the Jetson:

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
systemctl is-active ssh
hostname -I
```

6. Verify SSH authentication from the teammate's computer using the actual reachable IP and account. USB device-mode networking may expose `192.168.55.1`, but verify that interface exists before assuming it works.
7. Verify an independent reboot still reaches Ubuntu from SD.

The original handoff references the Farsight repo, branch `1-jetson-stream`, PR 20, and `tools/bootstrap_jetson.sh` / `jetson/setup.sh`. Those scripts have **not** been inspected or run here. The immediate goal is OS boot and SSH, then project setup can proceed with its owner.

## Primary references

- [Orin Nano JetPack 7.2.1 quick start](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/quick_start.html)
- [Software and physical Force Recovery instructions](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/howto.html#force-recovery-mode)
- [Orin Nano BSP setup](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/setup_bsp.html)
- [Carrier specification: Figure 1-4 and Table 3-4 for J14](https://developer.nvidia.com/downloads/assets/embedded/secure/jetson/orin_nano/docs/jetson_orin_nano_devkit_carrier_board_specification_sp.pdf)
- [Current Windows / WSL SDK Manager guidance](https://docs.nvidia.com/sdk-manager/wsl-systems/index.html)
- [SDK Manager Direct Flash guide](https://docs.nvidia.com/sdk-manager/install-with-sdkm-jetson-direct-flash/index.html)
- [NVIDIA APX / WinUSB driver installation guide](https://developer.nvidia.com/w/sdkmanager/resources/help/index.html)
- [Reported JetPack 7.2.1 bootloader / board-identification failure](https://forums.developer.nvidia.com/t/jetpack-7-2-installation-exit-100-for-apt-get-install-nvidia-l4t-bootloader/383607)
- [Systemd debug-shell boot option, if further investigation is needed](https://github.com/systemd/systemd/blob/main/man/systemd-debug-generator.xml)
- [Farsight repository](https://github.com/azizu06/farsight)
- [Project PR referenced by the earlier handoff](https://github.com/azizu06/farsight/pull/20)

Communication note: give one clear current path and explain each mode transition. Earlier instructions alternated between physical recovery and USB diagnostics, which confused the user. Prefer verified screens and exact errors over assumptions about the current boot stage.
