# fx-pedal

A USB-audio guitar multi-effects pedal built on an Arduino UNO Q, using
PipeWire's `filter-chain` module as a real-time LADSPA effects host and a
small daemon + CLI/console to arrange effects, tweak parameters live, and
manage presets and banks.

Not a toy passthrough demo — this drives real audio, with the effect chain
running as a proper host-level PipeWire config, latency-tuned and
concurrency-safe, controllable from a plain terminal today and designed to
be controllable from a touchscreen, Bluetooth app, or MIDI foot controller
later without any redesign.

## Requirements

- An Arduino UNO Q (or any Linux box running PipeWire 1.x — nothing here is
  actually UNO-Q-specific except the historical context)
- A class-compliant USB audio interface (tested with an IK Multimedia iRig
  USB; auto-detected, not hardcoded — see "Multiple/no audio device found"
  below)
- `python3` (3.11+), `pipewire`, `wireplumber`
- LADSPA plugins: `caps swh-plugins ladspa-sdk wah-plugins tap-plugins`
  (Debian/Ubuntu package names — `install.sh` checks for these and tells
  you the exact command if any are missing)

## Hardware setup — read this before `install.sh`, especially on a UNO Q

Getting a USB audio interface talking to this board reliably was the
hardest part of this whole project — harder than any of the software. If
`lsusb` doesn't show your interface at all, or it shows up and then
disconnects every ~30 seconds, it's almost certainly one of these two
things, in the order to check them. **The single biggest lever: use a
good externally-powered USB hub between the board and your interface** —
specifically, the Arduino-branded hub bundled with the UNO Q is confirmed
working, plugged in with a plain USB-C-to-USB-C cable.

### 1. Board stuck in USB *device* mode instead of *host* mode

Check: `lsusb` should list more than just root hubs. If it's empty (or only
shows `1d6b:0002`/`1d6b:0003` Linux root hubs with nothing behind them),
the board's USB-C port has come up as a device (sink), not a host — it
can't see anything plugged into it.

**Don't trust `/sys/class/typec/port0/data_role`** — on this board it's
read-only and can keep reporting `device` even when the port is genuinely
acting as a host. Trust `lsusb` instead.

Fixes, in order of preference:
- **Use a real, externally-powered USB hub between the board and your
  interface**, rather than plugging the interface straight into the board.
  Specifically, the **Arduino-branded hub bundled with the UNO Q** (the one
  with an Ethernet port) got this working reliably in this project, over a
  plain USB-C-to-USB-C cable — no manual override, no cable gymnastics
  needed. Not every hub behaves the same here (see #2 below for a
  topology-related failure mode with a different hub), so if a random hub
  doesn't work, trying this specific one is worth it before assuming
  something's wrong with the board.
- If that's not enough, force it: `sudo sh -c 'echo host >
  /sys/kernel/debug/usb/4e00000.usb/mode'`. This directly overrides the
  DWC3 USB controller's mode via debugfs, bypassing whatever the CC-pin
  negotiation got wrong. **Not persistent** — resets on reboot. (There's a
  small community fix — search "arduino-uno-q-usb-fix" — that turns this
  into a boot-time systemd service if you want it permanent; read it before
  running it, same as with any third-party root-level script.)

**A note on cable type**, since this project's own notes went back and
forth on it: USB-C-to-USB-C cables were suspected as a cause early on
(seen failing both "stuck in device mode" and "hub wouldn't power on at
all" with a *different*, lower-quality hub), but **the actual working
setup ended up being all USB-C-to-USB-C, with the Arduino-branded hub** —
so the hub itself, not the cable type, looks like it was the real
variable. Don't rule out a C-to-C cable on the strength of this alone; if
you hit connection trouble, suspect the hub/cable combination together
rather than assuming either one in isolation.

### 2. Interface enumerates, then disconnects on a repeating cycle

Symptom: `dmesg` shows the interface enumerate, then fail (`Feature Unit`
control errors, HID `-71`/EPROTO), then `USB disconnect` — repeating every
~30 seconds indefinitely.

This turned out to be about **hub topology, not power or drivers**. A
2-port hub that put the interface 3 tiers deep behind cascaded internal hub
chips (`lsusb -t` showing something like
`root_hub → Hub → Hub → your-interface`) reproduced this reliably. The
fix was switching to a different hub whose internal port layout put the
interface only 1 tier deep. If you hit this: try a different hub, or a
different port on the same hub — `lsusb -t` will show you exactly how deep
your interface sits, and shallower is better. USB autosuspend was ruled out
as a cause in this project (`/sys/module/usbcore/parameters/autosuspend`
disabled globally made no difference) — don't waste time there first.

### Once your interface shows up cleanly

`aplay -l` / `arecord -l` should list it as a card. From there,
`fx-pedal audio-device list` (after installing, below) should detect it
automatically — no manual configuration needed.

## Install

```
git clone <this-repo> ~/fx-pedal   # or wherever you like
~/fx-pedal/install.sh
```

This symlinks the CLI onto your `PATH` as `fx-pedal`, installs the
`fx-pedal-daemon` systemd `--user` unit (**not enabled** — you opt in), and
checks (doesn't silently install) the apt packages above.

```
fx-pedal audio-device list      # confirm your interface is detected
fx-pedal list | head             # see available effects
fx-pedal add foverdrive          # try one
fx-pedal chain                   # see what's loaded
```

## Two ways to use it

**One-shot**: `fx-pedal <command> ...` runs, does one thing, exits. This is
what every example below uses.

**Daemon + interactive console**: start a background daemon once, then
attach to it any time with an interactive prompt instead of a subprocess
per command:

```
systemctl --user start fx-pedal-daemon   # or: fx_daemon.py in the foreground

$ fx-pedal console
fx-pedal console. Type 'h' for help, 'q' to quit.
Shortcuts: l=list  p=params  c=chain  a=add  r=remove  s=set
fx-pedal> a chebstortion "Distortion=1.2"
Added 'Chebyshev distortion' (chebstortion) as 'chebstortion'. Chain is now: drive (foverdrive) -> chebstortion (chebstortion)
fx-pedal> s drive "Drive level" 2.5
Set drive:Drive level = 2.5 (live, no restart needed)
fx-pedal> preset save crunch
Saved preset 'crunch': drive (foverdrive) -> chebstortion (chebstortion)
fx-pedal> bank set-slot "Live Set 1" 1 crunch
Bank 'Live Set 1' slot 1 (PC 1) = crunch
fx-pedal> q
```

Quote multi-word args (control names, bank names with spaces). Every
console command has a one-letter shortcut and a full name (`l`/`list`,
`a`/`add`, ...) — `h` lists them all. `preset` and `bank` take their usual
subcommands as arguments.

The daemon isn't required for anything — it exists so a persistent client
(this console, and later a BLE server or an MCU Bridge handler for a
touchscreen) can hold a live connection instead of spawning a subprocess
per command. Newline-delimited JSON over a Unix socket at
`$XDG_RUNTIME_DIR/fx-pedal.sock`, dispatching straight to the same
`fx_core` functions the one-shot CLI calls — behavior is identical either
way, and running the daemon alongside one-shot commands is completely safe
(both go through `fx_core`'s own file locking).

`systemctl --user enable --now fx-pedal-daemon` makes it persistent across
logins; add `sudo loginctl enable-linger <user>` if you want it running
with nobody logged in at all (a plain systemd `--user` service otherwise
only starts once someone logs in).

**Rule: always use this tool. Never hand-edit `10-fx-pedal.conf` or the
`fx-pedal-*.json` state files directly** (they live in
`~/.config/pipewire/`, alongside the generated PipeWire config — see
"Where things live" below). The tool keeps the config, the state files, and
the actually-running PipeWire graph in sync, including rolling back
automatically if a change fails to start; editing by hand breaks that
guarantee.

## The one thing to understand: `live` vs `topology`

PipeWire can change a plugin's **parameter values** instantly, with no
audio interruption. It **cannot** change the effect chain's **shape**
(which plugins, in what order) without restarting the whole audio graph —
about 3 seconds of silence.

- `set`, and `preset load` **when the target preset uses the same effects
  in the same order** as what's currently loaded, are **instant**
  (`"live"`) — no restart.
- `add`, `remove`, and `preset load` **when the effect chain itself
  differs** are **`"topology"`** changes — expect a brief silence.

You don't have to think about which is which — `preset load` checks the
shape automatically and only pays the restart cost when the chain actually
needs to change — but it's worth knowing why loading one preset feels
instant and another doesn't.

## Command reference

### `audio-device list` / `audio-device select <n>`

Every USB audio device PipeWire currently sees, cross-referenced for
`device.bus == usb` so it's not tied to any specific interface. If exactly
one is plugged in, it's used automatically — no configuration needed. If
zero or multiple are found, every command that touches audio returns a
clear error telling you to run these.

```
$ fx-pedal audio-device list
1. iRig USB  (capture/playback)

(no device pinned - auto-detecting; only works while exactly one is plugged in)

$ fx-pedal audio-device select 1
Selected 'iRig USB' as the active audio device.
```

Selecting pins your choice in `fx-pedal-audio-device.json`, so it keeps
being used even after a second interface gets plugged in later.

### `list [--all]`

Installed LADSPA plugins usable in this chain (label + name) — i.e. the
ones `add` will actually accept. Plugins that aren't simple mono (1 audio
in, 1 audio out) are hidden by default, since trying to `add` one just
fails; a count of how many were hidden is shown instead of silently
dropping them.

```
$ fx-pedal list
foverdrive                   Fast overdrive
chebstortion                 Chebyshev distortion
autowah                      Auto Wah
...

(58 plugin(s) hidden - not mono in/out, would fail `add`. Use `list --all` to see them.)
```

`--all` shows everything, tagging each incompatible one:

```
$ fx-pedal list --all
...
CompressX2                   C* CompressX2 - Stereo compressor and saturating limiter  [not usable here - not mono in/out]
...
```

A single `.so` file can bundle several differently-shaped plugins under
different labels (`caps.so` alone has 26) — compatibility is checked and
reported per label, not per file.

### `params <label>`

A plugin's ports — what you can control, and its valid range/default.

```
$ fx-pedal params foverdrive
Fast overdrive  (foverdrive, /usr/lib/ladspa/foverdrive_1196.so)
  [control] Drive level: 1.0 to 3.0, default 1.0
  [audio input] Input
  [audio output] Output
```

### `chain`

What's currently loaded, and whether any value has been tweaked live since
it was last saved to a preset (shown as `[LIVE VALUE: ...]`).

```
$ fx-pedal chain
pipewire.service: active

drive  (foverdrive, /usr/lib/ladspa/foverdrive_1196.so)
    Drive level = 2.0
```

### `add <label> [--control "Name=value" ...]`

Appends an effect to the end of the chain. **Topology change** (~3s).
Unspecified controls use the plugin's default. Effect instance names are
auto-generated from the label (`chebstortion`, `chebstortion2`, ... if you
add the same plugin twice).

```
$ fx-pedal add chebstortion --control "Distortion=1.2"
Added 'Chebyshev distortion' (chebstortion) as 'chebstortion'.
Chain is now: drive (foverdrive) -> chebstortion (chebstortion)
```

**Only simple mono (1 audio in, 1 audio out) plugins are supported** — this
covers essentially every classic pedal effect (distortion, overdrive, wah,
compression), but rejects plugins with stereo output, like most reverbs
(`plate`, `tap_reverb`). Not yet solved.

### `remove <name>`

Removes an effect by its instance name (as shown by `chain`). **Topology
change.**

```
$ fx-pedal remove chebstortion
Removed 'chebstortion'. Chain is now: drive (foverdrive)
```

### `set <name> <control> <value>`

Changes a parameter **live** — instant, no restart, no audio interruption.
This is the one you'd wire a knob to.

```
$ fx-pedal set drive "Drive level" 2.5
Set drive:Drive level = 2.5 (live, no restart needed)
```

A value outside the plugin's documented range gets a warning but is still
applied.

### `preset save/load/list/delete <name>`

A preset is a named snapshot of the whole chain (which effects, in order,
with their current values — including any live `set` tweaks, pulled in
automatically at save time).

```
$ fx-pedal preset save clean-boost
Saved preset 'clean-boost': drive (foverdrive)

$ fx-pedal add chebstortion --control "Distortion=1.2"
$ fx-pedal preset save crunch
Saved preset 'crunch': drive (foverdrive) -> chebstortion (chebstortion)

$ fx-pedal remove chebstortion
$ fx-pedal preset load crunch
Loaded preset 'crunch': drive (foverdrive) -> chebstortion (chebstortion)
```

Loading `crunch` right after `clean-boost` (different chain shapes) pays the
~3s restart. Loading `crunch` twice in a row, or switching back and forth
between presets that share the same effect chain but different knob values,
is instant.

`preset delete` warns (doesn't block) if the preset is still referenced by
a bank slot.

### `bank list/show/create/set-slot/delete/midi-map`

Banks are ordered groups of presets, for foot-controller-style switching
(e.g. an iRig Blueboard sending Bluetooth MIDI Program Change messages —
not wired up yet, but this is the data model it'll consume). A bank has a
fixed number of slots (`slots_per_bank`, default 8); each slot holds a
preset name or is empty. The MIDI Program Change number for any slot is
**derived**, not stored: `pc = bank_index * slots_per_bank + slot_index` —
reordering or renaming banks keeps this consistent automatically.

```
$ fx-pedal bank create "Live Set 1"
Created bank 'Live Set 1' (8 slots).

$ fx-pedal bank set-slot "Live Set 1" 0 clean-boost
$ fx-pedal bank set-slot "Live Set 1" 1 crunch

$ fx-pedal bank show "Live Set 1"
Bank 'Live Set 1':
  slot 0 (PC   0): clean-boost
  slot 1 (PC   1): crunch
  slot 2 (PC   2): (empty)
  ...

$ fx-pedal bank midi-map
PC   0  (Live Set 1 slot 0):  clean-boost
PC   1  (Live Set 1 slot 1):  crunch
```

Clear a slot with `bank set-slot <bank> <slot> --clear`.

`bank midi-map` is exactly what a future MIDI Program-Change handler should
consume — it doesn't need to know about banks or slots at all, just
"PC number → preset name". `fx_core.load_preset_for_pc(pc)` does the full
lookup-and-load in one call.

### `nam-model get` / `nam-model set <path>`

Get/set the `.nam` capture file loaded by the
[nam-ladspa](nam-ladspa/README.md) plugin (label `nam_amp`) — real-time
neural amp/pedal modeling as a LADSPA effect, wrapping
[NeuralAmpModelerCore](https://github.com/sdatkinson/NeuralAmpModelerCore).
Not installed by default; see `nam-ladspa/README.md` for build/install.

LADSPA has no file/string ports, so the model path can't be a normal `set`
control — it's read once when the plugin loads. **Topology change** (~3s),
even though the chain shape doesn't change, because the only way to make
the plugin re-read a new file is to restart it.

```
$ fx-pedal add nam_amp
Added 'Neural Amp Modeler (NAM Core)' (nam_amp) as 'namamp'.
Chain is now: drive (foverdrive) -> namamp (nam_amp)

$ fx-pedal nam-model set ~/models/marshall-plexi.nam
NAM model set to '/home/arduino/models/marshall-plexi.nam'. Reloaded.
```

fx-pedal only manages a symlink at a fixed handoff path
(`~/.config/fx-pedal/nam_model.nam`) pointed at whatever file you give it —
it doesn't copy or import the `.nam` file's contents, so an external app
(or you) can manage a whole library of captures anywhere on disk and just
tell fx-pedal which one to point at. After restarting, it checks the
journal for the plugin's own load diagnostics and surfaces a warning if the
model failed to load (bad file, wrong format) instead of just trusting that
PipeWire came back up.

## Where things live

**Source** (this repo): `fx_ctl.py` (CLI), `fx_core.py` (all real logic —
`import` this from another interface, don't shell out to the CLI from a
long-running process), `fx_daemon.py` (the socket server),
`systemd/fx-pedal-daemon.service`.

**Runtime state** (`~/.config/pipewire/`, created on first use):
- `pipewire.conf.d/10-fx-pedal.conf` — the actual PipeWire config,
  regenerated on every topology change.
- `fx-pedal-chain.json` — the currently-loaded chain.
- `fx-pedal-presets.json` — saved presets.
- `fx-pedal-banks.json` — banks.
- `fx-pedal-audio-device.json` — pinned audio device, if you ran
  `audio-device select` (absent = auto-detect).
- `fx-pedal.lock` — internal, serializes concurrent operations. Never
  needs manual attention.

**`~/.config/fx-pedal/nam_model.nam`** (not under `~/.config/pipewire/`,
since it's not PipeWire-specific state) — a symlink managed by
`nam-model set`, pointing at whichever `.nam` file the
[nam-ladspa](nam-ladspa/README.md) plugin currently loads.

None of the runtime state is checked into this repo (see `.gitignore`) —
it's regenerated per-install/per-board.

## Design note: why this is a plain PipeWire config, not an "Arduino App"

On a UNO Q, the obvious way to package something like this is as an
`arduino-app-cli` App (Docker container, managed lifecycle, `app start`/
`app logs`). That was the first version — and it worked, but with real
round-trip latency in the **40–90ms** range, because the App container has
no path to real-time thread scheduling (no `/run/dbus`, no `cap_add`,
unprivileged). Moving the exact same PipeWire `filter-chain` config to run
directly in the host's own PipeWire session (what `install.sh` sets up)
turned out to *not* fix real-time scheduling either — that's a separate,
harder constraint (`rtkit-daemon` wants an actively logged-in seated
session, which a headless board doesn't have) — but it did let the graph's
**quantum** (buffer size) be tuned down safely, based on where xruns
actually start under normal (non-realtime) scheduling: clean up to 192–256
samples (~4–5ms/cycle) on this hardware, glitching from ~128 samples down.
That's what `fx_core.py` sets by default, bringing round-trip latency to
roughly **10–15ms** — usable for real-time monitoring, without needing
real-time scheduling at all. If you're adapting this for different
hardware, that xrun cliff is worth re-measuring rather than assumed (`pw-
metadata -n settings 0 clock.force-quantum <N>` to test a value, `pw-top -b`
to watch the `ERR` column for a stable window before trusting it — the
counter is cumulative, diff two samples rather than reading one).

## Writing another interface against this

Don't shell out to `fx_ctl.py` from another long-running process — `import
fx_core` directly (or talk to the daemon's Unix socket — see `fx_daemon.py`'s
`METHODS` table for the exact protocol) and call its functions
(`add_effect`, `remove_effect`, `set_control`, `preset_save/load/list/
delete`, `bank_*`, `load_preset_for_pc`, `audio_device_list/select`,
`nam_model_get/set`). Every
function takes its own lock and returns a plain dict — `{"ok": bool,
"kind": "live"|"topology", "message"|"error": str, ...}` — no printing, no
`sys.exit`, safe to call from anywhere.

## License

MIT — see `LICENSE`.
