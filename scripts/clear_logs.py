#!/usr/bin/env python3
"""Clears the persisted log file. Invoked by the GUI via pkexec."""
import sys

sys.path.insert(0, "/opt/arch-auto-update")
import common as c


def main():
    try:
        c.clear_log()
    except OSError as e:
        print(f"Не вдалося очистити логи: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
