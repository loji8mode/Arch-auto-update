# Arch Auto Update

A desktop tool for Arch Linux that downloads system updates in the background
and installs them only after your explicit confirmation. Updates are fetched
continuously, so they are ready when you are.

> The GUI is currently in Ukrainian.

## How it works

1. **Background check & download** – a systemd timer runs every 30 minutes
   (`arch-auto-update-check.timer`). It syncs the database and downloads
   packages into pacman's cache with `pacman -Syuw`. Nothing is installed.
2. **Weekly install prompt** – once a week (default: Sunday 12:00, editable
   in the app) the tray/GUI asks "Install updates now?". If updates are
   downloaded and ready, you answer Yes or No. If there is nothing to install,
   no prompt appears.
3. **Install on confirmation** – updates are installed from the local cache
   only after you press Yes, or press the manual "Install updates" button.

## Features

- PySide6 main window with status card, schedule (timer) editor and live log
- System tray icon with phase-aware status (checking, downloading, ready, installed, error)
- Smart error diagnosis (`diagnosis.py`): dependency conflicts, locked pacman
  database, low disk space, mirror timeouts and network errors are classified
  and explained in plain language, with a suggested action
- Self-recovery during downloads: retries with growing delays, automatic mirror
  refresh via `reflector`, and repair of a corrupted local sync database
- Gentle downloads: uses a temporary `pacman.conf` copy with `ParallelDownloads = 1`
  (your real `/etc/pacman.conf` is never modified)
- DKMS awareness: detects DKMS modules and installs matching kernel headers
  for new kernels, with a fallback rebuild if the DKMS hook still fails
- Safe by design: never deletes `db.lck` automatically, never installs
  without confirmation, safe to interrupt and resume at any point
- Lock-based coordination so checks, downloads and installs never overlap
- Update history and logs are kept across upgrades (`/var/lib/arch-auto-update`)

## Security model

Privileged actions go through Polkit with **one action per fixed-purpose
script** (check/download, install, delete downloaded packages, clear logs,
change timer, decline prompt). There is no blanket `pacman` or `systemctl`
rule. Thin wrapper scripts (`run_check_service.sh`, `run_install_service.sh`)
take no arguments and start the matching systemd unit, which also avoids
pacman failures seen when scripts run directly under `pkexec`.

## Requirements

`pacman`, `python3`, `python-pyside6`, `polkit` – installed automatically
if missing. Optional: `reflector` (automatic mirror switching).
A Polkit authentication agent is required for graphical prompts; the installer
adds `polkit-gnome` if none is found, which helps on bare window managers
such as i3 or openbox.

## Installation

    sudo ./install.sh

Re-running the installer upgrades an existing installation. It removes the old
version completely first (no orphaned files) and keeps your logs and state.

    arch-auto-update            # open the main window
    arch-auto-update --tray     # tray only (autostarts after install)

## Uninstallation

    sudo ./uninstall.sh

Removes the app, units and Polkit policy. It asks before deleting logs and
state, and never removes pacman packages.

## Project structure

    common.py          shared helpers, state/log handling, locking
    diagnosis.py       pacman output parser and error classifier
    main.py            GUI and tray (PySide6)
    scripts/           privileged scripts (check, install, prompt, timer, cleanup)
    systemd/           check/install/prompt services and timers
    polkit/            per-action Polkit policy
    install.sh, uninstall.sh, common_removal.sh
