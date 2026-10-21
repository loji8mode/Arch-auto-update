#!/usr/bin/env python3
"""
Deletes the cached package files for the currently pending updates
(not the whole pacman cache - just the packages that were downloaded
for the update that hasn't been installed yet).
Invoked by the GUI via pkexec.
"""
import sys
import glob
import os

sys.path.insert(0, "/opt/arch-auto-update")
import common as c


def main():
    with c.acquire_lock() as got:
        if not got:
            print("Інший процес оновлення вже виконується.", file=sys.stderr)
            sys.exit(1)

        names = c.pending_upgrade_names()

        # "pacman -Qu" only lists upgrades to packages that are already
        # installed. A kernel-headers package added by check_and_download.py
        # for DKMS may not have been installed before at all, so it never
        # shows up there - fold it in explicitly so its cached file also
        # gets cleaned up here.
        state = c.read_state()
        for extra in state.get("required_extra_packages") or []:
            if extra not in names:
                names.append(extra)

        if not names:
            print("Немає завантажених оновлень для видалення.")
            return

        removed = 0
        for n in names:
            for ext in ("pkg.tar.zst", "pkg.tar.xz", "pkg.tar.zst.sig", "pkg.tar.xz.sig"):
                for path in glob.glob(str(c.CACHE_DIR / f"{n}-*.{ext}")):
                    fname = os.path.basename(path)
                    remainder = fname[len(n) + 1:]
                    if not remainder or not remainder[0].isdigit():
                        continue  # e.g. skip "python-pip-..." when n == "python"
                    try:
                        os.remove(path)
                        removed += 1
                        c.append_log(f"Видалено завантажений пакет: {fname}")
                    except OSError as e:
                        c.log_failure(
                            f"Не вдалося видалити файл пакета '{fname}'",
                            output=str(e),
                            next_step="файл залишається в кеші, видалення інших файлів продовжується",
                        )

        state["phase"] = "pending"
        state["downloaded_bytes"] = 0
        state["progress_percent"] = 0
        c.write_state(state)
        c.append_log(f"Видалено {removed} завантажених файлів пакетів")


if __name__ == "__main__":
    main()
