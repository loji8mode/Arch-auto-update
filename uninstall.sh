#!/usr/bin/env bash
# Removes Arch Auto Update from the system.
# Run as root: sudo ./uninstall.sh
#
# This only removes files/units created by install.sh. It never
# uninstalls pacman packages (python-pyside6, polkit, reflector,
# polkit-gnome, ...) since those may have existed before this app
# was installed or may be used by other things.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запустіть цей скрипт з правами root: sudo ./uninstall.sh" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

. "$SRC_DIR/common_removal.sh"

echo "== Видалення файлів застосунку =="
remove_installed_app_files

read -r -p "Видалити також збережені логи та стан (/var/lib/arch-auto-update)? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
    rm -rf /var/lib/arch-auto-update
fi

echo
echo "Видалено. Пакети (python-pyside6, polkit, reflector, агент Polkit тощо)"
echo "залишено без змін - видаліть їх вручну через pacman, якщо потрібно."
