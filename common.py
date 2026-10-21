"""
Shared constants and helpers for Arch Auto Update.

Imported by both the GUI (running as the normal user) and the
root-privileged scripts under scripts/ (run via systemd or pkexec).
Installed to /opt/arch-auto-update/common.py by install.sh.
"""
import os
import re
import json
import time
import fcntl
import shlex
import contextlib
import subprocess
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
STATE_DIR = Path("/var/lib/arch-auto-update")
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "update.log"
LOCK_FILE = Path("/run/arch-auto-update.lock")

CACHE_DIR = Path("/var/cache/pacman/pkg")
MIRRORLIST = Path("/etc/pacman.d/mirrorlist")
SYNC_DB_DIR = Path("/var/lib/pacman/sync")
SYSTEM_PACMAN_CONF = Path("/etc/pacman.conf")
RUN_DIR = Path("/run/arch-auto-update")
GENTLE_PACMAN_CONF = RUN_DIR / "pacman.conf"

TIMER_UNIT_PATH = Path("/etc/systemd/system/arch-auto-update.timer")
# arch-auto-update.timer (TIMER_UNIT_PATH, editable from the GUI's
# Timer card) now controls the WEEKLY INSTALL PROMPT schedule, not
# checking/downloading - see arch-auto-update-install-prompt.service.
# Checking and downloading now happen continuously in the background
# all week instead, via the separate, fixed-interval
# arch-auto-update-check.timer (not user-editable, so there's no
# corresponding *_UNIT_PATH constant for it here).
WEEKLY_SERVICE_UNIT_PATH = Path("/etc/systemd/system/arch-auto-update-install-prompt.service")

# Force English, locale-independent pacman output so we can parse it
# reliably regardless of the system's configured language.
PACMAN_ENV = dict(os.environ)
PACMAN_ENV["LC_ALL"] = "C"
PACMAN_ENV["LANG"] = "C"

LOG_MAX_BYTES = 2_000_000
LOG_TRIM_KEEP_LINES = 5000

SYSTEMD_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_LABELS_UK = {
    "Mon": "Понеділок",
    "Tue": "Вівторок",
    "Wed": "Середа",
    "Thu": "Четвер",
    "Fri": "П'ятниця",
    "Sat": "Субота",
    "Sun": "Неділя",
}
DAY_LABELS_UK_REV = {v: k for k, v in DAY_LABELS_UK.items()}

# ---------------------------------------------------------------------------
# Time / size formatting
# ---------------------------------------------------------------------------
def now_str():
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def human_size(num_bytes):
    if not num_bytes:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGT]?i?B)", re.IGNORECASE)
_UNIT_MULT = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024 ** 2,
    "GIB": 1024 ** 3,
    "TIB": 1024 ** 4,
}


def parse_pacman_size(text):
    """Parse a pacman-style size string like '92.34 MiB' into bytes."""
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2).upper()
    mult = _UNIT_MULT.get(unit, 1)
    return int(value * mult)


# ---------------------------------------------------------------------------
# Gentle downloads
#
# Pacman's own download concurrency (ParallelDownloads in pacman.conf) is
# whatever the system happens to have configured - fine for an interactive
# "pacman -Syu" a person is watching and can Ctrl-C, but a burst of several
# packages downloading at once is exactly what overwhelms a weak/unstable
# connection when this app fires updates off unattended. Several files
# failing across many different mirrors all within the same second (rather
# than one mirror failing repeatedly) is the signature of that: the link
# itself buckling under concurrent connections, not a bad mirror.
#
# Rather than edit the system's own /etc/pacman.conf - which would also
# quietly change every manual "pacman -Syu" run from a terminal - we write
# our own copy, identical in every other respect, with ParallelDownloads
# forced to 1, and pass it to pacman with --config for every command this
# app runs. It's regenerated fresh from the real config on every run and
# lives under /run (cleared on reboot), so it can never drift out of sync
# with the system's actual pacman.conf.
# ---------------------------------------------------------------------------
_PARALLEL_DOWNLOADS_RE = re.compile(r"(?im)^[ \t]*ParallelDownloads[ \t]*=.*$")
_OPTIONS_HEADER_RE = re.compile(r"(?im)^\[options\][ \t]*$")


def prepare_gentle_pacman_conf():
    """Write a copy of /etc/pacman.conf to GENTLE_PACMAN_CONF with
    ParallelDownloads forced to 1 and return (path, None) on success,
    for passing to pacman as `--config <path>`. Returns (None, reason)
    if the system config couldn't be read or the copy couldn't be
    written, in which case callers should fall back to plain "pacman"
    (system-default concurrency) rather than fail outright - `reason`
    is the actual OS error (permissions, missing file, disk full, ...)
    so that fallback can be logged with something more useful than a
    bare "failed". The real /etc/pacman.conf is never modified."""
    try:
        text = SYSTEM_PACMAN_CONF.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"не вдалося прочитати {SYSTEM_PACMAN_CONF}: {e}"

    if _PARALLEL_DOWNLOADS_RE.search(text):
        text = _PARALLEL_DOWNLOADS_RE.sub("ParallelDownloads = 1", text, count=1)
    elif _OPTIONS_HEADER_RE.search(text):
        text = _OPTIONS_HEADER_RE.sub("[options]\nParallelDownloads = 1", text, count=1)
    else:
        # No [options] section at all (unusual) - prepend one rather
        # than silently skipping the limit.
        text = "[options]\nParallelDownloads = 1\n\n" + text

    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(GENTLE_PACMAN_CONF, text.encode("utf-8"))
    except OSError as e:
        return None, f"не вдалося записати {GENTLE_PACMAN_CONF}: {e}"
    return str(GENTLE_PACMAN_CONF), None


def pacman_cmd(args, gentle_conf=None):
    """Build a pacman argv list. When `gentle_conf` is a path (from
    prepare_gentle_pacman_conf), inserts --config so this command uses
    our one-file-at-a-time copy instead of the system pacman.conf;
    when it's None (config couldn't be prepared), falls back to plain
    pacman with whatever concurrency the system has configured."""
    if gentle_conf:
        return ["pacman", "--config", gentle_conf] + list(args)
    return ["pacman"] + list(args)


# ---------------------------------------------------------------------------
# Retry pacing
#
# A connection that just dropped mid-download needs a moment to recover,
# not another burst of requests right away. Delay grows with each
# successive attempt (10s, 20s, 30s, ... capped) rather than staying
# fixed, so a connection that needs longer to settle gets more room on
# each retry instead of being hit at the same pace every time.
# ---------------------------------------------------------------------------
RETRY_BASE_DELAY_SECONDS = 10
RETRY_MAX_DELAY_SECONDS = 60


def retry_delay_seconds(attempt):
    return min(RETRY_BASE_DELAY_SECONDS * attempt, RETRY_MAX_DELAY_SECONDS)


def wait_before_retry(attempt):
    """Log and actually sleep for a growing pause before the next retry
    - called right after a failure is diagnosed and before anything
    (mirror refresh, the next pacman attempt, ...) touches the network
    again."""
    delay = retry_delay_seconds(attempt)
    append_log(f"Пауза {delay} с перед повторною спробою (з'єднання могло щойно обірватися)...")
    time.sleep(delay)


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\r")


def strip_ansi(text):
    """Remove ANSI/terminal escape sequences and bare carriage returns
    from captured CLI output. Pacman itself disables color when its
    stdout isn't a tty, but hook scripts it runs (mkinitcpio, dkms,
    ...) don't always make the same check and can still emit color
    codes - left in place these can silently break line-based regex
    matching (e.g. the DKMS "missing kernel headers" detection) and
    clutter the log/GUI display."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def default_state():
    return {
        "phase": "idle",           # idle | checking | downloading | pending |
                                    # ready | installing | installed |
                                    # no_updates | error
        "package_count": 0,
        "total_size_bytes": 0,
        "downloaded_bytes": 0,
        "progress_percent": 0,
        "last_check": None,
        "error_message": None,
        # Structured diagnosis of the current "error" phase, if any -
        # see diagnosis.UpdateError.to_dict()/from_dict(). None
        # whenever phase isn't "error", or for an error that predates
        # this field (an older state.json, or a code path that hasn't
        # been taught to classify yet) - callers must not assume this
        # is present just because phase == "error"; error_message
        # above is always there as the plain-text fallback either way.
        "error": None,
        # Packages the most recent download attempt found sitting at a
        # locally-installed version newer than what any configured
        # repository currently offers (pacman's own "local (X) is
        # newer than Y (Z)" warning) - see
        # diagnosis.extract_local_newer_notices. This is informational,
        # never a failure: pacman itself just leaves that one package
        # alone and continues. One {"package", "local_version", "repo",
        # "repo_version"} dict per package. Reset to [] at the start of
        # every check and overwritten (not accumulated) by whichever
        # download attempt actually ran, so it always reflects the
        # latest run, never a stale one from days ago.
        "skipped_packages": [],
        # The pending upgrades check_and_download.py found (from
        # "pacman -Qu", see common.pending_upgrades) - one
        # {"name", "old_version", "new_version"} dict per package, in
        # the same order pacman reports them. Set once when a check
        # finds updates and left untouched through downloading/ready/
        # installing, so install_updates.py's own success path can
        # carry the exact same list forward into the "installed" phase
        # ("packages that were successfully updated") without asking
        # pacman again. Does NOT include "<kernel>-headers" packages
        # (see required_extra_packages below) - those are a DKMS
        # dependency being added, not a version upgrade, so they don't
        # fit the name/old-version/new-version shape this is for.
        "packages": [],
        # "<kernel>-headers" packages check_and_download.py determined
        # were required alongside a kernel update (DKMS modules present
        # on the system) - downloaded together with the regular updates
        # and read back by install_updates.py so they get installed too,
        # even though they may not already be installed themselves (a
        # plain sysupgrade alone never touches a package that isn't
        # already present).
        "required_extra_packages": [],
        # Set by install_prompt.py when the weekly timer fires and
        # finds phase == "ready" (downloaded updates waiting) - the
        # GUI/tray, already polling this file, turn this into the
        # "Install updates now?" Так/Ні dialog and clear it again as
        # soon as the person answers either way (see
        # main.py:maybe_show_weekly_install_prompt,
        # scripts/decline_install_prompt.py). Never set to True by
        # anything else, and never itself triggers an install -
        # install_updates.py only ever runs after an explicit Так.
        "install_prompt_pending": False,
        # Which script was running when phase last became "error":
        # "check" (check_and_download.py - covers both the sync/check
        # step and the download step) or "install" (install_updates.py).
        # phase alone can't tell these apart, since both set phase to
        # the same "error" value - this is what lets the GUI's Retry
        # button re-run the right one instead of guessing from other
        # state fields. Set at the very start of each script's main()
        # (see check_and_download.py / install_updates.py), so it's
        # already correct no matter where in that run a failure occurs.
        "failed_action": None,
    }


def is_install_retryable(state):
    """True when `state` represents a previously-failed install (phase
    "error" with failed_action "install" - see default_state above) -
    the one non-"ready" state install_updates.py is safe to run again
    from, since the packages it needs are already fully downloaded and
    cached from that earlier attempt. A failed check/download never
    reached that point, so this is False for it even though its phase
    is also "error".

    This is the single, shared definition of "an install retry is
    allowed right now" - used by install_updates.py's own startup
    guard AND by every UI entry point that can trigger an install (the
    main window's Retry button, the tray's Install Updates action) -
    so none of them can independently drift out of agreement about
    when a retry is allowed."""
    return state.get("phase") == "error" and state.get("failed_action") == "install"


def _atomic_write(path, data_bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def read_state():
    state = default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        state.update(saved)
    except (OSError, json.JSONDecodeError):
        pass
    return state


def write_state(state):
    data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write(STATE_FILE, data)


# ---------------------------------------------------------------------------
# Logging (persisted to disk, world-readable, capped in size)
# ---------------------------------------------------------------------------
def append_log(message, level="info"):
    line = f"[{now_str()}] {message}\n"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    try:
        os.chmod(LOG_FILE, 0o644)
    except OSError:
        pass
    _maybe_trim_log()


def _maybe_trim_log():
    try:
        if LOG_FILE.stat().st_size <= LOG_MAX_BYTES:
            return
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) > LOG_TRIM_KEEP_LINES:
            lines = lines[-LOG_TRIM_KEEP_LINES:]
            _atomic_write(LOG_FILE, "".join(lines).encode("utf-8"))
    except OSError:
        pass


def clear_log():
    _atomic_write(LOG_FILE, b"")


def format_cmd(cmd):
    """Render a subprocess argv list as one copy-pasteable shell command
    string for logging, e.g. ['pacman', '--config', '/run/...', '-Sy']
    becomes "pacman --config /run/... -Sy". A command already given as
    a string is returned as-is."""
    if cmd is None:
        return None
    if isinstance(cmd, (list, tuple)):
        return shlex.join(str(a) for a in cmd)
    return str(cmd)


def log_failure(action, cmd=None, returncode=None, output=None, next_step=None,
                 phase=None, level="error"):
    """One compact log line for a failed operation - written with
    enough detail to actually debug it without having to reproduce it
    by hand: what was attempted, the exact command (if a subprocess
    was involved), its exit code, the relevant error output, which
    phase this happened in, and what the app does next (retry with a
    fresh mirror, give up and report, fall back to a default, ...).
    Any field left as None is simply omitted, so this stays one line
    per failure instead of a fixed multi-line template - meant to
    replace bare, vague messages like "Error" or "Unexpected error"
    wherever this level of detail is actually available.

    `phase` defaults to whatever is currently in state.json - the
    script calling this has normally already set it before starting
    whatever just failed, so call sites don't need to pass it
    explicitly every time."""
    if phase is None:
        phase = read_state().get("phase")
    parts = [action]
    cmd_str = format_cmd(cmd)
    if cmd_str:
        parts.append(f"команда: {cmd_str}")
    if returncode is not None:
        parts.append(f"код виходу: {returncode}")
    if output:
        joined = " / ".join(l.strip() for l in str(output).splitlines() if l.strip())
        if joined:
            parts.append(f"вивід: {joined[:500]}")
    if phase:
        parts.append(f"фаза: {phase}")
    if next_step:
        parts.append(f"далі: {next_step}")
    append_log(" | ".join(parts), level=level)


def log_raw_output(heading, lines, level="error"):
    """Persist `heading` followed by each of `lines` verbatim, one
    pacman output line per log line - no truncation, no filtering.

    log_failure()'s own "вивід: ..." field is deliberately compact (one
    line, capped at 500 characters) - good enough as a quick summary,
    but not enough room for pacman's own explanation of *why* a
    transaction actually failed. For "could not satisfy dependencies"
    specifically, that explanation is pacman's ":: installing X (Y)
    breaks dependency 'Z' required by W" line (or several of them) -
    always separate from, and printed after, the generic "error: failed
    to prepare transaction (could not satisfy dependencies)" line, and
    easy to lose if only lines starting with "error:"/"warning:" are
    ever kept. Call this alongside log_failure, not instead of it, to
    keep that from happening.

    Writes nothing at all if `lines` is empty, so call sites that pass
    an empty list on the (normal) success path never add noise to the
    log."""
    cleaned = [l.strip() for l in (lines or []) if l and l.strip()]
    if not cleaned:
        return
    append_log(heading, level=level)
    for line in cleaned:
        append_log(f"  {line}", level=level)


# ---------------------------------------------------------------------------
# Locking - prevents two update processes (scheduled + manual) from
# running pacman at the same time.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Polkit authentication agent
#
# pkexec needs a running graphical authentication agent to show a password
# dialog. Lightweight X11 window managers (i3, openbox, dwm, ...) don't
# start one on their own the way a full desktop environment does, so we
# detect one and, if missing but installed, launch it ourselves. We never
# touch pacman/sudo permissions to work around this - the fix is always
# "make sure a real agent is running", never "skip authentication".
# ---------------------------------------------------------------------------
POLKIT_AGENT_CANDIDATES = [
    # (pacman package, absolute binary path, substring to match in cmdline)
    ("polkit-gnome", "/usr/lib/polkit-gnome/polkit-gnome-authentication-agent-1", "polkit-gnome-authentication-agent"),
    ("polkit-kde-agent", "/usr/lib/polkit-kde-authentication-agent-1", "polkit-kde-authentication-agent"),
    ("lxqt-policykit", "/usr/lib/lxqt-policykit/lxqt-policykit-agent", "lxqt-policykit-agent"),
    ("mate-polkit", "/usr/lib/mate-polkit/polkit-mate-authentication-agent-1", "polkit-mate-authentication-agent"),
    ("xfce-polkit", "/usr/lib/xfce-polkit/xfce-polkit", "xfce-polkit"),
    ("lxsession", "/usr/bin/lxpolkit", "lxpolkit"),
]


def find_installed_agent():
    """Return (package, path) of the first known agent binary present on disk."""
    for pkg, path, _match in POLKIT_AGENT_CANDIDATES:
        if os.path.isfile(path):
            return pkg, path
    return None, None


def is_agent_running():
    matches = [m for _p, _path, m in POLKIT_AGENT_CANDIDATES]
    try:
        for pid_dir in os.listdir("/proc"):
            if not pid_dir.isdigit():
                continue
            try:
                with open(f"/proc/{pid_dir}/cmdline", "rb") as f:
                    cmdline = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            if any(m in cmdline for m in matches):
                return True
    except OSError:
        pass
    return False


def ensure_polkit_agent():
    """Launch a polkit authentication agent for this session if one isn't
    already running and one is installed. Returns True if an agent is
    running afterwards (already was, or we just started it)."""
    if is_agent_running():
        return True
    _pkg, path = find_installed_agent()
    if not path:
        return False
    try:
        subprocess.Popen(
            [path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


@contextlib.contextmanager
def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ---------------------------------------------------------------------------
# Kernel / DKMS header handling
#
# If a kernel package (linux, linux-lts, linux-zen, linux-hardened, ...) is
# upgraded while DKMS modules (e.g. nvidia-dkms) are installed, DKMS needs
# matching "<kernel>-headers" for the NEW kernel version to rebuild
# against. Unlike the kernel package itself, the headers package is often
# not already installed at all, so a plain sysupgrade (pacman -Su) never
# touches it - it has to be requested explicitly. We detect this up front
# so the download phase can fetch matching headers before the DKMS hook
# ever runs, instead of only finding out once the pacman transaction (and
# its DKMS hook) has already failed.
#
# Every check below is deliberately done more than one independent way:
# a single marker file or a single command can be missing/fail for
# reasons unrelated to whether DKMS/a kernel package is actually
# present, and a false negative here means the person hits exactly the
# DKMS failure this is meant to prevent.
# ---------------------------------------------------------------------------
MODULES_DIR = Path("/usr/lib/modules")
DKMS_STATE_DIR = Path("/var/lib/dkms")


def is_kernel_package(name):
    """True if the installed package `name` owns a vmlinuz file under
    /usr/lib/modules - i.e. it IS a kernel package (linux, linux-lts,
    linux-zen, linux-hardened, an AUR kernel, ...). Checked directly
    against pacman's own file-ownership database (pacman -Ql) rather
    than relying on any particular marker file being present, since
    kernel-packaging conventions can vary and this only needs pacman's
    database to be correct, nothing else."""
    try:
        res = subprocess.run(["pacman", "-Ql", name], env=PACMAN_ENV,
                              capture_output=True, text=True)
    except OSError:
        return False
    if res.returncode != 0:
        return False
    for line in res.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and re.search(r"/usr/lib/modules/[^/]+/vmlinuz$", parts[1]):
            return True
    return False


def kernel_pkgbase_for_version(kernel_version):
    """Map an exact kernel release string (e.g. '6.18.44-1-lts', matching
    a directory under /usr/lib/modules) back to the package that
    provides it. Tries the "pkgbase" marker file every official/AUR
    kernel package writes first, then falls back to asking pacman
    directly which package owns a file at that path - used to turn a
    DKMS "missing headers for kernel X" error into the exact
    "<pkgbase>-headers" package name to install."""
    try:
        pkgbase = (MODULES_DIR / kernel_version / "pkgbase").read_text(encoding="utf-8").strip()
        if pkgbase:
            return pkgbase
    except OSError:
        pass
    for candidate_file in ("vmlinuz", "pkgbase", "modules.dep"):
        path = MODULES_DIR / kernel_version / candidate_file
        try:
            res = subprocess.run(["pacman", "-Qoq", str(path)], env=PACMAN_ENV,
                                  capture_output=True, text=True)
        except OSError:
            continue
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0].strip()
    return None


def has_dkms_modules():
    """True if there is at least one DKMS module registered on this
    system (e.g. nvidia-dkms, virtualbox-host-dkms, broadcom-wl-dkms).
    Checked three independent ways so a problem with any single method
    (dkms not on PATH, an unusually-named package, ...) doesn't cause a
    false negative and silently skip the headers this system needs."""
    try:
        res = subprocess.run(["dkms", "status"], env=PACMAN_ENV,
                              capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return True
    except (OSError, FileNotFoundError):
        pass
    # DKMS's own on-disk registry - doesn't depend on the dkms binary
    # being runnable, or even installed, at all.
    try:
        if any(DKMS_STATE_DIR.iterdir()):
            return True
    except OSError:
        pass
    # Any installed "*-dkms" package (nvidia-dkms, virtualbox-host-dkms, ...)
    try:
        res = subprocess.run(["pacman", "-Qq"], env=PACMAN_ENV,
                              capture_output=True, text=True)
    except OSError:
        return False
    return any(name.endswith("-dkms") for name in res.stdout.split())


def pending_upgrades():
    """Parsed "pacman -Qu" output: a list of {"name", "old_version",
    "new_version"} dicts, one per package pacman currently considers
    upgradable, in the same order "pacman -Qu" reports them (already
    alphabetical). Shared by every script that needs this list
    (check_and_download.py, delete_downloaded.py, install_updates.py)
    so the parsing logic - and the one subprocess call behind it -
    only lives in one place. See pending_upgrade_names() below for
    callers that only need the names."""
    cmd = ["pacman", "-Qu"]
    try:
        res = subprocess.run(cmd, env=PACMAN_ENV, capture_output=True, text=True)
    except OSError as e:
        log_failure(
            "Не вдалося отримати список пакетів для оновлення",
            cmd=cmd, output=str(e),
            next_step="перевірка триває так, ніби оновлень немає - це може бути "
                      "неточним, якщо pacman насправді недоступний",
        )
        return []
    # "pacman -Qu" exits non-zero (1) simply when there's nothing to
    # upgrade - that's the normal case, not a failure, so only treat
    # this as one if it actually printed something to stderr too.
    if res.returncode != 0 and (res.stderr or "").strip():
        log_failure(
            "Не вдалося отримати список пакетів для оновлення",
            cmd=cmd, returncode=res.returncode, output=res.stderr,
            next_step="перевірка триває так, ніби оновлень немає - це може бути "
                      "неточним, якщо тут насправді сталася помилка",
        )
        return []
    upgrades = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        # Normal shape: "pkgname old_version -> new_version" (pacman
        # may append a trailing "[ignored]" or similar marker after
        # that, which just becomes an extra, unused token here). Any
        # other shape is unexpected, but the name itself is still
        # trusted so nothing silently drops out of the update.
        if len(parts) >= 4 and parts[2] == "->":
            upgrades.append({"name": parts[0], "old_version": parts[1], "new_version": parts[3]})
        else:
            upgrades.append({"name": parts[0], "old_version": None, "new_version": None})
    return upgrades


def pending_upgrade_names():
    """Just the names, for callers that only need to know which
    packages are pending (kernel/DKMS detection, cache cleanup, ...) -
    see pending_upgrades() for the full name/old-version/new-version
    detail used to show the update list in the GUI."""
    return [u["name"] for u in pending_upgrades()]


def required_kernel_headers(update_names):
    """Given the set of package names about to be upgraded, return the
    "<kernel>-headers" package names (e.g. ["linux-lts-headers"]) that
    must be installed alongside them so any DKMS modules on this system
    can rebuild against the new kernel(s). Returns [] when there are no
    DKMS modules installed, or when none of the packages being updated
    are kernel packages. Never assumes there is only one installed
    kernel - every matching installed kernel is covered."""
    if not update_names or not has_dkms_modules():
        return []
    updating_kernels = sorted(n for n in update_names if is_kernel_package(n))
    return [f"{k}-headers" for k in updating_kernels]
