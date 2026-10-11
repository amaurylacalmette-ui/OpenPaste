#!/usr/bin/env sh
# Starts OpenPaste in the background.
# Linux users: sudo apt install xclip  (or wl-clipboard on Wayland)
nohup python3 "$(dirname "$0")/openpaste.py" "$@" >/tmp/openpaste.log 2>&1 &
echo "OpenPaste started (log: /tmp/openpaste.log)"
