#!/bin/sh
# Workaround for the JetPack 7.2.x ISO failing at "apt-get install nvidia-l4t-bootloader" (exit 100)
# when the firmware reports an Orin NX board name on an Orin Nano dev kit.
# Run from the installer's failure shell. Unofficial (NVIDIA forum user report), see docs/hardware.md.
set -e
CONF=/target/etc/nv_boot_control.conf
[ -f "$CONF" ] || { echo "No $CONF: not this failure. Take a photo of this screen."; exit 1; }
echo "== Before"; grep -E "TNSPEC|COMPATIBLE_SPEC" "$CONF"
if ! grep -q "orin-nx" "$CONF"; then
  echo "Board name is not orin-nx: different problem. Nothing changed. Take a photo of this screen."; exit 1
fi
mountpoint -q /target/proc || mount --bind /proc /target/proc
mountpoint -q /target/sys  || mount --bind /sys /target/sys
mountpoint -q /target/sys/firmware/efi/efivars || mount -t efivarfs efivarfs /target/sys/firmware/efi/efivars
cp -n "$CONF" "$CONF.bak"
sed -i 's/jetson-orin-nx-devkit-16gb/jetson-orin-nano-devkit/g; s/jetson-orin-nx-devkit/jetson-orin-nano-devkit/g' "$CONF"
echo "== After"; grep -E "TNSPEC|COMPATIBLE_SPEC" "$CONF"
if chroot /target dpkg --configure -a; then
  echo; echo "FIXED. Type: reboot   (then remove the USB sticks)"
else
  echo; echo "dpkg still failed. Take a photo of this screen. Next step: SDK Manager on Ubuntu or the NVIDIA booth."
fi
