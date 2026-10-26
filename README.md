# Opdracht 3 — Cisco Broadband Router config uit een CSV-bestand

`router_config.py` leest een CSV-bestand en maakt een tekstbestand dat je rechtstreeks op de CLI van een Cisco-router plakt.
Enkel de standaardbibliotheek van Python 3.10+ is nodig.

## Gebruik

```sh
python3 router_config.py examples/*.csv               # alle scenario's -> output/<naam>.txt
python3 router_config.py mijn.csv --hostname R1       # met hostname
python3 router_config.py mijn.csv --stdout            # enkel tonen
python3 -m unittest discover -s tests                 # tests
```

## CSV-formaat

Scheidingsteken `;`, `,` of TAB (wordt automatisch herkend). Hoofdingen zijn niet hoofdlettergevoelig
(`IP Address`, `Netmask`, `Default Gateway` worden ook herkend).

| kolom | betekenis |
|---|---|
| `network` | `wan` of `lan` |
| `interface` | bv. `gi0/0`, `fa0/1`, `GigabitEthernet0/1` |
| `description` | omschrijving van de interface / route |
| `vlan` | `0` of leeg = ongetagd, `1–4094` = dot1Q-subinterface |
| `ipaddress` | IP-adres, `dhcp` (enkel WAN), of netwerkadres (LAN zonder interface) |
| `subnetmask` | bv. `255.255.255.0` |
| `defaultgateway` | WAN: default gateway — LAN zonder interface: next hop |

## Wat het script genereert

| situatie in de CSV | config |
|---|---|
| WAN met `dhcp` | `ip address dhcp` (default route komt van de DHCP-server) |
| WAN met vast IP + gateway | `ip address …` + `ip route 0.0.0.0 0.0.0.0 <gateway>` |
| LAN met interface, vlan 0 | IP op de fysieke interface |
| LAN met interface, vlan > 0 | router-on-a-stick: fysieke interface zonder IP, subinterface `gi0/1.<vlan>` met `encapsulation dot1Q <vlan>` |
| LAN zonder interface | extern netwerk achter een andere router: `ip route <netwerk> <mask> <next hop> name <description>` |
| alle LAN's | `ip nat inside`, WAN `ip nat outside`, ACL `NAT-INSIDE` + `ip nat inside source list NAT-INSIDE interface <wan> overload` (PAT) |

## Controles

Het script stopt met een duidelijke foutmelding (met regelnummer) als:

- er niet precies één WAN-rij is;
- een IP-adres, masker of VLAN ongeldig is;
- de WAN-gateway niet in het WAN-subnet ligt;
- een LAN-IP het netwerk- of broadcastadres is;
- een VLAN of ongetagd LAN twee keer op dezelfde interface staat;
- WAN en LAN dezelfde interface gebruiken;
- subnetten overlappen;
- de next hop van een extern LAN niet in een rechtstreeks verbonden LAN ligt.

## Scenario's

De vier voorbeelden uit de opgave staan in [`examples/`](examples), de gegenereerde config in [`output/`](output):

1. `1_wan-dhcp_lan` — WAN via DHCP, één LAN
2. `2_wan-static_lan` — WAN met vast IP en default gateway, één LAN
3. `3_wan-static_static-routes` — Servers-LAN + Accounting en HR achter een interne router (statische routes)
4. `4_wan-dhcp_router-on-a-stick` — vijf VLAN's op `gi0/1` via dot1Q-subinterfaces
