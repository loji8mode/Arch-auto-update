#!/usr/bin/env python3
"""
Arch Auto Update - GUI / tray entrypoint

  arch-auto-update          opens the main window
  arch-auto-update --tray   runs a background tray icon instead

Pure viewer + trigger: reads shared state/log files written by the
root-level scripts, and triggers privileged actions (install, delete
downloaded packages, check now, change timer) via pkexec, using
whatever graphical Polkit authentication agent is running for this
session. The actual scheduled check-and-download runs independently
via systemd, whether or not this app is open at all.
"""
import sys
import html
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QProcess, QPointF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QTextCursor, QAction
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QGridLayout,
    QBoxLayout, QFrame, QPushButton, QProgressBar, QCheckBox, QComboBox,
    QTextEdit, QMessageBox, QFileDialog, QSizePolicy, QSystemTrayIcon, QMenu,
    QScrollArea,
)

sys.path.insert(0, "/opt/arch-auto-update")
import common as c
import diagnosis

APP_DIR = Path(__file__).resolve().parent
ICON_PATH = APP_DIR / "icon.svg"
STYLE_PATH = APP_DIR / "style.qss"
SCRIPTS_DIR = APP_DIR / "scripts"

PHASE_INFO = {
    # phase: (color, title, subtitle, glyph)
    "idle":        ("#8b93a7", "Очікування",             "Оновлення ще не перевірялися", "dots"),
    "checking":    ("#3b82f6", "Перевірка оновлень...",   "Синхронізація бази даних пакетів", "arrow"),
    "downloading": ("#3b82f6", "Завантаження...",         "Завантаження пакетів оновлень", "arrow"),
    "pending":     ("#f5a623", "Оновлення доступні",      "Очікується наступне завантаження", "dots"),
    "ready":       ("#22c55e", "Оновлення завантажено",   "Готово до встановлення", "check"),
    "installing":  ("#3b82f6", "Встановлення...",         "Виконується встановлення оновлень", "arrow"),
    "installed":   ("#22c55e", "Оновлення встановлено",   "Система оновлена", "check"),
    "no_updates":  ("#22c55e", "Оновлень не знайдено",    "Система вже оновлена", "check"),
    "error":       ("#ef4444", "Помилка",                 "Перевірте логи для деталей", "x"),
}

LOG_LEVEL_COLORS = {
    "error": "#f87171",
    "warn": "#fbbf24",
    "ok": "#4ade80",
    "info": "#c7cede",
    "meta": "#7c85a0",
}


def explain_error(err):
    """`err` is state.get("error") - the plain dict a diagnosis.UpdateError
    round-trips to through state.json, or None. Returns (icon, title,
    detail) in Ukrainian for the person to read without opening the
    log, or None if there's nothing structured to explain (the caller
    then falls back to the older plain state["error_message"] display -
    e.g. for an install failure, which doesn't go through the
    classifier yet, or a state.json written by an older version of
    this app).

    This is deliberately the ONLY place that turns an error `type` +
    its facts into an actual sentence - diagnosis.py itself never
    contains a human-readable string, so a future second frontend (or
    a RecoveryManager acting on some of these automatically) can reuse
    the exact same classification without inheriting this phrasing.
    Any `type` this function doesn't recognise - including ones a
    newer check_and_download.py might one day add - falls through to
    the same honest "couldn't determine the cause" explanation as
    diagnosis.UNKNOWN, rather than silently showing nothing."""
    if not err:
        return None
    t = err.get("type")

    if t == diagnosis.DEPENDENCY_CONFLICT:
        pkg = err.get("package") or "?"
        req_by = err.get("required_by") or "?"
        req_ver = err.get("required_version") or "?"
        avail_ver = err.get("available_version") or "?"
        return (
            "⚠️", "Конфлікт залежностей пакетів",
            f"Пакет {req_by} потребує {pkg} версії {req_ver}, а зараз "
            f"доступна версія {avail_ver}.\n\n"
            "Це часто буває тимчасовим явищем під час синхронізації "
            "репозиторіїв - спробуйте ще раз трохи пізніше."
        )

    if t == diagnosis.DB_LOCKED:
        running = err.get("lock_process_running")
        if running is True:
            detail = (
                "Схоже, зараз одночасно виконується інший процес pacman. "
                "Зачекайте, доки він завершиться, і спробуйте ще раз."
            )
        elif running is False:
            detail = (
                "Файл блокування бази даних (db.lck) існує, але жодного "
                "активного процесу pacman зараз не знайдено - можливо, "
                "він залишився після переривання попередньої операції.\n\n"
                "Arch Auto Update ніколи не видаляє цей файл автоматично: "
                "перш ніж видаляти /var/lib/pacman/db.lck вручну, "
                "переконайтесь, що жоден pacman справді не працює."
            )
        else:
            detail = (
                "Не вдалося визначити, чи працює зараз інший процес "
                "pacman. Arch Auto Update ніколи не видаляє файл "
                "блокування автоматично."
            )
        return ("🔒", "Пакетний менеджер зайнятий", detail)

    if t == diagnosis.DISK_SPACE:
        avail = err.get("available_bytes")
        need = err.get("required_bytes")
        partition = err.get("partition") or "/"
        avail_s = c.human_size(avail) if avail is not None else "невідомо скільки"
        if need is not None:
            detail = (
                f"Потрібно приблизно {c.human_size(need)}, а на "
                f"{partition} доступно лише {avail_s}."
            )
        else:
            # Never inventing a "required" figure pacman's own output
            # didn't actually support (see diagnosis.classify) - only
            # what we could reliably measure gets shown.
            detail = (
                f"На {partition} доступно лише {avail_s} вільного місця - "
                "цього недостатньо для оновлень."
            )
        detail += "\n\nЗвільніть трохи місця на диску і спробуйте ще раз."
        return ("💾", "Недостатньо місця на диску", detail)

    if t == diagnosis.MIRROR_TIMEOUT:
        return (
            "🌐", "Дзеркало не відповідає",
            "Обране дзеркало Arch не відповіло вчасно. Це не обов'язково "
            "означає проблему з вашою системою - спробуйте ще раз, "
            "можливо трохи пізніше."
        )

    if t == diagnosis.NETWORK_ERROR:
        return (
            "🌐", "Помилка мережі",
            "Не вдалося завантажити один або кілька пакетів. Дзеркало "
            "або ваше інтернет-з'єднання можуть бути тимчасово "
            "недоступні."
        )

    # UNKNOWN, or any type this GUI version doesn't recognise yet -
    # never pretend to know the cause when the classifier itself
    # didn't find one.
    return (
        "⚠️", "Не вдалося визначити причину",
        "Arch Auto Update не зміг розпізнати точну причину цієї "
        "помилки. Повний технічний вивід pacman збережено в логах "
        "нижче."
    )


def format_skipped_packages_notice(skipped):
    """One short, calm line for state["skipped_packages"] (see
    diagnosis.extract_local_newer_notices) plus a hover tooltip listing
    every package - point #6 of the design this implements: a locally
    newer package is never shown as if it were a failure, just a
    quiet, dismissible fact. Returns (text, tooltip) or None if
    `skipped` is empty."""
    if not skipped:
        return None
    n = len(skipped)
    word = "пакет" if n % 10 == 1 and n % 100 != 11 else (
        "пакети" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "пакетів"
    )
    text = (
        f"ℹ️ {n} {word} пропущено - локальна версія новіша за "
        "репозиторій, оновлення не виконувалось"
    )
    tooltip_lines = [
        f"{p['package']}: локально {p['local_version']}, "
        f"у {p['repo']} лише {p['repo_version']}"
        for p in skipped
    ]
    return text, "\n".join(tooltip_lines)


def make_status_pixmap(color_hex, glyph, size=64):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    color = QColor(color_hex)
    bg = QColor(color)
    bg.setAlpha(38)
    p.setBrush(bg)
    p.setPen(Qt.NoPen)
    p.drawEllipse(0, 0, size, size)

    pen = QPen(color)
    pen.setWidthF(size * 0.07)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)

    if glyph == "check":
        p.drawPolyline([
            QPointF(size * 0.28, size * 0.52),
            QPointF(size * 0.44, size * 0.68),
            QPointF(size * 0.74, size * 0.32),
        ])
    elif glyph == "x":
        p.drawLine(QPointF(size * 0.30, size * 0.30), QPointF(size * 0.70, size * 0.70))
        p.drawLine(QPointF(size * 0.70, size * 0.30), QPointF(size * 0.30, size * 0.70))
    elif glyph == "arrow":
        p.drawLine(QPointF(size * 0.5, size * 0.26), QPointF(size * 0.5, size * 0.60))
        p.drawPolyline([
            QPointF(size * 0.35, size * 0.46),
            QPointF(size * 0.5, size * 0.64),
            QPointF(size * 0.65, size * 0.46),
        ])
    else:  # dots
        p.setBrush(color)
        p.setPen(Qt.NoPen)
        for dx in (0.32, 0.5, 0.68):
            p.drawEllipse(QPointF(size * dx, size * 0.5), size * 0.045, size * 0.045)
    p.end()
    return pm


def classify_log_line(line):
    low = line.lower()
    if "error" in low or "помилк" in low or "не вдалося" in low:
        return "error"
    if "warning" in low or "попередж" in low:
        return "warn"
    if " ... ok" in low or "успішно" in low or "готово" in low or "встановлено" in low:
        return "ok"
    if "::" in line or "розпочат" in low or "дзеркал" in low:
        return "meta"
    return "info"


def format_packages_html(packages):
    """Renders state["packages"] (see common.pending_upgrades) as
    compact HTML for the read-only packages view: one line per
    package, "name old → new", name bold in the same light colour used
    for the stat values elsewhere in this card, version in the same
    muted colour used for their labels. Pkgrel ("-1", "-2", ...) is
    dropped for this compact display only - state["packages"] itself
    still holds the exact string pacman reported, this is purely a
    display trim, same as human_size() trims bytes for the size stat.
    Left to Qt's own word wrap (the view's default) instead of a fixed
    width, so a long name/version still fits a narrow window instead
    of forcing a horizontal scrollbar."""
    lines = []
    for pkg in packages:
        name = html.escape(str(pkg.get("name") or ""))
        old, new = pkg.get("old_version"), pkg.get("new_version")
        if old and new:
            old_v = old.rsplit("-", 1)[0] if "-" in old else old
            new_v = new.rsplit("-", 1)[0] if "-" in new else new
            if old_v == new_v:
                # Only the pkgrel changed (a rebuild, no pkgver bump) -
                # stripping it here would make a real update look like
                # nothing changed ("3.0 → 3.0"), so fall back to the
                # full version strings for this one instead.
                old_v, new_v = old, new
            old_v, new_v = html.escape(old_v), html.escape(new_v)
            lines.append(
                f'<span style="color:#eef1f6;font-weight:600;">{name}</span> '
                f'<span style="color:#8b93a7;">{old_v} → {new_v}</span>'
            )
        else:
            lines.append(f'<span style="color:#eef1f6;font-weight:600;">{name}</span>')
    return "<br>".join(lines)


def make_tray_icon(phase, size=32):
    """App icon with a small static colour badge reflecting the current
    phase. No animation - just swapped whenever the phase changes."""
    color_hex = PHASE_INFO.get(phase, PHASE_INFO["idle"])[0]
    base = QIcon(str(ICON_PATH)).pixmap(size, size)
    pm = QPixmap(base.size())
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, base)
    badge_d = size * 0.34
    x = size - badge_d - 1
    y = size - badge_d - 1
    p.setBrush(QColor(color_hex))
    p.setPen(QPen(QColor("#0b0f19"), size * 0.05))
    p.drawEllipse(QPointF(x + badge_d / 2, y + badge_d / 2), badge_d / 2, badge_d / 2)
    p.end()
    return QIcon(pm)


def start_privileged(script_name, args=None, finished_callback=None, parent=None):
    """Runs one of our root scripts via pkexec (graphical Polkit prompt).
    Returns the QProcess - the caller MUST keep a reference to it until
    it finishes, or it will be garbage collected mid-run."""
    c.ensure_polkit_agent()
    script_path = str(SCRIPTS_DIR / script_name)
    proc = QProcess(parent)
    proc.setProgram("pkexec")
    proc.setArguments([script_path] + [str(a) for a in (args or [])])
    # Make sure pkexec never sees a real terminal on stdin, so it can only
    # ever use the graphical agent, never fall back to a text prompt.
    proc.setStandardInputFile(QProcess.nullDevice())

    def _finished(code, _status):
        err = bytes(proc.readAllStandardError()).decode("utf-8", "replace").strip()
        if finished_callback:
            finished_callback(code, err)

    proc.finished.connect(_finished)
    proc.start()
    return proc


class _InstallPromptGuard:
    """Shared (module-level, not per-window) state for the weekly
    "Install updates now?" dialog. A single process can have more
    than one poller capable of noticing state["install_prompt_pending"]
    at once - e.g. the tray's own 5s refresh() and a MainWindow it
    opened on demand (TrayApp.show_window), which keeps polling every
    1.5s in the background even after the window is hidden again -
    so the guard has to be shared, not owned by whichever window
    happens to notice first.

    `open` - True only for the instant a QMessageBox.exec() for this
    dialog is actually on screen. Needed because QTimer callbacks
    still fire during Qt's nested modal event loop, so without this a
    window's own poll_timer could re-enter this same check while its
    own dialog is still up and open a second one on top of itself.

    `answered` - True from the moment the person clicks Так/Ні until
    state.json actually confirms it landed (install_prompt_pending
    back to False, or phase no longer "ready"). Needed because the
    privileged script that clears the flag runs asynchronously
    (pkexec -> systemd), so there's a real window - a poll tick or
    two - where the flag on disk still reads True right after the
    person has already answered; without this, that next poll would
    pop the exact same dialog again for an answer already given.
    """
    open = False
    answered = False


_install_prompt_guard = _InstallPromptGuard()


def maybe_show_weekly_install_prompt(state, run_privileged, parent=None):
    """Shows the fixed "Встановити оновлення зараз?" (Так/Ні) dialog
    the weekly timer schedules (see install_prompt.py) - this never
    installs anything by itself, and never prompts unless downloaded
    updates are actually sitting ready. `run_privileged` is whichever
    caller-specific helper actually starts the pkexec'd script and
    tracks the resulting QProcess (MainWindow._run_privileged /
    TrayApp._run_privileged) - passed in as a plain callback so this
    one function serves both the main window and the tray icon, which
    otherwise don't share a base class.
    """
    if not state.get("install_prompt_pending") or state.get("phase") != "ready":
        # Nothing pending, or the earlier answer has now actually
        # landed in state.json (phase moved on, or the flag itself
        # cleared) - either way, ready for next week's prompt again.
        _install_prompt_guard.answered = False
        return
    if _install_prompt_guard.open or _install_prompt_guard.answered:
        return

    _install_prompt_guard.open = True
    try:
        box = QMessageBox(parent)
        box.setWindowTitle("Щотижневе встановлення")
        box.setIcon(QMessageBox.Question)
        box.setText("Встановити оновлення зараз?")
        yes_btn = box.addButton("Так", QMessageBox.AcceptRole)
        box.addButton("Ні", QMessageBox.RejectRole)
        box.setDefaultButton(yes_btn)
        box.exec()
        _install_prompt_guard.answered = True
        if box.clickedButton() is yes_btn:
            run_privileged("run_install_service.sh")
        else:
            run_privileged("decline_install_prompt.py")
    finally:
        _install_prompt_guard.open = False


class FlowRow(QWidget):
    """A left-to-right row of widgets/layouts that automatically
    switches to a top-to-bottom stack once its own available width
    drops below `breakpoint`. Above the breakpoint it behaves exactly
    like a plain QHBoxLayout (preserving the original horizontal
    design); below it, every item gets the full row width stacked
    vertically instead of being squeezed/clipped/overlapped - used so
    the app stays usable in narrow tiled WM layouts.

    Items added with a non-zero `stretch` expand to soak up spare
    horizontal space (pushing later items to the right) exactly like a
    QHBoxLayout stretch factor - but the stretch is switched off while
    stacked, since it would otherwise turn into pointless vertical
    padding between rows.
    """

    def __init__(self, breakpoint, spacing=16, parent=None):
        super().__init__(parent)
        self._breakpoint = breakpoint
        self._vertical = None
        self._stretches = []
        self.box = QBoxLayout(QBoxLayout.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(spacing)
        # Establish a sane default (horizontal) look before the first
        # resizeEvent fires.
        self._apply_direction(vertical=False)

    def add_widget(self, widget, stretch=0):
        self.box.addWidget(widget, stretch)
        self._stretches.append(stretch)

    def add_layout(self, layout, stretch=0):
        self.box.addLayout(layout, stretch)
        self._stretches.append(stretch)

    def add_stretch(self, stretch=1):
        self.box.addStretch(stretch)
        self._stretches.append(stretch)

    def _apply_direction(self, vertical):
        self._vertical = vertical
        self.box.setDirection(QBoxLayout.TopToBottom if vertical else QBoxLayout.LeftToRight)
        for i, s in enumerate(self._stretches):
            self.box.setStretch(i, 0 if vertical else s)
        # Flipping direction changes this widget's preferred height (a
        # stack needs more height than a row). Without explicitly
        # invalidating + notifying the parent, Qt keeps using the old,
        # now-stale size hint and the parent layout won't grant the
        # extra height - the stacked content then overflows past this
        # widget's boundary into whatever comes after it.
        self.box.invalidate()
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        vertical = self.width() < self._breakpoint
        if vertical != self._vertical:
            self._apply_direction(vertical)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("Arch Auto Update")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1000, 740)
        # Kept low so the window can live in small tiled WM areas
        # (as narrow as ~380x440) without Qt refusing to shrink it;
        # every section below reflows responsively at that width.
        self.setMinimumSize(380, 440)

        self._log_offset = 0
        self._pending_process = None
        # Only re-set when the rendered content actually changes (see
        # _apply_state) - state.json is re-read every 1.5s, but calling
        # setHtml() unconditionally would reset the view's scroll
        # position that often, which would make scrolling through a
        # longer update list unusable.
        self._last_packages_html = None

        self._build_ui()
        self._init_timer_controls()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh)
        self.poll_timer.start(1500)
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _card(self):
        card = QFrame()
        card.setProperty("cls", "card")
        return card

    def _label(self, text, cls=None, object_name=None):
        lbl = QLabel(text)
        # Wrapping is the safe default everywhere: it costs nothing when
        # there's room (single line, unchanged look) but keeps a long
        # label - like the timer card's section title - from silently
        # forcing the card wider than a narrow tiled window can offer.
        lbl.setWordWrap(True)
        if cls:
            lbl.setProperty("cls", cls)
        if object_name:
            lbl.setObjectName(object_name)
        return lbl

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 20)
        outer.setSpacing(16)

        header = QHBoxLayout()
        icon_lbl = QLabel()
        icon_lbl.setPixmap(QIcon(str(ICON_PATH)).pixmap(28, 28))
        header.addWidget(icon_lbl)
        header.addSpacing(8)
        header.addWidget(self._label("Arch Auto Update", object_name="headerTitle"))
        header.addStretch(1)
        outer.addLayout(header)

        # The three cards below can need more total height than a very
        # short tiled window has to offer (e.g. ~500px tall). Rather
        # than force everything to overlap/clip to fit, let the body
        # scroll vertically - only the vertical scrollbar is ever
        # enabled, so this never introduces the horizontal scrolling
        # the layout is required to avoid. When there's enough height
        # (the normal/maximized case) the scrollbar simply never
        # appears and this looks identical to a plain layout.
        scroll = QScrollArea()
        scroll.setObjectName("bodyScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._body_scroll = scroll

        body = QWidget()
        body.setObjectName("scrollBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(16)
        body_layout.addWidget(self._build_status_card())
        body_layout.addWidget(self._build_timer_card())
        body_layout.addWidget(self._build_logs_card(), 1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    def _build_status_card(self):
        card = self._card()
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(14)

        v.addWidget(self._label("Статус оновлення", cls="sectionTitle"))

        # icon + title/subtitle, kept together as one block so the icon
        # always stays right next to its text regardless of row mode
        header = QWidget()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(16)

        self.status_icon = QLabel()
        self.status_icon.setFixedSize(64, 64)
        header_row.addWidget(self.status_icon)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self.status_title = self._label("", object_name="statusTitle")
        self.status_subtitle = self._label("", object_name="statusSubtitle")
        title_col.addWidget(self.status_title)
        title_col.addWidget(self.status_subtitle)
        title_col.addStretch(1)
        header_row.addLayout(title_col, 1)

        stats_grid = QGridLayout()
        stats_grid.setHorizontalSpacing(14)
        stats_grid.setVerticalSpacing(4)
        rows = [
            ("Кількість пакетів:", "package_count"),
            ("Розмір завантаження:", "total_size"),
            ("Завантажено:", "downloaded"),
            ("Остання перевірка:", "last_check"),
        ]
        self.stat_labels = {}
        for i, (key_text, key) in enumerate(rows):
            k = self._label(key_text, cls="statKey")
            val = self._label("—", cls="statVal")
            stats_grid.addWidget(k, i, 0)
            stats_grid.addWidget(val, i, 1)
            self.stat_labels[key] = val

        buttons_col = QVBoxLayout()
        buttons_col.setSpacing(8)
        self.retry_btn = QPushButton("🔄  Повторити спробу")
        self.retry_btn.setObjectName("retryBtn")
        self.retry_btn.clicked.connect(self.on_retry_clicked)
        self.install_btn = QPushButton("⬇  Встановити оновлення")
        self.install_btn.setObjectName("installBtn")
        self.install_btn.clicked.connect(self.on_install_clicked)
        self.delete_btn = QPushButton("🗑  Видалити завантажене")
        self.delete_btn.setObjectName("deleteBtn")
        self.delete_btn.clicked.connect(self.on_delete_clicked)
        buttons_col.addWidget(self.retry_btn)
        buttons_col.addWidget(self.install_btn)
        buttons_col.addWidget(self.delete_btn)

        # Side-by-side (icon/title | stats | buttons) on wide windows,
        # matching the original design; stacked top-to-bottom once the
        # card gets too narrow to fit all three comfortably, so buttons
        # always stay fully visible instead of being clipped/overlapped.
        top_row = FlowRow(breakpoint=900, spacing=20)
        top_row.add_widget(header, stretch=1)
        top_row.add_layout(stats_grid, stretch=0)
        top_row.add_layout(buttons_col, stretch=0)
        v.addWidget(top_row)

        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        self.progress_pct = self._label("0%", object_name="progressPct")
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.progress_pct)
        v.addLayout(progress_row)

        self.detail_label = self._label("", cls="muted")
        v.addWidget(self.detail_label)

        # Only shown for a classified error whose technical_lines are
        # worth jumping to - see _apply_state. Doesn't open a new
        # dialog/panel (that's more machinery than this needs): the
        # full technical output is already sitting in the logs card
        # below, this just scrolls it into view.
        self.details_btn = QPushButton("Показати технічні деталі в логах ↓")
        self.details_btn.setProperty("cls", "smallBtn")
        self.details_btn.clicked.connect(self.on_show_details_clicked)
        self.details_btn.setVisible(False)
        v.addWidget(self.details_btn)

        # "N packages skipped, local newer than repo" - informational,
        # shown regardless of phase (a fully successful run can still
        # have skipped a few packages this way), never styled as an
        # error. Hovering shows exactly which packages/versions.
        self.skipped_label = self._label("", cls="muted")
        self.skipped_label.setVisible(False)
        v.addWidget(self.skipped_label)

        self.packages_heading = self._label("", cls="muted")
        self.packages_heading.setVisible(False)
        v.addWidget(self.packages_heading)

        self.packages_view = QTextEdit()
        self.packages_view.setObjectName("packagesView")
        self.packages_view.setReadOnly(True)
        # Default word wrap (unlike log_view's NoWrap below) is exactly
        # what keeps a long name/version fitting a narrow tiled window
        # instead of needing a horizontal scrollbar; the fixed max
        # height plus its own vertical scrollbar is what keeps this
        # card compact for a long update list instead of growing the
        # whole window to fit it.
        self.packages_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.packages_view.setMaximumHeight(140)
        self.packages_view.setVisible(False)
        v.addWidget(self.packages_view)

        return card

    def _build_timer_card(self):
        card = self._card()
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)

        v.addWidget(self._label("Таймер (щотижневе встановлення оновлень)", cls="sectionTitle"))

        # Toggle / day / time / next-run side-by-side on wide windows;
        # stacked vertically once narrow, so none of these controls
        # (none of which can shrink below their content) ever overlap.
        row = FlowRow(breakpoint=600, spacing=24)

        self.timer_toggle = QCheckBox("Увімкнено")
        self.timer_toggle.setProperty("cls", "toggle")
        row.add_widget(self.timer_toggle)

        day_col = QVBoxLayout()
        day_col.setSpacing(4)
        day_col.addWidget(self._label("День тижня:", cls="muted"))
        self.day_combo = QComboBox()
        # Size to whichever day name is actually selected instead of a
        # fixed min-width, so it's never wider than it needs to be.
        self.day_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        for day in c.SYSTEMD_DAYS:
            self.day_combo.addItem(c.DAY_LABELS_UK[day], day)
        day_col.addWidget(self.day_combo)
        row.add_layout(day_col)

        time_col = QVBoxLayout()
        time_col.setSpacing(4)
        time_col.addWidget(self._label("Час:", cls="muted"))
        time_row = QHBoxLayout()
        time_row.setSpacing(4)
        self.hour_combo = QComboBox()
        self.hour_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        for h in range(24):
            self.hour_combo.addItem(f"{h:02d}", h)
        self.minute_combo = QComboBox()
        self.minute_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        for m in range(0, 60, 5):
            self.minute_combo.addItem(f"{m:02d}", m)
        time_row.addWidget(self.hour_combo)
        time_row.addWidget(self._label(":"))
        time_row.addWidget(self.minute_combo)
        time_col.addLayout(time_row)
        row.add_layout(time_col)

        row.add_stretch(1)

        next_col = QVBoxLayout()
        next_col.setSpacing(4)
        next_col.addWidget(self._label("Наступний запуск:", cls="muted"))
        self.next_run_value = self._label("—", object_name="nextRunValue")
        next_col.addWidget(self.next_run_value)
        row.add_layout(next_col)

        v.addWidget(row)
        return card

    def _build_logs_card(self):
        card = self._card()
        v = QVBoxLayout(card)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)

        # Title left, Clear/Export buttons right on wide windows; title
        # above the buttons once too narrow for both on one line.
        header = FlowRow(breakpoint=360, spacing=10)
        header.add_widget(self._label("Логи", cls="sectionTitle"), stretch=1)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(8)
        clear_btn = QPushButton("🗑  Очистити логи")
        clear_btn.setProperty("cls", "smallBtn")
        clear_btn.clicked.connect(self.on_clear_logs_clicked)
        export_btn = QPushButton("⬇  Експорт")
        export_btn.setProperty("cls", "smallBtn")
        export_btn.clicked.connect(self.on_export_clicked)
        buttons_row.addWidget(clear_btn)
        buttons_row.addWidget(export_btn)
        header.add_layout(buttons_row, stretch=0)

        v.addWidget(header)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.NoWrap)
        self.log_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        v.addWidget(self.log_view, 1)

        return card

    # ------------------------------------------------------------------
    # Timer controls: read current systemd state, then wire up signals
    # ------------------------------------------------------------------
    def _init_timer_controls(self):
        # Coalesces a burst of edits (day, then hour, then minute, ...)
        # into a single save fired shortly after the person stops
        # touching the controls, instead of one privileged save per
        # individual control change. That used to mean a quick run of
        # edits could race: change #2's save could still be "pending"
        # (see _run_privileged's busy-guard) when change #3 fired, which
        # then silently dropped #3 - so whichever combination of values
        # happened to land last, not necessarily what the person actually
        # picked, was what got saved. Also used as the "an edit is in
        # flight" signal for _maybe_resync_timer_controls below.
        self._timer_save_debounce = QTimer(self)
        self._timer_save_debounce.setSingleShot(True)
        self._timer_save_debounce.setInterval(600)
        self._timer_save_debounce.timeout.connect(self._commit_timer_settings)

        enabled, day, hour, minute = self._read_systemd_timer_config()
        self._apply_timer_values(enabled, day, hour, minute)

        self.timer_toggle.toggled.connect(self._on_timer_control_changed)
        self.day_combo.currentIndexChanged.connect(self._on_timer_control_changed)
        self.hour_combo.currentIndexChanged.connect(self._on_timer_control_changed)
        self.minute_combo.currentIndexChanged.connect(self._on_timer_control_changed)

    def _apply_timer_values(self, enabled, day, hour, minute):
        """Sets the four timer controls to match (enabled, day, hour,
        minute) without triggering a save. Used both for the initial
        read at startup and to re-sync from the actual saved config
        afterwards (_maybe_resync_timer_controls)."""
        for w in (self.timer_toggle, self.day_combo, self.hour_combo, self.minute_combo):
            w.blockSignals(True)

        self.timer_toggle.setChecked(enabled)
        idx = self.day_combo.findData(day)
        if idx >= 0:
            self.day_combo.setCurrentIndex(idx)
        idx = self.hour_combo.findData(hour)
        if idx >= 0:
            self.hour_combo.setCurrentIndex(idx)
        # snap minute to nearest 5-minute step present in the combo
        nearest_minute = min((m for m in range(0, 60, 5)), key=lambda m: abs(m - minute))
        idx = self.minute_combo.findData(nearest_minute)
        if idx >= 0:
            self.minute_combo.setCurrentIndex(idx)

        for w in (self.timer_toggle, self.day_combo, self.hour_combo, self.minute_combo):
            w.blockSignals(False)

    def _current_timer_values(self):
        return (
            self.timer_toggle.isChecked(),
            self.day_combo.currentData(),
            self.hour_combo.currentData(),
            self.minute_combo.currentData(),
        )

    def _read_systemd_timer_config(self):
        enabled, day, hour, minute = True, "Sun", 12, 0
        try:
            res = subprocess.run(["systemctl", "is-enabled", "arch-auto-update.timer"],
                                  capture_output=True, text=True)
            enabled = res.stdout.strip() == "enabled"
        except Exception:
            pass
        try:
            text = c.TIMER_UNIT_PATH.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("OnCalendar="):
                    parts = line.split("=", 1)[1].split()
                    if len(parts) >= 3:
                        day = parts[0]
                        hh, mm, *_ = parts[2].split(":")
                        hour, minute = int(hh), int(mm)
        except OSError:
            pass
        return enabled, day, hour, minute

    def _next_run_text(self):
        try:
            res = subprocess.run(
                ["systemctl", "show", "arch-auto-update.timer", "-p", "NextElapseUSecRealtime", "--value"],
                capture_output=True, text=True,
            )
            val = res.stdout.strip()
            if val and val != "n/a":
                return val
        except Exception:
            pass
        return "Вимкнено" if not self.timer_toggle.isChecked() else "—"

    # ------------------------------------------------------------------
    # Refresh loop (reads state + logs written by root-level scripts)
    # ------------------------------------------------------------------
    def refresh(self):
        state = c.read_state()
        self._apply_state(state)
        self._append_new_log_lines()
        self.next_run_value.setText(self._next_run_text())
        self._maybe_resync_timer_controls()
        maybe_show_weekly_install_prompt(state, self._run_privileged, parent=self)

    def _maybe_resync_timer_controls(self):
        # More than one window can have these controls open at once -
        # e.g. the tray runs its own hidden MainWindow while a person
        # can also separately launch the plain "Arch Auto Update" entry
        # from their application menu, giving two independent processes
        # that each only read the saved timer config once, at their own
        # startup. Without re-syncing, whichever one they touch second
        # would silently overwrite the other's still-fresh change with
        # its own stale snapshot - indistinguishable, from the person's
        # side, from "the timer changed on its own". Skipped while a
        # local edit is in flight (debounce pending, or a save is
        # currently being applied) so we never yank a control out from
        # under someone mid-edit.
        if self._timer_save_debounce.isActive() or self._pending_process is not None:
            return
        enabled, day, hour, minute = self._read_systemd_timer_config()
        nearest_minute = min((m for m in range(0, 60, 5)), key=lambda m: abs(m - minute))
        if (enabled, day, hour, nearest_minute) == self._current_timer_values():
            return
        self._apply_timer_values(enabled, day, hour, minute)

    def _apply_state(self, state):
        phase = state.get("phase", "idle")
        color, title, subtitle, glyph = PHASE_INFO.get(phase, PHASE_INFO["idle"])

        self.status_icon.setPixmap(make_status_pixmap(color, glyph))
        self.status_title.setText(title)
        self.status_title.setStyleSheet(f"color: {color};")

        explained = explain_error(state.get("error")) if phase == "error" else None
        if explained:
            _icon, err_title, err_detail = explained
            self.status_subtitle.setText(f"{_icon} {err_title}")
        elif phase == "error" and state.get("error_message"):
            first_line = state["error_message"].strip().splitlines()[0][:120]
            self.status_subtitle.setText(first_line or subtitle)
        else:
            self.status_subtitle.setText(subtitle)

        self.stat_labels["package_count"].setText(str(state.get("package_count", 0)))
        self.stat_labels["total_size"].setText(c.human_size(state.get("total_size_bytes", 0)))
        self.stat_labels["downloaded"].setText(c.human_size(state.get("downloaded_bytes", 0)))
        self.stat_labels["last_check"].setText(state.get("last_check") or "—")

        pct = int(state.get("progress_percent", 0) or 0)
        self.progress_bar.setValue(pct)
        self.progress_pct.setText(f"{pct}%")

        if phase in ("ready",):
            self.detail_label.setText("Всі пакети успішно завантажено")
        elif phase == "downloading":
            self.detail_label.setText(f"Завантажено {c.human_size(state.get('downloaded_bytes', 0))} з {c.human_size(state.get('total_size_bytes', 0))}")
        elif phase == "error":
            if explained:
                self.detail_label.setText(explained[2])
            else:
                self.detail_label.setText("Перевірте розділ логів нижче для деталей")
        else:
            self.detail_label.setText("")

        # Only worth a "show details" jump when there's technical
        # output attached to look at - a classified error always has
        # some, but the plain error_message path (e.g. an install
        # failure not covered by the classifier yet) has this same
        # information sitting in the log regardless, without a
        # dedicated pointer to it.
        has_technical_detail = bool((state.get("error") or {}).get("technical_lines"))
        self.details_btn.setVisible(phase == "error" and has_technical_detail)

        notice = format_skipped_packages_notice(state.get("skipped_packages"))
        if notice:
            text, tooltip = notice
            self.skipped_label.setText(text)
            self.skipped_label.setToolTip(tooltip)
            self.skipped_label.setVisible(True)
        else:
            self.skipped_label.setVisible(False)

        packages = state.get("packages") or []
        if packages:
            self.packages_heading.setText(
                "Успішно оновлено:" if phase == "installed" else "Пакети до оновлення:"
            )
            self.packages_heading.setVisible(True)
            packages_html = format_packages_html(packages)
            if packages_html != self._last_packages_html:
                self.packages_view.setHtml(packages_html)
                self._last_packages_html = packages_html
            self.packages_view.setVisible(True)
        else:
            self.packages_heading.setVisible(False)
            self.packages_view.setVisible(False)
            self._last_packages_html = None

        busy = self._pending_process is not None
        self.retry_btn.setVisible(phase == "error")
        self.retry_btn.setEnabled(not busy)
        self.install_btn.setVisible(phase == "ready")
        self.install_btn.setEnabled(not busy)
        self.delete_btn.setVisible(state.get("downloaded_bytes", 0) > 0 and phase not in ("installing",))
        self.delete_btn.setEnabled(not busy)

    def _append_new_log_lines(self):
        try:
            size = c.LOG_FILE.stat().st_size
        except OSError:
            size = 0

        if size < self._log_offset:
            # log was cleared or rotated externally
            self.log_view.clear()
            self._log_offset = 0

        if size == self._log_offset:
            return

        try:
            with open(c.LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._log_offset)
                new_text = f.read()
                self._log_offset = f.tell()
        except OSError:
            return

        if not new_text:
            return

        sb = self.log_view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4

        html_parts = []
        for line in new_text.splitlines():
            if not line:
                continue
            level = classify_log_line(line)
            color = LOG_LEVEL_COLORS.get(level, "#c7cede")
            safe = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            html_parts.append(f'<div style="color:{color}; white-space:pre;">{safe}</div>')

        if html_parts:
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.End)
            # insertHtml() merges the very first <div> of a fragment into
            # whatever block the cursor is already sitting in rather than
            # starting a new one - harmless the first time (empty
            # document), but on every later refresh() call it silently
            # glued the new batch onto the end of the previous one with
            # no line break at all. Force a real block boundary first.
            if not self.log_view.document().isEmpty():
                cursor.insertBlock()
            cursor.insertHtml("".join(html_parts))
            self.log_view.setTextCursor(cursor)
        if at_bottom:
            sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _run_privileged(self, script_name, args=None, on_done=None):
        if self._pending_process is not None:
            return

        def finished(code, err):
            self._pending_process = None
            if code != 0 and err:
                QMessageBox.warning(self, "Arch Auto Update", err[:600])
            if on_done:
                on_done(code)
            self.refresh()

        self._pending_process = start_privileged(
            script_name, args=args, finished_callback=finished, parent=self,
        )
        self.refresh()

    def on_install_clicked(self):
        state = c.read_state()
        count = state.get("package_count", 0)
        size = c.human_size(state.get("total_size_bytes", 0))
        box = QMessageBox(self)
        box.setWindowTitle("Встановити оновлення")
        box.setIcon(QMessageBox.Question)
        box.setText(f"Встановити {count} оновлень загальним розміром {size}?")
        install_btn = box.addButton("Встановити оновлення", QMessageBox.AcceptRole)
        box.addButton("Скасувати", QMessageBox.RejectRole)
        box.setDefaultButton(install_btn)
        box.exec()
        if box.clickedButton() is install_btn:
            self._run_privileged("run_install_service.sh")

    def on_retry_clicked(self):
        # Which operation failed - "install" (install_updates.py) or
        # anything else, which covers both the check and download steps
        # of check_and_download.py, since a failure in either sets the
        # same "error" phase - is recorded by the backend scripts
        # themselves in state["failed_action"] (see common.default_state).
        # Reuses the exact same handlers the Install button and a check
        # already use - install retry goes through on_install_clicked so
        # it gets the same confirmation dialog, and both ultimately call
        # _run_privileged with the same script/backend logic the tray
        # uses, so GUI Retry and tray actions can't diverge.
        state = c.read_state()
        if c.is_install_retryable(state):
            self.on_install_clicked()
        else:
            # Goes through run_check_service.sh (starts the systemd
            # unit) rather than pkexec-ing check_and_download.py
            # directly - see that script for why. Same script, same
            # state.json/log either way.
            self._run_privileged("run_check_service.sh")

    def on_show_details_clicked(self):
        # No separate dialog/panel - the full technical pacman output
        # is already sitting in the logs card below (see
        # common.log_raw_output / log_failure); this just brings it
        # into view instead of making the person scroll to find it.
        self.log_view.moveCursor(QTextCursor.End)
        self._body_scroll.ensureWidgetVisible(self.log_view)

    def on_delete_clicked(self):
        box = QMessageBox(self)
        box.setWindowTitle("Видалити завантажене")
        box.setIcon(QMessageBox.Question)
        box.setText("Видалити вже завантажені файли пакетів оновлень?")
        delete_btn = box.addButton("Видалити", QMessageBox.AcceptRole)
        box.addButton("Скасувати", QMessageBox.RejectRole)
        box.setDefaultButton(box.buttons()[-1])
        box.exec()
        if box.clickedButton() is delete_btn:
            self._run_privileged("delete_downloaded.py")

    def on_clear_logs_clicked(self):
        def done(code):
            if code == 0:
                self.log_view.clear()
                self._log_offset = 0
        self._run_privileged("clear_logs.py", on_done=done)

    def on_export_clicked(self):
        default_name = f"arch-auto-update-log-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "Експорт логів", default_name, "Текстові файли (*.txt)")
        if not path:
            return
        try:
            data = c.LOG_FILE.read_bytes()
            Path(path).write_bytes(data)
        except OSError as e:
            QMessageBox.warning(self, "Arch Auto Update", f"Не вдалося зберегти файл: {e}")

    def _on_timer_control_changed(self, *_):
        # Just (re)start the debounce window here - the actual save
        # happens in _commit_timer_settings once the person has stopped
        # touching the controls for a moment. See _init_timer_controls
        # for why.
        self._timer_save_debounce.start()

    def _commit_timer_settings(self):
        enabled, day, hour, minute = self._current_timer_values()
        self._run_privileged(
            "set_timer.py",
            args=["1" if enabled else "0", day, hour, minute],
        )


class TrayApp:
    """Background tray icon (arch-auto-update --tray). Opens MainWindow
    on demand instead of embedding it - the window itself is unchanged."""

    def __init__(self, app):
        self.app = app
        self.window = None
        self._pending_process = None

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(make_tray_icon("idle"))
        self.tray.setToolTip("Arch Auto Update")
        self._last_icon_phase = None

        menu = QMenu()
        self.act_open = QAction("Відкрити", menu)
        self.act_open.triggered.connect(self.show_window)
        self.act_check = QAction("Перевірити оновлення", menu)
        self.act_check.triggered.connect(self.on_check_clicked)
        self.act_install = QAction("Встановити оновлення", menu)
        self.act_install.triggered.connect(self.on_install_clicked)
        self.act_quit = QAction("Вийти", menu)
        self.act_quit.triggered.connect(self.app.quit)

        menu.addAction(self.act_open)
        menu.addAction(self.act_check)
        menu.addAction(self.act_install)
        menu.addSeparator()
        menu.addAction(self.act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        c.ensure_polkit_agent()

        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self.refresh)
        self.poll_timer.start(5000)
        self.refresh()

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def _run_privileged(self, script_name, args=None):
        if self._pending_process is not None:
            return

        def finished(code, err):
            self._pending_process = None
            if code != 0 and err:
                QMessageBox.warning(None, "Arch Auto Update", err[:600])
            self.refresh()

        self._pending_process = start_privileged(script_name, args=args, finished_callback=finished)
        self.refresh()

    def show_window(self):
        if self.window is None:
            self.window = MainWindow()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def refresh(self):
        state = c.read_state()
        phase = state.get("phase", "idle")
        if phase != self._last_icon_phase:
            self.tray.setIcon(make_tray_icon(phase))
            self._last_icon_phase = phase
        _color, title, _subtitle, _glyph = PHASE_INFO.get(phase, PHASE_INFO["idle"])
        self.tray.setToolTip(f"Arch Auto Update — {title}")
        retry_install = c.is_install_retryable(state)
        self.act_install.setVisible(phase == "ready" or retry_install)
        self.act_install.setText(
            "Повторити встановлення оновлень" if retry_install else "Встановити оновлення"
        )
        busy = self._pending_process is not None
        self.act_check.setEnabled(not busy)
        self.act_install.setEnabled(not busy)
        maybe_show_weekly_install_prompt(state, self._run_privileged, parent=None)

    def on_check_clicked(self):
        # See run_check_service.sh: goes through the systemd unit
        # instead of pkexec running check_and_download.py directly.
        self._run_privileged("run_check_service.sh")

    def on_install_clicked(self):
        # Same install trigger + confirmation flow whether this is a
        # fresh install (phase "ready") or a retry of a failed one (see
        # common.is_install_retryable) - install_updates.py itself (and
        # its own startup guard) is what actually decides what to
        # install either way, so nothing about the retry case is
        # re-implemented here.
        if self._pending_process is not None:
            return
        state = c.read_state()
        if state.get("phase") != "ready" and not c.is_install_retryable(state):
            return
        count = state.get("package_count", 0)
        size = c.human_size(state.get("total_size_bytes", 0))
        box = QMessageBox()
        box.setWindowTitle("Встановити оновлення")
        box.setIcon(QMessageBox.Question)
        box.setText(f"Встановити {count} оновлень загальним розміром {size}?")
        install_btn = box.addButton("Встановити оновлення", QMessageBox.AcceptRole)
        box.addButton("Скасувати", QMessageBox.RejectRole)
        box.setDefaultButton(install_btn)
        box.exec()
        if box.clickedButton() is not install_btn:
            return

        # See run_install_service.sh: goes through the systemd unit
        # instead of pkexec running install_updates.py directly.
        self._run_privileged("run_install_service.sh")


def main():
    parser = argparse.ArgumentParser(prog="arch-auto-update")
    parser.add_argument(
        "--tray", action="store_true",
        help="Запустити у фоновому режимі (трей), без відкриття вікна",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Arch Auto Update")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    try:
        app.setStyleSheet(STYLE_PATH.read_text(encoding="utf-8"))
    except OSError:
        pass

    c.ensure_polkit_agent()

    if args.tray:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("Системний трей недоступний у цьому середовищі.", file=sys.stderr)
            sys.exit(1)
        app.setQuitOnLastWindowClosed(False)
        tray_app = TrayApp(app)  # noqa: F841 - keep alive for the app's lifetime
        sys.exit(app.exec())

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
