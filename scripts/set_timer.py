#!/usr/bin/env python3
"""
Rewrites the OnCalendar= line in the systemd timer unit and
enables/disables it. Invoked by the GUI via pkexec whenever the
user changes the timer toggle, day, or time.

Usage: set_timer.py <enabled: 0|1> <day: Mon..Sun> <hour: 0-23> <minute: 0-59>
"""
import re
import sys
import subprocess

sys.path.insert(0, "/opt/arch-auto-update")
import common as c


def main():
    if len(sys.argv) != 5:
        print("Використання: set_timer.py <0|1> <Mon..Sun> <hour> <minute>", file=sys.stderr)
        sys.exit(1)

    enabled, day, hour, minute = sys.argv[1:5]

    if day not in c.SYSTEMD_DAYS:
        print("Некоректний день тижня", file=sys.stderr)
        sys.exit(1)
    try:
        hour_i, minute_i = int(hour), int(minute)
        if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
            raise ValueError
    except ValueError:
        print("Некоректний час", file=sys.stderr)
        sys.exit(1)

    if not c.TIMER_UNIT_PATH.exists():
        print(f"Файл таймера не знайдено: {c.TIMER_UNIT_PATH}", file=sys.stderr)
        sys.exit(1)

    text = c.TIMER_UNIT_PATH.read_text(encoding="utf-8")
    new_line = f"OnCalendar={day} *-*-* {hour_i:02d}:{minute_i:02d}:00"
    if re.search(r"^OnCalendar=.*$", text, re.MULTILINE):
        text = re.sub(r"^OnCalendar=.*$", new_line, text, flags=re.MULTILINE)
    else:
        text = text.replace("[Timer]", "[Timer]\n" + new_line, 1)
    c.TIMER_UNIT_PATH.write_text(text, encoding="utf-8")

    ok = True

    reload_cmd = ["systemctl", "daemon-reload"]
    res = subprocess.run(reload_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        ok = False
        c.log_failure(
            "Не вдалося виконати systemctl daemon-reload після зміни таймера",
            cmd=reload_cmd, returncode=res.returncode,
            output=res.stderr or res.stdout,
            next_step="розклад записано у файл таймера, але systemd міг не "
                      "підхопити зміну без ручного daemon-reload",
        )

    action_cmd = (
        ["systemctl", "enable", "--now", "arch-auto-update.timer"] if enabled == "1"
        else ["systemctl", "disable", "--now", "arch-auto-update.timer"]
    )
    res = subprocess.run(action_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        ok = False
        c.log_failure(
            f"Не вдалося {'увімкнути' if enabled == '1' else 'вимкнути'} таймер",
            cmd=action_cmd, returncode=res.returncode,
            output=res.stderr or res.stdout,
            next_step="розклад записано у файл таймера, але стан таймера міг "
                      "не змінитися - перевірте: systemctl status "
                      "arch-auto-update.timer",
        )

    if ok:
        c.append_log(
            f"Таймер оновлено: {'увімкнено' if enabled == '1' else 'вимкнено'}, "
            f"{day} о {hour_i:02d}:{minute_i:02d}"
        )


if __name__ == "__main__":
    main()
