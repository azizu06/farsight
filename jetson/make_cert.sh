#!/usr/bin/env bash
# Private CA + server certificate for the rover's HTTPS (no passwords, no network). Safe to re-run:
# existing files are kept. The leaf names the Jetson by every name/IP it answers to, so clients verify it.
#
# Usage: jetson/make_cert.sh [--dir /etc/farsight/tls] [--renew] [--new-ca] [--dry-run] [extra-name-or-ip ...]
#   --renew   new server certificate signed by the existing CA (after a hostname/IP change); clients keep ca.pem
#   --new-ca  replace everything; clients must fetch the new ca.pem
# Outputs in --dir: ca.pem (give this one to the Mac: --ca-file), cert.pem + key.pem (the server), ca-key.pem (private).
set -euo pipefail

DIR="${FARSIGHT_TLS_DIR:-/etc/farsight/tls}" RENEW=0 NEWCA=0 DRY=0 EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="${2:?--dir needs a path}"; shift ;;
    --renew) RENEW=1 ;;
    --new-ca) NEWCA=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    *) EXTRA+=("$1") ;;
  esac
  shift
done

# Names and addresses the rover answers to.
names=(farsight.local farsight localhost "$(hostname)")
ips=(127.0.0.1 192.168.55.1)  # 192.168.55.1 = the USB-C device-mode address of a Jetson dev kit
fqdn="$(hostname -f 2>/dev/null || true)"; [ -z "$fqdn" ] || names+=("$fqdn")
for ip in $(hostname -I 2>/dev/null || true); do ips+=("$ip"); done
if command -v tailscale >/dev/null 2>&1; then
  ts_name="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  [ -z "$ts_name" ] || names+=("$ts_name")
  for ip in $(tailscale ip -4 2>/dev/null || true); do ips+=("$ip"); done
fi
for item in ${EXTRA[@]+"${EXTRA[@]}"}; do
  if [[ "$item" =~ ^[0-9.]+$ || "$item" == *:* ]]; then ips+=("$item"); else names+=("$item"); fi
done

alt=""; n=0; seen=" "
for name in "${names[@]}"; do case "$seen" in *" D:$name "*) continue ;; esac; seen="$seen""D:$name "; n=$((n+1)); alt="${alt}DNS.$n = $name"$'\n'; done
n=0
for ip in "${ips[@]}"; do case "$seen" in *" I:$ip "*) continue ;; esac; seen="$seen""I:$ip "; n=$((n+1)); alt="${alt}IP.$n = $ip"$'\n'; done

if [ "$DRY" = 1 ]; then
  echo "[dry-run] would write ca.pem ca-key.pem cert.pem key.pem in $DIR (existing kept unless --renew/--new-ca) with names:"
  printf '%s' "$alt" | sed 's/^/  /'
  exit 0
fi

command -v openssl >/dev/null || { echo "openssl missing: sudo apt-get install -y openssl"; exit 1; }
if ! mkdir -p "$DIR" 2>/dev/null || [ ! -w "$DIR" ]; then
  sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 755 "$DIR"
fi

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
umask 077
if [ "$NEWCA" = 1 ] || [ ! -s "$DIR/ca.pem" ] || [ ! -s "$DIR/ca-key.pem" ]; then
  printf '[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3_ca\n[dn]\nCN=Farsight rover CA\n[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n' > "$work/ca.cnf"
  openssl genrsa -out "$DIR/ca-key.pem" 2048 2>/dev/null
  openssl req -x509 -new -key "$DIR/ca-key.pem" -sha256 -days 3650 -config "$work/ca.cnf" -out "$DIR/ca.pem"
  chmod 644 "$DIR/ca.pem"
  echo "created CA $DIR/ca.pem"
  RENEW=1
fi
if [ "$RENEW" = 1 ] || [ ! -s "$DIR/cert.pem" ] || [ ! -s "$DIR/key.pem" ]; then
  printf '[v3_leaf]\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\nsubjectAltName=@alt\n[alt]\n%s' "$alt" > "$work/leaf.cnf"
  openssl genrsa -out "$work/key.pem" 2048 2>/dev/null
  openssl req -new -key "$work/key.pem" -subj "/CN=farsight.local" -out "$work/leaf.csr"
  openssl x509 -req -in "$work/leaf.csr" -CA "$DIR/ca.pem" -CAkey "$DIR/ca-key.pem" -CAcreateserial -CAserial "$work/ca.srl" \
    -days 825 -sha256 -extfile "$work/leaf.cnf" -extensions v3_leaf -out "$work/cert.pem" 2>/dev/null
  mv "$work/key.pem" "$DIR/key.pem"; mv "$work/cert.pem" "$DIR/cert.pem"
  echo "created server certificate $DIR/cert.pem for:"
  printf '%s' "$alt" | sed 's/^/  /'
else
  echo "kept existing $DIR/cert.pem (use --renew after a name or IP change)"
fi
chmod 600 "$DIR/key.pem" "$DIR/ca-key.pem"; chmod 644 "$DIR/cert.pem"
echo "Copy to the Mac:  scp $(id -un)@farsight.local:$DIR/ca.pem ~/farsight-ca.pem   then use --ca-file ~/farsight-ca.pem"
