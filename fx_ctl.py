#!/usr/bin/env python3
"""CLI for the fx-pedal LADSPA effect chain. All the actual logic lives in
fx_core.py (import fx_core if you're writing another interface - a future
BLE GATT server, a Bridge handler, whatever - against the same effect
chain); this file just does argv -> fx_core call -> printed text.

Two modes:
  fx_ctl.py <command> ...   one-shot (calls fx_core directly, works with
                             or without the daemon running)
  fx_ctl.py console         interactive prompt attached to the fx-pedal
                             daemon - start the daemon first with
                             `python3 fx_daemon.py` or
                             `systemctl --user start fx-pedal-daemon`

See README.md next to this file for the full command reference and worked
examples.
"""

import argparse
import cmd as cmd_module
import json
import shlex
import socket
import sys
from pathlib import Path

# Make sure fx_core.py resolves even when this file is run through a
# symlink (e.g. installed as ~/.local/bin/fx-pedal) - sys.path[0] would
# otherwise be the symlink's own directory, not fx_core.py's real location.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fx_core as core


def _print_result(result):
    if result.get("warning"):
        print(f"Warning: {result['warning']}", file=sys.stderr)
    if not result["ok"]:
        print(result.get("error", "Unknown error"), file=sys.stderr)
        return False
    if "message" in result:
        print(result["message"])
    return True


# ---- shared formatting, used by both one-shot commands and the console ----

def _format_plugins(result):
    for p in result["plugins"]:
        marker = "  [not usable here - not mono in/out]" if p.get("compatible") is False else ""
        print(f"{p['label']:28s} {p['name']}{marker}")
    if result.get("hidden"):
        print(f"\n({result['hidden']} plugin(s) hidden - not mono in/out, would fail `add`. "
              "Use `list --all` to see them.)")


def _format_params(plugin, ports):
    print(f"{plugin['name']}  ({plugin['label']}, {plugin['so_path']})")
    for p in ports:
        if p["type"] == "control":
            rng = (f"{p['min']} to {p['max']}, default {p['default']}"
                    if p["min"] is not None else "(no range info)")
            print(f"  [control] {p['name']}: {rng}")
        else:
            print(f"  [{p['type']:5s} {p['direction']}] {p['name']}")


def _format_chain(chain, pipewire_active, live):
    if not chain:
        print("Chain is empty (pure passthrough, no effects loaded).")
        return
    print(f"pipewire.service: {'active' if pipewire_active else 'inactive'}")
    for fx in chain:
        print(f"\n{fx['name']}  ({fx['label']}, {fx['so_path']})")
        for k, v in (fx.get("control") or {}).items():
            live_v = live.get(f"{fx['name']}:{k}")
            note = "" if live_v is None or abs(live_v - v) < 1e-6 else f"   [LIVE VALUE: {live_v}]"
            print(f"    {k} = {v}{note}")


def _format_preset_list(presets):
    if not presets:
        print("No presets saved yet.")
        return
    for name, chain in presets.items():
        print(f"{name}: {core.chain_summary(chain)}")


def _format_bank_list(banks, slots_per_bank):
    if not banks:
        print(f"No banks created yet. (slots_per_bank = {slots_per_bank})")
        return
    for b in banks:
        filled = sum(1 for s in b["slots"] if s)
        print(f"{b['name']}: {filled}/{len(b['slots'])} slots filled")


def _format_bank_show(bank, entries):
    print(f"Bank '{bank['name']}':")
    for e in entries:
        print(f"  slot {e['slot']} (PC {e['pc']:3d}): {e['preset'] or '(empty)'}")


def _format_midi_map(entries, show_all):
    entries = entries if show_all else [e for e in entries if e["preset"]]
    if not entries:
        print("No slots mapped yet.")
        return
    for e in entries:
        print(f"PC {e['pc']:3d}  ({e['bank']} slot {e['slot']}):  {e['preset'] or '(empty)'}")


# ---- one-shot commands (call fx_core directly) ----

def cmd_list(args):
    _format_plugins(core.list_plugins(args.all))


def cmd_params(args):
    r = core.plugin_params(args.label)
    if not r["ok"]:
        sys.exit(r["error"])
    _format_params(r["plugin"], r["ports"])


def cmd_chain(args):
    r = core.get_chain()
    _format_chain(r["chain"], r["pipewire_active"], r["live_values"])


def cmd_add(args):
    controls = {}
    for kv in args.control or []:
        if "=" not in kv:
            sys.exit(f"--control expects NAME=VALUE, got: {kv!r}")
        k, v = kv.split("=", 1)
        controls[k] = v
    if not _print_result(core.add_effect(args.label, controls)):
        sys.exit(1)


def cmd_remove(args):
    if not _print_result(core.remove_effect(args.name)):
        sys.exit(1)


def cmd_set(args):
    if not _print_result(core.set_control(args.name, args.control, args.value)):
        sys.exit(1)


def cmd_preset_save(args):
    if not _print_result(core.preset_save(args.name)):
        sys.exit(1)


def cmd_preset_load(args):
    if not _print_result(core.preset_load(args.name)):
        sys.exit(1)


def cmd_preset_list(args):
    _format_preset_list(core.preset_list()["presets"])


def cmd_preset_delete(args):
    if not _print_result(core.preset_delete(args.name)):
        sys.exit(1)


def cmd_bank_list(args):
    r = core.bank_list()
    _format_bank_list(r["banks"], r["slots_per_bank"])


def cmd_bank_show(args):
    r = core.bank_show(args.name)
    if not r["ok"]:
        sys.exit(r["error"])
    _format_bank_show(r["bank"], r["entries"])


def cmd_bank_create(args):
    if not _print_result(core.bank_create(args.name)):
        sys.exit(1)


def cmd_bank_set_slot(args):
    preset = None if args.clear else args.preset
    if preset is None and not args.clear:
        sys.exit("Give a preset name or pass --clear.")
    if not _print_result(core.bank_set_slot(args.bank, args.slot, preset)):
        sys.exit(1)


def cmd_bank_delete(args):
    if not _print_result(core.bank_delete(args.name)):
        sys.exit(1)


def cmd_bank_midi_map(args):
    _format_midi_map(core.bank_midi_map()["map"], args.all)


def _format_audio_devices(devices, configured):
    if not devices:
        print("No USB audio devices found. Plug one in.")
        return
    for i, d in enumerate(devices, 1):
        marker = " *" if configured and configured.get("description") == d["description"] else ""
        dirs = []
        if d["source"]:
            dirs.append("capture")
        if d["sink"]:
            dirs.append("playback")
        print(f"{i}. {d['description']}{marker}  ({'/'.join(dirs) or 'no audio ports'})")
    if not configured:
        print("\n(no device pinned - auto-detecting; only works while exactly one is plugged in)")


def cmd_audio_device_list(args):
    r = core.audio_device_list()
    _format_audio_devices(r["devices"], r["configured"])


def cmd_audio_device_select(args):
    if not _print_result(core.audio_device_select(args.index)):
        sys.exit(1)


def cmd_nam_model_get(args):
    r = core.nam_model_get()
    if not r["ok"]:
        sys.exit(r.get("error", "Unknown error"))
    if r.get("warning"):
        print(f"Warning: {r['warning']}", file=sys.stderr)
    print(r["path"] or "(not set)")


def cmd_nam_model_set(args):
    if not _print_result(core.nam_model_set(args.path)):
        sys.exit(1)


# ---- console mode: interactive prompt attached to the fx-pedal daemon ----

class DaemonClient:
    def __init__(self, sock_path):
        self.sock_path = sock_path

    def call(self, method, **params):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(15)
            s.connect(str(self.sock_path))
        except OSError as e:
            return {"ok": False, "error":
                    f"Can't reach the fx-pedal daemon at {self.sock_path} ({e}). "
                    "Start it first: python3 fx_daemon.py  (or: "
                    "systemctl --user start fx-pedal-daemon)"}
        with s:
            s.sendall((json.dumps({"method": method, "params": params}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        try:
            return json.loads(buf.decode())
        except json.JSONDecodeError:
            return {"ok": False, "error": f"Bad response from daemon: {buf!r}"}


class FxConsole(cmd_module.Cmd):
    intro = ("fx-pedal console. Type 'h' for help, 'q' to quit.\n"
             "Shortcuts: l=list  p=params  c=chain  a=add  r=remove  s=set\n")
    prompt = "fx-pedal> "

    def __init__(self, client):
        super().__init__()
        self.client = client

    # -- discovery --
    def do_l(self, arg):
        "l [all] - List plugins usable in this chain (mono in/out). 'l all' includes the rest."
        r = self.client.call("list_plugins", include_incompatible=(arg.strip() == "all"))
        if r["ok"]:
            _format_plugins(r)
        else:
            print(r["error"])
    do_list = do_l

    def do_p(self, arg):
        'p <label> - Show a plugin\'s parameters, e.g.: p foverdrive'
        if not arg:
            print("usage: p <label>")
            return
        r = self.client.call("plugin_params", label=arg.strip())
        if r["ok"]:
            _format_params(r["plugin"], r["ports"])
        else:
            print(r["error"])
    do_params = do_p

    def do_c(self, arg):
        "c - Show the current effect chain."
        r = self.client.call("get_chain")
        if r["ok"]:
            _format_chain(r["chain"], r["pipewire_active"], r["live_values"])
        else:
            print(r["error"])
    do_chain = do_c

    # -- chain editing --
    def do_a(self, arg):
        'a <label> ["Control Name=value" ...] - Add an effect. '
        'e.g.: a chebstortion "Distortion=1.2"'
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print(f"parse error: {e}")
            return
        if not parts:
            print('usage: a <label> ["Control Name=value" ...]')
            return
        label, controls = parts[0], {}
        for kv in parts[1:]:
            if "=" not in kv:
                print(f"skipping {kv!r}, expected \"Name=value\"")
                continue
            k, v = kv.split("=", 1)
            controls[k] = v
        r = self.client.call("add_effect", label=label, controls=controls)
        if r.get("warning"):
            print(f"Warning: {r['warning']}")
        print(r.get("message") or r.get("error"))
    do_add = do_a

    def do_r(self, arg):
        "r <name> - Remove an effect from the chain, e.g.: r chebstortion"
        if not arg:
            print("usage: r <name>")
            return
        r = self.client.call("remove_effect", name=arg.strip())
        print(r.get("message") or r.get("error"))
    do_remove = do_r

    def do_s(self, arg):
        's <name> "<control>" <value> - Change a control live, no restart. '
        'e.g.: s drive "Drive level" 2.5'
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print(f"parse error: {e}")
            return
        if len(parts) != 3:
            print('usage: s <name> "<control>" <value>')
            return
        name, control, value = parts
        try:
            value = float(value)
        except ValueError:
            print(f"value must be a number, got {value!r}")
            return
        r = self.client.call("set_control", name=name, control=control, value=value)
        if r.get("warning"):
            print(f"Warning: {r['warning']}")
        print(r.get("message") or r.get("error"))
    do_set = do_s

    # -- presets --
    def do_preset(self, arg):
        "preset save|load|list|delete [name] - Manage presets."
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print(f"parse error: {e}")
            return
        if not parts:
            print("usage: preset save|load|list|delete [name]")
            return
        sub, rest = parts[0], parts[1:]
        if sub == "list":
            r = self.client.call("preset_list")
            _format_preset_list(r.get("presets", {}))
            return
        if sub not in ("save", "load", "delete") or not rest:
            print(f"usage: preset {sub} <name>" if sub in ("save", "load", "delete")
                  else "usage: preset save|load|list|delete [name]")
            return
        r = self.client.call(f"preset_{sub}", name=rest[0])
        if r.get("warning"):
            print(f"Warning: {r['warning']}")
        print(r.get("message") or r.get("error"))

    # -- banks --
    def do_bank(self, arg):
        ("bank list|show|create|set-slot|delete|midi-map [args] - Manage banks. "
         'e.g.: bank set-slot "Live Set 1" 0 clean-boost')
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print(f"parse error: {e}")
            return
        if not parts:
            print("usage: bank list|show|create|set-slot|delete|midi-map [args]")
            return
        sub, rest = parts[0], parts[1:]
        if sub == "list":
            r = self.client.call("bank_list")
            _format_bank_list(r.get("banks", []), r.get("slots_per_bank"))
        elif sub == "show":
            if not rest:
                print("usage: bank show <name>")
                return
            r = self.client.call("bank_show", name=rest[0])
            if r["ok"]:
                _format_bank_show(r["bank"], r["entries"])
            else:
                print(r["error"])
        elif sub == "create":
            if not rest:
                print("usage: bank create <name>")
                return
            print(self.client.call("bank_create", name=rest[0]).get("message"))
        elif sub == "set-slot":
            if len(rest) < 2:
                print('usage: bank set-slot <bank> <slot> <preset|--clear>')
                return
            bank, slot = rest[0], int(rest[1])
            preset = None if (len(rest) > 2 and rest[2] == "--clear") else (
                rest[2] if len(rest) > 2 else None)
            r = self.client.call("bank_set_slot", bank=bank, slot=slot, preset=preset)
            print(r.get("message") or r.get("error"))
        elif sub == "delete":
            if not rest:
                print("usage: bank delete <name>")
                return
            print(self.client.call("bank_delete", name=rest[0]).get("message"))
        elif sub == "midi-map":
            r = self.client.call("bank_midi_map")
            _format_midi_map(r.get("map", []), show_all="--all" in rest)
        else:
            print(f"unknown bank subcommand {sub!r}")

    # -- NAM model --
    def do_nam(self, arg):
        "nam get|set <path> - Get/set the .nam model file the NAM plugin loads."
        try:
            parts = shlex.split(arg)
        except ValueError as e:
            print(f"parse error: {e}")
            return
        if not parts:
            print("usage: nam get|set <path>")
            return
        sub, rest = parts[0], parts[1:]
        if sub == "get":
            r = self.client.call("nam_model_get")
            if r.get("warning"):
                print(f"Warning: {r['warning']}")
            print((r.get("path") or "(not set)") if r.get("ok") else r.get("error"))
        elif sub == "set":
            if not rest:
                print("usage: nam set <path>")
                return
            r = self.client.call("nam_model_set", path=rest[0])
            if r.get("warning"):
                print(f"Warning: {r['warning']}")
            print(r.get("message") or r.get("error"))
        else:
            print(f"unknown nam subcommand {sub!r}")

    # -- housekeeping --
    HELP_SECTIONS = [
        ("Discovery", [
            ("l/list", "List all installed LADSPA plugins"),
            ("p/params", "Show a plugin's parameters (control ports)"),
            ("c/chain", "Show the currently loaded effect chain"),
        ]),
        ("Chain editing", [
            ("a/add", "Add an effect to the end of the chain"),
            ("r/remove", "Remove an effect from the chain by name"),
            ("s/set", "Change a control value live, no restart needed"),
        ]),
        ("Presets & banks", [
            ("preset", "save/load/list/delete named snapshots of the chain"),
            ("bank", "list/show/create/set-slot/delete/midi-map - foot-controller groups of presets"),
        ]),
        ("NAM model", [
            ("nam", "get/set <path> - the .nam capture the Neural Amp Modeler plugin loads"),
        ]),
        ("Housekeeping", [
            ("h/help", "Show this help ('help <command>' for full usage)"),
            ("q/quit/exit", "Quit the console"),
        ]),
    ]

    def do_help(self, arg):
        "h/help [command] - List commands by category, or show one command's full usage."
        if arg:
            return super().do_help(arg)
        for title, cmds in self.HELP_SECTIONS:
            print(f"\n{title}:")
            for name, desc in cmds:
                print(f"  {name:<14} {desc}")
        print("\nType 'help <command>' for full usage of any command.")

    def do_h(self, arg):
        "h - Show this help."
        return self.do_help(arg)

    def do_q(self, arg):
        "q - Quit the console."
        return True
    do_quit = do_q
    do_exit = do_q

    def do_EOF(self, arg):
        print()
        return True

    def emptyline(self):
        pass


def cmd_console(args):
    client = DaemonClient(core.SOCKET_PATH)
    try:
        FxConsole(client).cmdloop()
    except KeyboardInterrupt:
        print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="List LADSPA plugins usable in this chain (mono in/out)")
    p.add_argument("--all", action="store_true",
                    help="Include plugins that can't be added here (not mono in/out)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("params", help="Show the parameters (control ports) of a plugin")
    p.add_argument("label", help="LADSPA plugin label, e.g. foverdrive")
    p.set_defaults(func=cmd_params)

    sub.add_parser("chain", help="Show the currently loaded effect chain").set_defaults(func=cmd_chain)

    p = sub.add_parser("add", help="Add an effect to the end of the chain")
    p.add_argument("label", help="LADSPA plugin label, e.g. foverdrive")
    p.add_argument("--control", action="append",
                    help='Control value as NAME=VALUE, e.g. --control "Drive level=2.5" '
                         '(repeatable; unspecified controls use the plugin default)')
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="Remove an effect from the chain by name")
    p.add_argument("name", help="Effect instance name, as shown by 'chain'")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("set", help="Change a control value live, no restart needed")
    p.add_argument("name", help="Effect instance name, as shown by 'chain'")
    p.add_argument("control", help="Control port name, e.g. 'Drive level'")
    p.add_argument("value", type=float)
    p.set_defaults(func=cmd_set)

    preset = sub.add_parser("preset", help="Save/load/list/delete named presets")
    preset_sub = preset.add_subparsers(dest="preset_cmd", required=True)

    p = preset_sub.add_parser("save", help="Save the current chain as a named preset")
    p.add_argument("name")
    p.set_defaults(func=cmd_preset_save)

    p = preset_sub.add_parser("load", help="Load a named preset (replaces the current chain)")
    p.add_argument("name")
    p.set_defaults(func=cmd_preset_load)

    preset_sub.add_parser("list", help="List saved presets").set_defaults(func=cmd_preset_list)

    p = preset_sub.add_parser("delete", help="Delete a saved preset")
    p.add_argument("name")
    p.set_defaults(func=cmd_preset_delete)

    bank = sub.add_parser("bank", help="Manage banks (ordered groups of presets, for MIDI PC switching)")
    bank_sub = bank.add_subparsers(dest="bank_cmd", required=True)

    bank_sub.add_parser("list", help="List banks").set_defaults(func=cmd_bank_list)

    p = bank_sub.add_parser("show", help="Show a bank's slots and derived MIDI PC numbers")
    p.add_argument("name")
    p.set_defaults(func=cmd_bank_show)

    p = bank_sub.add_parser("create", help="Create a new empty bank")
    p.add_argument("name")
    p.set_defaults(func=cmd_bank_create)

    p = bank_sub.add_parser("set-slot", help="Assign (or clear) a preset in a bank slot")
    p.add_argument("bank")
    p.add_argument("slot", type=int)
    p.add_argument("preset", nargs="?", help="Preset name (omit with --clear)")
    p.add_argument("--clear", action="store_true", help="Clear this slot instead of setting it")
    p.set_defaults(func=cmd_bank_set_slot)

    p = bank_sub.add_parser("delete", help="Delete a bank")
    p.add_argument("name")
    p.set_defaults(func=cmd_bank_delete)

    p = bank_sub.add_parser("midi-map", help="Show the full derived PC-number -> preset table")
    p.add_argument("--all", action="store_true", help="Include empty slots too")
    p.set_defaults(func=cmd_bank_midi_map)

    sub.add_parser("console",
                   help="Interactive console attached to the fx-pedal daemon"
                   ).set_defaults(func=cmd_console)

    audio = sub.add_parser("audio-device", help="Pick which USB audio interface the pedal uses")
    audio_sub = audio.add_subparsers(dest="audio_cmd", required=True)
    audio_sub.add_parser("list", help="List detected USB audio devices").set_defaults(
        func=cmd_audio_device_list)
    p = audio_sub.add_parser("select", help="Pin a specific device by its list number")
    p.add_argument("index", type=int)
    p.set_defaults(func=cmd_audio_device_select)

    nam = sub.add_parser("nam-model", help="Get/set the .nam file the Neural Amp Modeler plugin loads")
    nam_sub = nam.add_subparsers(dest="nam_cmd", required=True)
    nam_sub.add_parser("get", help="Show the currently active .nam model file").set_defaults(
        func=cmd_nam_model_get)
    p = nam_sub.add_parser("set", help="Point the plugin at a different .nam file (restarts PipeWire)")
    p.add_argument("path", help="Absolute path to a .nam model file")
    p.set_defaults(func=cmd_nam_model_set)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
