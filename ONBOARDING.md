# Site Awareness Dashboard (SAD) — Onboarding

SAD discovers Cisco network topology (CDP), ARP tables, and phone/VTC
clients across many sites, stores it in a local SQLite database, and
generates a static HTML dashboard from it. This doc assumes you
already know networking and can run a Python script — it's about how
*this project specifically* is put together, not a general tutorial.

Everything below assumes you're running commands from the **project
root** (the folder this file is in), not from inside `utilities/`.
Nearly every script resolves `sad.db` relative to the current working
directory, so running from the wrong place either fails outright or
quietly points at (or creates) the wrong database file.

## Project layout

```
sad.db                 the whole database - one file, SQLite
credentials.enc         your encrypted credential store (created on first launch)
write_queue/            spool directory the write queue uses - see "The write queue" below
dashboard/               generated HTML output (gitignore-worthy, regenerated on demand)
logs/                    Command Runner's per-run audit logs
session_logs/            raw Netmiko session logs, only when explicitly enabled for troubleshooting

gui.py                   the desktop app - your main day-to-day tool, and where credential setup starts
orchestrator.py          the CDP/ARP discovery engine gui.py's Discovery tab drives
dashboard_generate.py    builds the static HTML dashboard from sad.db
utilities/                shared library code, plus every CLI maintenance tool
  db.py                    the ONLY place that touches sad.db's schema/tables directly
  write_queue.py           the durable write queue every write actually goes through
  credential_store.py      shared crypto core for the credential store
  credential_manager.py    CLI alternative to gui.py's Credentials tab (rarely needed - see below)
  credential_loader.py     runtime reader that unlocks the store for a script/session
  cdp_parser.py / arp_parser.py / vrf_parser.py / mac_parser.py   raw CLI-output parsers
  inventory_import.py     the device inventory importer (already run - see "First-time setup" below)
  site_identity_import.py  bulk-assign site name/code from a CSV
  subnet_override_import.py  bulk-load subnet exceptions for sites split by a non-/24 boundary
  site_manager.py          interactive per-site setup (name/code, CDP seed, ARP seed override)
  csv_export.py            phone/VTC and device CSV exports
  cucm_enrich.py           pulls CUCM/RIS data for phone/VTC clients
  verify_scan.py           post-scan sanity report - a testing tool, see "Tools you probably won't need day-to-day" below
  test_cdp_live.py / test_arp_live.py   standalone live-gear parser tests, don't touch the database
  topology_layout.py / topology_svg.py   the per-site topology diagram (layout math, then SVG rendering)
  merge_duplicate_devices.py / repair_cross_site_links.py / repair_duplicate_links.py /
  repair_seed_flags.py / fix_bad_platforms.py   one-time/as-needed data-repair tools
  plugins/                 Command Runner plugins (see "Command Runner" below)
```

## First-time setup

Device inventory is already imported and the database already exists,
so a new person doesn't need to run `utilities/inventory_import.py` or
any of the CSV importers - that's a one-time step that's done. What's
left:

1. **Launch `gui.py`.** If no credential store exists yet for your OS
   login, it walks you through creating one right there (a master
   password), and immediately offers to set up your `tacacs`
   credential (the login SAD uses for every device) with a clean
   `Username:` / `Password:` prompt. **This master password is a new
   password you're inventing on the spot for this store specifically
   - it isn't your Windows login, your TACACS login, or any other
   existing password.** Pick something you haven't used elsewhere and
   can remember (or store in a password manager); there's no recovery
   path if you forget it (see "How the credential store is protected"
   below). If you skip the `tacacs` prompt, or need to add more
   credential types later (`cucm`, `vtc`), see "The credential store"
   gotcha below first - the flow for adding types *after* this
   first-run prompt is different and easy to get wrong.
2. **Name your sites and flag CDP seeds**, if any still need it:
   `gui.py`'s Site Manager tab (one at a time, interactively), or bulk
   with `python3 utilities/site_identity_import.py path/to/identities.csv`
   for names/codes and
   `python3 utilities/subnet_override_import.py path/to/overrides.csv`
   for any sites split by a non-/24 subnet boundary.
3. **Run discovery**: `gui.py`'s Discovery tab (recommended - live
   progress, no terminal babysitting) or `orchestrator.py` from the
   CLI. Run CDP first; ARP and MAC-table collection depend on CDP
   having already found the devices to poll.
4. **Generate the dashboard**: `gui.py`'s toolbar or
   `python3 dashboard_generate.py`.

From here, day-to-day use is: run Discovery periodically (all sites or
just the ones you care about) and re-run `dashboard_generate.py`.

## Tour: gui.py's tabs

- **Site Manager** — name/code a site, flag its CDP seed device (and,
  if different, its ARP seed or a device's ARP override IP), and
  review whatever's landed under the reserved "Unassigned" site (see
  the gotcha below).
- **Discovery** — run CDP/ARP/MAC-table collection against one site or
  all of them. Shows an overall progress bar plus one status column
  per active worker thread (color-coded by phase, hover for detail),
  and a live console below.
- **Export** — writes the phone/VTC and device CSV inventories under
  `dashboard/exports/` and regenerates the Exports page.
- **Command Runner** — SAD's one write-capable feature: preview
  (always a dry run) then commit a plugin's changes against real
  devices. Commit is disabled the instant scope/plugin/parameters
  change after a preview — you always have to re-preview the exact
  thing you're about to commit.
- **Credentials** — view/add/update/delete entries in the store this
  session already unlocked at login. Values are masked by default;
  revealing one is its own separate, logged action.

## Command Runner plugins

Plugins live in `utilities/plugins/`. Two shapes:
- **Per-device** (`plan(conn, device_row, params)`) — the harness
  resolves devices from SAD's own database and calls this once per
  device. `ip_helper_replace.py` and `update_interface_description.py`
  are this shape.
- **Standalone** (`run(log, params, credentials, commit)`) — the
  plugin owns its entire device list and connection flow itself, for
  devices SAD doesn't track or topologies a per-device loop can't
  express (e.g. an HA pair where you need to determine which node is
  master first). `bind_smtp_dataset.py` is this shape, and its own
  `commit=False` handling isn't harness-enforced the way a per-device
  plugin's is — read a standalone plugin's own code before trusting it
  in dry-run.

## Gotchas and intricacies

### The credential store: types are BAGS of fields, not flat keys

A credential isn't one entry called "username" and another called
"password." It's one **type** (e.g. `tacacs`, `cucm`, `vtc`), and that
type holds a small set of **named fields** you define yourself - e.g.
a `username` field and a `password` field, each with its own value.
This is why every place in the code that reads a credential looks two
levels deep: `creds["tacacs"]["username"]["value"]`, not
`creds["username"]`.

**The very first `tacacs` credential is the safe case.** As covered
above, the first time you launch `gui.py` with no store yet, it walks
you straight into a clean `Username:` / `Password:` form for `tacacs`
right after you set your master password - the field names get set
correctly (`username`, `password`) without you ever having to type a
field name yourself.

**Adding anything after that - a second `tacacs` down the road, or a
new type like `cucm`/`vtc` - is where the trap is, and it's an easy
one to walk into.** In the Credentials tab, "Add Type / Field" opens a
form with a **"Type name"** box, then rows of **"Field name"** and
**"Value"** boxes side by side - the Value box is masked (dots) by
default, since it defaults to "sensitive." There is no "is this a
standard username + password credential?" shortcut here the way there
is on first launch - you're filling in raw field name/value pairs
every time.

That layout invites exactly the wrong instinct: because the Value box
is masked and sits right next to a labeled row, it *looks* like
"Field name = what this is, Value = the secret," which nudges people
toward typing their actual **username** into the "Field name" box and
their actual **password** into the "Value" box. Do that and you get a
type with a field literally named after your username (e.g.
`svc_netadmin`), holding your password as its value, and **no field
named `username` or `password` at all** - `creds["cucm"]["username"]`
then raises a KeyError the first time anything tries to use it, and it
won't be obvious why just from looking at the Credentials tab (the
tree view will show *a* field with *a* value; it just won't be the one
the code is looking for).

**What to actually type:** for a normal username+password credential,
add two field rows - one row with Field name `username` and the
matching Value your login name, a second row with Field name
`password` and the matching Value your actual password. The "Field
name" is always the fixed word `username` or `password`, never your
real login name.

The reserved type names the rest of the code reads directly are
`tacacs` (`username`/`password`, read by every discovery and Command
Runner function), `cucm` (`username`/`password`, RIS access for phone
enrichment), and `vtc` (`username`/`password`, each VTC's own local
xAPI login) - those three field names have to be exactly
`username`/`password` for `cucm_enrich.py`/`orchestrator.py` to find
them. Any other type you add is yours to name and organize however you
like, field names included.

(The CLI tool `utilities/credential_manager.py` is an alternative way
to manage the same store and *does* have the "is this a standard
username + password credential? (Y/n)" shortcut for every type, not
just the first one - if you'd rather add `cucm`/`vtc` safely from a
terminal instead of the GUI's raw Field/Value form, that's the way to
do it.)

### How the credential store is protected

Each `credentials_<username>.enc` file is encrypted, not just hidden
or obscured - nobody can open it and read the stored logins, even with
direct file access to it, without your master password. Concretely:
the file's contents are encrypted with AES-256-GCM, and the actual
encryption key is derived from your master password (via PBKDF2, a
slow, deliberately expensive hashing process run 100,000 times) rather
than being your master password directly. Your master password itself
is never written to disk anywhere - not in the `.enc` file, not in a
config file, not in plaintext or otherwise. Each store also gets its
own random salt generated when it's created, so two people using the
same master password would still end up with completely different
encrypted files and different derived keys.

Practically, this means: someone else on a shared drive who can see or
even copy your `.enc` file still can't read what's in it without your
master password, and there's no "reset" or admin override that gets
your credentials back if you forget it - the master password is the
only thing that can decrypt that specific file, which is exactly what
makes it safe to leave sitting next to `sad.db` in the first place.

### Without `cucm`/`vtc` credentials, phone/VTC data looks complete but isn't

`tacacs` is the only credential the first-launch flow sets up for you.
`cucm` (CUCM/RIS access) and `vtc` (each VTC's own xAPI login) are
**not** configured by default - you have to add them yourself via the
Credentials tab or `credential_manager.py`. If they're missing, a scan
will still run, still find CDP-registered phones and VTC units, and
still populate rows for them - it does **not** fail loudly or skip
them. What you get instead is whatever CDP alone can see (hostname,
IP, model, switch/port), silently missing everything that only
`cucm_enrich.py`'s CUCM/RIS lookup or a VTC's own xAPI can supply
(e.g. registration status, extension, directory info, VTC call state).
The dashboard will look populated and correct at a glance - there's no
"credential missing" flag on individual rows - so this is easy to miss
entirely unless you already know to check for `cucm`/`vtc` in the
Credentials tab. If phone/VTC data looks thinner or less detailed than
expected, this is the first thing to check.

### The credential store is per-OS-user, automatically

`credential_store.resolve_credentials_path()` picks a filename based
on your OS login: if `credentials_<yourusername>.enc` already exists,
that's yours; otherwise, if a bare `credentials.enc` exists and you
have no per-user file yet, you inherit that one (so whoever was using
this before per-user files existed doesn't need to migrate anything);
otherwise you get a fresh `credentials_<yourusername>.enc` the first
time you add something. If several people share a machine or network
drive, don't be surprised to see multiple `credentials_*.enc` files
sitting next to `sad.db` — that's every person's own independently
password-protected store, not duplication to clean up.

### CDP seed, ARP seed, and ARP override IP - what each one actually controls

A **CDP seed** is the one device per site that discovery connects to
*first* to start the CDP walk for that site - it logs in, reads that
device's CDP neighbor table, then follows those neighbors outward to
find the rest of the site. Every site needs exactly one (flagged in
Site Manager, or auto-assigned by "Auto-flag seeds for single-device
sites" when a site only has one device). Without a seed, discovery has
nowhere to start and the site gets skipped.

An **ARP seed** is a separate flag, and most sites never need to touch
it: ARP collection uses the CDP seed automatically unless a site has
its own explicit ARP-seed override. You'd set one only when the CDP
seed isn't the right device to pull ARP from - for example, it's a
switch with no routed interfaces and no ARP table worth reading, while
the site's actual router (a different device) is. In that case, flag
the router as the ARP seed so ARP collection connects to it instead,
independent of whichever device is the CDP seed.

An **ARP override IP** is different again, and lives on a device, not
a site: it's a specific IP address tried *first* when connecting to
that device for ARP collection specifically, before falling back to
its normal ranked IPs (see "`device_ips` never overwrites, only adds"
below). The main use case is a shared HSRP/VRRP virtual IP (VIP) on an
HA pair - you want ARP collection to always land on whichever box is
currently active, not whichever one CDP happened to report. If you set
this for a VIP shared between two real devices, set it identically on
*both* devices, so it still works correctly even if the ARP-seed flag
later moves from one to the other.

All three are set from Site Manager (`gui.py`'s Site Manager tab, or
`utilities/site_manager.py` from the CLI).

### The write queue: writes are durable but not always immediate

`sad.db` lives on a network share, and multiple `gui.py` instances
(plus a multithreaded discovery scan's many worker threads) can all
want to write at once. SQLite's own file locking over SMB can't handle
that well, so nothing writes to `sad.db` directly — every write drops
a small JSON file into `write_queue/`, and whichever process currently
holds the drain lock applies the backlog in order. In practice this is
invisible almost all the time (a GUI action either applies immediately
or shows "queued - will apply shortly," and a scan's own writes block
until genuinely applied, via `queue_and_wait()`/`queue_batch_and_wait()`
in `write_queue.py`). Two things worth knowing:
- If `write_queue/` has leftover `.json` files sitting in it after
  everything should be done, something is durably failing to apply
  (not just "hasn't been picked up yet"). `verify_scan.py` (see below)
  checks this for you and prints the real error for anything stuck.
- A stale `.drain.lock` file older than 120 seconds is assumed
  abandoned (a crashed drainer) and gets stolen automatically — you
  should never need to delete it by hand.

### Logging: three different logs, for three different things

SAD keeps three separate kinds of log, and they don't overlap much:

- **The activity log** — lives inside `sad.db` itself (the
  `activity_log` table), not a separate file. Every notable action
  writes one row here automatically: logins and failed logins,
  discovery/ARP runs, dashboard generation, CSV exports, credential
  fields being added/updated/revealed/deleted, seed and ARP-override
  changes, and Command Runner runs - each with a timestamp, the OS
  username who did it, and a short description. This is a genuinely
  useful operational record for "who did what, and when" - it's
  **not** a hardened security audit trail, since anyone with file
  access to `sad.db` could in principle read or edit it directly (only
  `db.log_activity()` ever writes to it in normal use, but nothing
  stops a person with direct database access from doing otherwise).
  There's no dedicated viewer for it yet - it's queryable with any
  SQLite tool if you need to look something up.
- **Command Runner's per-run logs** — a full timestamped transcript
  under `logs/`, one file per Preview or Commit run, named after the
  plugin and the time it ran. This is the detailed record: every
  device the run touched, what it planned to change, and whether that
  change actually applied - the activity log's own Command Runner
  entry points at the exact file via its `detail_ref`. Worth checking
  after any Commit run, especially if a device or two didn't behave as
  expected.
- **Netmiko session logs** — raw bytes sent and received on the wire
  for every device connection a run makes, written to `session_logs/`.
  This is troubleshooting-only, off by default, and only available
  from the CLI (`orchestrator.py --session-log`, or answering yes to
  the session-log prompt in its interactive menu) - there's no
  equivalent toggle in `gui.py`'s Discovery tab. Turn it on only when
  actively debugging a connection problem, and treat anything in
  `session_logs/` as sensitive once created: because it's the literal
  raw session, it can include the credential exchange in the clear,
  so don't leave old session-log files sitting around longer than you
  need them.

### "Unassigned" isn't a real site

When a CDP-discovered neighbor can't be confidently attributed to any
known site (a WAN-facing address, an unrecognized hostname), it gets
filed under a reserved site literally named "Unassigned - Needs
Review" rather than guessed wrong or silently dropped. It never gets a
CDP seed device, so discovery deliberately skips it in "all sites"
runs (both `orchestrator.py` and `gui.py`) — you won't see it get
scanned, and that's correct, not a bug.

Check it periodically in Site Manager. **Note:** there's currently no
built-in way to actually reassign a device from Unassigned to its real
site once it's landed there - that's a real gap, not something you're
missing in the UI. Device site-reassignment is on the list of things
still to build.

### Two "last scanned" columns exist, and only one means what you'd think

`sites.last_cdp_discovery` is stamped only by a genuinely successful
CDP walk — it's the trustworthy signal for "was this site actually
scanned." `sites.last_run` is a separate, older column that still gets
bumped by unrelated operations (inventory imports, site creation,
etc.) — don't use it to judge scan freshness. `last_arp_collection`
and `last_mac_table_collection` work the same way as
`last_cdp_discovery`, each stamped only by its own successful phase.

### `device_ips` never overwrites, only adds

Every IP ever seen for a device is kept, tagged by source, and ranked
at read time (`manual` > `inventory_sync` > `cdp`) rather than the
newest report just overwriting the last one. This exists because
CDP-reported "management addresses" are sometimes wrong (e.g. a router
advertising an unroutable interface-local IP), so trusting whatever
came in most recently isn't safe. If a device's IP history looks like
it's accumulating old/wrong addresses, that's expected — what matters
is which one wins the ranking, not how many rows exist.

## Tools you probably won't need day-to-day

These live in `utilities/` and are useful to know about, but nothing
in normal day-to-day use requires them:

- **`verify_scan.py`** — a post-scan sanity report: per-site counts of
  devices/links/clients/ARP entries, flags for sites that look
  under-scanned, and a check of `write_queue/` for anything stuck.
  Handy after a big or unusual discovery run, or if something in the
  dashboard looks off and you want to confirm the underlying data
  actually landed. Run with `python3 utilities/verify_scan.py`.
- **`test_arp_live.py`** — connects to one real device and dumps its
  parsed VRF/ARP data to the console, without touching the database.
  For checking a parser against real gear before trusting a live scan
  with it.
- **`merge_duplicate_devices.py` / `repair_cross_site_links.py` /
  `repair_duplicate_links.py` / `repair_seed_flags.py` /
  `fix_bad_platforms.py`** — one-time or as-needed data-repair tools
  for specific known-drift scenarios. Each has its own docstring
  explaining exactly what it fixes and when you'd need it; not
  something to run speculatively.
