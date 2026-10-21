#!/usr/bin/env python3
"""
Installs updates that have already been downloaded to pacman's cache.
Never triggers new downloads by itself (pacman -Su re-uses the cache).
Run as root via arch-auto-update-install.service - started by the GUI
via pkexec + scripts/run_install_service.sh (never pkexec running this
script directly - see run_install_service.sh for why: pacman's own
post-transaction hooks were observed failing with "call to waitpid
failed (No child processes)" when this ran straight under pkexec,
exactly the same problem check_and_download.py already worked around
the same way). Triggered whenever the person presses "Так": on the
manual "Встановити оновлення" button/menu action, or on the weekly
"Install updates now?" prompt (see install_prompt.py).

Also installs any "<kernel>-headers" packages required for DKMS to
rebuild against a new kernel - a plain "pacman -Su" alone never
installs a package that wasn't already installed, even if it was
already downloaded to the cache. Two layers of protection here:

1. check_and_download.py already worked out which headers were
   needed at download time (state.json: required_extra_packages).
   We start from that.
2. We ALSO recompute the same check again, right now, from scratch
   (current DKMS status + current pending upgrades) and merge the
   result in. This closes the gap where the two could disagree -
   e.g. a DKMS module (nvidia-dkms, ...) was installed *after* the
   download already completed, or the first check simply failed for
   a transient reason (a "pacman -Si" hiccup) - either of which would
   otherwise silently leave DKMS without the headers it needs.

3. Belt and suspenders: if pacman still fails with the exact
   "Missing ... kernel headers for module ..." DKMS error despite
   both of the above (some edge case neither anticipated), we install
   the missing headers package and re-run the DKMS build for that
   specific module ourselves before giving up - so the person doesn't
   have to go type a pacman command by hand for something this app is
   in the best position to just fix.
"""
import re
import sys
import subprocess

sys.path.insert(0, "/opt/arch-auto-update")
import common as c

# Matches the DKMS/pacman-hook line reported when a kernel update
# succeeds but DKMS can't rebuild a module because matching headers for
# the new kernel aren't installed, e.g.:
#   ERROR: Missing 6.18.44-1-lts kernel headers for module nvidia/610.57.04.
DKMS_MISSING_HEADERS_RE = re.compile(
    r"Missing\s+(?P<kver>\S+)\s+kernel headers for module\s+(?P<module>\S+)",
    re.IGNORECASE,
)


def find_dkms_headers_failure(output_lines):
    """Return (kver, module) if the pacman output shows a DKMS hook
    failing specifically because of missing kernel headers, else
    None."""
    for line in output_lines:
        m = DKMS_MISSING_HEADERS_RE.search(line)
        if m:
            return m.group("kver"), m.group("module").rstrip(".")
    return None


def describe_dkms_failure(kver, module):
    """Precise, actionable explanation (naming the exact
    "<kernel>-headers" package) for when self-healing wasn't possible,
    instead of leaving the person with only a generic pacman error
    dump."""
    pkgbase = c.kernel_pkgbase_for_version(kver)
    headers_pkg = f"{pkgbase}-headers" if pkgbase else f"заголовки ядра для {kver}"
    return (
        f"Ядро оновлено успішно, але DKMS не зміг перебудувати модуль "
        f"'{module}' для {kver}: відсутні відповідні заголовки ядра. "
        f"Встановіть пакет вручну і повторіть спробу: "
        f"sudo pacman -S {headers_pkg}"
    )


def run_pacman_stream(targets, gentle_conf=None):
    """Runs 'pacman -Su [targets...]', streaming+logging output as it
    happens. Returns (success, error_lines, all_lines, returncode).
    Normally this re-uses what's already in the cache and touches the
    network only for the sync-db check pacman does up front, but on
    the rare chance a cached file is missing or partial and pacman has
    to re-fetch it at commit time, `gentle_conf` (see
    common.prepare_gentle_pacman_conf) keeps that fetch to one file at
    a time too."""
    cmd = c.pacman_cmd(["-Su", "--noconfirm", "--noprogressbar"] + targets, gentle_conf)
    proc = subprocess.Popen(
        cmd,
        env=c.PACMAN_ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    had_error = False
    error_lines = []
    all_lines = []
    for raw in proc.stdout:
        line = c.strip_ansi(raw.rstrip())
        if not line:
            continue
        all_lines.append(line)
        low = line.lower()
        if low.startswith("error:") or " error:" in low:
            had_error = True
            error_lines.append(line)
            c.append_log(line, level="error")
        else:
            c.append_log(line)
    proc.wait()

    # pacman's own exit status is the primary success/failure signal.
    # We also still honour any "error:" line we saw even if the exit
    # code was somehow 0 (defensive: some hook failures are only
    # visible as printed errors), but we must never call the install a
    # success just because the *kernel* part of the transaction went
    # fine while a later DKMS hook failed - that failure is what
    # pacman's own exit status reflects.
    success = proc.returncode == 0 and not had_error
    return success, error_lines, all_lines, proc.returncode, cmd


def self_heal_dkms_headers(kver, module, gentle_conf=None):
    """A DKMS hook just failed because headers for `kver` are missing.
    Try to fix that ourselves: install the matching "<kernel>-headers"
    package, then ask DKMS to rebuild `module` for `kver` directly.
    Returns True if the module builds successfully afterwards."""
    pkgbase = c.kernel_pkgbase_for_version(kver)
    if not pkgbase:
        c.log_failure(
            f"Автоматичне відновлення DKMS: не вдалося визначити пакет ядра "
            f"для версії {kver}",
            phase="installing",
            next_step="автоматичне відновлення скасовано - потрібне ручне "
                      "встановлення відповідного пакета заголовків ядра",
        )
        return False

    headers_pkg = f"{pkgbase}-headers"
    c.append_log(
        f"Автоматичне відновлення: встановлення відсутнього пакета "
        f"'{headers_pkg}'..."
    )
    # This package wasn't downloaded ahead of time, so this is a real
    # fresh network fetch - keep it gentle too.
    cmd = c.pacman_cmd(["-S", "--noconfirm", "--noprogressbar", headers_pkg], gentle_conf)
    res = subprocess.run(cmd, env=c.PACMAN_ENV, capture_output=True, text=True)
    if res.returncode != 0:
        c.log_failure(
            f"Автоматичне відновлення DKMS: не вдалося встановити '{headers_pkg}'",
            cmd=cmd, returncode=res.returncode, output=res.stderr or res.stdout,
            phase="installing",
            next_step="автоматичне відновлення скасовано - потрібне ручне "
                      "втручання",
        )
        return False
    c.append_log(f"Пакет '{headers_pkg}' встановлено успішно.")

    c.append_log(f"Повторна збірка модуля DKMS '{module}' для {kver}...")
    dkms_cmd = ["dkms", "install", module, "-k", kver, "--force"]
    res = subprocess.run(dkms_cmd, env=c.PACMAN_ENV, capture_output=True, text=True)
    ok = res.returncode == 0
    if not ok:
        c.log_failure(
            f"Автоматичне відновлення DKMS: не вдалося повторно зібрати "
            f"модуль '{module}'",
            cmd=dkms_cmd, returncode=res.returncode,
            output=(res.stdout or "") + (res.stderr or ""),
            phase="installing",
            next_step="автоматичне відновлення скасовано - потрібне ручне "
                      "втручання",
        )
        return False
    c.append_log(f"Модуль DKMS '{module}' успішно перебудовано для {kver}.")

    # The pacman transaction already ran its initramfs-regeneration
    # hook earlier in the same run, before DKMS failed - so the image
    # it produced doesn't contain the module we just built. Regenerate
    # it now so an early-KMS/initrd-loaded module is actually present,
    # not just installed under /usr/lib/modules.
    c.append_log("Перебудова initramfs з урахуванням щойно зібраного модуля...")
    mkinitcpio_cmd = ["mkinitcpio", "-P"]
    res = subprocess.run(mkinitcpio_cmd, env=c.PACMAN_ENV, capture_output=True, text=True)
    if res.returncode != 0:
        # Not fatal - the module itself built fine and will load for
        # anything that doesn't need it baked into the initrd - so
        # this stays a warning (self_heal still returns True below),
        # but the next_step gives the exact manual command anyway,
        # since only the initrd step needs re-running, not the whole
        # rebuild.
        c.log_failure(
            "Автоматичне відновлення DKMS: не вдалося перебудувати initramfs",
            cmd=mkinitcpio_cmd, returncode=res.returncode,
            output=(res.stdout or "") + (res.stderr or ""),
            phase="installing",
            next_step="модуль DKMS уже зібрано і встановлено - initramfs НЕ "
                      "перебудовано автоматично, виконайте вручну: sudo "
                      "mkinitcpio -P",
            level="warn",
        )
    else:
        c.append_log("Initramfs перебудовано.")
    return True


def main():
    with c.acquire_lock() as got:
        if not got:
            print("Інший процес оновлення вже виконується.", file=sys.stderr)
            sys.exit(1)

        state = c.read_state()
        phase = state.get("phase")
        # Normally only runs from "ready" (a completed download). Also
        # allowed when retrying a previously *failed install*
        # specifically (see common.is_install_retryable) - the packages
        # are already fully downloaded and cached from that earlier run,
        # nothing about that changes on a retry. Not allowed for any
        # other "error" (e.g. a failed check/download never reached a
        # fully-downloaded state to install from) or any other phase.
        if phase != "ready" and not c.is_install_retryable(state):
            print("Оновлення ще не готові до встановлення.", file=sys.stderr)
            sys.exit(1)

        # Start from what check_and_download.py already worked out...
        extra_targets = set(state.get("required_extra_packages") or [])
        # ...then check again right now, from scratch. If the two
        # disagree (a DKMS module appeared/disappeared, or the first
        # check missed something transiently) this is what actually
        # prevents the install from repeating the "missing kernel
        # headers" failure instead of just describing it afterwards.
        fresh_targets = c.required_kernel_headers(c.pending_upgrade_names())
        extra_targets.update(fresh_targets)
        extra_targets = sorted(extra_targets)

        state["phase"] = "installing"
        state["error_message"] = None
        state["failed_action"] = "install"
        # Whatever triggered this run (manual button, a weekly "Так",
        # or a retry) already answers the weekly prompt if one was
        # open - never leave it pending once an install has actually
        # started.
        state["install_prompt_pending"] = False
        c.write_state(state)
        c.append_log("Встановлення оновлень розпочато")
        if extra_targets:
            c.append_log(
                "Разом з оновленнями буде встановлено заголовки ядра для "
                f"активних модулів DKMS: {', '.join(extra_targets)}"
            )

        gentle_conf, gentle_conf_error = c.prepare_gentle_pacman_conf()
        if not gentle_conf:
            c.log_failure(
                "Не вдалося підготувати власний pacman.conf",
                output=gentle_conf_error,
                phase="installing",
                next_step="використовується системний pacman.conf (паралельність "
                          "завантаження за замовчуванням, без обмеження)",
                level="warn",
            )
        success, error_lines, all_lines, returncode, cmd = run_pacman_stream(extra_targets, gentle_conf)

        if not success:
            failure = find_dkms_headers_failure(all_lines)
            if failure and self_heal_dkms_headers(*failure, gentle_conf=gentle_conf):
                # The transaction itself already installed everything
                # else successfully - only the DKMS hook step failed,
                # and we just fixed that directly. Safe to treat the
                # whole thing as a success now.
                success = True
                c.append_log(
                    "Проблему з заголовками ядра DKMS усунено автоматично - "
                    "встановлення завершено успішно."
                )

        if success:
            # Carry the update list check_and_download.py already
            # determined through this reset, unchanged - it's exactly
            # what "pacman -Su" just installed (nothing since add
            # itself to the transaction), so the GUI can show it as
            # "successfully updated" without asking pacman again.
            installed_packages = state.get("packages") or []
            state = c.default_state()
            state["phase"] = "installed"
            state["packages"] = installed_packages
            c.write_state(state)
            c.append_log("Оновлення встановлено")
        else:
            failure = find_dkms_headers_failure(all_lines)
            if failure:
                message = describe_dkms_failure(*failure)
                next_step = (
                    "автоматичне відновлення заголовків ядра теж не вдалося - "
                    "потрібне ручне втручання (команда вище), після чого можна "
                    "повторити встановлення"
                )
            else:
                message = (
                    "\n".join(error_lines) or "\n".join(all_lines[-10:])
                    or f"pacman завершився з кодом {returncode} без рядків "
                       "error: у виводі"
                )
                next_step = (
                    "пакети вже завантажені й залишаються в кеші - повторити "
                    "встановлення можна кнопкою 'Повторити спробу' без "
                    "повторного завантаження"
                )
            state["phase"] = "error"
            state["error_message"] = message
            c.write_state(state)
            c.log_failure(
                "Встановлення оновлень не вдалося",
                cmd=cmd, returncode=returncode, output=message,
                phase="installing", next_step=next_step,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
