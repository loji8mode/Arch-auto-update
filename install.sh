#!/usr/bin/env bash
# Installs Arch Auto Update for the current system.
# Run once as root: sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Запустіть цей скрипт з правами root: sudo ./install.sh" >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPT_DIR="/opt/arch-auto-update"
REAL_USER="${SUDO_USER:-}"
REAL_HOME=""
if [[ -n "$REAL_USER" ]]; then
    REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
fi

. "$SRC_DIR/common_removal.sh"

NEW_VERSION="$(cat "$SRC_DIR/VERSION" 2>/dev/null || echo "?")"
OLD_VERSION=""
[[ -f "$OPT_DIR/VERSION" ]] && OLD_VERSION="$(cat "$OPT_DIR/VERSION" 2>/dev/null || true)"

if [[ -d "$OPT_DIR" || -f /etc/systemd/system/arch-auto-update.timer ]]; then
    if [[ -n "$OLD_VERSION" ]]; then
        echo "== Виявлено попередню версію ($OLD_VERSION) - видаляю перед оновленням до $NEW_VERSION =="
    else
        echo "== Виявлено попередню установку - видаляю перед перевстановленням =="
    fi
    # Removes every file/unit from the old install (whatever version it
    # was, even ones no longer part of this version) so an upgrade never
    # leaves orphaned files behind. Never touches /var/lib/arch-auto-update,
    # so update history/logs survive the upgrade.
    remove_installed_app_files
    echo "Попередню версію видалено."
fi

echo "== Перевірка залежностей =="
missing=()
command -v pacman >/dev/null 2>&1 || missing+=("pacman")
command -v python3 >/dev/null 2>&1 || missing+=("python3")
python3 -c "import PySide6" >/dev/null 2>&1 || missing+=("python-pyside6")
command -v pkexec >/dev/null 2>&1 || missing+=("polkit")
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Відсутні залежності: ${missing[*]}"
    echo "Встановлюю: pacman -S --needed --noconfirm ${missing[*]}"
    pacman -S --needed --noconfirm "${missing[@]}"
fi
if ! command -v reflector >/dev/null 2>&1; then
    echo "Увага: 'reflector' не встановлено. Автоматичне перемикання дзеркал"
    echo "при проблемах із завантаженням буде недоступне, поки ви не встановите"
    echo "його: sudo pacman -S reflector"
fi

echo "== Перевірка агента автентифікації Polkit =="
# pkexec needs a running graphical agent to show a password dialog.
# Full desktop environments (GNOME/KDE/XFCE/...) provide their own; a
# bare X11 WM (i3, openbox, dwm, ...) usually doesn't, which is what
# causes "pkexec ... Not authorized" / terminal password prompts.
AGENT_PKG=""
AGENT_PATH=""
declare -A AGENT_CANDIDATES=(
    ["polkit-gnome"]="/usr/lib/polkit-gnome/polkit-gnome-authentication-agent-1"
    ["polkit-kde-agent"]="/usr/lib/polkit-kde-authentication-agent-1"
    ["lxqt-policykit"]="/usr/lib/lxqt-policykit/lxqt-policykit-agent"
    ["mate-polkit"]="/usr/lib/mate-polkit/polkit-mate-authentication-agent-1"
    ["xfce-polkit"]="/usr/lib/xfce-polkit/xfce-polkit"
    ["lxsession"]="/usr/bin/lxpolkit"
)
for pkg in "${!AGENT_CANDIDATES[@]}"; do
    path="${AGENT_CANDIDATES[$pkg]}"
    if [[ -f "$path" ]]; then
        AGENT_PKG="$pkg"
        AGENT_PATH="$path"
        break
    fi
done
if [[ -z "$AGENT_PATH" ]]; then
    echo "Жодного агента автентифікації Polkit не знайдено. Встановлюю polkit-gnome"
    echo "(легкий та не прив'язаний до жодного стільничного середовища)..."
    pacman -S --needed --noconfirm polkit-gnome
    AGENT_PKG="polkit-gnome"
    AGENT_PATH="${AGENT_CANDIDATES[polkit-gnome]}"
fi
echo "Агент автентифікації: $AGENT_PKG ($AGENT_PATH)"
echo "Застосунок сам запускає цей агент при старті, якщо він ще не працює,"
echo "тож він працюватиме незалежно від вашого віконного менеджера."

echo "== Копіювання файлів у $OPT_DIR =="
mkdir -p "$OPT_DIR/scripts"
install -m 644 "$SRC_DIR/common.py" "$OPT_DIR/common.py"
install -m 644 "$SRC_DIR/diagnosis.py" "$OPT_DIR/diagnosis.py"
install -m 755 "$SRC_DIR/main.py" "$OPT_DIR/main.py"
install -m 644 "$SRC_DIR/style.qss" "$OPT_DIR/style.qss"
install -m 644 "$SRC_DIR/icon.svg" "$OPT_DIR/icon.svg"
install -m 644 "$SRC_DIR/VERSION" "$OPT_DIR/VERSION"
for f in check_and_download.py install_updates.py install_prompt.py decline_install_prompt.py delete_downloaded.py clear_logs.py set_timer.py; do
    install -m 755 "$SRC_DIR/scripts/$f" "$OPT_DIR/scripts/$f"
done
# Fixed-purpose wrappers the GUI/tray pkexec through instead of running
# check_and_download.py / install_updates.py directly - see the
# scripts themselves and polkit/io.github.archautoupdate.policy for why.
install -m 755 "$SRC_DIR/scripts/run_check_service.sh" "$OPT_DIR/scripts/run_check_service.sh"
install -m 755 "$SRC_DIR/scripts/run_install_service.sh" "$OPT_DIR/scripts/run_install_service.sh"

echo "== Встановлення політики Polkit =="
# One action per fixed script (see
# polkit/io.github.archautoupdate.policy) - none of this app's own
# operations (checking, downloading, installing after a Так, deleting
# downloaded files, clearing logs, changing the timer) prompt for a
# password. That's deliberately scoped per-action to these specific
# scripts, never a blanket pacman/systemctl rule.
mkdir -p /usr/share/polkit-1/actions
install -m 644 "$SRC_DIR/polkit/io.github.archautoupdate.policy" \
    /usr/share/polkit-1/actions/io.github.archautoupdate.policy

echo "== Каталог стану =="
mkdir -p /var/lib/arch-auto-update
chmod 755 /var/lib/arch-auto-update

echo "== Встановлення launcher-скрипта =="
cat > /usr/bin/arch-auto-update <<EOF
#!/bin/sh
exec python3 $OPT_DIR/main.py "\$@"
EOF
chmod 755 /usr/bin/arch-auto-update

echo "== Встановлення systemd юнітів =="
# check.service/.timer: continuous background checking+downloading all
# week. install.service: installs what's already downloaded, only
# ever started explicitly (Так on the manual button or the weekly
# prompt) - never enabled/scheduled itself. install-prompt.service +
# the weekly arch-auto-update.timer: the once-a-week "Install updates
# now?" confirmation - see the unit files themselves for how these
# fit together.
install -m 644 "$SRC_DIR/systemd/arch-auto-update-check.service" /etc/systemd/system/arch-auto-update-check.service
install -m 644 "$SRC_DIR/systemd/arch-auto-update-check.timer" /etc/systemd/system/arch-auto-update-check.timer
install -m 644 "$SRC_DIR/systemd/arch-auto-update-install.service" /etc/systemd/system/arch-auto-update-install.service
install -m 644 "$SRC_DIR/systemd/arch-auto-update-install-prompt.service" /etc/systemd/system/arch-auto-update-install-prompt.service
install -m 644 "$SRC_DIR/systemd/arch-auto-update.timer" /etc/systemd/system/arch-auto-update.timer
systemctl daemon-reload
systemctl enable --now arch-auto-update-check.timer
systemctl enable --now arch-auto-update.timer

echo "== Встановлення .desktop файлу та іконки =="
mkdir -p /usr/share/icons/hicolor/scalable/apps
install -m 644 "$SRC_DIR/icon.svg" /usr/share/icons/hicolor/scalable/apps/arch-auto-update.svg
cat > /usr/share/applications/arch-auto-update.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Arch Auto Update
Comment=Перевірка та завантаження оновлень Arch Linux
Exec=arch-auto-update
Icon=arch-auto-update
Terminal=false
Categories=System;Utility;
EOF
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true

if [[ -n "$REAL_USER" && -n "$REAL_HOME" && -d "$REAL_HOME" ]]; then
    echo "== Налаштування автозапуску трею для $REAL_USER =="
    AUTOSTART_DIR="$REAL_HOME/.config/autostart"
    mkdir -p "$AUTOSTART_DIR"
    cat > "$AUTOSTART_DIR/arch-auto-update-tray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Arch Auto Update (Tray)
Comment=Фоновий трей для перевірки оновлень Arch Linux
Exec=arch-auto-update --tray
Icon=arch-auto-update
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
    chown -R "$REAL_USER":"$REAL_USER" "$AUTOSTART_DIR/arch-auto-update-tray.desktop"
    echo "Трей запускатиметься автоматично при вході в графічну сесію."
    echo "Щоб вимкнути: видаліть $AUTOSTART_DIR/arch-auto-update-tray.desktop"
else
    echo "Увага: не вдалося визначити домашній каталог користувача (запустіть"
    echo "через sudo, а не з-під root), автозапуск трею не налаштовано."
    echo "Запустити трей вручну: arch-auto-update --tray"
fi

echo
echo "== Перевірка =="
ok=1
if systemctl is-active --quiet arch-auto-update-check.timer; then
    echo "[OK]    Таймер arch-auto-update-check.timer активний (фонова перевірка/завантаження)"
else
    echo "[УВАГА] Таймер не активний - перевірте: systemctl status arch-auto-update-check.timer"
    ok=0
fi
if systemctl is-active --quiet arch-auto-update.timer; then
    echo "[OK]    Таймер arch-auto-update.timer активний (щотижневе встановлення)"
else
    echo "[УВАГА] Таймер не активний - перевірте: systemctl status arch-auto-update.timer"
    ok=0
fi
if command -v pkexec >/dev/null 2>&1; then
    echo "[OK]    pkexec знайдено"
else
    echo "[УВАГА] pkexec не знайдено"
    ok=0
fi
if [[ -n "$AGENT_PATH" && -f "$AGENT_PATH" ]]; then
    echo "[OK]    Агент автентифікації Polkit встановлено ($AGENT_PKG)"
else
    echo "[УВАГА] Агент автентифікації Polkit не знайдено"
    ok=0
fi
[[ -x /usr/bin/arch-auto-update ]] && echo "[OK]    Launcher /usr/bin/arch-auto-update встановлено" || { echo "[УВАГА] Launcher відсутній"; ok=0; }
[[ -f /usr/share/applications/arch-auto-update.desktop ]] && echo "[OK]    .desktop файл встановлено" || { echo "[УВАГА] .desktop файл відсутній"; ok=0; }
[[ -f /usr/share/icons/hicolor/scalable/apps/arch-auto-update.svg ]] && echo "[OK]    Іконка встановлена" || { echo "[УВАГА] Іконка відсутня"; ok=0; }
[[ -f /usr/share/polkit-1/actions/io.github.archautoupdate.policy ]] && echo "[OK]    Політику Polkit встановлено (перевірка/завантаження/встановлення - без пароля, лише фіксовані дії цього застосунку)" || { echo "[УВАГА] Файл політики Polkit відсутній"; ok=0; }
[[ -x "$OPT_DIR/scripts/run_check_service.sh" ]] && echo "[OK]    Обгортку перевірки (systemd) встановлено" || { echo "[УВАГА] Обгортка перевірки відсутня"; ok=0; }
[[ -x "$OPT_DIR/scripts/run_install_service.sh" ]] && echo "[OK]    Обгортку встановлення (systemd) встановлено" || { echo "[УВАГА] Обгортка встановлення відсутня"; ok=0; }

echo
if [[ "$ok" -eq 1 ]]; then
    echo "Встановлення завершено успішно."
else
    echo "Встановлення завершено з попередженнями (див. вище)."
fi
echo "Запустити застосунок:            arch-auto-update"
echo "Запустити лише трей:             arch-auto-update --tray"
echo "Перевірка/завантаження:          кожні 30 хв у фоні (arch-auto-update-check.timer)"
echo "Запит на встановлення:           щотижня, налаштовується в застосунку (arch-auto-update.timer)"
echo "Перевірити таймери:               systemctl status arch-auto-update-check.timer arch-auto-update.timer"
echo "Запустити перевірку вручну:       sudo systemctl start arch-auto-update-check.service"
echo "Запустити встановлення вручну:    sudo systemctl start arch-auto-update-install.service"
