"""Core logic for the fx-pedal LADSPA effect chain: plugin discovery, chain
add/remove/set, presets, and banks. Every mutating function here takes an
exclusive file lock before touching state, and returns a plain
JSON-serializable dict - no printing, no sys.exit - so this module can be
called equally from the fx_ctl.py CLI, a future daemon, a Bridge handler, or
a BLE GATT server without any of them re-implementing this logic.

Every mutating result carries "kind": "live" (instant, no audio
interruption) or "topology" (triggers a PipeWire restart, ~3s silence) -
the one piece of PipeWire-specific truth every future caller needs, so it
only has to be encoded once, here.

The graph topology (which effects, in what order) can't be changed live in
PipeWire - "topology" operations rewrite
~/.config/pipewire/pipewire.conf.d/10-fx-pedal.conf and restart
pipewire.service. Chain state lives in fx-pedal-chain.json, presets in
fx-pedal-presets.json, banks in fx-pedal-banks.json, all alongside this
file. Only simple mono (1 audio in, 1 audio out) LADSPA plugins are
supported - that covers essentially every classic guitar-pedal-style effect
(distortion, overdrive, wah, compression, ...). The chain is always
terminated with a mono-to-stereo fan-out (two builtin 'copy' nodes) so
output drives both channels.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LADSPA_PATH = Path("/usr/lib/ladspa")
CONF_PATH = Path.home() / ".config/pipewire/pipewire.conf.d/10-fx-pedal.conf"
STATE_PATH = Path.home() / ".config/pipewire/fx-pedal-chain.json"
PRESETS_PATH = Path.home() / ".config/pipewire/fx-pedal-presets.json"
BANKS_PATH = Path.home() / ".config/pipewire/fx-pedal-banks.json"
LOCK_PATH = Path.home() / ".config/pipewire/fx-pedal.lock"
SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "fx-pedal.sock"
AUDIO_DEVICE_CONFIG_PATH = Path.home() / ".config/pipewire/fx-pedal-audio-device.json"

DEFAULT_SLOTS_PER_BANK = 8
LOCK_TIMEOUT_S = 10

ENV = {**os.environ, "LADSPA_PATH": str(LADSPA_PATH)}

PORT_RE = re.compile(
    r'^"([^"]+)" (input|output), (control|audio)'
    r'(?:, (-?[\d.]+) to (-?[\d.]+)(?:, default (-?[\d.]+))?)?$'
)


class LockTimeout(Exception):
    pass


class _locked:
    """Exclusive lock over the whole read-modify-write-apply critical
    section of any mutating operation, so two overlapping callers (two SSH
    terminals, a future BLE handler racing a physical encoder, etc.) can't
    interleave writes or stack concurrent `systemctl restart` calls."""

    def __enter__(self):
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(LOCK_PATH, "a")
        deadline = time.time() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() >= deadline:
                    self._fh.close()
                    raise LockTimeout(
                        f"Could not acquire fx-pedal.lock within {LOCK_TIMEOUT_S}s "
                        "- another fx_ctl.py operation appears stuck."
                    )
                time.sleep(0.05)

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def scan_plugins():
    """Return [{so_path, name, id, label}, ...] for every installed LADSPA plugin."""
    result = subprocess.run(["listplugins"], capture_output=True, text=True, env=ENV)
    plugins = []
    current_so = None
    for line in result.stdout.splitlines():
        m = re.match(r"^(\S+\.so):$", line)
        if m:
            current_so = m.group(1)
            continue
        m = re.match(r"^\t(.+) \((\d+)/(\S+)\)$", line)
        if m and current_so:
            plugins.append(
                {"so_path": current_so, "name": m.group(1),
                 "id": int(m.group(2)), "label": m.group(3)}
            )
    return plugins


def find_plugin(label):
    return next((p for p in scan_plugins() if p["label"] == label), None)


def get_ports(so_path):
    """Return the port list for a plugin (name/direction/type/min/max/default)."""
    result = subprocess.run(["analyseplugin", so_path], capture_output=True, text=True, env=ENV)
    ports = []
    in_ports = False
    for line in result.stdout.splitlines():
        if line.startswith("Ports:"):
            line = line[len("Ports:"):]
            in_ports = True
        if not in_ports:
            continue
        line = line.strip()
        if not line:
            continue
        m = PORT_RE.match(line)
        if m:
            name, direction, ptype, lo, hi, default = m.groups()
            ports.append({
                "name": name, "direction": direction, "type": ptype,
                "min": float(lo) if lo is not None else None,
                "max": float(hi) if hi is not None else None,
                "default": float(default) if default is not None else None,
            })
    return ports


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"chain": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def load_presets():
    if PRESETS_PATH.exists():
        return json.loads(PRESETS_PATH.read_text())
    return {}


def save_presets(presets):
    PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRESETS_PATH.write_text(json.dumps(presets, indent=2))


def load_banks():
    if BANKS_PATH.exists():
        return json.loads(BANKS_PATH.read_text())
    return {"slots_per_bank": DEFAULT_SLOTS_PER_BANK, "banks": []}


def save_banks(banks):
    BANKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BANKS_PATH.write_text(json.dumps(banks, indent=2))


# ---- audio device detection ----
#
# No specific interface (iRig or otherwise) is hardcoded - every USB audio
# device currently visible to PipeWire is discovered via `pw-dump`, cross-
# referencing each Sink/Source Node's `device.id` back to its parent Device
# object to check `device.bus == "usb"` (that property lives on the device,
# not the node, so a single node-level query alone will miss it).

class AudioDeviceError(RuntimeError):
    pass


def list_usb_audio_devices():
    """Return [{device_id, description, source, sink}, ...] for every USB
    audio device PipeWire currently sees. `source`/`sink` are the
    node.name values to use as target.object (None if that device doesn't
    expose that direction)."""
    result = subprocess.run(["pw-dump"], capture_output=True, text=True)
    data = json.loads(result.stdout)
    by_id = {obj["id"]: obj for obj in data}
    devices = {}
    for obj in data:
        props = obj.get("info", {}).get("props", {})
        media_class = props.get("media.class")
        if media_class not in ("Audio/Sink", "Audio/Source"):
            continue
        dev_id = props.get("device.id")
        dev_props = by_id.get(dev_id, {}).get("info", {}).get("props", {})
        if dev_props.get("device.bus") != "usb":
            continue
        entry = devices.setdefault(dev_id, {
            "device_id": dev_id,
            "description": dev_props.get("device.description") or f"USB device {dev_id}",
            "source": None, "sink": None,
        })
        key = "sink" if media_class == "Audio/Sink" else "source"
        entry[key] = props.get("node.name")
    return sorted(devices.values(), key=lambda d: d["device_id"])


def load_audio_device_config():
    if AUDIO_DEVICE_CONFIG_PATH.exists():
        return json.loads(AUDIO_DEVICE_CONFIG_PATH.read_text())
    return None


def save_audio_device_config(cfg):
    AUDIO_DEVICE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIO_DEVICE_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def resolve_audio_device():
    """Returns (source_node_name, sink_node_name). Raises AudioDeviceError
    with an actionable message if it can't unambiguously pick one - callers
    (render_conf, via _apply) turn that into a normal {"ok": False, ...}
    result rather than letting it propagate as a raw exception."""
    devices = list_usb_audio_devices()
    complete = [d for d in devices if d["source"] and d["sink"]]

    configured = load_audio_device_config()
    if configured:
        match = next((d for d in complete
                       if d["description"] == configured.get("description")), None)
        if match:
            return match["source"], match["sink"]

    if len(complete) == 1:
        return complete[0]["source"], complete[0]["sink"]
    if not complete:
        raise AudioDeviceError(
            "No USB audio device with both capture and playback found. Plug "
            "one in, then check `fx_ctl.py audio-device list`."
        )
    raise AudioDeviceError(
        f"{len(complete)} USB audio devices found - ambiguous. Run "
        "`fx_ctl.py audio-device list` then `fx_ctl.py audio-device select <n>` "
        "to choose one."
    )


def audio_device_list():
    devices = list_usb_audio_devices()
    configured = load_audio_device_config()
    return {"ok": True, "devices": devices, "configured": configured}


def audio_device_select(index):
    """index is 1-based, matching the numbering in `audio_device_list()`'s
    (unfiltered) device list - not a separate filtered list - so the number
    a user sees in `audio-device list` always means the same device here."""
    devices = list_usb_audio_devices()
    if not (1 <= index <= len(devices)):
        return {"ok": False,
                "error": f"Index must be 1-{len(devices)} (see `audio-device list`)."}
    chosen = devices[index - 1]
    if not (chosen["source"] and chosen["sink"]):
        return {"ok": False,
                "error": f"'{chosen['description']}' doesn't have both capture and "
                          "playback - can't be used as the pedal's audio device."}
    save_audio_device_config({"description": chosen["description"]})
    return {"ok": True,
            "message": f"Selected '{chosen['description']}' as the active audio device."}


def render_conf(state):
    source, sink = resolve_audio_device()
    chain = state["chain"]
    node_lines = []
    link_lines = []
    prev_out = None
    graph_in = None

    for fx in chain:
        controls = fx.get("control") or {}
        control_str = " ".join(f'"{k}" = {v}' for k, v in controls.items())
        node = (
            f'{{ type = ladspa  name = {fx["name"]}  plugin = "{fx["so_path"]}"  '
            f'label = {fx["label"]}'
        )
        if control_str:
            node += f'  control = {{ {control_str} }}'
        node += ' }'
        node_lines.append(node)

        in_port = f'{fx["name"]}:{fx["input_port"]}'
        out_port = f'{fx["name"]}:{fx["output_port"]}'
        if graph_in is None:
            graph_in = in_port
        if prev_out is not None:
            link_lines.append(f'{{ output = "{prev_out}"  input = "{in_port}" }}')
        prev_out = out_port

    if prev_out is None:
        node_lines.append('{ type = builtin  name = passthrough  label = copy }')
        graph_in = "passthrough:In"
        prev_out = "passthrough:Out"

    node_lines.append('{ type = builtin  name = copyL  label = copy }')
    node_lines.append('{ type = builtin  name = copyR  label = copy }')
    link_lines.append(f'{{ output = "{prev_out}"  input = "copyL:In" }}')
    link_lines.append(f'{{ output = "{prev_out}"  input = "copyR:In" }}')

    nodes_block = "\n                    ".join(node_lines)
    links_block = "\n                    ".join(link_lines)

    return f'''context.properties = {{
    default.clock.quantum     = 256
    default.clock.min-quantum = 256
}}

context.modules = [
    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = "FX Pedal"
            media.name       = "FX Pedal"
            filter.graph = {{
                nodes = [
                    {nodes_block}
                ]
                links = [
                    {links_block}
                ]
                inputs  = [ "{graph_in}" ]
                outputs = [ "copyL:Out" "copyR:Out" ]
            }}
            capture.props  = {{
                target.object  = "{source}"
                audio.channels = 1
            }}
            playback.props = {{
                target.object  = "{sink}"
                audio.channels = 2
            }}
        }}
    }}
]
'''


def _apply(state):
    """Write the conf and restart pipewire.service. Returns (ok, error)."""
    try:
        conf_text = render_conf(state)
    except AudioDeviceError as e:
        return False, str(e)
    CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONF_PATH.write_text(conf_text)
    subprocess.run(["systemctl", "--user", "reset-failed", "pipewire.service"],
                    capture_output=True)
    subprocess.run(["systemctl", "--user", "restart", "pipewire.service"],
                    capture_output=True)
    time.sleep(3)
    result = subprocess.run(["systemctl", "--user", "is-active", "pipewire.service"],
                             capture_output=True, text=True)
    if result.stdout.strip() == "active":
        return True, None
    journal = subprocess.run(
        ["journalctl", "--user", "-u", "pipewire.service", "--since", "10 seconds ago",
         "--no-pager"], capture_output=True, text=True)
    return False, journal.stdout[-2000:]


def _apply_or_rollback(state, old_state):
    ok, err = _apply(state)
    if ok:
        return True, None
    save_state(old_state)
    _apply(old_state)
    return False, err


def find_input_node_id():
    result = subprocess.run(["pw-cli", "ls", "Node"], capture_output=True, text=True)
    current_id = None
    for line in result.stdout.splitlines():
        m = re.match(r"\s*id (\d+), type PipeWire:Interface:Node", line)
        if m:
            current_id = int(m.group(1))
        if 'node.name = "input.filter-chain' in line:
            return current_id
    return None


def get_live_values():
    node_id = find_input_node_id()
    if node_id is None:
        return {}
    result = subprocess.run(["pw-cli", "enum-params", str(node_id), "Props"],
                             capture_output=True, text=True)
    values = {}
    for m in re.finditer(r'String "([^"]+:[^"]+)"\s*\n\s*Float ([\d.eE+-]+)', result.stdout):
        values[m.group(1)] = float(m.group(2))
    return values


def _set_live_value(fx_name, control_name, value):
    node_id = find_input_node_id()
    if node_id is None:
        return False
    param_json = f'{{ params = [ "{fx_name}:{control_name}", {value} ] }}'
    result = subprocess.run(["pw-cli", "set-param", str(node_id), "Props", param_json],
                             capture_output=True, text=True)
    return result.returncode == 0


def chain_summary(chain):
    return " -> ".join(f"{fx['name']} ({fx['label']})" for fx in chain) or "(empty - passthrough)"


def _sync_live_into_state(state):
    """Pull any live-tweaked (set_control, not yet saved) values back into
    state before persisting, so a preset save captures what's actually
    playing."""
    live = get_live_values()
    for fx in state["chain"]:
        for k in list((fx.get("control") or {}).keys()):
            live_v = live.get(f"{fx['name']}:{k}")
            if live_v is not None:
                fx["control"][k] = live_v
    return state


def _pipewire_active():
    result = subprocess.run(["systemctl", "--user", "is-active", "pipewire.service"],
                             capture_output=True, text=True)
    return result.stdout.strip() == "active"


# ---- read-only, no lock needed ----

def list_plugins():
    return {"ok": True, "plugins": sorted(scan_plugins(), key=lambda p: p["name"].lower())}


def plugin_params(label):
    plugin = find_plugin(label)
    if plugin is None:
        return {"ok": False, "error": f"No installed LADSPA plugin with label '{label}'."}
    ports = get_ports(plugin["so_path"])
    return {"ok": True, "plugin": plugin, "ports": ports}


def get_chain():
    state = load_state()
    live = get_live_values()
    return {
        "ok": True,
        "chain": state["chain"],
        "pipewire_active": _pipewire_active(),
        "live_values": live,
    }


# ---- mutating, all lock-protected ----

def add_effect(label, controls=None):
    with _locked():
        state = load_state()
        old_state = json.loads(json.dumps(state))
        plugin = find_plugin(label)
        if plugin is None:
            return {"ok": False, "kind": "topology",
                    "error": f"No installed LADSPA plugin with label '{label}'."}
        ports = get_ports(plugin["so_path"])

        audio_in = [p for p in ports if p["direction"] == "input" and p["type"] == "audio"]
        audio_out = [p for p in ports if p["direction"] == "output" and p["type"] == "audio"]
        if len(audio_in) != 1 or len(audio_out) != 1:
            return {"ok": False, "kind": "topology",
                    "error": f"'{label}' has {len(audio_in)} audio input(s) and "
                             f"{len(audio_out)} audio output(s) - only simple mono "
                             "(1-in/1-out) effects are supported."}

        control_ports = {p["name"]: p for p in ports
                          if p["type"] == "control" and p["direction"] == "input"}
        resolved = {}
        for k, v in (controls or {}).items():
            if k not in control_ports:
                avail = ", ".join(control_ports) or "(none)"
                return {"ok": False, "kind": "topology",
                        "error": f"'{label}' has no control port {k!r}. Available: {avail}"}
            resolved[k] = float(v)
        for name, p in control_ports.items():
            if name not in resolved and p["default"] is not None:
                resolved[name] = p["default"]

        base = re.sub(r"[^a-zA-Z0-9]", "", label).lower() or "fx"
        name = base
        existing = {fx["name"] for fx in state["chain"]}
        i = 1
        while name in existing:
            i += 1
            name = f"{base}{i}"

        state["chain"].append({
            "name": name, "so_path": plugin["so_path"], "label": plugin["label"],
            "input_port": audio_in[0]["name"], "output_port": audio_out[0]["name"],
            "control": resolved,
        })
        save_state(state)
        ok, err = _apply_or_rollback(state, old_state)
        if not ok:
            return {"ok": False, "kind": "topology",
                    "error": f"New config failed to start, rolled back. {err}",
                    "chain": old_state["chain"]}
        return {"ok": True, "kind": "topology", "added": name,
                "chain": state["chain"],
                "message": f"Added '{plugin['name']}' ({plugin['label']}) as '{name}'. "
                            f"Chain is now: {chain_summary(state['chain'])}"}


def remove_effect(name):
    with _locked():
        state = load_state()
        old_state = json.loads(json.dumps(state))
        before = len(state["chain"])
        state["chain"] = [fx for fx in state["chain"] if fx["name"] != name]
        if len(state["chain"]) == before:
            names = ", ".join(fx["name"] for fx in old_state["chain"]) or "(none)"
            return {"ok": False, "kind": "topology",
                    "error": f"No effect named '{name}' in the chain. Currently loaded: {names}"}
        save_state(state)
        ok, err = _apply_or_rollback(state, old_state)
        if not ok:
            return {"ok": False, "kind": "topology",
                    "error": f"New config failed to start, rolled back. {err}",
                    "chain": old_state["chain"]}
        return {"ok": True, "kind": "topology", "removed": name,
                "chain": state["chain"],
                "message": f"Removed '{name}'. Chain is now: {chain_summary(state['chain'])}"}


def set_control(name, control, value):
    with _locked():
        state = load_state()
        fx = next((fx for fx in state["chain"] if fx["name"] == name), None)
        if fx is None:
            names = ", ".join(f["name"] for f in state["chain"]) or "(none)"
            return {"ok": False, "kind": "live",
                    "error": f"No effect named '{name}' in the chain. Currently loaded: {names}"}

        ports = get_ports(fx["so_path"])
        control_ports = {p["name"]: p for p in ports
                          if p["type"] == "control" and p["direction"] == "input"}
        if control not in control_ports:
            avail = ", ".join(control_ports) or "(none)"
            return {"ok": False, "kind": "live",
                    "error": f"'{fx['label']}' has no control port {control!r}. Available: {avail}"}

        warning = None
        port = control_ports[control]
        if port["min"] is not None and not (port["min"] <= value <= port["max"]):
            warning = (f"{value} is outside the plugin's documented range "
                       f"({port['min']} to {port['max']}) - set anyway.")

        if not _set_live_value(name, control, value):
            return {"ok": False, "kind": "live",
                    "error": "pw-cli set-param failed - is pipewire.service running?"}

        fx.setdefault("control", {})[control] = value
        save_state(state)
        return {"ok": True, "kind": "live", "warning": warning,
                "message": f"Set {name}:{control} = {value} (live, no restart needed)"}


def preset_save(name):
    with _locked():
        state = _sync_live_into_state(load_state())
        save_state(state)
        presets = load_presets()
        presets[name] = json.loads(json.dumps(state["chain"]))
        save_presets(presets)
        return {"ok": True, "kind": "live",
                "message": f"Saved preset '{name}': {chain_summary(state['chain'])}"}


def _chain_shape(chain):
    return [(fx["name"], fx["label"]) for fx in chain]


def preset_load(name):
    with _locked():
        presets = load_presets()
        if name not in presets:
            avail = ", ".join(presets) or "(none)"
            return {"ok": False, "kind": "topology",
                    "error": f"No preset named '{name}'. Available: {avail}"}
        old_state = load_state()
        target_chain = json.loads(json.dumps(presets[name]))
        new_state = {"chain": target_chain}

        if _chain_shape(old_state["chain"]) == _chain_shape(target_chain):
            # Same effects, same order, same instance names - the running
            # PipeWire graph doesn't need to change shape, just values. Skip
            # the ~3s restart and live-set whatever differs.
            changed = []
            for old_fx, new_fx in zip(old_state["chain"], target_chain):
                old_controls = old_fx.get("control") or {}
                new_controls = new_fx.get("control") or {}
                for k, v in new_controls.items():
                    if old_controls.get(k) != v:
                        if _set_live_value(new_fx["name"], k, v):
                            changed.append(f"{new_fx['name']}:{k}={v}")
            save_state(new_state)
            return {"ok": True, "kind": "live", "fast_path": True,
                    "chain": target_chain,
                    "message": f"Loaded preset '{name}' (fast path, no restart): "
                                f"{chain_summary(target_chain)}"
                                + (f" [changed: {', '.join(changed)}]" if changed else "")}

        save_state(new_state)
        ok, err = _apply_or_rollback(new_state, old_state)
        if not ok:
            return {"ok": False, "kind": "topology",
                    "error": f"New config failed to start, rolled back. {err}",
                    "chain": old_state["chain"]}
        return {"ok": True, "kind": "topology", "fast_path": False,
                "chain": target_chain,
                "message": f"Loaded preset '{name}': {chain_summary(target_chain)}"}


def preset_list():
    return {"ok": True, "presets": load_presets()}


def preset_delete(name):
    with _locked():
        presets = load_presets()
        if name not in presets:
            avail = ", ".join(presets) or "(none)"
            return {"ok": False, "kind": "live",
                    "error": f"No preset named '{name}'. Available: {avail}"}
        del presets[name]
        save_presets(presets)

        warning = None
        banks = load_banks()
        referencing = [b["name"] for b in banks["banks"] if name in (b.get("slots") or [])]
        if referencing:
            warning = (f"Preset '{name}' was still referenced by bank(s): "
                       f"{', '.join(referencing)} - those slots now point at a "
                       "deleted preset.")
        return {"ok": True, "kind": "live", "warning": warning,
                "message": f"Deleted preset '{name}'."}


# ---- banks ----

def bank_list():
    return {"ok": True, **load_banks()}


def bank_show(name):
    banks = load_banks()
    bank = next((b for b in banks["banks"] if b["name"] == name), None)
    if bank is None:
        avail = ", ".join(b["name"] for b in banks["banks"]) or "(none)"
        return {"ok": False, "error": f"No bank named '{name}'. Available: {avail}"}
    slots_per_bank = banks["slots_per_bank"]
    bank_index = banks["banks"].index(bank)
    entries = []
    for slot_index, preset in enumerate(bank["slots"]):
        entries.append({
            "slot": slot_index,
            "pc": bank_index * slots_per_bank + slot_index,
            "preset": preset,
        })
    return {"ok": True, "bank": bank, "entries": entries}


def bank_create(name):
    with _locked():
        banks = load_banks()
        if any(b["name"] == name for b in banks["banks"]):
            return {"ok": False, "error": f"Bank '{name}' already exists."}
        banks["banks"].append({"name": name, "slots": [None] * banks["slots_per_bank"]})
        save_banks(banks)
        return {"ok": True, "message": f"Created bank '{name}' "
                                        f"({banks['slots_per_bank']} slots)."}


def bank_set_slot(bank_name, slot_index, preset_name):
    with _locked():
        banks = load_banks()
        bank = next((b for b in banks["banks"] if b["name"] == bank_name), None)
        if bank is None:
            avail = ", ".join(b["name"] for b in banks["banks"]) or "(none)"
            return {"ok": False, "error": f"No bank named '{bank_name}'. Available: {avail}"}
        if not (0 <= slot_index < banks["slots_per_bank"]):
            return {"ok": False, "error": f"Slot index must be 0-{banks['slots_per_bank'] - 1}."}
        if preset_name is not None:
            presets = load_presets()
            if preset_name not in presets:
                avail = ", ".join(presets) or "(none)"
                return {"ok": False, "error": f"No preset named '{preset_name}'. Available: {avail}"}
        bank["slots"][slot_index] = preset_name
        save_banks(banks)
        bank_index = banks["banks"].index(bank)
        pc = bank_index * banks["slots_per_bank"] + slot_index
        return {"ok": True,
                "message": f"Bank '{bank_name}' slot {slot_index} (PC {pc}) = "
                            f"{preset_name or '(cleared)'}"}


def bank_delete(name):
    with _locked():
        banks = load_banks()
        before = len(banks["banks"])
        banks["banks"] = [b for b in banks["banks"] if b["name"] != name]
        if len(banks["banks"]) == before:
            avail = ", ".join(b["name"] for b in load_banks()["banks"]) or "(none)"
            return {"ok": False, "error": f"No bank named '{name}'. Available: {avail}"}
        save_banks(banks)
        return {"ok": True, "message": f"Deleted bank '{name}'."}


def bank_midi_map():
    banks = load_banks()
    slots_per_bank = banks["slots_per_bank"]
    table = []
    for bank_index, bank in enumerate(banks["banks"]):
        for slot_index, preset in enumerate(bank["slots"]):
            table.append({
                "pc": bank_index * slots_per_bank + slot_index,
                "bank": bank["name"],
                "slot": slot_index,
                "preset": preset,
            })
    return {"ok": True, "slots_per_bank": slots_per_bank, "map": table}


def load_preset_for_pc(pc):
    """Convenience for a future MIDI Program-Change handler: PC number in,
    preset loaded, full stop - caller doesn't need to know about banks/slots."""
    m = bank_midi_map()
    entry = next((e for e in m["map"] if e["pc"] == pc), None)
    if entry is None or entry["preset"] is None:
        return {"ok": False, "kind": "topology",
                "error": f"No preset mapped to PC {pc}."}
    return preset_load(entry["preset"])
