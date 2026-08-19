#!/usr/bin/env python3
"""FX Pedal control daemon - a persistent process holding a Unix socket
that any client can connect to and issue fx_core commands against, without
spawning a subprocess per command. Modeled on the Asterisk PBX pattern:
run this as a background daemon, then attach to it interactively with
`fx_ctl.py console` (the equivalent of `asterisk -r`).

This is NOT required for fx_ctl.py's one-shot commands (add/remove/set/...)
to work - those call fx_core directly, under its own file lock, regardless
of whether this daemon is running. The daemon exists for persistent
clients that want a live connection instead of a subprocess per command:
the interactive console today, a future BLE GATT server or MCU Bridge
handler later. All of them - this daemon included - end up calling the
exact same fx_core functions, so behavior is identical either way; flock
in fx_core serializes everyone safely regardless of how many processes
(this daemon, one-shot fx_ctl.py invocations, anything else) are calling
in at once.

Protocol: newline-delimited JSON over the socket.
  Request:  {"method": "<fx_core function name>", "params": {...}}
  Response: whatever that fx_core function returns, as one JSON line.
"""

import json
import socketserver
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fx_core as core

METHODS = {
    "list_plugins": lambda p: core.list_plugins(),
    "plugin_params": lambda p: core.plugin_params(p["label"]),
    "get_chain": lambda p: core.get_chain(),
    "add_effect": lambda p: core.add_effect(p["label"], p.get("controls")),
    "remove_effect": lambda p: core.remove_effect(p["name"]),
    "set_control": lambda p: core.set_control(p["name"], p["control"], p["value"]),
    "preset_save": lambda p: core.preset_save(p["name"]),
    "preset_load": lambda p: core.preset_load(p["name"]),
    "preset_list": lambda p: core.preset_list(),
    "preset_delete": lambda p: core.preset_delete(p["name"]),
    "bank_list": lambda p: core.bank_list(),
    "bank_show": lambda p: core.bank_show(p["name"]),
    "bank_create": lambda p: core.bank_create(p["name"]),
    "bank_set_slot": lambda p: core.bank_set_slot(p["bank"], p["slot"], p.get("preset")),
    "bank_delete": lambda p: core.bank_delete(p["name"]),
    "bank_midi_map": lambda p: core.bank_midi_map(),
    "load_preset_for_pc": lambda p: core.load_preset_for_pc(p["pc"]),
}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            line = line.decode().strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                fn = METHODS.get(req.get("method"))
                if fn is None:
                    result = {"ok": False, "error": f"Unknown method {req.get('method')!r}"}
                else:
                    result = fn(req.get("params") or {})
            except Exception as e:
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self.wfile.write((json.dumps(result) + "\n").encode())


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if core.SOCKET_PATH.exists():
        core.SOCKET_PATH.unlink()
    core.SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    server = Server(str(core.SOCKET_PATH), Handler)
    print(f"fx-pedal daemon listening on {core.SOCKET_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        core.SOCKET_PATH.unlink(missing_ok=True)
        print("fx-pedal daemon stopped.", flush=True)


if __name__ == "__main__":
    sys.exit(main())
