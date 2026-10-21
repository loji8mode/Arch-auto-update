#!/usr/bin/env python3
"""
Checks for Arch Linux updates and downloads them using pacman's own
package cache. Never installs anything. Run as root, either by
arch-auto-update-check.service - triggered continuously through the
week by arch-auto-update-check.timer, and also on demand from the
GUI/tray's "Перевірити оновлення" - or manually.

Safe to interrupt at any point (network loss, reboot, app closed):
the next run re-syncs the database and resumes using whatever pacman
already has cached, downloading only what is missing. Also safe to
run back-to-back many times a day: a cycle that finds nothing new
already downloaded finishes in a couple of seconds, and
common.acquire_lock() means an overlapping run (this timer firing
again mid-download, a manual check while one is already in progress,
...) just logs a skip instead of racing pacman against itself.
"""
import re
import sys
import glob
import shutil
import subprocess

sys.path.insert(0, "/opt/arch-auto-update")
import common as c
import diagnosis

# is_mirror_problem/is_local_db_problem/is_generic_sync_error below
# decide whether to *retry* (fresh mirror / cleared local db / drop our
# own pacman.conf); diagnosis.classify() decides how to *explain* a
# failure to the person. Both questions start from "does this text look
# like a network problem" etc, so both now read the same hint lists
# from diagnosis.py instead of keeping two hand-maintained copies that
# could silently drift apart.
MIRROR_ERROR_HINTS = diagnosis.NETWORK_ERROR_HINTS + diagnosis.TIMEOUT_HINTS

# Pacman prints "failed to synchronize all databases (unexpected
# error)" as a generic top-level wrapper any time "pacman -Sy" fails -
# on its own it says nothing about why. One common cause is a
# corrupted/truncated local copy of a sync database (e.g. left over
# from a previous run that got interrupted - a full disk, a killed
# process, a power loss mid-download) - switching mirrors can't fix
# that, re-fetching a *fresh* local database does. These hints are the
# specific, unambiguous signatures of that case.
LOCAL_DB_ERROR_HINTS = diagnosis.LOCAL_DB_ERROR_HINTS

# The generic wrapper text itself, kept separate from the specific
# corruption signatures above. Confirmed the hard way: a run that hit
# only this generic message, three times in a row, was being treated
# as pure local-db corruption (see is_local_db_problem below) and
# never once tried a fresh mirror - even though the --debug diagnostic
# for that exact run (curl completing every file "successfully" while
# pacman's own download validation still rejected each one) pointed
# squarely at the mirror, and the very next scheduled check succeeded
# with no other change. Treated as "cause unknown" below instead of
# "assume local corruption": every recovery step gets a turn - mirror
# refresh first, since it's the cheaper, non-destructive one and, per
# that same incident, more often the actual fix.
GENERIC_SYNC_ERROR_HINTS = diagnosis.GENERIC_SYNC_ERROR_HINTS


def is_mirror_problem(text):
    low = (text or "").lower()
    return any(h in low for h in MIRROR_ERROR_HINTS)


def is_local_db_problem(text):
    low = (text or "").lower()
    return any(h in low for h in LOCAL_DB_ERROR_HINTS)


def is_generic_sync_error(text):
    low = (text or "").lower()
    return any(h in low for h in GENERIC_SYNC_ERROR_HINTS)


def repair_local_sync_db():
    """Clears pacman's local copy of the sync databases so the next
    "pacman -Sy" re-downloads them from scratch instead of trying (and
    failing) to work with whatever's on disk. This is the standard fix
    for "failed to synchronize all databases (unexpected error)".
    Only ever removes pacman's own re-downloadable cache files under
    /var/lib/pacman/sync - never anything from the installed system."""
    removed = 0
    try:
        for path in c.SYNC_DB_DIR.glob("*.db*"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    if removed:
        c.append_log(
            f"Видалено {removed} файл(ів) локальної бази даних пакетів (схоже, "
            "пошкоджені) — повторне завантаження з нуля..."
        )
    return removed > 0


def run(cmd):
    return subprocess.run(cmd, env=c.PACMAN_ENV, capture_output=True, text=True)


def run_pacman(pacman_args, gentle_conf=None):
    return run(c.pacman_cmd(pacman_args, gentle_conf))


def current_mirror():
    try:
        with open(c.MIRRORLIST, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("Server"):
                    url = line.split("=", 1)[1].strip()
                    return url.split("//", 1)[-1].split("/", 1)[0]
    except OSError:
        pass
    return "невідомо"


def refresh_mirrors():
    if shutil.which("reflector") is None:
        c.append_log(
            "reflector не встановлено — автоматичне оновлення списку дзеркал "
            "недоступне. Встановіть його: sudo pacman -S reflector",
            level="warn",
        )
        return False
    c.append_log("Проблема з дзеркалом, оновлення списку дзеркал (reflector)...")
    cmd = [
        "reflector", "--latest", "15", "--sort", "rate",
        "--protocol", "https", "--save", str(c.MIRRORLIST),
    ]
    res = run(cmd)
    if res.returncode == 0:
        c.append_log(f"Список дзеркал оновлено. Використовується дзеркало: {current_mirror()}")
        return True
    c.log_failure(
        "Не вдалося оновити список дзеркал (reflector)",
        cmd=cmd, returncode=res.returncode, output=res.stderr or res.stdout,
        next_step="продовжуємо з поточним списком дзеркал без оновлення",
    )
    return False


def diagnostic_sync_probe(gentle_conf):
    """Last resort after every normal retry has already failed with the
    same generic, uninformative "unexpected error" - pacman's regular
    output for this case is a single terse line with nothing else to
    go on (already confirmed: mirror switches don't fix it, clearing
    the local db doesn't fix it, and it fails identically with or
    without this app's own pacman.conf). Runs one extra 'pacman -Sy
    --debug' purely to surface what's actually failing underneath it -
    DNS, TLS/certificates, a proxy, disk space - none of which show up
    in the normal, non-debug output at all. Only logs lines that look
    genuinely diagnostic, not the full (very large) --debug transcript."""
    res = run_pacman(["-Sy", "--debug"], gentle_conf)
    out = (res.stdout or "") + (res.stderr or "")
    keywords = (
        "curl", "ssl", "tls", "certificate", "proxy", "resolve", "dns",
        "timeout", "connect", "errno", "no space", "cert", "handshake",
        "clock", "date",
    )
    interesting = [
        line.strip() for line in out.splitlines()
        if any(k in line.lower() for k in keywords)
    ]
    if interesting:
        c.append_log(
            "Діагностика (pacman --debug) - рядки, що можуть пояснити "
            "справжню причину:",
            level="warn",
        )
        for line in interesting[:20]:
            c.append_log(f"  {line}", level="warn")
    else:
        c.append_log(
            "Діагностика (pacman --debug) не дала додаткових деталей - "
            "потрібна перевірка вручну (мережа, сертифікати, системний "
            "час, вільне місце на диску)."
        )


def sync_db(max_attempts=4, allow_reflector=True, allow_db_repair=True, gentle_conf=None):
    """Returns (True, None, None) on success, or (False, reason,
    update_error) on failure, where `reason` is the most specific error
    line seen (already logged in full above this point) - used for a
    more useful status message than a generic "sync failed" in the
    GUI - and `update_error` is a diagnosis.UpdateError classifying the
    last attempt, for the GUI's plain-language explanation (see
    diagnosis.py)."""
    tried_mirror_refresh = False
    tried_db_repair = False
    tried_without_gentle_conf = False
    last_reason = "невідома помилка"
    last_all_lines = []
    last_returncode = None
    for attempt in range(1, max_attempts + 1):
        cmd = c.pacman_cmd(["-Sy"], gentle_conf)
        res = run(cmd)
        out = (res.stdout or "") + (res.stderr or "")
        if res.returncode == 0 and "error:" not in out.lower():
            if attempt > 1 and gentle_conf is None and tried_without_gentle_conf:
                c.append_log(
                    "Синхронізація вдалася із системним pacman.conf (без "
                    "--config) - власний файл конфігурації застосунку "
                    "виглядає причетним до попередніх невдач.",
                    level="warn",
                )
            return True, None, None

        # Log every error/warning line, not just the last one - the
        # specific reason (which repo, which file, what kind of
        # failure) is often earlier in the output, and is exactly what
        # tells a mirror problem apart from a local one - or what's
        # needed to debug this by hand if every retry still fails. Note
        # which pacman.conf was in play for each attempt too, since
        # that's exactly the kind of thing needed to tell "this app's
        # own invocation is somehow the problem" apart from "pacman
        # itself/the mirror/the local db is the problem" - the two
        # would otherwise look identical in the log.
        conf_note = "з власним pacman.conf (--config)" if gentle_conf else "із системним pacman.conf"
        all_lines = [l for l in out.strip().splitlines() if l.strip()]
        problem_lines = [
            l for l in all_lines
            if "error:" in l.lower() or "warning:" in l.lower()
        ] or (all_lines[-1:] if all_lines else [])
        for line in problem_lines:
            c.append_log(f"Помилка синхронізації бази даних ({conf_note}): {line.strip()}", level="error")
        # "-Sy" doesn't resolve dependencies, so it won't hit the
        # ":: installing X breaks dependency Y" case download_updates()
        # can - but it can still print other unlabelled context lines
        # (signature/keyring trust prompts, "downloading ... .db" lines
        # around a truncated error, ...) that the plain "error:"/
        # "warning:" filter above would otherwise drop just as silently.
        other_lines = [l for l in all_lines if l not in problem_lines]
        c.log_raw_output(
            f"Повний вивід pacman -Sy ({conf_note}), рядки без "
            "\"error:\"/\"warning:\":",
            other_lines,
        )
        last_reason = problem_lines[-1].strip() if problem_lines else "невідома помилка"
        last_all_lines = all_lines
        last_returncode = res.returncode

        # Decide what happens next BEFORE logging the compact failure
        # summary below, so its "далі" field always names the actual
        # next step about to happen, not a guess made after the fact.
        generic = is_generic_sync_error(out)
        mirror_like = is_mirror_problem(out) or generic
        corruption_like = is_local_db_problem(out) or generic
        if attempt >= max_attempts:
            remedy, next_step = None, "діагностика (pacman --debug) і завершення з помилкою"
        elif mirror_like and allow_reflector and not tried_mirror_refresh:
            remedy, next_step = "mirror", "оновлення списку дзеркал (reflector) і повторна спроба"
        elif corruption_like and allow_db_repair and not tried_db_repair:
            remedy, next_step = "db_repair", "очищення локальної бази даних і повторна спроба"
        elif gentle_conf and not tried_without_gentle_conf and (mirror_like or corruption_like):
            remedy, next_step = "drop_conf", "повторна спроба із системним pacman.conf (без --config)"
        else:
            remedy, next_step = None, "діагностика (pacman --debug) і завершення з помилкою"

        c.log_failure(
            f"Не вдалося синхронізувати базу даних (спроба {attempt}/{max_attempts}, {conf_note})",
            cmd=cmd, returncode=res.returncode,
            output=" / ".join(l.strip() for l in problem_lines) or out,
            phase="checking", next_step=next_step,
        )

        if remedy is None:
            break
        if remedy == "mirror":
            # Pause first - before anything (reflector included) makes
            # another network request - then try a fresh mirror. Tried
            # ahead of local-db repair even for the ambiguous generic
            # message: cheaper, non-destructive, and (per the incident
            # documented above GENERIC_SYNC_ERROR_HINTS) more often the
            # actual fix.
            tried_mirror_refresh = True
            c.wait_before_retry(attempt)
            refresh_mirrors()
        elif remedy == "db_repair":
            tried_db_repair = True
            repair_local_sync_db()
            c.wait_before_retry(attempt)
        elif remedy == "drop_conf":
            # Still failing the same way even after a fresh mirror and
            # a cleared local sync database. If this app has been
            # using its own gentle pacman.conf (--config) this whole
            # time, that copy itself is now a suspect - a manual
            # "pacman -Syu" using the system's own pacman.conf might
            # still work fine even though this doesn't. Try once,
            # plainly, without --config, purely to tell the two apart.
            # Never loosens the concurrency limit for the actual
            # package downloads later - only -Sy fetches a couple of
            # small database files - and only runs as a last resort,
            # after every other retry has already failed.
            tried_without_gentle_conf = True
            gentle_conf = None
            c.wait_before_retry(attempt)

    # Every normal retry (fresh mirror, cleared local db, and - if
    # relevant - dropping this app's own pacman.conf) has already
    # failed the same way. One extra --debug probe, purely for
    # diagnosis, before giving up and reporting the failure.
    diagnostic_sync_probe(gentle_conf)
    update_error = diagnosis.classify(last_all_lines, returncode=last_returncode, phase="checking")
    return False, last_reason, update_error


def get_download_sizes(names, gentle_conf=None):
    """Returns (sizes, versions): {name: download_size_bytes} and
    {name: exact "pkgver-pkgrel" version string}, both parsed from a
    single "pacman -Si" call - the version is what lets main() check
    pacman's own cache for a file that already matches exactly what's
    needed (see all_targets_cached), not just a size estimate."""
    sizes = {}
    versions = {}
    if not names:
        return sizes, versions
    res = run_pacman(["-Si"] + names, gentle_conf)
    cur_name = None
    for line in res.stdout.splitlines():
        if line.startswith("Name"):
            cur_name = line.split(":", 1)[1].strip()
        elif line.startswith("Version") and cur_name:
            versions[cur_name] = line.split(":", 1)[1].strip()
        elif line.startswith("Download Size") and cur_name:
            sizes[cur_name] = c.parse_pacman_size(line.split(":", 1)[1].strip())
    return sizes, versions


def all_targets_cached(names, versions):
    """True if every package in `names` already has its EXACT needed
    version (from `versions`, keyed by name) sitting in pacman's cache
    as a complete file. Pacman only ever uses the bare
    "name-version-arch.pkg.tar.*" filename once a download has fully
    finished - while one is in progress it's always
    "...pkg.tar.*.part", renamed only on completion - so checking for
    that exact, non-.part filename is the same signal pacman itself
    relies on, not a separate guess that could ever disagree with what
    pacman actually does when it runs. A package pacman -Si didn't
    return a version for (see missing_sizes in main()) is treated as
    NOT cached, so it always goes through pacman's own -Syuw rather
    than being silently skipped."""
    for n in names:
        version = versions.get(n)
        if not version:
            return False
        stem = f"{glob.escape(n)}-{glob.escape(version)}-*"
        matches = (
            glob.glob(str(c.CACHE_DIR / f"{stem}.pkg.tar.zst"))
            or glob.glob(str(c.CACHE_DIR / f"{stem}.pkg.tar.xz"))
        )
        if not matches:
            return False
    return True


def build_patterns(names):
    # Matches "<name>-<digit...>" as a whole filename token, so
    # "python-3.12..." doesn't get confused with "python-pip-1.0...".
    return {n: re.compile(r"(?<![\w.+-])" + re.escape(n) + r"-\d") for n in names}


def download_updates(names, sizes, total_size, state, extra_targets=None,
                      max_attempts=3, gentle_conf=None):
    patterns = build_patterns(names)
    completed = set()
    extra_targets = extra_targets or []

    def progress_snapshot():
        done = sum(sizes.get(n, 0) for n in completed)
        pct = int(done / total_size * 100) if total_size else 0
        state["downloaded_bytes"] = done
        state["progress_percent"] = min(pct, 99)
        c.write_state(state)

    for attempt in range(1, max_attempts + 1):
        cmd = c.pacman_cmd(["-Syuw", "--noconfirm", "--noprogressbar"] + extra_targets, gentle_conf)
        proc = subprocess.Popen(
            cmd,
            env=c.PACMAN_ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        had_error = False
        error_lines = []
        other_lines = []
        all_lines = []
        for raw in proc.stdout:
            line = c.strip_ansi(raw.rstrip())
            if not line:
                continue
            all_lines.append(line)
            low = line.lower()
            if ".pkg.tar" in low:
                for n, pat in patterns.items():
                    if n not in completed and pat.search(line):
                        completed.add(n)
                        progress_snapshot()
                        c.append_log(f"{line.strip()} ... OK")
                        break
            elif low.startswith("error:") or " error:" in low:
                had_error = True
                error_lines.append(line)
                c.append_log(line, level="error")
            elif low.startswith("warning:"):
                c.append_log(line, level="warn")
            else:
                # Everything else pacman prints during dependency
                # resolution - crucially including its own explanation
                # of *why* "could not satisfy dependencies" happened,
                # e.g. ":: installing X (Y) breaks dependency 'Z'
                # required by W". That line starts with ":: ", not
                # "error:" or "warning:", so none of the branches above
                # ever catch it - previously it was read from pacman's
                # stdout and then discarded here, appearing nowhere in
                # the log. Kept in memory only; only written out if
                # this attempt ultimately fails (below), so a normal
                # successful run's log stays exactly as compact as it
                # always was.
                other_lines.append(line)
        proc.wait()

        # "local is newer than repo" is never a failure by itself (see
        # diagnosis.extract_local_newer_notices) - pacman just leaves
        # that one package alone and continues. Recorded on every
        # attempt, success or failure, so the GUI can show "N packages
        # skipped" as its own small, calm, informational note instead
        # of this only being visible by scrolling the log - and
        # overwritten (not accumulated) each time so it always reflects
        # this run, not a stale one.
        state["skipped_packages"] = diagnosis.extract_local_newer_notices(all_lines)

        if proc.returncode == 0 and not had_error:
            return True

        combined = "\n".join(error_lines)
        if attempt < max_attempts and is_mirror_problem(combined):
            c.log_failure(
                f"Завантаження оновлень не вдалося (спроба {attempt}/{max_attempts}, "
                "схоже на проблему з дзеркалом)",
                cmd=cmd, returncode=proc.returncode, output=combined,
                phase="downloading",
                next_step="оновлення списку дзеркал і повторна спроба",
            )
            # Pause first, before refresh_mirrors or the next attempt
            # puts any more load on the connection.
            c.wait_before_retry(attempt)
            refresh_mirrors()
            sync_db(max_attempts=1, allow_reflector=False, gentle_conf=gentle_conf)
            continue
        else:
            update_error = diagnosis.classify(
                all_lines, returncode=proc.returncode, phase="downloading",
                context={"required_bytes": total_size},
            )
            state["phase"] = "error"
            state["error"] = update_error.to_dict()
            state["error_message"] = update_error.summary_text()
            c.write_state(state)
            c.log_raw_output(
                "Повний вивід pacman, рядки без \"error:\"/\"warning:\" "
                "(саме тут зазвичай знаходиться справжня причина "
                "\"could not satisfy dependencies\" - наприклад, який саме "
                "пакет ламає яку саме залежність):",
                other_lines,
            )
            c.log_failure(
                "Завантаження оновлень не вдалося",
                cmd=cmd, returncode=proc.returncode,
                output=combined or "(pacman завершився з помилкою без рядків error:)",
                phase="downloading",
                next_step="стан збережено - завантаження відновиться при наступній "
                          "фоновій перевірці, використовуючи вже кешовані файли",
            )
            return False
    return False


def main():
    with c.acquire_lock() as got:
        if not got:
            c.append_log("Процес оновлення вже виконується, пропуск запуску.")
            return

        state = c.read_state()
        state["phase"] = "checking"
        state["error_message"] = None
        state["error"] = None
        state["skipped_packages"] = []
        state["failed_action"] = "check"
        c.write_state(state)
        c.append_log("Перевірка оновлень розпочата")

        gentle_conf, gentle_conf_error = c.prepare_gentle_pacman_conf()
        if gentle_conf:
            c.append_log(
                "Власний pacman.conf підготовлено: завантаження по одному "
                "файлу за раз (системний /etc/pacman.conf не змінюється)"
            )
        else:
            c.log_failure(
                "Не вдалося підготувати власний pacman.conf",
                output=gentle_conf_error,
                phase="checking",
                next_step="використовується системний pacman.conf (паралельність "
                          "завантаження за замовчуванням, без обмеження)",
                level="warn",
            )

        ok, reason, update_error = sync_db(gentle_conf=gentle_conf)
        if not ok:
            state["phase"] = "error"
            state["error_message"] = (
                f"Не вдалося синхронізувати базу даних пакетів: {reason}"
                if reason else "Не вдалося синхронізувати базу даних пакетів"
            )
            if update_error:
                state["error"] = update_error.to_dict()
            c.write_state(state)
            # Each attempt's own command/exit code/output is already
            # logged in full by sync_db above - this closing line just
            # states the outcome and what happens next, not vague
            # boilerplate: the reason (already the most specific line
            # seen) and that the next attempt happens automatically,
            # in the background, without the person needing to do
            # anything.
            c.append_log(
                f"Перевірка оновлень зупинена: {reason or 'синхронізацію бази даних не вдалося виконати'}. "
                "Наступна спроба відбудеться автоматично на найближчому фоновому "
                "циклі перевірки (arch-auto-update-check.timer).",
                level="error",
            )
            return

        upgrades = c.pending_upgrades()
        names = [u["name"] for u in upgrades]
        if not names:
            state = c.default_state()
            state["phase"] = "no_updates"
            state["last_check"] = c.now_str()
            c.write_state(state)
            c.append_log("Оновлень не знайдено")
            return

        # If a kernel package is among the updates and this system has
        # DKMS modules (e.g. nvidia-dkms) installed, matching
        # "<kernel>-headers" must be downloaded too - otherwise the
        # install-time DKMS hook fails with "Missing ... kernel headers
        # for module ..." after the kernel itself already installed.
        # Logged either way so this is debuggable if it's ever wrong,
        # rather than silently skipping headers with no visible trace.
        kernel_updates = [n for n in names if c.is_kernel_package(n)]
        extra_headers = []
        if kernel_updates:
            dkms_present = c.has_dkms_modules()
            if dkms_present:
                extra_headers = [f"{k}-headers" for k in kernel_updates]
                c.append_log(
                    f"Серед оновлень є ядро: {', '.join(kernel_updates)}. Активні "
                    f"модулі DKMS знайдено — додатково буде завантажено заголовки "
                    f"ядра: {', '.join(extra_headers)}"
                )
            else:
                c.append_log(
                    f"Серед оновлень є ядро: {', '.join(kernel_updates)}. "
                    "Активних модулів DKMS не знайдено — заголовки ядра не потрібні."
                )

        all_targets = list(names)
        for h in extra_headers:
            if h not in all_targets:
                all_targets.append(h)

        sizes, versions = get_download_sizes(all_targets, gentle_conf)

        # A computed "<kernel>-headers" name might not actually exist in
        # the configured repos (renamed/discontinued package, unusual
        # kernel flavour, ...). Don't force an unknown target into the
        # transaction - that would fail the whole download - just warn
        # so the person can sort it out manually.
        missing_headers = [h for h in extra_headers if h not in sizes]
        extra_headers = [h for h in extra_headers if h in sizes]
        for h in missing_headers:
            all_targets.remove(h)
            c.append_log(
                f"Пакет заголовків ядра '{h}' не знайдено в репозиторіях — "
                "автоматичне завантаження неможливе, встановіть його вручну "
                "за потреби.",
                level="warn",
            )

        # Separate from the headers case above (which already has its
        # own specific handling) - any OTHER target pacman -Si didn't
        # return a size for. Doesn't block the download (pacman itself
        # will still determine the real size when it runs), but left
        # completely unlogged this only shows up as an undercounted
        # total/progress percentage with no visible explanation.
        missing_sizes = [n for n in all_targets if n not in sizes and n not in extra_headers]
        if missing_sizes:
            c.append_log(
                f"Розмір завантаження не визначено для {len(missing_sizes)} "
                f"пакет(ів): {', '.join(missing_sizes)} — загальний розмір і "
                "прогрес будуть занижені для них; саме завантаження це не "
                "блокує.",
                level="warn",
            )

        total_size = sum(sizes.get(n, 0) for n in all_targets)

        state["package_count"] = len(all_targets)
        state["total_size_bytes"] = total_size
        state["last_check"] = c.now_str()
        state["required_extra_packages"] = extra_headers
        state["packages"] = upgrades

        if all_targets_cached(all_targets, versions):
            # Every needed package is already a complete, exact-version
            # file in pacman's own cache - there is nothing to fetch.
            # Skip the visible "downloading" phase and its start/finish
            # log lines entirely, instead of flashing through them for
            # a download that wouldn't actually download anything
            # (pacman itself would just confirm this and return in a
            # couple of seconds - this check turns that into one clear
            # log line instead of an unexplained blip in the GUI every
            # ~30 minutes). Normal download/progress/resume behaviour
            # below is completely unchanged for the case where anything
            # is actually missing or incomplete.
            state["downloaded_bytes"] = total_size
            state["progress_percent"] = 100
            state["phase"] = "ready"
            state["failed_action"] = None
            c.write_state(state)
            c.append_log(
                f"Усі необхідні пакети вже завантажені в кеш pacman "
                f"({len(all_targets)} пакетів, {c.human_size(total_size)}) — "
                "повторне завантаження не потрібне."
            )
            c.append_log("Оновлення готово до встановлення")
            return

        state["downloaded_bytes"] = 0
        state["progress_percent"] = 0
        state["phase"] = "downloading"
        c.write_state(state)
        c.append_log(f"Оновлення доступні: {len(names)} пакетів ({c.human_size(total_size)})")
        c.append_log(f"Використовується дзеркало: {current_mirror()}")
        c.append_log("Завантаження розпочато")

        ok = download_updates(all_targets, sizes, total_size, state,
                               extra_targets=extra_headers, gentle_conf=gentle_conf)
        if ok:
            state["phase"] = "ready"
            state["downloaded_bytes"] = total_size
            state["progress_percent"] = 100
            state["failed_action"] = None
            c.write_state(state)
            c.append_log(f"Завантаження завершено успішно ({c.human_size(total_size)})")
            c.append_log("Оновлення готово до встановлення")


if __name__ == "__main__":
    main()
