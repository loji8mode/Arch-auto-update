"""
Output parser / error classifier layer for Arch Auto Update's recovery
system.

    pacman
      |
      v
  raw output lines (already captured verbatim by check_and_download.py -
  see common.log_raw_output)
      |
      v
  classify() -- this module
      |
      v
  UpdateError - plain, JSON-serializable FACTS only (type, retryable,
  recommended_action, whichever package/version/byte numbers were
  actually found in the text). No Ukrainian, no sentences, no
  formatting - see main.py's ERROR_PRESENTATION for how those facts
  become the title/explanation a person actually reads.

This split exists because check_and_download.py already writes the
complete, verbatim pacman output to the log - that part doesn't need
fixing again. What it couldn't do is turn "error: failed to prepare
transaction (could not satisfy dependencies)" into something a person
can act on without reading pacman's output line by line themselves.
That's what this module does, and keeping it separate from main.py is
what lets a future RecoveryManager (something that could act on some
error types automatically, e.g. a plain retry for a network hiccup)
reuse the exact same classification without re-parsing pacman text a
second time, and lets any other frontend do the same.

classify() never raises and never guesses past what the text actually
supports - a pattern it doesn't recognize becomes type=UNKNOWN with
everything pacman said preserved verbatim in `technical_lines`, not a
best-effort diagnosis dressed up as a confident one. Byte counts are
only ever filled in from a real measurement (shutil.disk_usage, or a
size this app already computed elsewhere, e.g. via `pacman -Si`) -
never derived from pacman's own "N blocks needed" text, since that
would require assuming a block size pacman doesn't actually state.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Optional


# ---------------------------------------------------------------------------
# Error types - plain strings, not an enum, so UpdateError round-trips
# through state.json as plain JSON with no custom decoding needed.
# ---------------------------------------------------------------------------
DEPENDENCY_CONFLICT = "dependency_conflict"
DB_LOCKED = "db_locked"
DISK_SPACE = "insufficient_disk_space"
MIRROR_TIMEOUT = "mirror_timeout"
NETWORK_ERROR = "network_error"
UNKNOWN = "unknown"

# Recommended actions - what the GUI's primary action button should do.
# Kept as data here (not decided in main.py) since which action makes
# sense is a property of the classification itself, not of how it's
# drawn on screen.
RETRY = "retry"
RETRY_LATER = "retry_later"
FREE_SPACE = "free_space"
CHECK_LOCK = "check_lock"
MANUAL = "manual"

# ---------------------------------------------------------------------------
# Text hints. Single source of truth: check_and_download.py's own
# is_mirror_problem/is_local_db_problem/is_generic_sync_error (used to
# decide whether to *retry*) import these same lists, so "is this a
# network problem" can no longer drift between the retry logic and the
# GUI's explanation of what happened - previously each kept its own
# hand-maintained copy.
# ---------------------------------------------------------------------------
NETWORK_ERROR_HINTS = [
    "failed retrieving file",
    "failed to connect",
    "network is unreachable",
    "could not resolve host",
    "ssl error",
    "404",
]
# "timed out" is its own hint, not just covered by "timeout" - curl's
# actual message is "Operation timed out ...", and "timeout" is not a
# substring of "timed out" (different word order). Both spellings show
# up in the wild, so both are listed.
TIMEOUT_HINTS = [
    "timeout",
    "timed out",
    "operation too slow",
]
LOCAL_DB_ERROR_HINTS = [
    "unrecognized archive format",
    "invalid or corrupted database",
    "invalid or corrupted file",
]
GENERIC_SYNC_ERROR_HINTS = ["unexpected error"]


@dataclass
class UpdateError:
    type: str
    retryable: bool = True
    recommended_action: str = RETRY
    package: Optional[str] = None
    required_by: Optional[str] = None
    required_version: Optional[str] = None
    available_version: Optional[str] = None
    required_bytes: Optional[int] = None
    available_bytes: Optional[int] = None
    partition: Optional[str] = None
    # None = couldn't determine either way (never used to decide
    # anything automatically - see _is_pacman_running).
    lock_process_running: Optional[bool] = None
    technical_lines: list = field(default_factory=list)

    def to_dict(self):
        d = dict(self.__dict__)
        d["technical_lines"] = list(self.technical_lines)
        return d

    @classmethod
    def from_dict(cls, d):
        if not d:
            return None
        valid = {f.name for f in dataclass_fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})

    def summary_text(self):
        """One short, plain line - NOT the polished explanation (that's
        main.py's job, built from `type` plus these fields). This is
        only for state["error_message"]/log back-compat and as a
        fallback for any surface that hasn't been taught about
        structured errors."""
        if self.type == DEPENDENCY_CONFLICT and self.package:
            return (
                f"{self.required_by} потребує {self.package} "
                f"{self.required_version}, репозиторій пропонує "
                f"{self.available_version}"
            )
        if self.type == DB_LOCKED:
            return "База даних пакетів заблокована (db.lck)"
        if self.type == DISK_SPACE:
            return "Недостатньо вільного місця на диску"
        if self.type == MIRROR_TIMEOUT:
            return "Дзеркало не відповіло вчасно"
        if self.type == NETWORK_ERROR:
            return "Помилка мережі під час завантаження"
        if self.technical_lines:
            return self.technical_lines[-1]
        return "Невідома помилка"


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# ":: installing qemu-common (11.1.1-1) breaks dependency
#  'qemu-common=11.0.3-1' required by qemu-block-gluster"
# Also matches the "removing" phrasing pacman uses for the same kind of
# conflict, and any of the =, >=, <=, >, < dependency operators.
_DEP_BREAK_RE = re.compile(
    r"::\s+(?:installing|removing)\s+(?P<pkg>\S+)\s+\((?P<pkg_version>[^)]+)\)\s+"
    r"breaks dependency\s+'(?P<dep>[^=<>']+)(?P<op>[=<>]+)(?P<dep_version>[^']+)'"
    r"\s+required by\s+(?P<required_by>\S+)",
    re.IGNORECASE,
)

# "warning: pipewire: local (1:1.6.8-1.2) is newer than extra (1:1.6.8-1)"
_LOCAL_NEWER_RE = re.compile(
    r"warning:\s+(?P<pkg>\S+):\s+local\s+\((?P<local_version>[^)]+)\)\s+"
    r"is newer than\s+(?P<repo>\S+)\s+\((?P<repo_version>[^)]+)\)",
    re.IGNORECASE,
)

# "error: Partition / too full: 172351 blocks needed, 115656 blocks free"
_DISK_SPACE_RE = re.compile(
    r"partition\s+(?P<partition>\S+)\s+too full:\s+(?P<needed>\d+)\s+blocks needed,"
    r"\s+(?P<free>\d+)\s+blocks free",
    re.IGNORECASE,
)


def _is_pacman_running():
    """True/False if we could tell, None if we couldn't. This is purely
    informational (see UpdateError.lock_process_running) - it is NEVER
    used to decide whether to remove /var/lib/pacman/db.lck ourselves.
    This app does not delete that file automatically under any
    circumstance, stale-looking or not; only the person, having been
    told what this found, can choose to."""
    try:
        res = subprocess.run(
            ["pgrep", "-x", "pacman"], capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode not in (0, 1):
        # pgrep uses exit codes >1 for "couldn't even run the check"
        # (bad usage, no /proc, ...) - genuinely unknown, not "no".
        return None
    return res.returncode == 0


def _disk_free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def extract_local_newer_notices(lines):
    """Every "warning: X: local (Y) is newer than Z (W)" line, pulled
    out as plain facts (see point #6 in the design doc this implements:
    this is informational, never treated as a failure - pacman itself
    just skips upgrading that one package and moves on to the rest of
    the transaction). Returned regardless of whether the overall
    attempt succeeded or failed, so a fully successful download that
    quietly skipped a few packages this way can still surface that,
    instead of it only being visible by scrolling the log."""
    notices = []
    for line in lines:
        m = _LOCAL_NEWER_RE.search(line)
        if m:
            notices.append({
                "package": m.group("pkg"),
                "local_version": m.group("local_version"),
                "repo": m.group("repo"),
                "repo_version": m.group("repo_version"),
            })
    return notices


def classify(lines, returncode=None, phase=None, context=None):
    """Turns the lines pacman printed for one failed attempt into a
    single UpdateError describing the most useful thing found. Checked
    in order from the most specific/actionable pattern to the most
    generic, so a run that prints both a "local is newer" warning *and*
    a real dependency break (exactly what happened in the qemu-common
    case this was built for) is reported as the dependency conflict,
    not as noise about the harmless warning next to it.

    `context` is a plain dict for facts THIS classifier can't derive
    from pacman's text alone but the caller already knows and measured
    honestly - currently just an optional "required_bytes" (the total
    download size this app already computed via `pacman -Si`, for the
    insufficient-disk-space case during the download phase). Never
    used to invent a number pacman itself didn't provide a reliable
    basis for.
    """
    context = context or {}
    lines = [l for l in (lines or []) if l and l.strip()]
    text = "\n".join(lines)
    low = text.lower()

    m = _DEP_BREAK_RE.search(text)
    if m:
        return UpdateError(
            type=DEPENDENCY_CONFLICT,
            retryable=True,
            recommended_action=RETRY_LATER,
            package=m.group("dep"),
            required_by=m.group("required_by"),
            required_version=m.group("dep_version"),
            available_version=m.group("pkg_version"),
            technical_lines=lines,
        )

    if "unable to lock database" in low or "could not lock database" in low:
        return UpdateError(
            type=DB_LOCKED,
            retryable=True,
            recommended_action=CHECK_LOCK,
            lock_process_running=_is_pacman_running(),
            technical_lines=lines,
        )

    m = _DISK_SPACE_RE.search(text)
    if m or "not enough free disk space" in low or "no space left on device" in low:
        partition = m.group("partition") if m else (context.get("partition") or "/")
        return UpdateError(
            type=DISK_SPACE,
            retryable=False,
            recommended_action=FREE_SPACE,
            partition=partition,
            available_bytes=_disk_free_bytes(partition),
            # Only ever set from a number this app measured itself
            # (see the docstring above) - left as None, not guessed,
            # when the caller didn't supply one.
            required_bytes=context.get("required_bytes"),
            technical_lines=lines,
        )

    if any(h in low for h in TIMEOUT_HINTS):
        return UpdateError(
            type=MIRROR_TIMEOUT,
            retryable=True,
            recommended_action=RETRY_LATER,
            technical_lines=lines,
        )

    if any(h in low for h in NETWORK_ERROR_HINTS):
        return UpdateError(
            type=NETWORK_ERROR,
            retryable=True,
            recommended_action=RETRY,
            technical_lines=lines,
        )

    return UpdateError(
        type=UNKNOWN,
        retryable=True,
        recommended_action=MANUAL,
        technical_lines=lines,
    )
