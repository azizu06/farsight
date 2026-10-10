"""Read-only macOS IPv4 operator-route evidence, not link qualification.

python -m tools.qualify_route
Only networksetup queries, route get, and ifconfig are run. No settings change,
host traffic, account queries, SSIDs, MAC addresses, or device IDs are reported.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile


INTERFACE = r"[A-Za-z][A-Za-z0-9_.-]*"


def command(argv):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=3, check=False,
                                env=dict(os.environ, LC_ALL="C"))
        if len(result.stdout) > 65536:
            return dict(ok=False, stdout="", error="output_too_large")
        return dict(ok=result.returncode == 0, stdout=result.stdout,
                    error=None if result.returncode == 0 else "command_failed")
    except subprocess.TimeoutExpired:
        return dict(ok=False, stdout="", error="timeout")
    except OSError:
        return dict(ok=False, stdout="", error="command_unavailable")


def hardware(text):
    ports = []
    for block in re.split(r"(?m)^Hardware Port: ", text)[1:]:
        name = block.splitlines()[0].strip()
        devices = re.findall(rf"(?m)^Device: ({INTERFACE})\s*$", block)
        ports.append(dict(name=name, device=devices[0] if len(devices) == 1 else None))
    return ports


def services(text):
    found = []
    headers = list(re.finditer(r"(?m)^\((\d+|\*)\)[ \t]*(\*?)([^\n]+)", text))
    for index, header in enumerate(headers):
        block = text[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)]
        name = header[3].strip()
        disabled = header[1] == "*" or header[2] == "*"
        match = re.findall(rf"\(Hardware Port: ([^\n]+), Device: ({INTERFACE})\)", block)
        found.append(dict(name=name, disabled=disabled, port=match[0][0] if len(match) == 1 else None,
                          device=match[0][1] if len(match) == 1 else None))
    return found


def interfaces(text):
    found, invalid = {}, False
    pattern = rf"(?m)^({INTERFACE}): flags=[^<\n]*<([^>]*)>[^\n]*\n"
    matches = list(re.finditer(pattern, text))
    for index, match in enumerate(matches):
        block = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        networks = []
        for line in block.splitlines():
            if line.strip().startswith("inet "):
                address = re.fullmatch(r"\s*inet\s+(\S+)(?:\s+-->\s+(\S+))?\s+netmask\s+(\S+)(?:\s+broadcast\s+\S+)?\s*", line)
                try:
                    if not address:
                        raise ValueError()
                    ip, peer, mask = address.groups()
                    if peer is not None:
                        ipaddress.IPv4Address(peer)
                    mask = str(ipaddress.IPv4Address(int(mask, 16))) if mask.startswith("0x") else mask
                    networks.append(ipaddress.IPv4Interface(f"{ip}/{mask}").network)
                except ValueError:
                    invalid = True
        status = re.findall(r"(?m)^\s*status: (active|inactive)\s*$", block)
        if match[1] in found:
            invalid = True
        found[match[1]] = dict(up="UP" in match[2].split(","),
                               status=status[0] if len(status) == 1 else None, networks=networks)
    return found, invalid or not matches


def assess(outputs, wifi_outputs, phone_service="iPhone USB", jetson_subnet="192.168.55.0/24"):
    subnet = ipaddress.IPv4Network(jetson_subnet)
    checks = {}
    def check(name, passed, reason, **evidence):
        checks[name] = dict(passed=bool(passed), reason=reason, **evidence)
    for name in ("hardware", "services", "default_route", "interfaces"):
        result = outputs.get(name, {})
        check(name + "_query", result.get("ok") is True,
              "collected" if result.get("ok") is True else result.get("error", "missing_evidence"))
    ports = hardware(outputs.get("hardware", {}).get("stdout", ""))
    listed = services(outputs.get("services", {}).get("stdout", ""))
    phone = [item for item in listed if item["name"] == phone_service]
    selected = phone[0] if len(phone) == 1 else None
    phone_ok = bool(selected and not selected["disabled"] and selected["port"] == "iPhone USB" and selected["device"])
    check("iphone_usb_service", phone_ok, "identified" if phone_ok else "missing_disabled_or_ambiguous_iphone_usb_service",
          matches=len(phone), interface=selected["device"] if selected else None)
    wifi = [item["device"] for item in ports if item["name"] in ("Wi-Fi", "AirPort") and item["device"]]
    check("wifi_inventory", len(wifi) == 1, "identified" if len(wifi) == 1 else "missing_or_ambiguous_wifi_hardware",
          count=len(wifi))
    wifi_evidence = []
    for device in wifi:
        result = wifi_outputs.get(device, {})
        match = re.fullmatch(rf"Wi-Fi Power \({re.escape(device)}\): (On|Off)\s*", result.get("stdout", "").strip())
        state = match[1].lower() if match and result.get("ok") else "unknown"
        wifi_evidence.append(dict(interface=device, power=state))
    off = len(wifi) == 1 and all(item["power"] == "off" for item in wifi_evidence)
    check("wifi_off", off, "off" if off else "wifi_on_or_power_unverified", interfaces=wifi_evidence)
    raw_route = outputs.get("default_route", {}).get("stdout", "")
    route_interfaces = re.findall(rf"(?m)^\s*interface:\s*({INTERFACE})\s*$", raw_route)
    destinations = re.findall(r"(?m)^\s*destination:\s*(\S+)\s*$", raw_route)
    gateways = re.findall(r"(?m)^\s*gateway:\s*(\S+)\s*$", raw_route)
    flags = re.findall(r"(?m)^\s*flags:\s*<([^>]+)>\s*$", raw_route)
    try:
        gateway_valid = len(gateways) == 1 and not ipaddress.IPv4Address(gateways[0]).is_unspecified
    except ValueError:
        gateway_valid = False
    route_ok = (phone_ok and len(route_interfaces) == 1 and route_interfaces[0] == selected["device"]
                and destinations == ["default"] and gateway_valid and len(flags) == 1
                and {"UP", "GATEWAY"}.issubset(flags[0].split(",")))
    check("default_ipv4_via_iphone_usb", route_ok, "matched" if route_ok else "default_route_missing_or_not_iphone_usb",
          interface=route_interfaces[0] if len(route_interfaces) == 1 else None)
    adapters, invalid = interfaces(outputs.get("interfaces", {}).get("stdout", ""))
    check("interface_parse", not invalid, "parsed" if not invalid else "missing_or_malformed_interface_evidence")
    adapter = adapters.get(selected["device"]) if selected else None
    active = bool(adapter and adapter["up"] and adapter["status"] == "active" and adapter["networks"])
    check("iphone_usb_active_ipv4", active, "active" if active else "iphone_usb_interface_missing_inactive_or_without_ipv4")
    direct = [device for device, item in adapters.items() if item["up"] and item["status"] != "inactive"
              and any(net.overlaps(subnet) for net in item["networks"])]
    check("no_active_direct_jetson_usb_subnet", not invalid and not direct,
          "no_active_subnet_detected" if not invalid and not direct else "direct_jetson_usb_bypass_or_unknown_interfaces",
          subnet=str(subnet), interfaces=direct)
    failed = [name for name, item in checks.items() if not item["passed"]]
    return dict(schema_version=1, route_policy_passed=not failed, checks=checks, failed_checks=failed,
                scope="Local macOS IPv4 default route, iPhone USB service, Wi-Fi radio power, and configured direct Jetson subnet",
                limitations=["Does not verify the phone uses cellular rather than Wi-Fi",
                             "Does not verify Tailscale peer paths, per-flow routes, or IPv6 routes",
                             "Does not qualify carrier capacity, link latency, headset display, camera, or motor safety"],
                mutations_performed=False)


def collect(phone_service="iPhone USB", jetson_subnet="192.168.55.0/24", *, runner=command, system=None):
    if (system or platform.system()) != "Darwin":
        return dict(schema_version=1, route_policy_passed=False, failed_checks=["unsupported_platform"],
                    scope="macOS only", mutations_performed=False)
    queries = dict(hardware=["/usr/sbin/networksetup", "-listallhardwareports"],
                   services=["/usr/sbin/networksetup", "-listnetworkserviceorder"],
                   default_route=["/sbin/route", "-n", "get", "default"],
                   interfaces=["/sbin/ifconfig", "-a"])
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = {key: executor.submit(runner, argv) for key, argv in queries.items()}
        outputs = {key: future.result() for key, future in pending.items()}
    wifi = {}
    for item in hardware(outputs["hardware"].get("stdout", "")):
        if item["name"] in ("Wi-Fi", "AirPort") and item["device"]:
            wifi[item["device"]] = runner(["/usr/sbin/networksetup", "-getairportpower", item["device"]])
    report = assess(outputs, wifi, phone_service, jetson_subnet)
    report["collected_at_utc"] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone-service", default="iPhone USB", help="Exact configured operator phone service name")
    parser.add_argument("--jetson-usb-subnet", default="192.168.55.0/24")
    parser.add_argument("--output", type=Path, help="Atomically save the same JSON evidence, including failed checks")
    args = parser.parse_args(argv)
    if not args.phone_service.strip() or len(args.phone_service) > 128 or any(ord(c) < 32 for c in args.phone_service):
        parser.error("Phone service must be a nonempty printable name of at most 128 characters")
    try:
        subnet = ipaddress.IPv4Network(args.jetson_usb_subnet)
    except ValueError:
        parser.error("Jetson USB subnet must be an IPv4 network in CIDR form")
    report = collect(args.phone_service, str(subnet))
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.output is not None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent,
                                             prefix=".farsight-route-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
            os.replace(temporary, args.output)
        except OSError as exc:
            parser.exit(2, f"Could not persist route evidence ({type(exc).__name__}).\n")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return 0 if report["route_policy_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
