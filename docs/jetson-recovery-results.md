# Jetson recovery results — October 10, 2026

Verified on Sai's Mac and the physical Orin Nano. This records the current rig
state after the earlier installer and Windows flash failures; those older
handoffs remain history.

## Verified state

- Module P3767-0005, carrier P3768-0000.
- Running Jetson Linux R39.2.1, Ubuntu 24.04.4, kernel 6.8.12-1021-tegra.
- Installed SD system is mounted at `/dev/mmcblk0p1`: 114 GB total,
  approximately 100 GB free during recovery.
- Existing human login account is `ubuntu`. The password was reset from the
  installer rescue environment. Do not reset the NVIDIA service accounts
  `nvsciipc` or `nvsciipc2` merely because their UIDs exceed 1000.
- The human confirmed the graphical desktop opens and stays open. `loginctl`
  independently showed `ubuntu` active on `seat0/tty2`.
- USB serial login and SSH login both succeeded. A fresh SSH login succeeded
  after restarting the USB device-mode service.
- `sudo dpkg --audit` printed nothing. Home ownership and `/tmp` permissions
  were correct; disk space was not the login-loop cause.
- System time and RTC were set from the Mac's UTC clock. NTP is enabled, but
  internet synchronization was not established.

These checks qualify OS login and local USB access. CUDA/TensorRT execution,
application provisioning, camera, Quest, motors, power under load, and a full
cold power-cycle after the final changes remain separate rig checks.

## Failure mechanisms and repairs

### Normal boot was blocked by a saved UEFI state

The SD root and EFI filesystems were readable in UEFI as FS2 and FS3.
Directly launching `FS3:\EFI\BOOT\BOOTAA64.efi` selected recovery boot, then
reported `Android image header not seen` and failed to boot a recovery partition.

UEFI showed **OS chain A status: Unbootable**, no OS chain B status, and
**L4T Boot Mode: ExtLinux**. The human changed chain A to **Normal**, retained
ExtLinux, and saved the change. Linux subsequently reached a text login.

The bootloader version check later reported 39.2.1 and bootloader slots A and B
normal. Bootloader slots and OS-chain status are different fields. The original
installer crash's cause was not independently reproduced in this recovery.

### GNOME could not access NVIDIA GPU memory

The `ubuntu` account initially belonged to ubuntu, adm, cdrom, sudo, dip and lxd,
but not video or render. NVIDIA GPU nodes were owned by those missing groups.
GNOME logged:

```text
NvRmMemInitNvmap failed: error Permission denied
libnvrm_gpu.so: NvRmGpuLibOpen failed
Failed to setup: The GPU /dev/dri/card2 chosen as primary is not supported by EGL.
```

Repair:

```sh
sudo usermod -aG video,render ubuntu
```

The existing user sessions were terminated once so the user manager would obtain
the new supplementary groups; this also ended the serial login, which was then
re-established. The GDM login screen was restarted after its DisplayPort output
was disabled. DisplayPort subsequently showed `enabled` and DPMS `On`; the
human then confirmed successful desktop login.

### USB networking configured the wrong interface

The USB cable provides serial, a read-only L4T status volume, and Ethernet
functions. Local SSH over this link does not require internet or a separate
Ethernet cable.

The installed NVIDIA startup script hardcodes `ifname="usb0"` in both its RNDIS
and ECM/NCM configuration blocks. On this rig, configfs reported:

```text
functions/rndis.usb0/ifname -> usb0
functions/ncm.usb0/ifname   -> usb1
```

Only usb0 was initially up and attached to l4tbr0. The Mac's NCM interface was
therefore inactive. Bringing usb1 up and attaching it to l4tbr0 restored the link.
SSH was installed but inactive; it was enabled with:

```sh
sudo systemctl enable --now ssh
```

On the Mac, the **Linux for Tegra** network service uses static IPv4
**192.168.55.100/24**, without a gateway or DNS changes. The Jetson bridge is
**192.168.55.1/24**. Wi-Fi was left unchanged. Connect with:

```sh
ssh ubuntu@192.168.55.1
```

The USB network hook below is installed on the Jetson as
`/usr/local/sbin/farsight-usb-network-up`, mode 0755:

```python
#!/usr/bin/python3
"""Attach USB ECM/NCM interfaces to NVIDIA's existing USB bridge."""
from pathlib import Path
import subprocess
import time

bridge = Path("/sys/class/net/l4tbr0")
functions = Path("/sys/kernel/config/usb_gadget/l4t/functions")
if bridge.exists() and functions.exists():
    for function in functions.iterdir():
        if not function.name.startswith(("ncm.", "ecm.")):
            continue
        ifname = (function / "ifname").read_text().strip()
        for attempt in range(25):
            if (Path("/sys/class/net") / ifname).exists():
                break
            time.sleep(0.2)
        subprocess.run(["/sbin/ip", "link", "set", "dev", ifname, "master", "l4tbr0"], check=True)
        subprocess.run(["/sbin/ip", "link", "set", "dev", ifname, "up"], check=True)
        print(f"Attached {ifname} ({function.name}) to l4tbr0", flush=True)
```

The installed systemd drop-in is
`/etc/systemd/system/nv-l4t-usb-device-mode.service.d/farsight-ncm.conf`:

```ini
[Service]
ExecStartPost=/usr/local/sbin/farsight-usb-network-up
```

After `systemctl daemon-reload`, the helper was run successfully and the
USB device-mode service was restarted. Its journal recorded:

```text
Attached usb1 (ncm.usb0) to l4tbr0
Finished nv-l4t-usb-device-mode.service
```

Both usb0 and usb1 were forwarding on l4tbr0 afterward. A new authenticated SSH
connection returned the corrected user groups. Systemd verification produced
only existing unrelated warnings about obsolete syslog output settings.

## Recovery environment and host-console notes

The USB installer's **Install Jetson ISO** entry opens a submenu that includes
**Boot Into Rescue Shell**. At the maintenance prompt, press Enter. That live
environment lacked both `nv-l4t-usb-device-mode.service` and the `g_serial`
module; these absences did not diagnose a defective USB cable.

For password recovery, mount the installed SD root separately and run
`chroot MOUNTPOINT passwd ubuntu`. Changing the live installer's password does
not change the installed account. Sync and unmount the SD before rebooting;
`reboot -f` was required because ordinary reboot could not reach the system bus.

On the installed OS, a standard serial terminal worked even though earlier raw
file-descriptor probes returned no prompt:

```sh
TERM=xterm-256color screen /dev/cu.usbmodem14202250042963 115200
```

The device name is specific to this board; enumerate `/dev/cu.usbmodem*` on a
different host. Root-shell and password-reset actions were performed on the
Jetson, not on the Mac.

