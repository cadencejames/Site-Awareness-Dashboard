# Site Awareness Dashboard (SAD)

A Python/SQLite/Tkinter tool for discovering and documenting Cisco
network topology across many sites, and turning that data into a
browsable static HTML dashboard - built for an air-gapped, no-Flask,
no-external-JS environment where something like Cisco Prime or
Catalyst Center isn't an option.

SAD connects to your Cisco gear over SSH (via Netmiko), walks CDP to
discover devices and links site by site, pulls ARP tables and MAC
address tables, correlates phone/VTC clients through CUCM/RIS, and
generates a set of static HTML pages you can open in any browser - no
server, no database engine beyond a local SQLite file, no internet
access required to run or view it.

## What it does

- **CDP-based topology discovery** - starting from one seed device per
  site, walks CDP neighbors outward to build a full device/link map,
  multithreaded across sites.
- **ARP table collection** - global and per-VRF, with support for a
  dedicated ARP-collection seed and per-device override IPs (e.g. a
  shared HSRP/VRRP VIP) when the CDP seed isn't the right device to
  pull ARP from.
- **MAC address table collection** (opt-in) - correlates switch
  MAC tables against CDP-known links to distinguish real end-host
  clients from inter-switch uplinks.
- **Phone/VTC identification and enrichment** - detects Cisco IP
  phones and VTC units (Webex Room/Desk devices, TelePresence) via
  their CDP-reported platform string, then optionally enriches them
  with CUCM/RIS data (registration status, extension, serial number).
- **Static HTML dashboard** - a dark, network-ops-styled set of pages:
  a cross-site index, one page per site with device/link/ARP/client
  tables and an auto-laid-out topology diagram, and a cross-site
  client search page. Everything is generated once and works as plain
  files - open `dashboard/index.html` in a browser, no server needed.
- **CSV exports** - phone/VTC and full device inventories, regenerated
  on demand.
- **Command Runner** - a small plugin system for making changes across
  a scope of devices, with a mandatory dry-run preview before anything
  can be committed.
- **Encrypted, per-user credential storage** - AES-256-GCM with a
  PBKDF2-derived key from a master password you set yourself; nothing
  is ever stored in plaintext, and each OS user on a shared machine
  gets their own independent, separately-protected store.
- **A durable write queue** - since `sad.db` can live on a shared
  network drive with multiple people/processes writing concurrently,
  every write goes through a spooled queue rather than hitting SQLite
  directly, avoiding the file-locking problems that come with SQLite
  over SMB.

## Requirements

- Python 3.9+
- [Netmiko](https://github.com/ktbyers/netmiko) - device SSH sessions
- [cryptography](https://cryptography.io/) - credential store encryption
- [requests](https://requests.readthedocs.io/) - CUCM/RIS HTTP calls
- Tkinter (for `gui.py` - bundled with most Python installs; on some
  Linux distros it's a separate package, e.g. `python3-tk`)

```
pip install netmiko cryptography requests
```

Everything else is Python standard library - no web framework, no
JavaScript build step, no external services beyond your own Cisco
devices and (optionally) CUCM.

## Getting started

1. Run `python3 gui.py`. On first launch it walks you through creating
   an encrypted credential store and setting up your device login
   credentials.
2. Import your device inventory with `utilities/inventory_import.py`
   (generic CSV importer - point it at whatever inventory export you
   have).
3. Name your sites and flag a CDP seed device per site, either in
   `gui.py`'s Site Manager tab or in bulk via
   `utilities/site_identity_import.py`.
4. Run discovery from the Discovery tab (or `orchestrator.py` from the
   CLI).
5. Generate the dashboard - `gui.py`'s toolbar or
   `python3 dashboard_generate.py`.

See `ONBOARDING.md` for the full walkthrough, a tour of every tab, and
a set of gotchas worth knowing before you dig in (particularly around
the credential store's field-naming, which has a real gotcha in how
you add credential types).

## Project layout

```
gui.py                   desktop app - day-to-day tool
orchestrator.py          CDP/ARP/MAC-table discovery engine
dashboard_generate.py    builds the static HTML dashboard

utilities/
  db.py                    schema and all database access
  write_queue.py           durable write queue
  credential_store.py / credential_manager.py / credential_loader.py
  cdp_parser.py / arp_parser.py / vrf_parser.py / mac_parser.py
  inventory_import.py / site_identity_import.py / subnet_override_import.py
  site_manager.py          interactive per-site setup
  csv_export.py / cucm_enrich.py
  topology_layout.py / topology_svg.py
  verify_scan.py           post-scan sanity report
  plugins/                 Command Runner plugins
```

## Status

Actively developed against a real production environment - built and
refined feature by feature (topology discovery, staleness tracking,
client/phone/VTC tracking, CUCM enrichment, CSV export, credential
management, multi-user write handling) rather than designed all at
once. `ONBOARDING.md` tracks the open/known-gap items still on the
list.

## Disclaimer

This was built for one specific air-gapped, Cisco-only environment and
makes some assumptions to match it (no LLDP, no external web
frameworks or CDN-loaded JS, SQLite over a network share instead of a
real database server). It's shared as a reference/starting point, not
a polished general-purpose product - adapting it to a different
environment will likely need real changes, not just configuration.