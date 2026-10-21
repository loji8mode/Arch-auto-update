#!/bin/bash
# Fixed-purpose wrapper for polkit/pkexec: starts the systemd unit that
# actually checks for and downloads updates, and waits for it to finish.
#
# Why this exists instead of pkexec running check_and_download.py
# directly: a direct "pkexec check_and_download.py" invocation was
# observed, reproducibly, to make pacman's own database sync fail with
# a generic "error: failed to synchronize all databases (unexpected
# error)" - every single time, across different mirrors, with or
# without this app's own pacman.conf. The exact same script, run under
# this exact same systemd unit (arch-auto-update-check.service) -
# whether via the weekly timer or via a plain "systemctl start" - was
# confirmed to work reliably every time. Routing pkexec through
# systemd instead of running the script directly sidesteps whatever
# about pkexec's own process environment was causing that.
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
exec systemctl start --wait arch-auto-update-check.service
