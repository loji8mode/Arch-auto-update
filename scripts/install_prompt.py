#!/usr/bin/env python3
"""
Runs at the scheduled WEEKLY time - arch-auto-update.timer ->
arch-auto-update-install-prompt.service, root, no pkexec/polkit
involved (same as arch-auto-update-check.service when the timer
fires it directly - polkit only ever comes into it when the
unprivileged GUI asks for something on demand).

Never installs or downloads anything itself. The only thing this
ever does is flip state["install_prompt_pending"] to True when there
are already-downloaded updates sitting in "ready" - the GUI/tray,
already polling state.json continuously, turn that into the "Install
updates now?" Так/Ні dialog (see main.py's
maybe_show_weekly_install_prompt). If nothing is actually downloaded
and ready yet, this is a no-op: no prompt for updates that don't
exist, and nothing is ever installed without that explicit Так.
"""
import sys

sys.path.insert(0, "/opt/arch-auto-update")
import common as c


def main():
    # Not strictly touching pacman, but taking the same lock other
    # scripts use for any state.json read-modify-write keeps this
    # from racing a concurrent check/download/install that's also
    # about to update state.json right now. If something else is
    # actively running, phase won't be "ready" anyway - just skip
    # cleanly, same as every other script does when the lock is busy.
    with c.acquire_lock() as got:
        if not got:
            c.append_log(
                "Заплановане щотижневе встановлення: інший процес оновлення "
                "вже виконується, пропуск перевірки."
            )
            return

        state = c.read_state()
        if state.get("phase") == "ready":
            state["install_prompt_pending"] = True
            c.write_state(state)
            c.append_log(
                "Заплановане щотижневе встановлення: оновлення готові, "
                "очікується підтвердження користувача"
            )
        else:
            c.append_log(
                "Заплановане щотижневе встановлення: завантажених "
                "оновлень немає, підтвердження не потрібне"
            )


if __name__ == "__main__":
    main()
