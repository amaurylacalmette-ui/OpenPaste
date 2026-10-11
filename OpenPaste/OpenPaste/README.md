# OpenPaste

A lightweight clipboard history manager for your desktop.

OpenPaste quietly remembers everything you copy. Your normal workflow never
changes: **Ctrl+C still copies exactly as always** (OpenPaste only watches the
clipboard, it never intercepts your keys). Whenever you need something you
copied earlier, press **Ctrl+Alt+C** and the OpenPaste panel pops up right at
your cursor with all your past copies - pick one and it is pasted straight
into the app you were working in.

## Features

- **Automatic history** - every copy is saved, newest first, and survives
  restarts (stored in `~/.openpaste/history.json`)
- **Global hotkey** - `Ctrl+Alt+C` opens/closes the panel from anywhere
- **Normal Ctrl+C** - untouched; OpenPaste never blocks or rewrites copying
- **Search bar** - type to filter your history live (e.g. `meet` finds that
  meeting link you copied yesterday)
- **Auto-paste** - clicking an item (or pressing Enter) puts it on the
  clipboard AND pastes it into your previous window; `Shift+Enter` copies
  without pasting
- **Pin favorites** - keep frequently used snippets at the top; pinned items
  never expire and survive "Clear all"
- **Delete / Clear all** - remove single entries with `Del` or the DEL label,
  or wipe unpinned history at once
- **Re-copy detection** - copying the same text again moves it back to the
  top instead of duplicating it
- **Single instance** - launching OpenPaste again just opens the panel;
  `python openpaste.py quit` stops it
- **Dark popup UI** - borderless panel near your mouse cursor, like
  PowerToys "Paste As" / Ditto

## Keyboard shortcuts (in the panel)

| Key           | Action                                    |
|---------------|-------------------------------------------|
| `Ctrl+Alt+C`  | open / close panel (works everywhere)     |
| type          | live search                               |
| Up / Down     | move selection                            |
| Enter         | paste selected item into previous app     |
| Shift+Enter   | copy selected item only (no paste)        |
| Del           | delete selected item                      |
| Ctrl+P        | pin / unpin selected item                 |
| Esc           | close panel                               |
| Right-click   | context menu (copy only / pin / delete)   |

## Installation

Requires Python 3.9+.

```bash
# 1. dependencies
pip install pyperclip pynput

# 2. Linux only - clipboard backend
sudo apt install xclip          # X11
sudo apt install wl-clipboard   # Wayland (partial support, see below)

# 3. run
python openpaste.py             # or: ./run_openpaste.sh
```

On **Windows** double-click `run_openpaste.bat` (starts without a console
window). On **macOS** grant Accessibility permission when prompted
(System Settings - Privacy & Security - Accessibility) so the hotkey and
auto-paste work.

## Start automatically at login

- **Windows**: press `Win+R`, type `shell:startup`, copy a shortcut to
  `run_openpaste.bat` into that folder.
- **macOS**: System Settings - General - Login Items, add OpenPaste.
- **Linux**: create `~/.config/systemd/user/openpaste.service`:

```ini
[Unit]
Description=OpenPaste clipboard manager

[Service]
ExecStart=/usr/bin/env python3 /path/to/openpaste.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Then run `systemctl --user enable --now openpaste`.

## Configuration

Open `openpaste.py` and edit the constants at the top:

| Constant         | Default            | Meaning                              |
|------------------|--------------------|--------------------------------------|
| `HOTKEY`         | `<ctrl>+<alt>+c`   | global toggle hotkey (pynput syntax) |
| `MAX_ITEMS`      | `50`               | history size (pinned never expire)   |
| `AUTO_PASTE`     | `True`             | paste on pick, not just copy         |
| `PASTE_DELAY_MS` | `150`              | focus-return delay before auto-paste |
| `POLL_INTERVAL`  | `0.3`              | clipboard check interval (seconds)   |
| `BORDERLESS`     | `True`             | set `False` if your WM misbehaves    |

## Command line

```bash
python openpaste.py toggle   # open/close the panel of the running instance
python openpaste.py show     # open the panel
python openpaste.py quit     # stop OpenPaste
```

This is also the fallback if a Wayland session blocks the global hotkey:
bind `python3 /path/to/openpaste.py toggle` to a shortcut in your desktop
environment settings.

## Troubleshooting

- **Nothing is recorded (Linux)** - install `xclip` (X11) or
  `wl-clipboard` (Wayland) and restart OpenPaste.
- **Hotkey does nothing (Linux/Wayland)** - global input capture is
  restricted on Wayland. Use an X11 session, or bind
  `openpaste.py toggle` to a custom shortcut.
- **Hotkey does nothing (macOS)** - grant Accessibility permission to your
  terminal/Python (see Installation).
- **Auto-paste lands in the wrong window** - increase `PASTE_DELAY_MS`
  (e.g. `300`).
- **Panel looks broken / cannot be focused (Linux)** - set
  `BORDERLESS = False`.
- **Ctrl+C feels normal** - that is by design; OpenPaste never intercepts
  keys, it only reads the clipboard after you copy.

## Privacy

Everything stays on your machine. History is a plain JSON file in
`~/.openpaste/` that you can read or delete at any time. No network access,
no telemetry.
