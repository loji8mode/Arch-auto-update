#!/usr/bin/env bash
# Shared by install.sh (to completely remove any previously-installed
# version before laying down a fresh one) and uninstall.sh (to remove
# the app for good). Meant to be sourced, not run directly:
#
#   . "$SRC_DIR/common_removal.sh"
#   remove_installed_app_files
#
# Deliberately never touches /var/lib/arch-auto-update (update history
# and state). install.sh must never lose that across an upgrade, and
# uninstall.sh only removes it after asking - so callers that want it
# gone handle that themselves.
remove_installed_app_files() {
    systemctl disable --now arch-auto-update.timer >/dev/null 2>&1 || true
    systemctl disable --now arch-auto-update-check.timer >/dev/null 2>&1 || true

    rm -f /etc/systemd/system/arch-auto-update-check.service
    rm -f /etc/systemd/system/arch-auto-update-check.timer
    rm -f /etc/systemd/system/arch-auto-update-install.service
    rm -f /etc/systemd/system/arch-auto-update-install-prompt.service
    rm -f /etc/systemd/system/arch-auto-update.timer
    systemctl daemon-reload >/dev/null 2>&1 || true

    rm -f /usr/share/polkit-1/actions/io.github.archautoupdate.policy
    rm -f /usr/bin/arch-auto-update
    rm -f /usr/share/applications/arch-auto-update.desktop
    rm -f /usr/share/icons/hicolor/scalable/apps/arch-auto-update.svg
    # Wiped wholesale (not just the files the CURRENT version happens
    # to install) so a script renamed/removed between versions never
    # lingers here as an orphan - this is what makes removal "complete"
    # across an upgrade, not just a same-file overwrite.
    rm -rf /opt/arch-auto-update

    local remove_user="${SUDO_USER:-}"
    local remove_home=""
    if [[ -n "$remove_user" ]]; then
        remove_home="$(getent passwd "$remove_user" | cut -d: -f6)"
    fi
    if [[ -n "$remove_home" && -f "$remove_home/.config/autostart/arch-auto-update-tray.desktop" ]]; then
        rm -f "$remove_home/.config/autostart/arch-auto-update-tray.desktop"
    fi
}
