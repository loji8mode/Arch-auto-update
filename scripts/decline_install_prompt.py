#!/usr/bin/env python3
"""
Clears the pending weekly-install confirmation after the person
answers "Ні" to the "Install updates now?" dialog (see
main.py:maybe_show_weekly_install_prompt / install_prompt.py). Never
touches pacman, the downloaded packages, or anything else - they stay
exactly as they were, phase stays "ready", and the GUI's own Install
button keeps working normally for installing later. Invoked by the
GUI via pkexec.
"""
import sys

sys.path.insert(0, "/opt/arch-auto-update")
import common as c


def main():
    state = c.read_state()
    if state.get("install_prompt_pending"):
        state["install_prompt_pending"] = False
        c.write_state(state)
        c.append_log(
            "Заплановане встановлення відхилено користувачем — завантажені "
            "оновлення залишаються в кеші, встановити їх можна вручну "
            "будь-коли"
        )


if __name__ == "__main__":
    main()
