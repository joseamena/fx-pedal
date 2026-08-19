#!/bin/bash
# Installs the fx-pedal CLI: symlinks fx_ctl.py onto your PATH as
# `fx-pedal`, installs the systemd --user unit for the optional daemon,
# and checks (but does not silently install) the apt packages needed for
# LADSPA effects. Safe to re-run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo "== fx-pedal install =="
echo "Repo: $REPO_DIR"

mkdir -p "$BIN_DIR" "$SYSTEMD_DIR"

ln -sf "$REPO_DIR/fx_ctl.py" "$BIN_DIR/fx-pedal"
chmod +x "$REPO_DIR/fx_ctl.py" "$REPO_DIR/fx_daemon.py"
echo "Linked: $BIN_DIR/fx-pedal -> $REPO_DIR/fx_ctl.py"

sed "s|__REPO_DIR__|$REPO_DIR|g" "$REPO_DIR/systemd/fx-pedal-daemon.service" \
    > "$SYSTEMD_DIR/fx-pedal-daemon.service"
systemctl --user daemon-reload
echo "Installed systemd unit: $SYSTEMD_DIR/fx-pedal-daemon.service (not enabled/started)"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo
       echo "NOTE: $BIN_DIR isn't on your PATH. Add this to ~/.bashrc:"
       echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
       ;;
esac

echo
echo "== Checking required apt packages (LADSPA plugins) =="
MISSING=()
for pkg in caps swh-plugins ladspa-sdk wah-plugins tap-plugins; do
    dpkg -l "$pkg" 2>/dev/null | grep -q "^ii" || MISSING+=("$pkg")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "Missing (needed for effects to be available): ${MISSING[*]}"
    echo "Install with:"
    echo "  sudo apt install ${MISSING[*]}"
else
    echo "All present."
fi

echo
echo "== Next steps =="
echo "  fx-pedal audio-device list      # confirm your USB audio interface is detected"
echo "  fx-pedal list | head             # see available effects"
echo "  fx-pedal add foverdrive          # try one"
echo "  fx-pedal chain                   # see what's loaded"
echo
echo "Full command reference: $REPO_DIR/README.md"
echo "Optional daemon + interactive console (like 'asterisk -r'):"
echo "  systemctl --user start fx-pedal-daemon"
echo "  fx-pedal console"
