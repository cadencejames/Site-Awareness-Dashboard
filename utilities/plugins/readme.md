# Writing Command Runner plugins

This folder holds plugins for SAD's Command Runner (the "Command Runner"
tab in `gui.py`). The Command Runner and `orchestrator.py` handle
credentials, dry-run previews, the commit confirmation, and a
persistent audit log of every run — a plugin only has to describe what
it wants to do.

There are two plugin shapes, for two different situations:

- **Per-device plugins** (`plan()`) — for tasks against devices SAD
  already tracks in its own database. The harness resolves the device
  list from whatever scope you pick in the GUI (one site or all
  sites), connects to each device, and calls your plugin once per
  device. See `ip_helper_replace.py`.
- **Standalone plugins** (`run()`) — for tasks where the device list
  isn't SAD-tracked data at all (a fixed set of appliances SAD never
  discovers), or where the task's own connection topology doesn't fit
  "one device at a time" (an HA pair where you must check both nodes
  before knowing which one to act on). The plugin owns its entire
  device list and connection flow itself. See `bind_smtp_dataset.py`.

Both shapes show up in the same plugin dropdown and use the same
Preview/Commit buttons — the GUI adapts around whichever shape you
picked (for a standalone plugin, the scope picker is disabled, since
scope has no meaning for it).

This is SAD's only feature that can push real config to real devices.
Everything else in SAD only ever reads. Keep that in mind when writing
a plugin, whichever shape it is.

---

## Shape 1: Per-device plugins

A file only counts as a valid per-device plugin if it defines all four
of these. If any are missing, SAD silently skips the file (it won't
show up in the Command Runner's dropdown, and nothing crashes) — so a
typo in one of these names is the first thing to check if a plugin
isn't appearing.

```python
NAME = "Replace ip helper-address"
DESCRIPTION = "Finds interfaces with old ip helper-address values and replaces them with new ones."
PARAMS = [
    {"name": "old_ip_1", "label": "Old IP #1"},
    {"name": "new_ip_1", "label": "New IP #1"},
    {"name": "old_ip_2", "label": "Old IP #2 (optional)", "required": False},
    {"name": "new_ip_2", "label": "New IP #2 (optional)", "required": False},
]

def plan(conn, device_row, params):
    ...
```

**`NAME`** — a short string, shown in the Command Runner's plugin
dropdown and used to build the audit log's filename.

**`DESCRIPTION`** — a sentence or two, shown under the dropdown once
this plugin is selected. Say what it does and what it needs.

**`PARAMS`** — a list of dicts, one per input value you want the GUI to
prompt for. The Command Runner tab builds its parameter form directly
from this list, so you don't write any GUI code yourself. Each dict:

| Key | Required? | Meaning |
|---|---|---|
| `name` | yes | The key this value will appear under in the `params` dict `plan()` receives. |
| `label` | yes | The text shown next to the input field in the GUI. |
| `required` | no (defaults `True`) | If `True`, the GUI blocks Preview/Commit until this field is filled in. Set `False` for a genuinely optional value. |

Every value the GUI collects is a plain string — if a plugin needs a
number or wants to validate a value's shape (an IP address, say), do
that inside `plan()` itself.

### Writing `plan()`

```python
def plan(conn, device_row, params):
    ...
    return [
        {"description": "Gi1/0/1: remove [10.10.10.10] / add [10.10.10.20]",
         "commands": ["interface Gi1/0/1", "no ip helper-address 10.10.10.10", "ip helper-address 10.10.10.20"]},
    ]
```

**Arguments:**
- `conn` — a live, already-connected Netmiko connection to this one
  device. Use `conn.send_command("show ...")` to read whatever you
  need. This is a real Netmiko connection object, so anything Netmiko
  itself supports is available — but see the read-only rule below.
- `device_row` — this device's row from SAD's own database (a
  `sqlite3.Row`, so use `device_row["hostname"]`, `device_row["platform"]`,
  etc.). Useful if a plugin needs to branch on device type.
- `params` — a plain dict of whatever the user typed into the GUI's
  parameter form, keyed by each param's `name`. Values are always
  strings, and an optional field the user left blank will be an empty
  string, not missing — check for that (`if params.get("old_ip_2")`),
  don't assume every key is populated.

**Return value:** a list of *change-groups*. Each is a dict with:
- `"description"` — a short, human-readable summary shown in the
  dry-run preview and the audit log. This is the only thing the person
  running the plugin sees before deciding whether to commit, so make
  it say plainly what's about to change.
- `"commands"` — the actual list of config-mode command strings to
  send if this run gets committed.

**Return an empty list (`[]`) if this device needs no changes at all.**
That's the normal, expected result for most devices most of the time —
the harness just logs "No changes needed" and moves on. If none of a
plugin's parameters are filled in, or nothing on this device matches
what you're looking for, return `[]` immediately rather than doing any
work.

### Read-only, no exceptions

**`plan()` must never call anything that changes the device** — no
`conn.send_config_set()`, no `conn.save_config()`, nothing that writes.
Only ever call `conn.send_command()` (a `show` command or similar).

This matters because `plan()` runs in *both* dry-run and commit mode —
that's what makes a dry-run preview trustworthy in the first place: the
exact same code that decides what *would* change is the code that runs
before showing you the preview. If `plan()` ever changed something
itself, a "preview" wouldn't actually be a preview. The harness
(`orchestrator.run_plugin()`) is the only thing that ever calls
`send_config_set()`, and only after an explicit commit. **The harness
enforces this structurally for this shape** — see Shape 2 below for why
that's not true there.

### Worked example

`ip_helper_replace.py` in this folder is a complete, working per-device
plugin — read it alongside this guide. It was ported from a real
standalone script: the script's "decide what needs to change" logic
became `plan()` almost unchanged; only the script's own
CLI/connection/apply code went away, since the harness already does
all of that.

---

## Shape 2: Standalone plugins

Use this shape when a task's devices aren't in SAD's own database, or
when the task needs to touch more than one device to even decide what
to do (an HA pair, checking both nodes before knowing which is master)
in a way a strict one-device-at-a-time loop can't express.

A file only counts as a valid standalone plugin if it defines `NAME`,
`DESCRIPTION`, and `PARAMS` (same meaning as Shape 1 above), plus:

```python
def run(log, params: dict, credentials: dict, commit: bool) -> None:
    ...
```

**A file must define exactly one of `plan` or `run` — never both, never
neither.** Defining both (or neither) makes SAD treat the file as
invalid and silently skip it, the same as a missing `NAME`.

**Arguments:**
- `log` — a function; call `log("some line")` to write a line to both
  the GUI's live console and the persistent audit log file, the same
  place a per-device plugin's output ends up. Call `log()` with no
  argument for a blank line.
- `params` — same idea as Shape 1: a plain dict of whatever the user
  typed into the GUI's parameter form, keyed by param name, values
  always strings.
- `credentials` — a dict shaped `{"username": ..., "password": ...,
  "secret": ...}`, ready to hand straight to Netmiko's `ConnectHandler`
  as `**credentials`. `secret` (an enable/privileged-mode password) is
  `None` if the user's `tacacs` credential entry doesn't have one
  configured — Netmiko handles a `None` secret fine.
- `commit` — `True` if this is a real run, `False` if it's a dry-run
  preview. **Your plugin is entirely responsible for honoring this**
  — see below.

**This function owns its ENTIRE execution flow** — connecting to
whatever devices it needs (via `from netmiko import ConnectHandler`
directly, same as a standalone script would), deciding what to do,
and actually doing it.

### Dry-run safety is YOUR responsibility for this shape

This is the most important difference from Shape 1. A per-device
plugin's `plan()` is *structurally* prevented from changing anything —
the harness is the only thing that ever calls `send_config_set()`, and
only in commit mode. A standalone plugin's `run()` has no such
guardrail, because it does everything itself, including deciding
whether to apply anything at all.

**Every place your plugin would change a device must be guarded by an
explicit check on `commit`, and nothing should be sent unless that
check passes.** Look at `bind_smtp_dataset.py`'s `_process_pair()` for
the pattern: it always determines what commands *would* run and logs
them, but only actually sends them past an `if not commit: return`
check. Get this wrong and dry-run stops being a real preview — it
becomes a run that just happens to also show output first.

### Worked example

`bind_smtp_dataset.py` in this folder is a complete, working standalone
plugin. It manages 2 fixed Citrix ADC HA pairs (not SAD-tracked
devices), checks both nodes of a pair to find the current master
before doing anything, and refuses to guess (logs an error and moves
to the next pair) if it can't cleanly identify exactly one master —
worth reading as an example of a plugin author's own safety
discipline, since the harness can't enforce that discipline for this
shape the way it does for Shape 1.

Its `ADC_PAIRS` device list is a fixed constant at the top of the file,
edited directly rather than exposed as a GUI param — like `CUCM_HOST`
in `cucm_enrich.py`, this is infrastructure config that rarely changes,
not something worth typing in on every run. Only genuinely per-run
values (like the IP being bound) become declared `PARAMS`.

---

## Testing a plugin before you ever run it for real

Both shapes are easy to test completely offline, with fake connections
standing in for real devices — no real device involved at all.

**The recommended way: put the test right in the plugin file itself**,
in an `if __name__ == "__main__":` block at the end. This is a standard
Python pattern - that block only runs when you execute the file
directly (`python3 some_plugin.py`); it's never triggered by normal
plugin loading (`list_plugins()` *imports* plugin files, which never
runs their `__main__` block), so there's no risk of a self-test
interfering with the Command Runner. All the plugins in this folder
follow this pattern - read any of them for a real example.

A Shape 1 (`plan()`) self-test just needs a fake `conn` with a
`send_command()` method:

```python
if __name__ == "__main__":
    class _FakeConn:
        def send_command(self, cmd):
            return "interface Gi1/0/1\n ip helper-address 10.10.10.10\n!\nend\n"

    changes = plan(_FakeConn(), {"hostname": "test-sw"}, {
        "old_ip_1": "10.10.10.10", "new_ip_1": "10.10.10.20",
        "old_ip_2": "", "new_ip_2": "",
    })
    print(f"Found {len(changes)} change(s):")
    for change in changes:
        print(f"  {change['description']}")

    assert len(changes) == 1  # adjust to whatever your sample config should produce
    print("\nSelf-test passed.")
```

A Shape 2 (`run()`) self-test needs to fake out `ConnectHandler` itself
(with `unittest.mock.patch`), since the plugin makes its own
connections. See `bind_smtp_dataset.py`'s own `__main__` block for a
complete, real example, including faking two different nodes of a pair
reporting different HA states.

Run either with `python3 utilities/plugins/your_plugin.py` and check
the output - no separate test script or REPL session needed. Worth
doing this for any new plugin before ever pointing it at a real
device. The Command Runner's own Preview button is the next, real-
device version of this same check: dry-run runs the identical logic
against real devices, with nothing applied, so it's the natural next
step once a plugin's offline self-test looks right.

## Checklist for a new plugin

**Both shapes:**
- [ ] File lives in `utilities/plugins/`, ends in `.py`, doesn't start with `_`
- [ ] Defines `NAME`, `DESCRIPTION`, `PARAMS`, and **exactly one** of `plan()` / `run()`
- [ ] Every `PARAMS` entry has `name` and `label`; `required: False` set on anything genuinely optional
- [ ] Has an embedded `if __name__ == "__main__":` self-test, and it passes when run directly, before ever running Preview against a real device

**Per-device (`plan()`) only:**
- [ ] Only ever calls `conn.send_command()` — never anything that writes
- [ ] Returns `[]` when there's nothing to do, not an empty-ish truthy value
- [ ] Each returned change's `"description"` is something you'd trust seeing in a dry-run before committing

**Standalone (`run()`) only:**
- [ ] Every place the device would actually change is guarded by an explicit `commit` check
- [ ] Logs what it *would* do in both modes, via `log(...)`, not just when `commit` is `True`
- [ ] Refuses to guess and clearly reports why, rather than proceeding on ambiguous state (see `bind_smtp_dataset.py`'s master-election refusal for the pattern)

## Having an AI write a plugin for you

Everything above this section is written so an AI assistant can follow
it directly. To have one write a plugin for you: copy this entire
README into a chat with your AI of choice, paste the prompt block
below at the end of it, fill in your own request where marked, and
send it.

```
You are writing a plugin for SAD's Command Runner feature, using the
documentation above as the complete and authoritative spec for what a
plugin file must contain. Follow it exactly.

First, decide which shape fits my request: Shape 1 (plan()) if the
task is against devices SAD already tracks in its own database, one
at a time. Shape 2 (run()) if the devices aren't SAD-tracked at all,
or if the task needs its own connection topology (like checking two
nodes of an HA pair before knowing which to act on) that a strict
one-device-at-a-time loop can't express. If it's genuinely unclear
which shape fits, ask me rather than guessing.

What to produce, for either shape:
- One complete, ready-to-save .py file - nothing else should be needed
  to drop it into utilities/plugins/ and have it work.
- Define NAME, DESCRIPTION, PARAMS, and exactly one of plan() or run(),
  exactly as documented above.
- Include an embedded self-test: an `if __name__ == "__main__":` block
  at the end of the file (following the "Testing a plugin" section
  above) that exercises the plugin against fake connections and
  asserts the result is correct, so I can run the file directly and
  confirm it behaves correctly before ever pointing it at a real
  device. This block must never run during normal plugin loading -
  only when the file is executed directly.
- If anything about my request below is ambiguous - exact command
  syntax, which platforms it needs to handle (IOS vs NX-OS), what
  should or shouldn't count as a match, which shape fits - ask me
  before writing anything, rather than guessing.

If Shape 1 (plan()):
- plan() must ONLY ever call conn.send_command() (read-only "show"
  commands). NEVER call conn.send_config_set(), conn.save_config(), or
  anything else that would change a device - the surrounding system is
  what actually applies changes, and only after a human has explicitly
  reviewed a dry-run and approved it. This rule has no exceptions.
- Return [] when a given device needs no changes at all.
- Give every returned change a clear, specific, human-readable
  "description" - this is what a person will actually read in a
  dry-run before deciding whether to commit, so it needs to be
  something they can trust.

If Shape 2 (run()):
- run() is responsible for its own dry-run safety - there is no
  structural guarantee from the surrounding system the way there is
  for plan(). Every place the plugin would change a device MUST be
  guarded by an explicit check on the commit argument, with nothing
  sent unless that check passes. Log what would happen in BOTH modes,
  via the provided log() function, not only when commit is True.
- If the task involves anything ambiguous or ambiguous-state-like
  (e.g. electing a master between nodes), refuse to proceed and log a
  clear reason rather than guessing - never silently pick one.

My request:
<describe what you want the plugin to do here>
```