#!/usr/bin/env python3
"""
Generate a Cisco IOS configuration for a broadband router from a CSV file.

The output is a plain text file you paste on the router CLI.

CSV columns (header names are case-insensitive, ';' ',' or TAB delimited):
    network;interface;description;vlan;ipaddress;subnetmask;defaultgateway

Rules
  * network = wan  -> the uplink. ipaddress 'dhcp' or a static address; a static
                      WAN with a defaultgateway gets a default route.
  * network = lan, interface filled in
                   -> a directly connected LAN. vlan 0/empty = the physical
                      interface, vlan > 0 = a dot1Q sub-interface (router-on-a-stick).
  * network = lan, interface empty
                   -> a remote LAN behind another router: ipaddress/subnetmask is
                      the network, defaultgateway the next hop -> static route.
  * Every LAN (connected and remote) is NAT'ed (PAT) behind the WAN interface.

Usage
    python3 router_config.py examples/*.csv
    python3 router_config.py my.csv --hostname R1 --output-dir output
    python3 router_config.py my.csv --stdout
"""

import argparse
import csv
import ipaddress
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

NAT_ACL = "NAT-INSIDE"

# Accepted header spellings -> internal field name
HEADER_ALIASES = {
    "network": "network",
    "type": "network",
    "interface": "interface",
    "description": "description",
    "vlan": "vlan",
    "ipaddress": "ipaddress",
    "ip": "ipaddress",
    "subnetmask": "subnetmask",
    "netmask": "subnetmask",
    "mask": "subnetmask",
    "defaultgateway": "defaultgateway",
    "gateway": "defaultgateway",
    "nexthop": "defaultgateway",
}

INTERFACE_PREFIXES = [
    ("tengigabitethernet", "TenGigabitEthernet"),
    ("gigabitethernet", "GigabitEthernet"),
    ("fastethernet", "FastEthernet"),
    ("ethernet", "Ethernet"),
    ("te", "TenGigabitEthernet"),
    ("gi", "GigabitEthernet"),
    ("g", "GigabitEthernet"),
    ("fa", "FastEthernet"),
    ("f", "FastEthernet"),
    ("eth", "Ethernet"),
    ("e", "Ethernet"),
]


class CsvError(Exception):
    """Raised when the CSV content is invalid; message lists every problem."""


@dataclass
class Wan:
    interface: str
    description: str
    dhcp: bool
    address: ipaddress.IPv4Interface | None
    gateway: ipaddress.IPv4Address | None


@dataclass
class ConnectedLan:
    interface: str
    description: str
    vlan: int
    address: ipaddress.IPv4Interface

    @property
    def name(self) -> str:
        return f"{self.interface}.{self.vlan}" if self.vlan else self.interface


@dataclass
class RemoteLan:
    description: str
    network: ipaddress.IPv4Network
    next_hop: ipaddress.IPv4Address


@dataclass
class RouterPlan:
    wan: Wan
    connected: list[ConnectedLan]
    remote: list[RemoteLan]


def expand_interface(name: str) -> str:
    """'gi0/1' -> 'GigabitEthernet0/1'. Unknown prefixes are returned unchanged."""
    name = name.strip().replace(" ", "")
    match = re.fullmatch(r"([A-Za-z-]+)([\d/.:]+)", name)
    if not match:
        return name
    prefix, numbers = match.group(1).lower(), match.group(2)
    for short, full in INTERFACE_PREFIXES:
        if prefix == short:
            return full + numbers
    return name


def read_rows(path: Path) -> list[dict[str, str]]:
    """Read the CSV, detect the delimiter and normalise the header names."""
    text = path.read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise CsvError(f"{path}: file is empty")

    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";"

    reader = csv.reader(lines, delimiter=delimiter)
    header = next(reader)
    fields = []
    for column in header:
        key = re.sub(r"[\s_-]", "", column).lower()
        fields.append(HEADER_ALIASES.get(key, key))

    missing = {"network", "interface", "ipaddress", "subnetmask"} - set(fields)
    if missing:
        raise CsvError(f"{path}: missing column(s): {', '.join(sorted(missing))}")

    rows = []
    for values in reader:
        values = [v.strip() for v in values] + [""] * (len(fields) - len(values))
        rows.append(dict(zip(fields, values)))
    return rows


def parse_plan(rows: list[dict[str, str]]) -> RouterPlan:
    """Validate the rows and turn them into a RouterPlan."""
    errors: list[str] = []
    wans: list[Wan] = []
    connected: list[ConnectedLan] = []
    remote: list[RemoteLan] = []

    for line_no, row in enumerate(rows, start=2):
        where = f"line {line_no}"
        network = row.get("network", "").lower()
        interface = expand_interface(row.get("interface", ""))
        description = row.get("description", "")
        ip = row.get("ipaddress", "")
        mask = row.get("subnetmask", "")
        gateway = row.get("defaultgateway", "")
        vlan_text = row.get("vlan", "") or "0"

        try:
            vlan = int(vlan_text)
            if not 0 <= vlan <= 4094:
                raise ValueError
        except ValueError:
            errors.append(f"{where}: vlan '{vlan_text}' is not a number between 0 and 4094")
            continue

        if network == "wan":
            if not interface:
                errors.append(f"{where}: the WAN needs an interface")
                continue
            if ip.lower() == "dhcp":
                wans.append(Wan(interface, description or "WAN", True, None, None))
                continue
            try:
                address = ipaddress.IPv4Interface(f"{ip}/{mask}")
                gw = ipaddress.IPv4Address(gateway) if gateway else None
            except ValueError as exc:
                errors.append(f"{where}: {exc}")
                continue
            if gw and gw not in address.network:
                errors.append(f"{where}: gateway {gw} is not in WAN subnet {address.network}")
                continue
            wans.append(Wan(interface, description or "WAN", False, address, gw))

        elif network == "lan":
            if interface:
                try:
                    address = ipaddress.IPv4Interface(f"{ip}/{mask}")
                except ValueError as exc:
                    errors.append(f"{where}: {exc}")
                    continue
                if address.ip in (address.network.network_address, address.network.broadcast_address):
                    errors.append(f"{where}: {address.ip} is the network/broadcast address of {address.network}")
                    continue
                connected.append(ConnectedLan(interface, description, vlan, address))
            else:
                try:
                    net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
                    hop = ipaddress.IPv4Address(gateway)
                except ValueError as exc:
                    errors.append(f"{where}: a LAN without interface needs network, mask and next hop ({exc})")
                    continue
                remote.append(RemoteLan(description, net, hop))
        else:
            errors.append(f"{where}: network must be 'wan' or 'lan', got '{row.get('network', '')}'")

    if len(wans) != 1:
        errors.append(f"exactly one WAN row is required, found {len(wans)}")

    # One untagged LAN per interface, unique VLAN per interface, no overlapping subnets
    seen_names: set[str] = set()
    for lan in connected:
        if lan.name in seen_names:
            what = f"VLAN {lan.vlan}" if lan.vlan else "an untagged LAN"
            errors.append(f"{lan.interface} has {what} more than once")
        seen_names.add(lan.name)
        if wans and lan.interface == wans[0].interface:
            errors.append(f"{lan.interface} is used for both WAN and LAN")

    all_networks = [(lan.description or lan.name, lan.address.network) for lan in connected]
    all_networks += [(r.description, r.network) for r in remote]
    if wans and wans[0].address:
        all_networks.append(("WAN", wans[0].address.network))
    for i, (name_a, net_a) in enumerate(all_networks):
        for name_b, net_b in all_networks[i + 1:]:
            if net_a.overlaps(net_b):
                errors.append(f"subnets overlap: {name_a} {net_a} and {name_b} {net_b}")

    # The next hop of a remote LAN must be reachable through a connected LAN
    for r in remote:
        if not any(r.next_hop in lan.address.network for lan in connected):
            errors.append(f"next hop {r.next_hop} for {r.description} {r.network} is not in any connected LAN")

    if errors:
        raise CsvError("\n".join(errors))
    return RouterPlan(wans[0], connected, remote)


def wildcard(network: ipaddress.IPv4Network) -> str:
    return str(network.hostmask)


def route_name(description: str) -> str:
    """IOS route names cannot contain spaces."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", description)[:32]


def render(plan: RouterPlan, source: str, hostname: str | None = None) -> str:
    """Build the IOS configuration text."""
    out: list[str] = []
    add = out.append

    add(f"! Generated by router_config.py from {source} on {datetime.now():%Y-%m-%d %H:%M}")
    add("enable")
    add("configure terminal")
    if hostname:
        add(f"hostname {hostname}")
    add("!")

    # WAN
    wan = plan.wan
    add(f"interface {wan.interface}")
    add(f" description {wan.description}")
    if wan.dhcp:
        add(" ip address dhcp")
    else:
        add(f" ip address {wan.address.ip} {wan.address.netmask}")
    add(" ip nat outside")
    add(" no shutdown")
    add("!")

    # LANs: physical interfaces first, then their sub-interfaces
    parents = list(dict.fromkeys(lan.interface for lan in plan.connected))
    for parent in parents:
        lans = [lan for lan in plan.connected if lan.interface == parent]
        untagged = next((lan for lan in lans if lan.vlan == 0), None)
        add(f"interface {parent}")
        if untagged:
            if untagged.description:
                add(f" description {untagged.description}")
            add(f" ip address {untagged.address.ip} {untagged.address.netmask}")
            add(" ip nat inside")
        else:
            add(" description Trunk to LAN switch")
            add(" no ip address")
        add(" no shutdown")
        add("!")
        for lan in sorted((l for l in lans if l.vlan), key=lambda l: l.vlan):
            add(f"interface {lan.name}")
            if lan.description:
                add(f" description {lan.description}")
            add(f" encapsulation dot1Q {lan.vlan}")
            add(f" ip address {lan.address.ip} {lan.address.netmask}")
            add(" ip nat inside")
            add("!")

    # Routing
    if not wan.dhcp and wan.gateway:
        add(f"ip route 0.0.0.0 0.0.0.0 {wan.gateway}")
    for r in plan.remote:
        name = f" name {route_name(r.description)}" if r.description else ""
        add(f"ip route {r.network.network_address} {r.network.netmask} {r.next_hop}{name}")
    if (not wan.dhcp and wan.gateway) or plan.remote:
        add("!")

    # NAT (PAT) for every inside network
    inside = [lan.address.network for lan in plan.connected] + [r.network for r in plan.remote]
    add(f"ip access-list standard {NAT_ACL}")
    for net in inside:
        add(f" permit {net.network_address} {wildcard(net)}")
    add("!")
    add(f"ip nat inside source list {NAT_ACL} interface {wan.interface} overload")
    add("!")
    add("end")
    add("write memory")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a Cisco broadband router config from a CSV file.")
    parser.add_argument("csv", nargs="+", type=Path, help="one or more CSV files")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"), help="where to write <csv-name>.txt (default: output)")
    parser.add_argument("--hostname", help="router hostname to configure")
    parser.add_argument("--stdout", action="store_true", help="print the config instead of writing files")
    args = parser.parse_args(argv)

    failed = 0
    for path in args.csv:
        try:
            plan = parse_plan(read_rows(path))
        except (CsvError, OSError) as exc:
            print(f"[ERROR] {path}:\n  " + str(exc).replace("\n", "\n  "), file=sys.stderr)
            failed += 1
            continue

        config = render(plan, path.name, args.hostname)
        if args.stdout:
            print(config)
            continue
        args.output_dir.mkdir(parents=True, exist_ok=True)
        target = args.output_dir / f"{path.stem}.txt"
        target.write_text(config, encoding="utf-8")
        print(f"[OK] {path} -> {target}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
