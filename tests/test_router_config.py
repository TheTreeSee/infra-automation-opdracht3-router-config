import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import router_config as rc

HEADER = "network;interface;description;vlan;ipaddress;subnetmask;defaultgateway"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def plan_from(*lines: str, header: str = HEADER) -> rc.RouterPlan:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.csv"
        path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
        return rc.parse_plan(rc.read_rows(path))


class ExampleScenarios(unittest.TestCase):
    def config(self, name: str) -> str:
        path = EXAMPLES / name
        return rc.render(rc.parse_plan(rc.read_rows(path)), path.name)

    def test_wan_dhcp(self):
        cfg = self.config("1_wan-dhcp_lan.csv")
        self.assertIn(" ip address dhcp", cfg)
        self.assertNotIn("ip route 0.0.0.0", cfg)
        self.assertIn(" permit 192.168.1.0 0.0.0.255", cfg)

    def test_wan_static_default_route(self):
        cfg = self.config("2_wan-static_lan.csv")
        self.assertIn(" ip address 172.23.80.200 255.255.254.0", cfg)
        self.assertIn("ip route 0.0.0.0 0.0.0.0 172.23.80.1", cfg)

    def test_remote_lans_become_static_routes_and_are_natted(self):
        cfg = self.config("3_wan-static_static-routes.csv")
        self.assertIn("ip route 192.168.2.0 255.255.255.0 192.168.0.1 name Accounting", cfg)
        self.assertIn("ip route 192.168.3.0 255.255.255.0 192.168.0.1 name HR", cfg)
        self.assertIn(" permit 192.168.3.0 0.0.0.255", cfg)

    def test_router_on_a_stick(self):
        cfg = self.config("4_wan-dhcp_router-on-a-stick.csv")
        self.assertIn("interface GigabitEthernet0/1\n description Trunk to LAN switch\n no ip address", cfg)
        for vlan in (10, 20, 30, 40, 50):
            self.assertIn(f"interface GigabitEthernet0/1.{vlan}\n", cfg)
            self.assertIn(f" encapsulation dot1Q {vlan}\n", cfg)
        self.assertEqual(cfg.count(" ip nat inside\n"), 5)

    def test_nat_overload_on_wan(self):
        for path in EXAMPLES.glob("*.csv"):
            cfg = rc.render(rc.parse_plan(rc.read_rows(path)), path.name)
            self.assertIn("ip nat inside source list NAT-INSIDE interface GigabitEthernet0/0 overload", cfg)


class Parsing(unittest.TestCase):
    def test_comma_delimiter_and_header_aliases(self):
        plan = plan_from(
            "wan,gi0/0,WAN,0,dhcp,,",
            "lan,g0/1,LAN,,10.0.0.1,255.255.255.0,",
            header="Network,Interface,Description,VLAN,IP Address,Netmask,Default Gateway",
        )
        self.assertEqual(plan.connected[0].interface, "GigabitEthernet0/1")
        self.assertEqual(plan.connected[0].vlan, 0)

    def test_interface_names(self):
        self.assertEqual(rc.expand_interface("fa0/1"), "FastEthernet0/1")
        self.assertEqual(rc.expand_interface("Gi0/0/1"), "GigabitEthernet0/0/1")
        self.assertEqual(rc.expand_interface("GigabitEthernet0/0"), "GigabitEthernet0/0")


class Validation(unittest.TestCase):
    def assertCsvError(self, fragment: str, *lines: str):
        with self.assertRaises(rc.CsvError) as ctx:
            plan_from(*lines)
        self.assertIn(fragment, str(ctx.exception))

    def test_needs_exactly_one_wan(self):
        self.assertCsvError("exactly one WAN", "lan;gi0/1;LAN;0;10.0.0.1;255.255.255.0;")

    def test_gateway_outside_wan_subnet(self):
        self.assertCsvError(
            "not in WAN subnet",
            "wan;gi0/0;WAN;0;172.23.80.200;255.255.254.0;10.0.0.1",
            "lan;gi0/1;LAN;0;10.1.0.1;255.255.255.0;",
        )

    def test_next_hop_must_be_connected(self):
        self.assertCsvError(
            "not in any connected LAN",
            "wan;gi0/0;WAN;0;dhcp;;",
            "lan;gi0/1;LAN;0;192.168.0.1;255.255.255.0;",
            "lan;;HR;0;192.168.3.0;255.255.255.0;10.9.9.9",
        )

    def test_duplicate_vlan(self):
        self.assertCsvError(
            "VLAN 10 more than once",
            "wan;gi0/0;WAN;0;dhcp;;",
            "lan;gi0/1;A;10;192.168.10.1;255.255.255.0;",
            "lan;gi0/1;B;10;192.168.11.1;255.255.255.0;",
        )

    def test_overlapping_subnets(self):
        self.assertCsvError(
            "subnets overlap",
            "wan;gi0/0;WAN;0;dhcp;;",
            "lan;gi0/1;A;10;192.168.10.1;255.255.0.0;",
            "lan;gi0/1;B;20;192.168.20.1;255.255.255.0;",
        )

    def test_network_address_as_interface_ip(self):
        self.assertCsvError(
            "network/broadcast",
            "wan;gi0/0;WAN;0;dhcp;;",
            "lan;gi0/1;LAN;0;192.168.1.0;255.255.255.0;",
        )

    def test_wan_and_lan_on_same_interface(self):
        self.assertCsvError(
            "both WAN and LAN",
            "wan;gi0/0;WAN;0;dhcp;;",
            "lan;gi0/0;LAN;0;192.168.1.1;255.255.255.0;",
        )


if __name__ == "__main__":
    unittest.main()
