#!/bin/bash
# Fixed-purpose wrapper for polkit/pkexec: starts the systemd unit
# that actually installs already-downloaded updates, and waits for it
# to finish.
#
# Why this exists instead of pkexec running install_updates.py
# directly: the exact same problem documented in run_check_service.sh
# for check_and_download.py - pkexec's own process environment
# breaking pacman's internal waitpid() calls - turns out to hit the
# install step just as hard. Reproduced this way: the pacman
# transaction's actual file-copy phase always completed fine, but
# pacman's OWN post-transaction hook step right after it
# (ConditionNeedsUpdate, update-info-dir, mkinitcpio, ...) failed to
# reap the child processes those hooks forked - "error: call to
# waitpid failed (No child processes)" - every single time, only when
# install_updates.py ran straight under pkexec. That's also why a
# second, unrelated attempt right after would quietly "succeed":
# pacman found "there is nothing to do", because the first attempt's
# transaction had, in fact, already gone through - only the hook
# step's own success/failure reporting was broken. Routing this
# through systemd instead of pkexec directly fixes it exactly the way
# it already does for arch-auto-update-check.service.
#
# Deliberately takes no arguments and does exactly this one fixed
# thing. Polkit's org.freedesktop.policykit.exec.path annotation (see
# polkit/io.github.archautoupdate.policy) can only restrict *which
# executable* pkexec is allowed to run, never its arguments - so
# pointing pkexec straight at systemctl itself would let anyone run
# "pkexec systemctl <anything>" under this same authorization. Keeping
# this wrapper this narrow (one command, no arguments, nothing taken
# from the environment or from argv) is what keeps that restriction
# meaningful.
exec systemctl start --wait arch-auto-update-install.service
