"""Memory Calendar - a desktop photo/calendar manager."""
import sys, os, re, io, json, datetime, shutil, hashlib, logging, platform
import subprocess, time, copy, contextlib, stat, tempfile, threading, functools
from pathlib import Path
from logging.handlers import RotatingFileHandler

import cv2
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_SUPPORT = True
except ImportError:
    HEIF_SUPPORT = False

# xxhash removed 

# unused imports removed 
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QGraphicsView, QGraphicsScene, QGraphicsTextItem, QGraphicsItem,
    QListWidget, QListWidgetItem, QFileDialog, QMessageBox, QLabel,
    QAbstractItemView, QAbstractSpinBox, QProgressBar, QStackedWidget, QMenu,
    QTextBrowser, QTextEdit, QPlainTextEdit,
    QDialog, QComboBox, QInputDialog, QFrame,
    QTabWidget, QCheckBox, QSpinBox, QLineEdit, QDialogButtonBox,
    QKeySequenceEdit,
    QScrollArea, QGridLayout,
    QRubberBand,
)
from PySide6.QtCore import (
    Qt, QThread, Signal, QMimeData, QUrl, QSize, QThreadPool, QRunnable, Slot,
    QObject, QPoint, QPointF, QRectF, QTimer, QRect, QEventLoop,
)
from PySide6.QtGui import (
    QImageReader, QDrag, QPainter, QColor, QPixmap, QWheelEvent,
    QDesktopServices, QImage, QIcon, QShortcut, QKeySequence, QFont, QPen,
    QMouseEvent, QPainterPath, QPolygonF, QAction,
)


# ============================================================
#                       UTILITIES
# ============================================================
# suppress_stderr is called from concurrent video workers (up to 4
# simultaneously — _VIDEO_SEMAPHORE(4) does not serialize stderr dance).
# attempted "lock only the dup2 dance, not the yield" but implemented
# per-worker save/restore, which is broken under concurrency:
#   A: orig_A = dup(2) [real]; dup2(devnull, 2)
#   B: orig_B = dup(2) [devnull!]; dup2(devnull, 2)  ← dup of devnull
#   A exits: dup2(orig_A, 2) → stderr=real
#   B exits: dup2(orig_B, 2) → stderr=devnull FOREVER
# Effect: logging.StreamHandler dies for the rest of the session. Only
# visible when running from source (GUI builds with --noconsole have
# sys.stderr=None, early-return). Reproducible on first video-gallery
# scroll. Fix: refcount — first entrant saves the real fd, last exiter
# restores it. Lock still held only during fd dance, not across yield.
_STDERR_LOCK = threading.Lock()
_STDERR_COUNT = 0
_STDERR_ORIGINAL = None


@contextlib.contextmanager
def suppress_stderr():
    global _STDERR_COUNT, _STDERR_ORIGINAL
    # guard sys.stderr None (--noconsole) and non-fileno stderr.
    if sys.stderr is None:
        yield
        return
    try:
        stderr_fileno = sys.stderr.fileno()
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        yield
        return
    # refcounted suppression. Only the first entrant saves the real
    # fd and opens devnull; only the last exiter restores it. This is
    # what 's comment promised but did not implement.
    suppressed_ok = False
    with _STDERR_LOCK:
        if _STDERR_COUNT == 0:
            try:
                _STDERR_ORIGINAL = os.dup(stderr_fileno)
                devnull = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(devnull, stderr_fileno)
                finally:
                    try:
                        os.close(devnull)
                    except OSError:
                        pass
                suppressed_ok = True
            except OSError:
                # Roll back: close any partially-created fds.
                if _STDERR_ORIGINAL is not None:
                    try:
                        os.close(_STDERR_ORIGINAL)
                    except OSError:
                        pass
                    _STDERR_ORIGINAL = None
                suppressed_ok = False
        else:
            # Already suppressed by an earlier worker — just bump refcount.
            suppressed_ok = True
        if suppressed_ok:
            _STDERR_COUNT += 1
    if not suppressed_ok:
        yield
        return
    try:
        yield
    finally:
        with _STDERR_LOCK:
            _STDERR_COUNT -= 1
            if _STDERR_COUNT == 0 and _STDERR_ORIGINAL is not None:
                try:
                    os.dup2(_STDERR_ORIGINAL, stderr_fileno)
                except OSError:
                    pass
                try:
                    os.close(_STDERR_ORIGINAL)
                except OSError:
                    pass
                _STDERR_ORIGINAL = None


def _get_data_dir() -> Path:
    try:
        home = Path.home()
        system = platform.system()
        if system == "Windows":
            base = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
        elif system == "Darwin":
            base = home / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
        d = base / "MemoryCalendar"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        d = Path(tempfile.gettempdir()) / "MemoryCalendar"
        d.mkdir(parents=True, exist_ok=True)
        return d


DATA_DIR = _get_data_dir()
CONFIG_FILE = DATA_DIR / "config.json"
DEFAULT_CACHE_DIR = DATA_DIR / "thumb_cache"
TRASH_DIR = DATA_DIR / "trash"
TRASH_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR = DATA_DIR / "calendar_backups"  # 
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_KEEP = 20  # retention
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_log_file = LOG_DIR / "app.log"
_fh = RotatingFileHandler(_log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
# stderr guard — PyInstaller --noconsole has None stderr
_log_handlers = [_fh]
if sys.stderr is not None:
    _log_handlers.insert(0, logging.StreamHandler())
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=_log_handlers)
logger = logging.getLogger("memory_calendar")


def set_trash_dir(new_path):
    """switch the trash folder at runtime.

    Returns True on success. On failure (bad path, permissions,
    disk error) TRASH_DIR is left unchanged and False is returned --
    the caller must keep the old value and report via status bar.
    """
    global TRASH_DIR
    try:
        # NB-31: reject empty/dot/root/non-absolute paths.
        # Empty string -> Path(".") -> clean_trash() would iterdir()
        # in CWD and could unlink unrelated *.jpg. Root path
        # ("/", "C:\\") -> mkdir(exist_ok=True) passes, but
        # move_to_trash then fails on every file (PermissionError),
        # and clean_trash would iterdir() over the whole drive.
        # Non-absolute -> depends on CWD, unpredictable.
        # Real foot-gun demonstrated by user test: setting trash
        # to DATA_DIR root (C:\\...\\MemoryCalendar) or Documents
        # would let clean_trash purge unrelated files by retention.
        if new_path is None:
            logger.warning("set_trash_dir: None rejected")
            return False
        _s = str(new_path).strip()
        if not _s or _s in (".", ".."):
            logger.warning(
                f"set_trash_dir: empty/dot rejected: {new_path!r}")
            return False
        p = Path(_s)
        if not p.is_absolute():
            logger.warning(
                f"set_trash_dir: non-absolute rejected: {new_path!r}")
            return False
        # (P1): resolve() to catch .. and symlink-to-root
        try:
            p = p.resolve()
        except Exception:
            pass
        if p.parent == p:
            logger.warning(
                f"set_trash_dir: root path rejected: {new_path!r}")
            return False
        p.mkdir(parents=True, exist_ok=True)
        TRASH_DIR = p
        logger.info(f"trash_dir set to {p}")
        return True
    except Exception as e:
        logger.warning(f"set_trash_dir failed ({new_path}): {e}")
        return False


PRIO_FOLDER = "folder"
PRIO_EXIF = "exif"
PRIO_CTIME = "ctime"
PRIO_MTIME = "mtime"
DEFAULT_PRIORITY = [PRIO_FOLDER, PRIO_EXIF, PRIO_CTIME, PRIO_MTIME]
_VALID_PRIO_ITEMS = frozenset((PRIO_FOLDER, PRIO_EXIF, PRIO_CTIME, PRIO_MTIME))


def _sanitize_priority_list(plist):
    """(NB-28): validate priority elements, not just list-ness.

    A corrupted JSON like "priority": [1, 2, 3] or [null, null] would
    pass isinstance(list) but make get_file_date iterate non-matching
    items → scan returns 0 files ("library vanished" appearance; files
    on disk are intact). This helper:
      - keeps only whitelisted tokens + custom:* strings
      - falls back to DEFAULT_PRIORITY when nothing valid remains
    """
    if not isinstance(plist, list):
        return list(DEFAULT_PRIORITY)
    valid = [p for p in plist
             if isinstance(p, str)
             and (p in _VALID_PRIO_ITEMS or p.startswith("custom:"))]
    return valid if valid else list(DEFAULT_PRIORITY)
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
VALID_EXTS = frozenset((".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tiff")) | frozenset(VIDEO_EXTS)
VALID_EXTS_TUPLE = tuple(VALID_EXTS)
# MONTHS/WEEKDAYS removed — use month_name()/weekday_name()
SKIP_DIRS = {"$recycle.bin", "system volume information", ".thumbnails",
             "@eadir", "__macosx", ".cache", ".local", "node_modules"}

# platform detection + hotkey registry (skeleton).
# Used by hotkey editor. Defined now so module-level helpers
# (default_hotkey, resolved_hotkey) can compile and be imported
# for without another refactor.
BETA_PLATFORM = platform.system() in ("Darwin", "Linux")

HOTKEY_ACTIONS = {
    # app context - MainWindow, ApplicationShortcut
    "undo":         {"label": "hk_undo",         "default": "Ctrl+Z",       "ctx": "app"},
    "redo":         {"label": "hk_redo",         "default": "Ctrl+Shift+Z", "ctx": "app"},
    "redo_alt":     {"label": "hk_redo_alt",     "default": "Ctrl+Y",
                     "platform_defaults": {"darwin": ""}, "ctx": "app"},
    "copy":         {"label": "hk_copy",         "default": "Ctrl+C",       "ctx": "app"},
    "cut":          {"label": "hk_cut",          "default": "Ctrl+X",       "ctx": "app"},
    "paste":        {"label": "hk_paste",        "default": "Ctrl+V",       "ctx": "app"},
    "delete":       {"label": "hk_delete",       "default": "Del",
                     "platform_defaults": {"darwin": "Backspace"}, "ctx": "app"},
    "select_all":   {"label": "hk_select_all",   "default": "Ctrl+A",       "ctx": "app"},
    "remove_event": {"label": "hk_remove",       "default": "Ctrl+Shift+E", "ctx": "app"},
    # widget context - ImageViewer, WidgetWithChildrenShortcut
    "assign_event": {"label": "hk_assign",       "default": "Ctrl+E",       "ctx": "widget"},
    "prev_image":   {"label": "hk_prev",         "default": "Left",         "ctx": "widget"},
    "next_image":   {"label": "hk_next",         "default": "Right",        "ctx": "widget"},
    "close_viewer": {"label": "hk_close",        "default": "Esc",          "ctx": "widget"},
}


def _platform_key():
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _default_hotkey(action_id, profile=None):
    spec = HOTKEY_ACTIONS[action_id]
    plat = profile if profile in ("windows", "darwin", "linux") else _platform_key()
    return spec.get("platform_defaults", {}).get(plat, spec["default"])


def _resolved_hotkey(config, action_id):
    # (C1+C3): defensive guard for corrupted config.
    # Callers include SettingsWidget.__init__ / setup_shortcuts,
    # both run before any user interaction -- crash here means
    # the app never opens.
    overrides = config.get("hotkeys")
    if not isinstance(overrides, dict):
        overrides = {}
    _v = overrides.get(action_id)
    if isinstance(_v, str):
        return _v
    profile = config.get("hotkey_profile", "auto")
    if profile == "auto":
        profile = None
    return _default_hotkey(action_id, profile)


def _system_font_family():
    """native font per platform. Empty string = Qt system default."""
    if platform.system() == "Windows":
        return "Segoe UI"
    if platform.system() == "Darwin":
        return ".AppleSystemUIFont"
    return ""

FONT_FAMILY = _system_font_family()  # per-platform system font

# video semaphore — cap concurrent cv2.VideoCapture threads
_VIDEO_SEMAPHORE = threading.Semaphore(4)

ROLE_FILEPATH = Qt.ItemDataRole.UserRole
ROLE_LOADED = Qt.ItemDataRole.UserRole + 1

# NB-115: module-level cache-filename patterns. Previously every
# call site recompiled them (Python caches by pattern string, but
# explicit module constants are cleaner and easier to grep).
_CACHE_FILENAME_RE = re.compile(r"^(q_)?[0-9a-f]{40}[.]jpg$")
_QUAD_CACHE_FILENAME_RE = re.compile(r"^q_[0-9a-f]{40}[.]jpg$")

# NB-110: minimum on-disk file count for the auto-backup drop
# threshold. Tiny drop (5 -> 4) is 20% but only 1 file; do not
# bother with a backup. Same for cal / cat split counters.
_MIN_BACKUP_THRESHOLD = 20


# ============================================================
#                     i18n SYSTEM
# ============================================================
DEFAULT_LANGUAGE = {
    "app_title": "Memory Calendar",
    "brand": "📸 Memory Calendar",
    "nav_calendar": "📅 Calendar",
    "nav_manage": "📂 Manage",
    "nav_settings": "⚙ Settings",
    "nav_help": "❓ Help",
    "nav_about": "ℹ️ About",
    "toolbar_add_folder": "📁 Add folder",
    "toolbar_view_days": "View: Days",
    "toolbar_view_months": "View: Months",
    "toolbar_view_years": "View: Years",
    "toolbar_view_events": "View: Events",
    "toolbar_view_duplicates": "View: Duplicates",
    "toolbar_rescan": "🔄 Rescan",
    "toolbar_paste": "📋 Paste",
    "toolbar_cancel": "✕ Cancel",
    "btn_back": "← Back",
    "btn_close": "✕ Close",
    "btn_undo": "↶",
    "btn_redo": "↷",
    "btn_cut": "✂ Cut",
    "btn_copy": "📋 Copy",
    "btn_delete": "🗑 Delete",
    "btn_delete_dups": "🗑 Delete duplicates",
    "btn_rotate_left": "↺ Left",
    "btn_rotate_right": "↻ Right",
    "btn_event": "🏷 Event",
    "sort_label": "Sort:",
    "sort_name": "By name",
    "sort_date": "By date",
    "sort_photos_first": "Photos first",
    "sort_videos_first": "Videos first",
    "scroll_top": "▲",
    "scroll_bottom": "▼",
    "gallery_title": "Gallery",
    "gallery_duplicates_title": "Duplicates — {n} groups",
    "status_ready": "Ready",
    "status_scanning": "Scanning... {cur} of {total}",
    "status_found": "Done. Files: {total} (duplicates: {dups})",
    "status_nothing_undo": "Nothing to undo",
    "status_nothing_redo": "Nothing to redo",
    "status_undo_done": "Action undone",
    "status_redo_done": "Action redone",
    "status_copied": "Copied: {n}",
    "status_cut": "Cut: {n}",
    "status_no_selection": "No file selected",
    "status_analyzing": "Analyzing files for duplicates...",
    "status_files_added": "Files added to event: {name}",
    "status_removed_from_event": "Removed from event: {n}",
    "menu_open": "Open",
    "menu_show_in_folder": "📁 Show in folder",
    "menu_assign_event": "🏷 Assign event",
    "menu_copy": "Copy (Ctrl+C)",
    "menu_cut": "Cut (Ctrl+X)",
    "menu_delete": "Delete (Del)",
    "menu_rotate_left": "↺ Rotate left",
    "menu_rotate_right": "↻ Rotate right",
    "menu_assign_event_sc": "🏷 Assign event (Ctrl+E)",
    "menu_remove_from_event": "🗑 Remove from event",
    "confirm_delete": "Delete",
    "confirm_delete_file": "Delete this file? (Ctrl+Z to undo)",
    "confirm_delete_files": "Delete {n} file(s)? (Ctrl+Z to undo)",
    "confirm_delete_dups": "Delete {n} extra copies?\nExactly one file per group will remain.",
    "confirm_delete_dups_partial": "Delete {n} selected file(s)? (Ctrl+Z to undo)",
    "confirm_overwrite": "Overwrite?",
    "confirm_overwrite_body": "{name} already exists.\nReplace?",
    "confirm_clear_cal": "Clear the CURRENT calendar?",
    "confirm_delete_cal": "Delete calendar '{name}'? (Files remain)",
    "confirm_delete_event": "Delete event '{name}'? (Files remain)",
    "confirm_exit": "Are you sure you want to quit?",
    "error_title": "Error",
    "error_rotate": "Could not rotate: {err}",
    "error_delete": "Could not delete: {err}\nFile may be open in another program.",
    "error_partial_delete": "Failed to delete {n} files. They may be open in another program.",
    "error_paste": "Could not paste {path}: {err}",
    "error_load": "Could not load: {err}",
    "error_no_dups_group": "No extra copies in this group.",
    "error_calendar_empty": "Calendar is empty — nothing to export.",
    "error_invalid_format": "Format must contain at least one of Y, M, D.",
    "info_dups_none": "No duplicates found.",
    "info_dups_found": "{n} duplicate group(s) found",
    "info_dups_all_gone": "All duplicates deleted!",
    "info_no_more_dups": "No more duplicates.",
    "info_cache_cleared": "Cache files removed: {n}",
    "info_done": "Done",
    "warn_scan_running": "Scanning is already running.",
    "title_scan_running": "Please wait",
    "title_assign_event": "Assign event",
    "label_existing_events": "Existing events:",
    "label_new_event": "Or enter a new name:",
    "placeholder_new_event": "New event name",
    "hint_assign_event": "If the field above is filled, a new event will be created.\nIf empty, the selected one is used.",
    "title_new_calendar": "New calendar",
    "title_rename_calendar": "Rename calendar",
    "title_import_calendar": "Import",
    "label_calendar_name": "Enter name:",
    "label_rename_calendar": "New name for \"{old}\":",
    "label_import_calendar": "Calendar name:",
    "title_save_calendar": "Save calendar",
    "title_new_event": "New event",
    "title_rename_event": "Rename event",
    "label_event_name": "Name:",
    "label_event_new_name": "New name:",
    "title_new_format": "New format",
    "label_format_hint": "Format tokens: YYYY, YY, MM, M, DD, D.\nExamples: YYYY.MM.DD or YY_MM_DD",
    "title_language": "Language",
    "settings_title": "Settings",
    "settings_priority_desc": "Drag priorities up/down — the first source with a valid date wins.",
    "settings_add_format": "➕ Add custom folder format",
    "settings_remove_format": "🗑 Remove selected format",
    "settings_cache_box": "<b>Preview cache</b>",
    "settings_cache_path": "Path:",
    "settings_cache_change": "Change folder",
    "settings_cache_auto": "Auto-clean cache every",
    "settings_cache_days": " days",
    "settings_cache_size": "Used: {size:.1f} MB",
    "settings_cache_clear": "Clear cache now",
    "settings_display_box": "<b>Display & behavior</b>",
    "settings_dups_per_row": "Duplicate groups per row:",
    "settings_undo_limit": "Undo history depth (Ctrl+Z):",
    "settings_safe_delete": "Safe delete: default 'No' above",
    "settings_safe_files": " file(s)",
    "settings_trash_box": "<b>Trash (deleted files)</b>",
    "settings_trash_path": "Folder:",
    "settings_trash_change": "Change folder",
    "settings_trash_retention": "Keep deleted files for",
    "settings_safe_media_add": "Safe media add: block UI above",
    "settings_safe_media_files": " media file(s)",
    "dialog_scanning_title": "Adding media files",
    "dialog_scanning_body": "Please wait while media files are being added to the calendar…",
    "status_wait_scan": "Please wait — scan in progress",
    "status_scan_cancelled": "Scan cancelled",
    "settings_trash_hint": "Deleted files are moved here instead of being permanently removed (Ctrl+Z restores them). Do NOT point this inside your photo library — trashed files may be re-scanned as if they were originals.",
    "title_trash_warning": "Trash folder",
    "warn_trash_folder": "Use this folder for trash?\n\nAuto-cleanup removes ONLY files created by Memory Calendar (name pattern: mc_<timestamp>_<filename>.<ext>) that are older than the retention period. Any other files in this folder will be left untouched.\n\nRecommendation: do NOT select a folder containing important or irreplaceable files. Use a dedicated empty folder.\n\nContinue with this folder?",
    "settings_theme": "Theme:",
    "settings_theme_light": "Light",
    "settings_theme_dark": "Dark gray",
    "settings_lang_box": "<b>Language</b>",
    "settings_lang_load": "Load language (JSON)",
    "settings_lang_export": "Export language template",
    "settings_lang_reset": "Reset to English",
    "settings_lang_current": "Current language: {name}",
    "settings_share_box": "<b>Share calendar</b>",
    "settings_share_hint": "Export the current calendar to JSON. Share it together with your photos so others can import without rescanning.",
    "settings_share_export": "Export current calendar (JSON)",
    "settings_share_import": "Import calendar (JSON)",
    "settings_data_box": "<b>Data safety</b>",
    "settings_data_hint": "If external drives are disconnected, calendar entries become 'missing'. Do not auto-remove them — reconnect the drive first. Use the buttons below only when you are sure.",
    "settings_find_missing": "🔍 Find missing files",
    "settings_restore_backup": "↶ Restore from backup",
    "info_no_missing_files": "No missing files found.",
    "title_clean_missing": "Clean missing files",
    "confirm_clean_missing": "{total} file(s) in calendar(s) no longer exist on disk.\nRemove these entries? (Files on disk are not touched.)",
    "status_cleaned_missing": "Removed {n} missing entries",
    "status_finding_missing": "Finding missing files...",
    "info_no_backups": "No backups available.",
    "title_restore_backup": "Restore from backup",
    "label_restore_backup": "Select backup to restore:",
    "confirm_restore_body": "Restore '{name}'? Current calendars will be replaced.",
    "info_restore_done": "Backup restored.",
    "manager_title": "Manage",
    "manager_tab_cal": "📅 Calendars",
    "manager_tab_ev": "🏷 Events",
    "manager_active_cal": "Active calendar:",
    "manager_new": "➕ New",
    "manager_rename": "✏ Rename",
    "manager_load": "📂 Load",
    "manager_save": "💾 Save",
    "manager_clear": "🗑 Clear",
    "manager_delete_cal": "🗑 Delete calendar",
    "manager_events_list": "Events list:",
    "manager_add": "Add",
    "manager_rename_ev": "Rename",
    "manager_delete": "Delete",
    "help_title": "Guide",
    "help_import": "📁 Import",
    "help_import_body": "Click «Add folder» or drag a folder from your file manager into the window.",
    "help_views": "📅 Calendar views",
    "help_views_body": "Days, Months, Years, Events, Duplicates. Navigation: <b>Ctrl + wheel</b> to zoom.",
    "help_gallery": "🖼 Gallery",
    "help_gallery_body": "{modifier}+C/X/V/{delete}, right-click menu, double-click to open, ▲ and ▼ to scroll.",
    "help_viewer": "🔍 Viewer",
    "help_viewer_body": "Wheel — zoom, left drag — pan, ←/→ — prev/next, Esc — close.<br/><b>{delete} — delete, {modifier}+C — copy, {modifier}+E — event</b>.<br/>Buttons <b>↺</b> and <b>↻</b> rotate left/right.",
    "help_dups": "🗑 Duplicates",
    "help_dups_body": "The «Duplicates» view opens groups as cards. Click a card to open its files.<br/>«Delete duplicates» removes extra copies, keeping exactly one file per group.<br/>Groups per row is configurable (1–4).",
    "help_undo": "↶ Undo",
    "help_undo_body": "{modifier}+Z — undo (files restored from trash).<br/>{modifier}+Shift+Z or {modifier}+Y — redo.<br/>History depth is configurable in Settings.<br/><b>Note:</b> cut+paste done <i>outside</i> the app (via Windows Explorer) cannot be undone by {modifier}+Z — use {modifier}+V inside the app to keep undo history.",
    "help_themes": "🎨 Themes",
    "help_themes_body": "Settings provides 2 themes: Light and Dark gray.<br/>You can also load a custom translation from Settings.",
    "dialog_yes": "Yes",
    "dialog_no": "No",
    "dialog_ok": "OK",
    "dialog_cancel": "Cancel",
    "dialog_save": "Save",
    "dialog_discard": "Discard",
    "prio_folder": "Folder name (YYYY_MM_DD)",
    "prio_exif": "EXIF (capture date)",
    "prio_ctime": "File creation date (OS)",
    "prio_mtime": "File modification date (OS)",
    "prio_custom": "Custom folder format: {fmt}",
    "empty_state": "Nothing here yet.\nClick «Add folder» or drag a folder with photos into the window.",
    "no_preview": "No\npreview",
    "loading": "Loading...",
    "heic_unavailable": "HEIC: support unavailable",
    "cannot_load": "Could not load: {name}",
    "file_unavailable": "File unavailable",
    "trash_restore_error": "File already exists",
    "title_duplicates": "Duplicates",
    "title_exit": "Exit",
    "data_cleanup_title": "Data cleanup",
    "data_cleanup_body": "Removed {n} invalid entries from calendar data.\nIf you lost files, please re-add the folder.",
    "undo_restore_failed": "Could not restore {n} file(s). They may already exist on disk.",
    "import_pick_calendar": "This file contains multiple calendars. Pick one:",
    "import_invalid_format": "Invalid calendar file: expected {date: [files]} object.",
    "import_no_valid_dates": "No valid dates found in this file.\nSkipped {n} invalid entries.",
    "import_partial_success": "Imported, but skipped {n} invalid entries",
    "month_1": "January",
    "month_2": "February",
    "month_3": "March",
    "month_4": "April",
    "month_5": "May",
    "month_6": "June",
    "month_7": "July",
    "month_8": "August",
    "month_9": "September",
    "month_10": "October",
    "month_11": "November",
    "month_12": "December",
    "weekday_1": "Mon",
    "weekday_2": "Tue",
    "weekday_3": "Wed",
    "weekday_4": "Thu",
    "weekday_5": "Fri",
    "weekday_6": "Sat",
    "weekday_7": "Sun",
    "status_counting_files": "Counting files...",
    "status_stopping_scan": "Stopping scan...",
    "status_closing": "Closing...",
    "status_paste_gallery_only": "Paste is only available in Gallery view",
    "error_name_exists": "Name already exists: {name}",
    "status_nothing_to_paste": "Nothing to paste — files are already in place",
    "status_busy": "Another operation is in progress",
    "status_redo_conflicts": "Redo: {n} file(s) skipped (target busy)",
    "status_redo_skipped_overwrite": "Redo skipped: paste-with-overwrite cannot be replayed safely (disk state would desync). Use Ctrl+Z again, or paste again manually.",
    "error_cant_delete_last_cal": "Cannot delete the last calendar",
    "status_paste_dups_forbidden": "Paste is not available in Duplicates view",
    "status_cut_partial": "{moved} moved externally, {left} still in clipboard",
    "status_purge_skipped": "Skipped cleanup: {n} of {total} files missing (disk offline?)",
    "warn_backup_failed": "Backup failed — saving anyway. Check disk space/permissions.",
    "backup_fail_title": "Backup failed",
    "backup_fail_body": "Could not back up the current calendar data (disk full or permission issue?).\n\nSaving now will overwrite the previous state without a backup. Continue?",
    "backup_fail_cancelled": "Save cancelled - backup failed. Free disk space or fix permissions, then try again.",
    "error_save_failed": "Could not save data to disk.",
    "confirm_exit_anyway": "Save failed. Close anyway and risk losing unsaved changes?",
    "status_cut_done": "Cut done — {n} file(s) moved externally. Ctrl+Z cannot undo external moves.",
    "status_move_done": "Moved — {n} file(s) transferred externally. Ctrl+Z cannot undo external moves.",
    "status_paste_partial": "{n} pasted, {left} kept in clipboard",
    "title_cache_warning": "Preview cache folder",
    "warn_cache_folder": "This folder contains {n} .jpg file(s) not created by Memory Calendar. Auto-cleanup only removes its own cache files (name pattern: 40-hex-char.jpg or q_40-hex-char.jpg), but pointing cache at a photo folder is a bad idea. Continue anyway?",
    "status_paste_empty_event": "Cannot paste into empty event (no target folder)",
    "dups_rescan_needed": "Duplicates view cleared by undo/redo. Press \u00abRescan\u00bb to refresh.",
    # hotkey labels (used from hotkey editor)
    "hk_undo": "Undo", "hk_redo": "Redo", "hk_redo_alt": "Redo (alternative)",
    "hk_copy": "Copy", "hk_cut": "Cut", "hk_paste": "Paste",
    "hk_delete": "Delete", "hk_select_all": "Select all",
    "hk_remove": "Remove from event", "hk_assign": "Assign event",
    "hk_prev": "Previous image", "hk_next": "Next image",
    "hk_close": "Close viewer",
    "hk_section_title": "Keyboard shortcuts",
    "hk_section_hint": "Click a shortcut field and press the new combination. Empty = disabled.",
    "hk_reset_all": "Reset to platform defaults",
    "hk_disable": "Disable",
    "hk_conflict": "'{a}' conflicts with '{b}'. Assign anyway?",
    "hk_profile_label": "Platform profile:",
    "hk_profile_auto": "Auto (detected: {os})",
    "hk_btn_global": "Global", "hk_btn_gallery": "In gallery / viewer",
    "modifier_key": "Ctrl", "delete_key": "Del",
    # menubar labels
    "menu_file": "&File", "menu_edit": "&Edit",
    "menu_settings": "&Settings", "menu_help": "&Help",
    "menu_quit": "&Quit", "menu_preferences": "&Preferences...",
    "menu_about": "&About",
    "about_text": "Memory Calendar v{version}\nPhoto / calendar manager\n\n{details}",
    # beta platform marker
    "beta_title_suffix": "[BETA]",
    "beta_status_msg": "BETA build - only Windows is tested. Please report issues.",
    "beta_about_note": "NOTE: This build is BETA on macOS/Linux. Only Windows is tested by the maintainer.",
}


class Translator(QObject):
    language_changed = Signal()

    def __init__(self):
        super().__init__()
        self._current = dict(DEFAULT_LANGUAGE)
        self._name = "English"

    def t(self, key, **kwargs):
        text = self._current.get(key, DEFAULT_LANGUAGE.get(key, key))
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                # (re-apply ): ValueError from str.format when
                # the loaded language file has unbalanced braces
                # (e.g. "{cur} of {total"). Without this, a corrupted
                # lang JSON crashed t("status_scanning", ...) mid-scan.
                return text
        return text

    @property
    def name(self):
        return self._name

    def load_from_file(self, path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Language file must be a JSON object")
        # (NB-29): coerce non-str values. A corrupted language JSON
        # with e.g. "app_title": 123 would break setWindowTitle mid-
        # retranslate (partial state, some labels updated, others not).
        merged = dict(DEFAULT_LANGUAGE)
        for _k, _v in data.items():
            merged[_k] = _v if isinstance(_v, str) else str(_v)
        # (U3a): preserve platform-specific modifier_key /
        # delete_key (Cmd/Backspace on macOS) if the custom language
        # file did not explicitly override them. __main__ sets them
        # before MainWindow; without this preservation, loading any
        # language wipes them back to DEFAULT_LANGUAGE ("Ctrl"/"Del")
        # and Help shows wrong shortcuts on macOS.
        for _pk in ("modifier_key", "delete_key"):
            if _pk not in data and _pk in self._current:
                merged[_pk] = self._current[_pk]
        self._current = merged
        self._name = data.get("_language_name", Path(path).stem)
        self.language_changed.emit()

    def export_to_file(self, path):
        # atomic lang export
        import tempfile
        dir_ = os.path.dirname(str(path)) or "."
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=dir_)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                # NB-68: strip platform-specific keys from template.
                # Otherwise a re-import on macOS/Linux overwrites
                # modifier_key/delete_key with "Ctrl"/"Del" — the
                # preservation in load_from_file only applies when
                # the key is ABSENT from the loaded JSON.
                _export = {k: v for k, v in DEFAULT_LANGUAGE.items()
                           if k not in ("modifier_key", "delete_key")}
                json.dump(_export, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def reset_to_default(self):
        # (U3b): preserve platform-specific modifier_key /
        # delete_key across reset.
        _mod = self._current.get(
            "modifier_key",
            DEFAULT_LANGUAGE.get("modifier_key", "Ctrl"))
        _del = self._current.get(
            "delete_key",
            DEFAULT_LANGUAGE.get("delete_key", "Del"))
        self._current = dict(DEFAULT_LANGUAGE)
        self._current["modifier_key"] = _mod
        self._current["delete_key"] = _del
        self._name = "English"
        self.language_changed.emit()


tr = Translator()
def t(key, **kwargs):
    return tr.t(key, **kwargs)


def month_name(i_1based):
    """Returns localized month name. i_1based: 1=January, 12=December."""
    return t(f"month_{i_1based}")


def weekday_name(i_0based):
    """Returns localized short weekday. i_0based: 0=Monday, 6=Sunday."""
    return t(f"weekday_{i_0based + 1}")


_RealQMessageBox = QMessageBox


def _msg(parent, title, text, icon):
    mb = _RealQMessageBox(parent)
    mb.setWindowTitle(title)
    mb.setText(text)
    mb.setIcon(icon)
    ok_btn = mb.addButton(t("dialog_ok"), _RealQMessageBox.ButtonRole.AcceptRole)
    mb.setDefaultButton(ok_btn)
    mb.exec()
    return ok_btn


def msg_info(parent, title, text):
    return _msg(parent, title, text, _RealQMessageBox.Icon.Information)


def msg_warning(parent, title, text):
    return _msg(parent, title, text, _RealQMessageBox.Icon.Warning)


def msg_error(parent, title, text):
    return _msg(parent, title, text, _RealQMessageBox.Icon.Critical)


# msg_question removed 


class _LocalizedMessageBoxMeta(type):
    def __getattr__(cls, name):
        return getattr(_RealQMessageBox, name)
    # __instancecheck__ (msgbox)
    def __instancecheck__(cls, obj):
        return isinstance(obj, _RealQMessageBox)


class _LocalizedMessageBox(metaclass=_LocalizedMessageBoxMeta):
    """Drop-in replacement: QMessageBox.* static calls get localized buttons."""
    StandardButton = _RealQMessageBox.StandardButton
    ButtonRole = _RealQMessageBox.ButtonRole
    Icon = _RealQMessageBox.Icon

    @staticmethod
    def _build(parent, title, text, icon):
        mb = _RealQMessageBox(parent)
        mb.setWindowTitle(title)
        mb.setText(text)
        mb.setIcon(icon)
        return mb

    @staticmethod
    def information(parent, title, text, *args, **kwargs):
        mb = _LocalizedMessageBox._build(parent, title, text, _RealQMessageBox.Icon.Information)
        mb.addButton(t("dialog_ok"), _RealQMessageBox.ButtonRole.AcceptRole)
        mb.exec()
        return _RealQMessageBox.StandardButton.Ok

    @staticmethod
    def warning(parent, title, text, *args, **kwargs):
        mb = _LocalizedMessageBox._build(parent, title, text, _RealQMessageBox.Icon.Warning)
        mb.addButton(t("dialog_ok"), _RealQMessageBox.ButtonRole.AcceptRole)
        mb.exec()
        return _RealQMessageBox.StandardButton.Ok

    @staticmethod
    def critical(parent, title, text, *args, **kwargs):
        mb = _LocalizedMessageBox._build(parent, title, text, _RealQMessageBox.Icon.Critical)
        mb.addButton(t("dialog_ok"), _RealQMessageBox.ButtonRole.AcceptRole)
        mb.exec()
        return _RealQMessageBox.StandardButton.Ok

    @staticmethod
    def about(parent, title, text, *args, **kwargs):
        # NB-104: localize the OK button. Previously about() went
        # straight to _RealQMessageBox.about via metaclass, whose
        # button label is system-locale, not app-locale.
        mb = _LocalizedMessageBox._build(parent, title, text, _RealQMessageBox.Icon.Information)
        mb.addButton(t("dialog_ok"), _RealQMessageBox.ButtonRole.AcceptRole)
        mb.exec()
        return _RealQMessageBox.StandardButton.Ok

    @staticmethod
    def question(parent, title, text, buttons=None, defaultButton=None, *args, **kwargs):
        mb = _LocalizedMessageBox._build(parent, title, text, _RealQMessageBox.Icon.Question)
        BT = _RealQMessageBox.StandardButton
        if buttons is None:
            buttons = BT.Yes | BT.No
        b_int = int(buttons)
        button_map = {}
        if b_int & int(BT.Yes):
            button_map[BT.Yes] = mb.addButton(t("dialog_yes"), _RealQMessageBox.ButtonRole.YesRole)
        if b_int & int(BT.No):
            button_map[BT.No] = mb.addButton(t("dialog_no"), _RealQMessageBox.ButtonRole.NoRole)
        if b_int & int(BT.Ok):
            button_map[BT.Ok] = mb.addButton(t("dialog_ok"), _RealQMessageBox.ButtonRole.AcceptRole)
        if b_int & int(BT.Cancel):
            button_map[BT.Cancel] = mb.addButton(t("dialog_cancel"), _RealQMessageBox.ButtonRole.RejectRole)
        if b_int & int(BT.Save):
            button_map[BT.Save] = mb.addButton(t("dialog_save"), _RealQMessageBox.ButtonRole.AcceptRole)
        if b_int & int(BT.Discard):
            button_map[BT.Discard] = mb.addButton(t("dialog_discard"), _RealQMessageBox.ButtonRole.DestructiveRole)
        known = int(BT.Yes | BT.No | BT.Ok | BT.Cancel | BT.Save | BT.Discard)  # warn unsupported mask 
        unknown = b_int & ~known
        if unknown:
            logger.warning(f"LocalizedMsgBox.question: unsupported buttons mask {unknown:#x}, "
                           f"adding fallback OK")
            button_map[BT.Ok] = mb.addButton(t("dialog_ok"), _RealQMessageBox.ButtonRole.AcceptRole)
        if not button_map:
            button_map[BT.Ok] = mb.addButton(t("dialog_ok"), _RealQMessageBox.ButtonRole.AcceptRole)
        # Default: No if available (safety for destructive dialogs); else Ok; else Yes
        if defaultButton and defaultButton in button_map:
            mb.setDefaultButton(button_map[defaultButton])
        elif BT.No in button_map:
            mb.setDefaultButton(button_map[BT.No])
        elif BT.Cancel in button_map:
            mb.setDefaultButton(button_map[BT.Cancel])
        elif BT.Ok in button_map:
            mb.setDefaultButton(button_map[BT.Ok])
        mb.exec()
        clicked = mb.clickedButton()
        if clicked is None:
            # x-close returns cancel
            if BT.Cancel in button_map:
                return BT.Cancel
            # NB-105: prefer any RejectRole button over blind No.
            # If caller passed only Ok (no No/Cancel), "X" was
            # previously returned as No, which caller didn't ask for.
            try:
                for _bv, _bw in button_map.items():
                    if mb.buttonRole(_bw) == _RealQMessageBox.ButtonRole.RejectRole:
                        return _bv
            except Exception:
                pass
            return BT.No
        # NB-82: identity-first resolution. Multiple standard buttons
        # share the same role (Save and Ok both use AcceptRole; Discard
        # and Yes both use Destructive/YesRole). Role-based matching
        # would mis-classify Save as Ok. Compare widgets by identity
        # first; fall back to role only when identity comparison fails
        # (shouldn't happen in PySide6, kept for defensive correctness).
        for btn_val, btn_widget in button_map.items():
            try:
                if clicked == btn_widget:
                    return btn_val
            except Exception:
                continue
        try:
            role = mb.buttonRole(clicked)
        except Exception:
            role = None
        _RoleMap = {
            _RealQMessageBox.ButtonRole.YesRole: BT.Yes,
            _RealQMessageBox.ButtonRole.NoRole: BT.No,
            _RealQMessageBox.ButtonRole.AcceptRole: BT.Ok,
            _RealQMessageBox.ButtonRole.RejectRole: BT.Cancel,
            _RealQMessageBox.ButtonRole.DestructiveRole: BT.Yes,
        }
        if role in _RoleMap:
            _cand = _RoleMap[role]
            if _cand in button_map:
                return _cand
        return BT.No


QMessageBox = _LocalizedMessageBox


try:
    from PySide6.QtWidgets import QInputDialog as _RealQInputDialog
except ImportError:
    _RealQInputDialog = None


if _RealQInputDialog is not None:
    class _LocalizedInputDialogMeta(type):
        def __getattr__(cls, name):
            return getattr(_RealQInputDialog, name)
        # __instancecheck__ (input)
        def __instancecheck__(cls, obj):
            return isinstance(obj, _RealQInputDialog)

    class _LocalizedInputDialog(metaclass=_LocalizedInputDialogMeta):
        """Replacement for QInputDialog with localized OK/Cancel buttons."""
        InputMode = _RealQInputDialog.InputMode
        EchoMode = QLineEdit.EchoMode  # EchoMode lives on QLineEdit
        InputDialogOption = _RealQInputDialog.InputDialogOption

        @staticmethod
        def _run(dlg):  # _run without default_ok 
            dlg.setOkButtonText(t("dialog_ok"))
            dlg.setCancelButtonText(t("dialog_cancel"))
            dlg.exec()
            return bool(dlg.result())

        @staticmethod
        def getText(parent, title, label, echo=None, text="", flags=None, inputMethodHints=None):
            dlg = _RealQInputDialog(parent)
            dlg.setWindowTitle(title)
            dlg.setLabelText(label)
            dlg.setInputMode(_RealQInputDialog.InputMode.TextInput)
            if echo is not None:
                # setTextEchoMode — QInputDialog has no setEchoMode
                dlg.setTextEchoMode(echo)
            if text:
                dlg.setTextValue(text)
            # getText flags 
            if flags is not None:
                dlg.setWindowFlags(flags)
            if inputMethodHints is not None:
                dlg.setInputMethodHints(inputMethodHints)
            ok = _LocalizedInputDialog._run(dlg)
            return dlg.textValue(), ok

        @staticmethod
        def getMultiLineText(parent, title, label, text="", flags=None, inputMethodHints=None):
            dlg = _RealQInputDialog(parent)
            dlg.setWindowTitle(title)
            dlg.setLabelText(label)
            dlg.setInputMode(_RealQInputDialog.InputMode.TextInput)
            dlg.setOption(_RealQInputDialog.InputDialogOption.UsePlainTextEditForTextInput, True)
            if text:
                dlg.setTextValue(text)
            # getMultiLineText flags 
            if flags is not None:
                dlg.setWindowFlags(flags)
            if inputMethodHints is not None:
                dlg.setInputMethodHints(inputMethodHints)
            ok = _LocalizedInputDialog._run(dlg)
            return dlg.textValue(), ok

        @staticmethod
        def getItem(parent, title, label, items, current=0, editable=True, flags=None, inputMethodHints=None):
            dlg = _RealQInputDialog(parent)
            dlg.setWindowTitle(title)
            dlg.setLabelText(label)
            dlg.setComboBoxItems(list(items))
            dlg.setComboBoxEditable(editable)
            if items:
                dlg.setTextValue(items[current] if 0 <= current < len(items) else items[0])
            # getItem flags 
            if flags is not None:
                dlg.setWindowFlags(flags)
            if inputMethodHints is not None:
                dlg.setInputMethodHints(inputMethodHints)
            ok = _LocalizedInputDialog._run(dlg)
            return dlg.textValue(), ok

        @staticmethod
        def getInt(parent, title, label, value=0, minValue=-2147483647, maxValue=2147483647, step=1, flags=None, inputMethodHints=None):
            dlg = _RealQInputDialog(parent)
            dlg.setWindowTitle(title)
            dlg.setLabelText(label)
            dlg.setInputMode(_RealQInputDialog.InputMode.IntInput)
            dlg.setIntRange(minValue, maxValue)
            dlg.setIntStep(step)
            dlg.setIntValue(value)
            # getInt flags 
            if flags is not None:
                dlg.setWindowFlags(flags)
            if inputMethodHints is not None:
                dlg.setInputMethodHints(inputMethodHints)
            ok = _LocalizedInputDialog._run(dlg)
            return dlg.intValue(), ok

        @staticmethod
        def getDouble(parent, title, label, value=0.0, minValue=-1.7976931348623157e+308, maxValue=1.7976931348623157e+308, decimals=1, flags=None, inputMethodHints=None):
            dlg = _RealQInputDialog(parent)
            dlg.setWindowTitle(title)
            dlg.setLabelText(label)
            dlg.setInputMode(_RealQInputDialog.InputMode.DoubleInput)
            dlg.setDoubleRange(minValue, maxValue)
            dlg.setDoubleDecimals(decimals)
            dlg.setDoubleValue(value)
            # getDouble flags 
            if flags is not None:
                dlg.setWindowFlags(flags)
            if inputMethodHints is not None:
                dlg.setInputMethodHints(inputMethodHints)
            ok = _LocalizedInputDialog._run(dlg)
            return dlg.doubleValue(), ok

    QInputDialog = _LocalizedInputDialog


# ============================================================
#                     THEMES
# ============================================================
THEMES = {
    "light": {
        "bg": "#f5f7fa", "surface": "#ffffff", "primary": "#3b82f6",
        "primary_dk": "#2563eb", "primary_lt": "#dbeafe", "text": "#1f2937",
        "muted": "#6b7280", "border": "#e5e7eb", "hover": "#f3f4f6",
        "danger": "#ef4444", "danger_dk": "#dc2626",
        "scroll_handle": "#cbd5e1", "scroll_handle_hover": "#94a3b8",
        "tooltip_text": "#ffffff",
    },
    "dark": {
        "bg": "#2b2b2b", "surface": "#383838", "primary": "#e0e0e0",
        "primary_dk": "#4a4a4a", "primary_lt": "#3a3a3a", "text": "#f5f5f5",
        "muted": "#c0c0c0", "border": "#4a4a4a", "hover": "#424242",
        "danger": "#f87171", "danger_dk": "#dc2626",
        "scroll_handle": "#5a5a5a", "scroll_handle_hover": "#7a7a7a",
        "tooltip_text": "#1f2937",
    },
}

COLOR_BG = "#f5f7fa"
COLOR_SURFACE = "#ffffff"
COLOR_PRIMARY = "#3b82f6"
COLOR_PRIMARY_DK = "#2563eb"
COLOR_PRIMARY_LT = "#dbeafe"
COLOR_TEXT = "#1f2937"
COLOR_MUTED = "#6b7280"
COLOR_BORDER = "#e5e7eb"
COLOR_HOVER = "#f3f4f6"
COLOR_DANGER = "#ef4444"
COLOR_DANGER_DK = "#dc2626"
COLOR_SCROLL_HANDLE = "#cbd5e1"
COLOR_SCROLL_HANDLE_HOVER = "#94a3b8"
COLOR_TOOLTIP_TEXT = "#ffffff"
CURRENT_THEME = "light"


def apply_theme(name):
    global CURRENT_THEME, COLOR_BG, COLOR_SURFACE, COLOR_PRIMARY, COLOR_PRIMARY_DK
    global COLOR_PRIMARY_LT, COLOR_TEXT, COLOR_MUTED, COLOR_BORDER, COLOR_HOVER
    global COLOR_DANGER, COLOR_DANGER_DK, COLOR_SCROLL_HANDLE, COLOR_SCROLL_HANDLE_HOVER
    global COLOR_TOOLTIP_TEXT
    if name not in THEMES:
        name = "light"
    CURRENT_THEME = name
    th = THEMES[name]
    COLOR_BG = th["bg"]
    COLOR_SURFACE = th["surface"]
    COLOR_PRIMARY = th["primary"]
    COLOR_PRIMARY_DK = th["primary_dk"]
    COLOR_PRIMARY_LT = th["primary_lt"]
    COLOR_TEXT = th["text"]
    COLOR_MUTED = th["muted"]
    COLOR_BORDER = th["border"]
    COLOR_HOVER = th["hover"]
    COLOR_DANGER = th["danger"]
    COLOR_DANGER_DK = th["danger_dk"]
    COLOR_SCROLL_HANDLE = th["scroll_handle"]
    COLOR_SCROLL_HANDLE_HOVER = th["scroll_handle_hover"]
    COLOR_TOOLTIP_TEXT = th["tooltip_text"]


apply_theme("light")


# ============================================================
#                     TRASH & SAFE IO
# ============================================================
def _process_ui():
    """process UI events without dispatching user input.

    Safe for file-operation loops (delete/paste/dups-delete): prevents
    re-entrancy through keyboard shortcuts / clicks while progress bar
    updates are pending.
    """
    # NB-114: QEventLoop imported at module top.
    QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)


def move_to_trash(filepath, max_retries=5):  # 10 retries × 0.15s
#   = 1.5s freeze per locked file; 5 retries = 0.75s worst-case
#   (transient AV scans are usually <0.5s)
    for i in range(max_retries):
        try:
            if not filepath:
                logger.warning("move_to_trash: empty filepath")
                return False, None, Exception("empty filepath")
            if not os.path.exists(filepath):
                # (P1): distinguish "file truly deleted"
                # from "external drive disconnected". Without this
                # check, disconnecting a drive mid-delete makes
                # os.path.exists()==False → reported as "success"
                # → calendar purged while file remains on disk.
                # Same defence as _watch_cut_files
                # (_file_confirmed_missing).
                _parent = os.path.dirname(filepath) or "."
                try:
                    _parent_ok = (os.path.isdir(_parent)
                                  and os.access(_parent, os.R_OK))
                except OSError:
                    _parent_ok = False
                if not _parent_ok:
                    logger.warning(
                        f"move_to_trash: parent inaccessible "
                        f"(drive offline?): {filepath}")
                    return (False, None,
                            Exception("parent inaccessible"))
                return True, None, None
            _orig_mode = None
            try:
                _orig_mode = os.stat(filepath).st_mode
                os.chmod(filepath, _orig_mode | stat.S_IWRITE)
            except OSError:
                pass
            base = os.path.basename(filepath)
            ts = int(time.time() * 1000000)
            # (P0): collision-safe name. Two files with the
            # same basename arriving in the same microsecond would
            # make shutil.move OVERWRITE the earlier trash file —
            # real data loss. Bump a numeric suffix until free.
            _name = f"mc_{ts}_{base}"
            trash_path = str(TRASH_DIR / _name)
            _n = 1
            while os.path.exists(trash_path):
                trash_path = str(TRASH_DIR / f"mc_{ts}_{_n}_{base}")
                _n += 1
                if _n > 1000:
                    # pathological — bail out instead of loop
                    raise OSError(
                        f"trash name collision for {base}")
            shutil.move(filepath, trash_path)
            # NB-134: preserve original read-only mode in trash so
            # Ctrl+Z restore returns the file with its intended mode.
            if _orig_mode is not None:
                try:
                    os.chmod(trash_path, _orig_mode)
                except OSError:
                    pass
            logger.info(f"move_to_trash OK: {filepath} -> {trash_path}")
            return True, trash_path, None
        except FileNotFoundError as _fnf:
            # (P2): distinguish "source gone" (race, success)
            # from "trash destination unavailable". In the latter
            # case source still exists -- reporting success would
            # purge the calendar entry while file remains.
            if os.path.exists(filepath):
                logger.warning(
                    f"move_to_trash: FileNotFoundError but "
                    f"source exists (trash unavailable?): "
                    f"{filepath}: {_fnf}")
                return False, None, _fnf
            # (P1 data-loss-adjacent): same parent-dir
            # accessibility check as G32 initial-miss branch.
            # Disconnected drive between initial exists() check
            # and shutil.move() -> both raise FileNotFoundError.
            # Reporting success would purge the calendar entry
            # while file remains on the (offline) drive.
            _parent = os.path.dirname(filepath) or "."
            try:
                _parent_ok = (os.path.isdir(_parent)
                              and os.access(_parent, os.R_OK))
            except OSError:
                _parent_ok = False
            if not _parent_ok:
                logger.warning(
                    f"move_to_trash: FileNotFoundError + "
                    f"parent inaccessible (drive offline?): "
                    f"{filepath}")
                return False, None, _fnf
            return True, None, None
        except PermissionError as e:
            logger.warning(f"move_to_trash PermissionError ({i+1}/{max_retries}): {filepath}: {e}")
            if i == max_retries - 1:
                return False, None, e
            time.sleep(0.15)
        except Exception as e:
            logger.warning(f"move_to_trash exception: {filepath}: {e}")
            return False, None, e
    return False, None, Exception("Unknown")


def restore_from_trash(trash_path, original_path):
    try:
        os.makedirs(os.path.dirname(original_path) or ".", exist_ok=True)
        if os.path.exists(original_path):
            return False, Exception(t("trash_restore_error"))
        shutil.move(trash_path, original_path)
        return True, None
    except Exception as e:
        return False, e


def clean_trash(days=7):
    """retention based on TIME FILE ENTERED TRASH, not original mtime.

    `shutil.move` preserves the source file's mtime. A photo taken 2 years
    ago that the user just deleted would land in TRASH_DIR with mtime
    2 years ago — and _AutoCleanWorker (fires ~2s after next app start)
    would unlink it immediately, permanently. That was real data-loss for
    any deleted file older than `days`.

    Filename format from move_to_trash: f"{ts}_{base}" where ts is
    microseconds since epoch. Parse ts to determine age. Fallback to
    st_mtime for files with unrecognized names (manually placed, legacy).
    """
    try:
        cutoff = time.time() - days * 86400
        for f in TRASH_DIR.iterdir():
            if not f.is_file():
                continue
            try:
                # (tightening ): move_to_trash produces exactly
                # f"{ts}_{base}" with ts = int(time.time() * 1e6),
                # which is 16 decimal digits since 2001-09-09. The
                # previous ^\d+_ matched ANY leading digits, so
                # "2024_vacation.jpg" parsed as ts=2024 → ts_sec=0.002
                # → < cutoff → DELETED. Real data-loss for user data
                # in a user-choosen trash folder.
                # Now: require 16 digits AND timestamp >= 2001-09-09
                # (1e9 seconds since epoch). Both must hold.
                # 15-17 digits covers times before 2001 and
                # after 2286. ts_sec < 1e9 guard below still
                # filters foreign files.
                m = re.match(r"^mc_(\d{15,17})_", f.name)
                if not m:
                    continue
                ts_sec = int(m.group(1)) / 1_000_000.0
                if ts_sec < 1_000_000_000:  # pre-2001-09-09, not ours
                    continue
                if ts_sec < cutoff:
                    f.unlink()
            except (OSError, ValueError):
                pass
    except Exception:
        pass


# safe_remove removed 


# ============================================================
#                     FILE UTILITIES
# ============================================================
def _atomic_write_json(path, data):
    """Write JSON atomically: temp file in same dir → os.replace."""
    import tempfile
    dir_ = os.path.dirname(str(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _count_files_in_state(state):
    """total number of file paths across all calendars."""
    try:
        total = 0
        for cal in state.get("calendars", {}).values():
            for files in cal.values():
                total += len(files)
        # (P1 data-loss-adjacent): count categories too.
        # Otherwise the automatic-backup threshold (drop >5% or
        # to zero) never fires when only events are lost -- e.g.
        # corrupted categories on load, sanitize drops them,
        # _do_save_json writes empty categories back with no
        # backup (file_count unchanged -> no threshold). User
        # loses event assignments silently. Including categories
        # in the count keeps the trigger consistent with actual
        # data loss.
        #
        # (P1 regression): total-only counting DILUTES the
        # calendar-only threshold. Example: 100 cal + 100 cat;
        # deleting 6 cal files (6% cal drop) is only 3% total
        # drop -> no backup (fired because cal-only count
        # was 100->94). Use _count_files_in_state_split() in
        # _do_save_json to preserve both signals independently.
        for files in state.get("categories", {}).values():
            total += len(files)
        return total
    except Exception:
        return -1


def _count_files_in_state_split(state):
    """returns (calendar_count, category_count).

    -1, -1 on any error. Used by _do_save_json to fire backup when
    EITHER calendar OR category count drops >5% independently.
    Prevents regression where merging both into one count
    diluted calendar-only deletions when categories were large.
    """
    try:
        cal = 0
        for c in state.get("calendars", {}).values():
            for files in c.values():
                cal += len(files)
        cat = 0
        for files in state.get("categories", {}).values():
            cat += len(files)
        return cal, cat
    except Exception as _e:
        # NB-109: at least log. Previously silent -1 masked MemoryError
        # / data corruption vs "no data".
        logger.debug(f"_count_files_in_state_split failed: {_e}")
        return -1, -1


def _count_files_in_json_split(path):
    """split counts from on-disk JSON. (-1, -1) on error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _count_files_in_state_split(json.load(f))
    except Exception as _e:
        # NB-109: log so genuine failures are distinguishable from
        # "no file yet".
        logger.debug(f"_count_files_in_json_split failed: {_e}")
        return -1, -1


def _count_files_in_json(path):
    """total file paths in an on-disk state file. -1 on error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _count_files_in_state(data)
    except Exception:
        return -1


def _safe_backup_mtime(p):
    """(NB-16): sort key tolerant to concurrent unlink."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _cleanup_backups(keep=BACKUP_KEEP):
    try:
        # NB-16: sort by mtime, not lexicographic. Collision-
        # suffixed files (_1, _2) share the base file's second but
        # sort AFTER it as strings — wrong file was retired.
        files = sorted(BACKUP_DIR.glob("calendar_data_*.json"),
                       key=_safe_backup_mtime)
        if len(files) <= keep:
            return
        for f in files[:-keep]:
            try:
                f.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _backup_calendar_data(path, reason=""):
    """copy current state file into BACKUP_DIR with timestamp.
    Safe to call on non-existent file (returns None).

    NB-94: atomic write via tempfile + os.replace. Previously
    shutil.copy2 left a partial backup file on disk-full / I/O
    error; the partial file was indistinguishable from a valid one
    until someone tried to load it (json.load would then fail in
    restore_from_backup). No data-loss risk since the original
    remains intact, but the partial consumed a slot in BACKUP_KEEP
    and was treated as fresh by _cleanup_backups.
    """
    try:
        if not path or not os.path.exists(path):
            return None
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"calendar_data_{ts}.json"
        n = 1
        while backup.exists():
            backup = BACKUP_DIR / f"calendar_data_{ts}_{n}.json"
            n += 1
        # atomic write: temp + os.replace
        fd, tmp = tempfile.mkstemp(
            prefix=".bk_", suffix=".json", dir=str(BACKUP_DIR))
        try:
            with open(path, "rb") as src, os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            os.replace(tmp, str(backup))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.info(f"backup created: {backup.name} ({reason})")
        _cleanup_backups()
        return backup
    except Exception as e:
        logger.error(f"backup failed: {e}")
        return False  # distinguish from None (= no source file)


def get_cache_size_mb(cache_dir):
    try:
        # NB-113: only count files WE created (same pattern as
        # clear_cache / _AutoCleanWorker). Previously any *.jpg in
        # cache_dir was summed; if user pointed cache at a photo
        # folder, "Used: N MB" wildly misreported.
        return sum(
            f.stat().st_size
            for f in Path(cache_dir).glob("*.jpg")
            if f.exists() and _CACHE_FILENAME_RE.match(f.name)
        ) / (1024 * 1024)
    except Exception:
        return 0.0


def clear_cache(cache_dir):
    # (P0): guard against empty/dot cache_dir → CWD wipe.
    # (P0): also reject root — C:\ would glob("*.jpg") in the
    # whole drive root and unlink every JPG (real data-loss).
    try:
        _s = str(cache_dir) if cache_dir is not None else ""
        if not _s or _s in (".", ""):
            logger.warning("clear_cache: empty cache_dir, skipping")
            return 0
        _p = Path(_s)
        if not _p.is_absolute():
            logger.warning(f"clear_cache: non-absolute {_s}, skipping")
            return 0
        # (P1): resolve() to catch .. and symlink-to-root bypass
        try:
            _p = _p.resolve()
        except Exception:
            pass
        if _p.parent == _p:
            logger.warning(f"clear_cache: root {_s}, skipping")
            return 0
        # (P1): NotADirectoryError guard -- cache_dir may point
        # at a file (corrupted config, manual edit). Path.glob on a
        # file raises; previously uncaught -> UI slot crash.
        if not _p.is_dir():
            logger.warning(
                f"clear_cache: not a directory {_s}, skipping")
            return 0
    except Exception:
        return 0
    count = 0
    # (P0): only touch files WE created (same reason as
    # clean_trash prefix in /). NB-115: module-level _CACHE_FILENAME_RE.
    try:
        for f in Path(cache_dir).glob("*.jpg"):
            if not _CACHE_FILENAME_RE.match(f.name):
                continue
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
    except OSError:
        pass
    return count


def get_file_size(filepath):
    try:
        return os.path.getsize(filepath)
    except OSError:
        return None


def get_file_hash(filepath, running_check=None):
    """Dedup signature: size + content (SHA-256).  # 
    - files <= 6MB: full-content hash (exact)
    - videos  >6MB: full-content hash (sampled hash false-positives real
      video pairs — two montage versions with identical head/middle/tail
      would collide, causing user to delete the wrong file)
    - photos  >6MB: 2MB head + 2MB middle + 2MB tail (photo false positives
      astronomically unlikely; photos are static, no "edited middle")"""
    try:
        size = os.path.getsize(filepath)
        sample = 2 * 1024 * 1024
        h = hashlib.sha256()
        h.update(str(size).encode())
        ext = os.path.splitext(filepath)[1].lower()
        # video always full-hashed — sampled collides on montage edits
        _full_required = (size <= sample * 3) or (ext in VIDEO_EXTS)
        with open(filepath, "rb") as f:
            if _full_required:
                # read in chunks to keep memory bounded on huge files
                while True:
                    # (NB-10): cooperative cancellation. Cancel
                    # during hash of a 4 GB video used to wait until
                    # the current f.read() chunk loop finished — could
                    # be minutes. Now abort between 4 MB chunks and
                    # return None; caller skips this file and its own
                    # _running check fires on the next iteration.
                    if running_check is not None and not running_check():
                        return None
                    chunk = f.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            else:
                # (extending NB-10): also check running_check
                # between samples. For photos >6MB on slow network
                # drives, 3×2MB reads may take seconds; cancel
                # would otherwise have to wait.
                if running_check is not None and not running_check():
                    return None
                h.update(f.read(sample))
                if running_check is not None and not running_check():
                    return None
                f.seek(size // 2 - sample // 2)
                h.update(f.read(sample))
                if running_check is not None and not running_check():
                    return None
                f.seek(-sample, os.SEEK_END)
                h.update(f.read(sample))
        return h.hexdigest()
    except Exception:
        return None


def get_cache_path(filepath, cache_dir):
    try:
        # (P0): avoid Path("")/Path(".") — would write to CWD.
        _s = str(cache_dir) if cache_dir is not None else ""
        if not _s or _s in (".", "") or not Path(_s).is_absolute():
            return None
        # (P0): reject root; resolve() for .. / symlink
        _p = Path(_s)
        try:
            _p = _p.resolve()
        except Exception:
            pass
        if _p.parent == _p:
            return None
        st = os.stat(filepath)
        key = hashlib.sha1(f"{filepath}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
        return Path(cache_dir) / f"{key}.jpg"
    except Exception:
        return None


def get_exif_date(filepath):
    """DateTimeOriginal (36867) lives in Exif sub-IFD 0x8769,
    not in IFD0. Read sub-IFD first, then fall back to IFD0 DateTime."""
    try:
        with Image.open(filepath) as img:
            exif = img.getexif()
            if not exif:
                return None
            candidates = []
            try:
                sub = exif.get_ifd(0x8769)
                if sub:
                    candidates.append(sub.get(36867))
                    candidates.append(sub.get(36868))
            except Exception:
                pass
            candidates.append(exif.get(306))
            candidates.append(exif.get(36867))
            candidates.append(exif.get(36868))
            for val in candidates:
                if not val:
                    continue
                if isinstance(val, bytes):
                    val = val.decode("ascii", errors="ignore")
                try:
                    return datetime.datetime.strptime(
                        str(val).strip(), "%Y:%m:%d %H:%M:%S").date()
                except ValueError:
                    continue
    except Exception as e:
        logger.debug(f"get_exif_date failed for {filepath}: {e}")
    return None


def _get_ctime_date(filepath):
    try:
        st = os.stat(filepath)
    except OSError:
        return None
    bt = getattr(st, "st_birthtime", None)
    if bt:
        try:
            return datetime.date.fromtimestamp(bt)
        except (OSError, ValueError):
            pass
    if platform.system() == "Windows":
        try:
            return datetime.date.fromtimestamp(st.st_ctime)
        except (OSError, ValueError):
            pass
    return None


def parse_folder_date(folder_name, fmt):
    """Tokenize a custom folder-name format like YYYY_MM_DD and search for it."""
    tokens = []
    i = 0
    n = len(fmt)
    while i < n:
        four = fmt[i:i+4]
        two = fmt[i:i+2]
        if four == "YYYY":
            tokens.append(("Y4", None)); i += 4
        elif two == "YY":
            tokens.append(("Y2", None)); i += 2
        elif two == "MM":
            tokens.append(("M2", None)); i += 2
        elif two == "DD":
            tokens.append(("D2", None)); i += 2
        elif fmt[i] == "Y":
            tokens.append(("Y4", None)); i += 1
        elif fmt[i] == "M":
            tokens.append(("M1", None)); i += 1
        elif fmt[i] == "D":
            tokens.append(("D1", None)); i += 1
        else:
            tokens.append(("lit", fmt[i])); i += 1

    parts = []
    seen = set()
    # NB-76: dedup by LETTER (Y/M/D), not by subtype. Previously
    # "MM M" produced two named groups (?P<M>...) — re.error — and
    # silently returned None. Subsequent occurrences of the same
    # letter now use an unnamed \d{1,4}.
    def _letter(kind):
        if kind.startswith("Y"):
            return "Y"
        if kind.startswith("M"):
            return "M"
        if kind.startswith("D"):
            return "D"
        return kind
    for kind, val in tokens:
        if kind == "lit":
            parts.append(re.escape(val))
            continue
        _k = _letter(kind)
        if _k in seen:
            parts.append(r"\d{1,4}")
            continue
        seen.add(_k)
        if kind == "Y4":
            parts.append(r"(?P<Y>\d{4})")
        elif kind == "Y2":
            parts.append(r"(?P<y>\d{2})")
        elif kind in ("M1", "M2"):
            parts.append(r"(?P<M>\d{1,2})")
        elif kind in ("D1", "D2"):
            parts.append(r"(?P<D>\d{1,2})")

    try:
        regex = "".join(parts)
        m = re.search(regex, folder_name)
    except re.error:
        return None
    if not m:
        return None
    gd = m.groupdict()
    try:
        y = gd.get("Y") or gd.get("y")
        if y and len(y) == 2:
            y = "20" + y if int(y) < 50 else "19" + y
        mo = gd.get("M")
        d = gd.get("D")
        if y and mo and d:
            return datetime.date(int(y), int(mo), int(d))
    except (ValueError, TypeError):
        pass
    return None


DEFAULT_FOLDER_FORMATS = ("YYYY_MM_DD", "YYYY-MM-DD", "YYYY.MM.DD")


def get_file_date(filepath, folder_name, priority_list):
    for prio in priority_list:
        if prio == PRIO_FOLDER and folder_name:
            for fmt in DEFAULT_FOLDER_FORMATS:
                r = parse_folder_date(folder_name, fmt)
                if r:
                    return r
        elif str(prio).startswith("custom:") and folder_name:
            fmt = str(prio).split(":", 1)[1]
            r = parse_folder_date(folder_name, fmt)
            if r:
                return r
        elif prio == PRIO_EXIF:
            r = get_exif_date(filepath)
            if r:
                return r
        elif prio == PRIO_CTIME:
            r = _get_ctime_date(filepath)
            if r:
                return r
        elif prio == PRIO_MTIME:
            try:
                return datetime.date.fromtimestamp(os.path.getmtime(filepath))
            except (OSError, ValueError):
                pass
    return None


def show_in_explorer(filepath):
    if not os.path.exists(filepath):
        return
    filepath = os.path.normpath(filepath)
    try:
        if platform.system() == "Windows":
            # explorer.exe does NOT understand Python's auto-quoting.
            # Pass a raw command string with explicit quotes around the path:
            #   explorer /select,"C:\path\file.ext"
            # Both `/select,` and quotes must be glued, no space before path.
            subprocess.Popen(f'explorer /select,"{filepath}"')
        elif platform.system() == "Darwin":
            subprocess.run(["open", "-R", filepath])
        else:
            # (P0): try freedesktop dbus first (highlights file),
            # fallback to xdg-open (opens folder only)
            try:
                subprocess.run([
                    "dbus-send", "--session",
                    "--dest=org.freedesktop.FileManager1",
                    "--type=method_call",
                    "/org/freedesktop/FileManager1",
                    "org.freedesktop.FileManager1.ShowItems",
                    f"array:string:{QUrl.fromLocalFile(filepath).toString()}",
                    "string:",
                ], check=True, timeout=3, capture_output=True)
            except Exception:
                subprocess.run(["xdg-open", os.path.dirname(filepath)],
                               timeout=3, capture_output=True)
    except Exception as e:
        logger.error(f"Cannot open file manager: {e}")


def _invalidate_quad_cache_async(cache_dir):
    """fire-and-forget cleanup of q_*.jpg (rotated images).

    (P0 data-loss): strict pattern + cache_dir validation.
    Previous glob("q_*.jpg") would delete ANY user file named
    q_*.jpg if cache_dir was mis-pointed at a photo folder --
    not into trash, unrecoverable. Now matches only
    ^q_[0-9a-f]{40}[.]jpg$ (ThumbnailLoader quad shape), same
    as clear_cache/_AutoCleanWorker (G37).
    """
    try:
        if cache_dir is None:
            return
        _s = str(cache_dir)
        if not _s or _s in (".", ""):
            logger.warning("invalidate_quad_cache: empty cache_dir")
            return
        cd = Path(_s)
        if not cd.is_absolute():
            logger.warning(
                f"invalidate_quad_cache: non-absolute {_s}")
            return
        try:
            cd = cd.resolve()
        except Exception:
            pass
        if cd.parent == cd:
            logger.warning(f"invalidate_quad_cache: root {_s}")
            return
        if not cd.is_dir():
            return
        # NB-115: module-level regex.
        class _Worker(QRunnable):
            def __init__(self, d, pat):
                super().__init__()
                self.d = d
                self.pat = pat
                self.setAutoDelete(True)
            @Slot()
            def run(self):
                try:
                    for qf in self.d.glob("q_*.jpg"):
                        if not self.pat.match(qf.name):
                            continue
                        try:
                            qf.unlink(missing_ok=True)
                        except OSError:
                            pass
                except Exception:
                    pass
        QThreadPool.globalInstance().start(_Worker(cd, _QUAD_CACHE_FILENAME_RE))
    except Exception as e:
        logger.debug(f"quad-cache worker start failed: {e}")


def _extract_video_frame(filepath, timeout=5.0):  # preview timeout
    result = {"img": None}
    # daemon thread — cv2.VideoCapture may hang on broken/network files
    def worker():
        # acquire semaphore
        # semaphore 1s — don't block worker for full preview timeout
        acquired = _VIDEO_SEMAPHORE.acquire(timeout=1.0)
        if not acquired:
            logger.warning(f"Video semaphore busy, skipping: {filepath}")
            return
        try:
            with suppress_stderr():
                cap = cv2.VideoCapture(filepath)
                try:
                    if not cap.isOpened():  # skip invalid cap
                        return
                    fps = cap.get(cv2.CAP_PROP_FPS) or 0
                    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                    if fps > 0 and total > 10:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * 0.1))
                    ret, frame = cap.read()
                    if not ret:
                        # fix cap.set
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret, frame = cap.read()
                    if ret:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        # NB-19: explicit .copy(). Pillow today
                        # copies the buffer internally, but future
                        # versions may not — cheap insurance.
                        result["img"] = Image.fromarray(frame).copy()
                finally:
                    cap.release()
        except Exception as e:
            logger.debug(f"_extract_video_frame exception: {e}")
        finally:
            # release semaphore
            _VIDEO_SEMAPHORE.release()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.warning(f"Video frame extraction timed out after {timeout}s: {filepath}")
        return None  # do not read result — worker may still write
    img = result["img"]
    if img is None:
        logger.info(f"Could not extract video frame from {filepath}")
    return img


def generate_quad_thumbnail(filepaths, size=600):
    canvas = Image.new("RGB", (size, size), (240, 242, 245))
    valid = [f for f in filepaths if os.path.exists(f)]
    n = len(valid)
    if n == 0:
        return canvas

    def _load(fp):
        ext = os.path.splitext(fp)[1].lower()
        if ext in VIDEO_EXTS:
            return _extract_video_frame(fp, timeout=2.0)
        with Image.open(fp) as raw:
            return ImageOps.exif_transpose(raw).convert("RGB")

    half = size // 2
    if n == 1:
        try:
            img = _load(valid[0])
            if img:
                img = ImageOps.fit(img, (size, size), method=Image.Resampling.LANCZOS)
                canvas.paste(img, (0, 0))
        except Exception: pass
    elif n == 2:
        for i, fp in enumerate(valid[:2]):
            try:
                img = _load(fp)
                if img:
                    img = ImageOps.fit(img, (half, size), method=Image.Resampling.LANCZOS)
                    canvas.paste(img, (i * half, 0))
            except Exception: pass
    elif n == 3:
        try:
            img = _load(valid[0])
            if img:
                img = ImageOps.fit(img, (size, half), method=Image.Resampling.LANCZOS)
                canvas.paste(img, (0, 0))
        except Exception: pass
        for i, fp in enumerate(valid[1:3]):
            try:
                img = _load(fp)
                if img:
                    img = ImageOps.fit(img, (half, half), method=Image.Resampling.LANCZOS)
                    canvas.paste(img, (i * half, half))
            except Exception: pass
    else:
        for i, fp in enumerate(valid[:4]):
            try:
                img = _load(fp)
                if img:
                    img = ImageOps.fit(img, (half, half), method=Image.Resampling.LANCZOS)
                    canvas.paste(img, ((i % 2) * half, (i // 2) * half))
            except Exception: pass
    return canvas


# ============================================================
#                     THREADS
# ============================================================
class _AutoCleanWorker(QRunnable):
    """Runs cache/trash cleanup off the UI thread."""
    def __init__(self, cache_dir, cache_days, trash_days):
        super().__init__()
        self.cache_dir = Path(cache_dir)
        self.cache_days = cache_days
        self.trash_days = trash_days
        self.setAutoDelete(True)

    @Slot()
    def run(self):
        try:
            # (P0): refuse empty/dot paths — Path("") == Path(".")
            # and would unlink *.jpg in CWD.
            _cd_str = str(self.cache_dir)
            if not _cd_str or _cd_str in (".", ""):
                logger.warning("auto_clean: empty cache_dir, skipping")
                # (NB-33): still clean trash even with bad cache_dir.
                clean_trash(days=self.trash_days)
                return
            # (P0): reject root — C:\ glob("*.jpg") wipes
            # every JPG in the drive root.
            if self.cache_dir.parent == self.cache_dir:
                logger.warning(
                    "auto_clean: root cache_dir rejected, skipping")
                clean_trash(days=self.trash_days)
                return
            # (NB-33): cache_days == 0 signals user disabled cache
            # auto-clean; trash retention is independent (see D10).
            # (P1): also require is_absolute and not-root after
            # resolve() -- defensive against callers that bypass
            # load_config / on_cache_settings_changed.
            if not self.cache_dir.is_absolute():
                logger.warning(
                    "auto_clean: non-absolute cache_dir, skipping")
                clean_trash(days=self.trash_days)
                return
            _resolved_cd = self.cache_dir
            try:
                _resolved_cd = self.cache_dir.resolve()
            except Exception:
                pass
            if _resolved_cd.parent == _resolved_cd:
                logger.warning(
                    "auto_clean: root cache_dir, skipping")
                clean_trash(days=self.trash_days)
                return
            if (self.cache_days > 0
                    and self.cache_dir.exists()
                    and self.cache_dir.is_dir()):
                cutoff = time.time() - self.cache_days * 86400
                # (P0): only touch our cache files (see
                # clear_cache comment). NB-115: module-level regex.
                for f in self.cache_dir.glob("*.jpg"):
                    if not _CACHE_FILENAME_RE.match(f.name):
                        continue
                    try:
                        if f.stat().st_mtime < cutoff:
                            f.unlink()
                    except OSError:
                        pass
            clean_trash(days=self.trash_days)
        except Exception:
            logger.exception("auto_clean failed")


class ScannerThread(QThread):
    progress = Signal(int, int)
    file_found = Signal(str, str)

    def __init__(self, root_path, priority_list):
        super().__init__()
        self.root_path = root_path
        self.priority_list = priority_list
        self._running = True

    def stop(self):
        self._running = False
        return self.wait(5000)  # return wait result 

    def run(self):
        total = 0
        try:
            for root, dirs, files in os.walk(self.root_path, followlinks=False):
                # (NB-30): cooperative cancel during counting phase.
                # Previously: first os.walk ignored _running, so stop()
                # waited up to 5s, cancel_scan returned False, dialog
                # stayed up. On slow network drives counting alone can
                # take 10+ seconds.
                if not self._running:
                    return
                dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
                total += sum(1 for f in files if f.lower().endswith(VALID_EXTS_TUPLE))
        except Exception:
            pass
        if not self._running:
            return
        self.progress.emit(0, total)
        processed = 0
        try:
            for root, dirs, files in os.walk(self.root_path, followlinks=False):
                if not self._running:
                    break
                dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
                folder_name = os.path.basename(root)
                for file in files:
                    if not self._running:
                        break
                    if not file.lower().endswith(VALID_EXTS_TUPLE):
                        continue
                    filepath = os.path.join(root, file)
                    date_obj = get_file_date(filepath, folder_name, self.priority_list)
                    if date_obj:
                        self.file_found.emit(filepath, date_obj.strftime("%Y-%m-%d"))
                    processed += 1
                    if processed % 5 == 0:
                        self.progress.emit(processed, total)
        except Exception as e:
            logger.exception(f"Scan error: {e}")
        if self._running:
            self.progress.emit(total, total)


class DuplicateScanThread(QThread):
    progress = Signal(int, int)
    scan_done = Signal(dict)

    def __init__(self, filepaths):
        super().__init__()
        self.filepaths = list(filepaths)
        self._running = True

    def stop(self):
        self._running = False
        self.wait(5000)

    def run(self):
        try:
            size_groups = {}
            total = len(self.filepaths)
            for i, fp in enumerate(self.filepaths):
                if not self._running:
                    return
                size = get_file_size(fp)
                if size is not None:
                    size_groups.setdefault(size, []).append(fp)
                if i % 100 == 0:
                    self.progress.emit(i, total)

            candidates = [g for g in size_groups.values() if len(g) > 1]
            total_hash = sum(len(g) for g in candidates)
            # NB-09: hashing continues after sizing. Emit against
            # a single monotonic base so the progress bar keeps
            # climbing instead of appearing to reset when phase
            # switches from sizing (i / total) to hashing
            # (done / total_hash).
            _overall = total + total_hash
            hashes = {}
            done = 0
            for group in candidates:
                if not self._running:
                    return
                for fp in group:
                    if not self._running:
                        return
                    h = get_file_hash(
                        fp, running_check=lambda: self._running)
                    if h:
                        hashes.setdefault(h, []).append(fp)
                    done += 1
                    if done % 5 == 0:
                        self.progress.emit(total + done, _overall)
            dups = {k: v for k, v in hashes.items() if len(v) > 1}
            if self._running:
                self.scan_done.emit(dups)
        except Exception as e:
            logger.exception(f"Dedup error: {e}")
            if self._running:
                self.scan_done.emit({})  # unblock UI on error


# ============================================================
#                     THUMBNAILS
# ============================================================
class ThumbnailSignals(QObject):
    result = Signal(QImage, str)


class ThumbnailLoader(QRunnable):
    def __init__(self, filepaths, key=None, is_single=False, cache_dir=None):
        super().__init__()
        self.filepaths = filepaths
        self.key = key
        self.is_single = is_single
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self.signals = ThumbnailSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self):
        qimg = QImage()
        # NB-112: validate cache_dir before any path build. Single-
        # file branch goes through get_cache_path() (which has root /
        # CWD guards); quad branch built cache_path directly and
        # bypassed those checks. Defence-in-depth: if load_config
        # was ever bypassed (manual config edit, future refactor),
        # quad write would land in CWD or drive root.
        try:
            _cs = str(self.cache_dir)
            if (not _cs or _cs in (".", "")
                    or not Path(_cs).is_absolute()):
                logger.warning(
                    f"ThumbnailLoader: bad cache_dir {_cs!r}")
                try:
                    self.signals.result.emit(qimg, self.key)
                except RuntimeError:
                    pass
                return
            _cp = Path(_cs).resolve()
            if _cp.parent == _cp:
                logger.warning(
                    f"ThumbnailLoader: root cache_dir {_cs}")
                try:
                    self.signals.result.emit(qimg, self.key)
                except RuntimeError:
                    pass
                return
        except Exception:
            pass
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            if self.is_single:
                filepath = self.filepaths[0]
                cache_path = get_cache_path(filepath, self.cache_dir)
                if cache_path and cache_path.exists():
                    cached = QImage(str(cache_path))
                    if not cached.isNull():
                        qimg = cached
                    else:
                        try:
                            cache_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                if qimg.isNull():
                    ext = os.path.splitext(filepath)[1].lower()
                    if ext in VIDEO_EXTS:
                        img = _extract_video_frame(filepath)
                    else:
                        with Image.open(filepath) as raw:
                            # HEIC: pillow-heif may return non-8-bit
                            # modes (10-bit, CMYK) that LANCZOS/fit
                            # cannot process. Convert to RGB first.
                            img = ImageOps.exif_transpose(raw).convert("RGB")
                    if img:
                        # (NB-04): .convert("RGB") — RGBA/LA/P tobytes
                        # ("raw","RGB") raises ValueError → thumbnail was
                        # permanently "..." for transparent PNG/WebP.
                        # Same fix as generate_quad_thumbnail.
                        img = ImageOps.fit(
                            img, (400, 400),
                            method=Image.Resampling.LANCZOS)
                        data = img.tobytes("raw", "RGB")
                        qimg = QImage(data, img.width, img.height, img.width * 3,
                                      QImage.Format.Format_RGB888).copy()
                        if cache_path:
                            try:
                                qimg.save(str(cache_path), "JPEG", 78)
                            except Exception:
                                pass
            else:
                key_parts = "|".join(self.filepaths[:4])
                # include mtime so rotated files invalidate cache
                try:
                    mtimes = [str(os.stat(f).st_mtime_ns) for f in self.filepaths[:4] if os.path.exists(f)]
                    key_parts += "|" + "|".join(mtimes)
                except OSError:
                    pass
                key_hash = hashlib.sha1(key_parts.encode()).hexdigest()
                cache_path = self.cache_dir / f"q_{key_hash}.jpg"
                if cache_path.exists():
                    cached = QImage(str(cache_path))
                    if not cached.isNull():
                        qimg = cached
                    else:
                        try:
                            cache_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                if qimg.isNull():
                    pil_img = generate_quad_thumbnail(self.filepaths)
                    data = pil_img.tobytes("raw", "RGB")
                    qimg = QImage(data, pil_img.width, pil_img.height, pil_img.width * 3,
                                  QImage.Format.Format_RGB888).copy()
                    try:
                        qimg.save(str(cache_path), "JPEG", 78)
                    except Exception:
                        pass
        except Exception as e:
            # HEIC: elevated to warning so thumbnail failures
            # show up in app.log without a full logging overhaul.
            logger.warning(f"Thumbnail error: {e}")
        try:
            self.signals.result.emit(qimg, self.key)
        except RuntimeError:
            pass


# ============================================================
#                     CALENDAR CARD
# ============================================================
class CalendarCardItem(QGraphicsItem):
    def __init__(self, key, count, x, y, size):
        super().__init__()
        self.key = key
        self.count = count
        self.size = size
        self.pixmap = None
        self._hover = False
        self.setPos(x, y)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAcceptHoverEvents(True)

    def boundingRect(self):
        m = 8
        return QRectF(-m, -m, self.size + 2 * m, self.size + 2 * m)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(0, 0, self.size, self.size)
        hover = self._hover
        shadow_alpha = int(15 + 45 * hover) if CURRENT_THEME == "light" else int(5 + 15 * hover)
        shadow_offset = 2 + 2 * hover
        painter.setBrush(QColor(0, 0, 0, shadow_alpha))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(
            rect.translated(shadow_offset * 0.4, shadow_offset * 0.6).adjusted(2, 2, 2, 2), 14, 14)

        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        painter.setClipPath(path)
        painter.fillPath(path, QColor(COLOR_SURFACE))

        # NB-11: Null QPixmap is truthy in Qt — use isNull().
        # Before: .scaled() ran on null pixmap, drew nothing, wasted CPU.
        if self.pixmap is not None and not self.pixmap.isNull():
            scaled = self.pixmap.scaled(self.size, self.size,
                                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        Qt.TransformationMode.SmoothTransformation)
            x_off = (scaled.width() - self.size) // 2
            y_off = (scaled.height() - self.size) // 2
            painter.drawPixmap(-x_off, -y_off, scaled)
        else:
            painter.setPen(QColor(COLOR_MUTED))
            painter.setFont(QFont(FONT_FAMILY, 11))
            painter.drawText(QRectF(0, 0, self.size, self.size),
                             Qt.AlignmentFlag.AlignCenter, t("no_preview"))

        if self.count > 0:
            badge_h = 26
            text = f" {self.count} "
            painter.setFont(QFont(FONT_FAMILY, 10, QFont.Weight.Bold))
            fm = painter.fontMetrics()
            badge_w = max(badge_h, fm.horizontalAdvance(text) + 16)
            badge_rect = QRectF(self.size - badge_w - 8, self.size - badge_h - 8, badge_w, badge_h)
            painter.setBrush(QColor(0, 0, 0, 200))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(badge_rect, badge_h / 2, badge_h / 2)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, str(self.count))

        painter.setClipping(False)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if hover:
            painter.setPen(QPen(QColor(COLOR_PRIMARY), 2.0))
        else:
            painter.setPen(QPen(QColor(COLOR_BORDER), 1.0))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)


# ============================================================
#                     CALENDAR VIEW
# ============================================================
class CalendarView(QGraphicsView):
    item_selected = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.items = {}
        self.threadpool = QThreadPool()
        self.threadpool.setMaxThreadCount(4)
        self.setAcceptDrops(False)
        self.current_mode = "days"
        self.cache_dir = None
        self._pending = []
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(50)
        self._load_timer.timeout.connect(self._load_visible)
        self.verticalScrollBar().valueChanged.connect(self._schedule)
        self.horizontalScrollBar().valueChanged.connect(self._schedule)
        self.update_theme()

    def update_theme(self):
        self.setStyleSheet(f"border: none; background-color: {COLOR_BG};")

    def _schedule(self, *_):  # unified signature
        if self._pending:
            self._load_timer.start()

    def _load_visible(self):
        if not self._pending:
            return
        vp = self.viewport().rect()
        src = self.mapToScene(vp).boundingRect()
        still = []
        loaded = 0
        for item, fps in self._pending:
            try:
                rect = item.sceneBoundingRect()
            except RuntimeError:
                # catch RuntimeError — item deleted, drop it
                continue
            if rect.intersects(src):
                if loaded < 4:
                    w = ThumbnailLoader(fps, key=item.key, is_single=False, cache_dir=self.cache_dir)
                    w.signals.result.connect(self.on_loaded)
                    self.threadpool.start(w)
                    loaded += 1
                else:
                    still.append((item, fps))
            else:
                still.append((item, fps))
        self._pending = still
        # NB-23: only re-arm when this tick actually loaded
        # something. If loaded==0, all remaining items are off-screen;
        # a scroll/resize will call _schedule again. Otherwise the
        # timer spins every 50ms doing O(n) sceneBoundingRect calls.
        if self._pending and loaded > 0:
            self._load_timer.start()

    def update_data(self, data, categories, mode, cache_dir):
        self.scene_.clear()
        self.items.clear()
        self._pending.clear()
        self.current_mode = mode
        self.cache_dir = cache_dir
        if not data and mode != "events":
            self._draw_empty()
        elif mode == "days":
            self.draw_days(data)
        elif mode == "months":
            self.draw_months(data)
        elif mode == "years":
            self.draw_years(data)
        elif mode == "events":
            self.draw_events(categories)
            # NB-107: no visible cards = empty state. Previously
            # Events view with zero (or only-missing) categories
            # rendered an empty scene — inconsistent with Days /
            # Months / Years which show "Nothing here yet".
            if not self.items:
                self._draw_empty()
        self.setSceneRect(self.scene_.itemsBoundingRect())
        self._schedule()

    # draw_loading_state removed 

    def _draw_empty(self):
        h = QGraphicsTextItem("📷")
        h.setFont(QFont(FONT_FAMILY, 72))
        h.setDefaultTextColor(QColor(COLOR_MUTED))
        h.setPos(0, 40)
        self.scene_.addItem(h)
        txt = QGraphicsTextItem(t("empty_state"))
        txt.setFont(QFont(FONT_FAMILY, 15))
        txt.setDefaultTextColor(QColor(COLOR_MUTED))
        txt.setPos(0, 160)
        self.scene_.addItem(txt)

    def _add_month_header(self, title, y):
        h = QGraphicsTextItem(title)
        h.setFont(QFont(FONT_FAMILY, 26, QFont.Weight.Bold))
        h.setDefaultTextColor(QColor(COLOR_TEXT))
        h.setPos(0, y)
        self.scene_.addItem(h)
        a = QGraphicsTextItem("▔▔▔▔▔▔▔▔▔▔▔")
        a.setFont(QFont(FONT_FAMILY, 8))
        a.setDefaultTextColor(QColor(COLOR_PRIMARY))
        a.setPos(0, y + 38)
        self.scene_.addItem(a)
        return y + 58

    def draw_days(self, data):
        _date_re = re.compile(r'^\d{4}-\d{2}-\d{2}$')
        dates = sorted(k for k in data.keys() if isinstance(k, str) and _date_re.match(k))
        months = {}
        for d in dates:
            months.setdefault(d[:7], []).append(d)
        y = 20
        cell = 180
        sp = 15
        colw = cell + sp
        for mkey, days in months.items():
            year, month = map(int, mkey.split("-"))
            y = self._add_month_header(f"{month_name(month)} {year}", y)
            for i in range(7):
                wd = QGraphicsTextItem(weekday_name(i))
                wd.setFont(QFont(FONT_FAMILY, 11, QFont.Weight.Bold))
                wd.setDefaultTextColor(QColor(COLOR_DANGER if i >= 5 else COLOR_MUTED))
                wd.setPos(i * colw + 10, y)
                self.scene_.addItem(wd)
            y += 26
            first = datetime.date(year, month, 1)
            first_wd = first.weekday()
            for day_str in days:
                dd = datetime.datetime.strptime(day_str, "%Y-%m-%d").date()
                if dd.month != month:
                    continue
                col = dd.weekday()
                row = (dd.day + first_wd - 1) // 7
                item = CalendarCardItem(day_str, len(data[day_str]),
                                        col * colw, y + row * (cell + sp), cell)
                self.scene_.addItem(item)
                self.items[day_str] = item
                if data[day_str]:
                    self._pending.append((item, data[day_str][:4]))
            rows_used = 0
            if days:
                last = datetime.datetime.strptime(days[-1], "%Y-%m-%d").date()
                rows_used = (last.day + first_wd - 1) // 7 + 1
            y += max(1, rows_used) * (cell + sp) + 30

    def draw_months(self, data):
        _date_re = re.compile(r'^\d{4}-\d{2}-\d{2}$')
        years = {}
        for ds, files in data.items():
            if not isinstance(ds, str) or not _date_re.match(ds) or not isinstance(files, list):
                continue
            years.setdefault(ds[:4], {}).setdefault(ds[:7], []).extend(files)  # filtered
        y = 20
        cell = 180
        sp = 15
        colw = cell + sp
        for ykey in sorted(years.keys()):
            y = self._add_month_header(f"{ykey}", y)
            md = years[ykey]
            for row in range(2):
                for col in range(6):
                    mi = row * 6 + col
                    if mi >= 12:
                        continue
                    hh = QGraphicsTextItem(month_name(mi + 1))
                    hh.setFont(QFont(FONT_FAMILY, 11, QFont.Weight.Bold))
                    hh.setDefaultTextColor(QColor(COLOR_MUTED))
                    hh.setPos(col * colw + 10, y)
                    self.scene_.addItem(hh)
                y += 26
                for col in range(6):
                    mi = row * 6 + col
                    if mi >= 12:
                        continue
                    ms = f"{ykey}-{mi+1:02d}"
                    if ms in md:
                        files = md[ms]
                        item = CalendarCardItem(ms, len(files), col * colw, y, cell)
                        self.scene_.addItem(item)
                        self.items[ms] = item
                        if files:
                            self._pending.append((item, files[:4]))
                y += cell + sp
            y += 30

    def draw_years(self, data):
        _date_re = re.compile(r'^\d{4}-\d{2}-\d{2}$')
        years = {}
        for ds, files in data.items():
            if not isinstance(ds, str) or not _date_re.match(ds) or not isinstance(files, list):
                continue
            years.setdefault(ds[:4], []).extend(files)  # filtered
        y = 20
        cell = 220
        sp = 20
        colw = cell + sp
        rowh = cell + 40 + sp
        cols = 5
        col = 0
        row = 0
        for ykey in sorted(years.keys()):
            files = years[ykey]
            x = col * colw
            yy = y + row * rowh
            h = QGraphicsTextItem(f"{ykey}")
            h.setFont(QFont(FONT_FAMILY, 15, QFont.Weight.Bold))
            h.setDefaultTextColor(QColor(COLOR_TEXT))
            h.setPos(x + 8, yy)
            self.scene_.addItem(h)
            item = CalendarCardItem(ykey, len(files), x, yy + 34, cell)
            self.scene_.addItem(item)
            self.items[ykey] = item
            if files:
                self._pending.append((item, files[:4]))
            col += 1
            if col >= cols:
                col = 0
                row += 1

    def draw_events(self, categories):
        y = 20
        cell = 180
        sp = 15
        colw = cell + sp
        rowh = cell + 26 + sp  # header 26 + cell + spacing (was overlapping)
        cols = 6
        col = 0
        row = 0
        for ev_name, files in categories.items():
            valid = [f for f in files if os.path.exists(f)]
            if not valid:
                continue
            x = col * colw
            yy = y + row * rowh
            h = QGraphicsTextItem(ev_name)
            h.setFont(QFont(FONT_FAMILY, 11, QFont.Weight.Bold))
            h.setDefaultTextColor(QColor(COLOR_MUTED))
            h.setPos(x + 10, yy)
            self.scene_.addItem(h)
            item = CalendarCardItem(ev_name, len(valid), x, yy + 26, cell)
            self.scene_.addItem(item)
            self.items[ev_name] = item
            self._pending.append((item, valid[:4]))
            col += 1
            if col >= cols:
                col = 0
                row += 1

    @Slot(QImage, str)
    def on_loaded(self, qimg, key):
        if key in self.items and not qimg.isNull():
            try:
                self.items[key].pixmap = QPixmap.fromImage(qimg)
                self.items[key].update()
            except RuntimeError:
                pass

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, CalendarCardItem):
            self.item_selected.emit(item.key, self.current_mode)
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            f = 1.15 if event.angleDelta().y() > 0 else 0.85
            self.scale(f, f)
            event.accept()
        else:
            super().wheelEvent(event)

    def resizeEvent(self, event):
        # re-schedule visible load — after resize, previously
        # off-screen cards may now be in viewport. Without this they
        # stay placeholder until user scrolls manually.
        super().resizeEvent(event)
        self._schedule()


# ============================================================
#                     GALLERY
# ============================================================
class GalleryListWidget(QListWidget):
    files_moved_externally = Signal(list)  # drag-Move to Explorer

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.current_icon_size = 200
        self.setIconSize(QSize(self.current_icon_size, self.current_icon_size))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setSpacing(10)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setWordWrap(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._file_to_item = {}
        self._pending = set()
        self._retry_done = set()  # NB-13: cap retry at 1 per file
        self.threadpool = QThreadPool()
        self.threadpool.setMaxThreadCount(4)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._load_visible)
        self.verticalScrollBar().valueChanged.connect(self._schedule)
        self.cache_dir = None
        self.columns = 1
        self.update_theme()

    def update_theme(self):
        self.setStyleSheet(
            f"QListWidget {{ background-color: transparent; border: none; }} "
            f"QListWidget::item {{ background-color: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; "
            f"border-radius: 12px; padding: 8px; margin: 5px; color: {COLOR_TEXT}; }} "
            f"QListWidget::item:hover {{ border: 1px solid {COLOR_PRIMARY}; background-color: {COLOR_HOVER}; }} "
            f"QListWidget::item:selected {{ border: 2px solid {COLOR_PRIMARY}; "
            f"background-color: {COLOR_PRIMARY_LT}; color: {COLOR_TEXT}; }}")

    def clear_all(self):
        self.clear()
        self._file_to_item.clear()
        self._pending.clear()
        self._retry_done.clear()  # NB-13

    def remove_item_by_filepath(self, filepath):
        self._pending.discard(filepath)
        self._retry_done.discard(filepath)  # NB-13
        item = self._file_to_item.pop(filepath, None)
        if item:
            item.setIcon(QIcon())
            row = self.row(item)
            if row >= 0:
                self.takeItem(row)

    def update_thumbnail_for_file(self, filepath):
        self._pending.add(filepath)
        self._schedule()

    def add_header(self, title):
        item = QListWidgetItem(title)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setFont(QFont(FONT_FAMILY, 15, QFont.Weight.Bold))
        item.setBackground(QColor(COLOR_BG))
        item.setForeground(QColor(COLOR_TEXT))
        item.setData(ROLE_FILEPATH, None)
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.addItem(item)

    def add_file(self, filepath, is_dup):
        text = os.path.basename(filepath)
        if is_dup:
            text = "⚠️ " + text
        item = QListWidgetItem(text)
        item.setData(ROLE_FILEPATH, filepath)
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        item.setSizeHint(QSize(self.current_icon_size + 20, self.current_icon_size + 50))
        if is_dup:
            item.setForeground(QColor(COLOR_DANGER))
        item.setToolTip(os.path.basename(filepath))
        self._file_to_item[filepath] = item
        self._pending.add(filepath)
        self.addItem(item)

    def start_loading(self):
        self._update_headers()
        self._schedule()

    def _schedule(self, *_):
        if self._pending:
            self._timer.start()

    def _load_visible(self):
        if not self._pending:
            return
        vp = self.viewport().rect()
        vis = []
        for i in range(self.count()):
            item = self.item(i)
            fp = item.data(ROLE_FILEPATH)
            if fp is None or fp not in self._pending:
                continue
            if not self.visualItemRect(item).intersects(vp):
                continue
            vis.append((item, fp))
        for item, fp in vis[:5]:
            self._pending.discard(fp)
            item.setData(ROLE_LOADED, True)
            w = ThumbnailLoader([fp], key=fp, is_single=True, cache_dir=self.cache_dir)
            w.signals.result.connect(self._on_thumb)
            self.threadpool.start(w)
        if len(vis) > 5:
            self._timer.start(50)

    @Slot(QImage, str)
    def _on_thumb(self, qimg, filepath):
        if qimg.isNull():
            # NB-13: one retry per file on transient failure
            # (auto-clean race, AV lock, network blip). Previously
            # the file stayed at "..." forever until scroll/resize.
            if filepath not in self._retry_done:
                self._retry_done.add(filepath)
                if filepath not in self._pending:
                    self._pending.add(filepath)
                self._schedule()
            return
        item = self._file_to_item.get(filepath)
        if item is None:
            return
        try:
            pixmap = QPixmap.fromImage(qimg)
            ext = os.path.splitext(filepath)[1].lower()
            if ext in VIDEO_EXTS:
                p = QPainter(pixmap)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                c = QPoint(pixmap.width() // 2, pixmap.height() // 2)
                r = max(pixmap.width(), pixmap.height()) // 8
                p.setBrush(QColor(0, 0, 0, 160))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(c, r, r)
                p.setBrush(QColor(255, 255, 255))
                # QPolygonF expects QPointF; QPoint worked via implicit
                # conversion but is not guaranteed across PySide6 versions
                p.drawPolygon(QPolygonF([
                    QPointF(c.x() - r // 3, c.y() - r // 2),
                    QPointF(c.x() - r // 3, c.y() + r // 2),
                    QPointF(c.x() + r // 2, c.y()),
                ]))
                p.end()
            item.setIcon(QIcon(pixmap))
        except RuntimeError:
            pass

    def _update_headers(self):
        w = self.viewport().width() - 30
        for i in range(self.count()):
            it = self.item(i)
            if it.data(ROLE_FILEPATH) is None:
                it.setSizeHint(QSize(w, 35))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_headers()
        self._schedule()

    def startDrag(self, supportedActions):
        items = self.selectedItems()
        if not items:
            return
        # snapshot BEFORE drag.exec(). Its nested event loop lets
        # _cut_watch_timer fire and rebuild the gallery via clear_all() —
        # touching C++ QListWidgetItem after that raises RuntimeError.
        paths_snapshot = [it.data(ROLE_FILEPATH) for it in items
                          if it.data(ROLE_FILEPATH)]
        icon_snapshot = None
        try:
            icon_snapshot = items[0].icon()
        except RuntimeError:
            pass
        mime = QMimeData()
        dragged = [p for p in paths_snapshot if os.path.exists(p)]
        if not dragged:
            return
        mime.setUrls([QUrl.fromLocalFile(p) for p in dragged])
        drag = QDrag(self)
        drag.setMimeData(mime)
        # QIcon has no __bool__ — explicitly check isNull
        if icon_snapshot is not None and not icon_snapshot.isNull():
            try:
                drag.setPixmap(icon_snapshot.pixmap(64, 64))
            except RuntimeError:
                pass
        result = drag.exec(
            Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
        if result == Qt.DropAction.MoveAction:
            self.files_moved_externally.emit(dragged)

    def wheelEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            d = event.angleDelta().y()
            if d > 0:
                self.current_icon_size = min(400, self.current_icon_size + 20)
            else:
                self.current_icon_size = max(100, self.current_icon_size - 20)
            self.setIconSize(QSize(self.current_icon_size, self.current_icon_size))
            for i in range(self.count()):
                item = self.item(i)
                if item.data(ROLE_FILEPATH) is not None:
                    item.setSizeHint(QSize(self.current_icon_size + 20, self.current_icon_size + 50))
            event.accept()
        else:
            super().wheelEvent(event)


class DuplicateThumbnail(QLabel):
    def __init__(self, filepath, cache_dir, icon_size, threadpool=None):
        super().__init__()
        self.filepath = filepath
        self.icon_size = icon_size
        self.is_selected = False
        self.setFixedSize(icon_size, icon_size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setText("...")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(f"background-color: {COLOR_BG}; border: 1px solid {COLOR_BORDER}; border-radius: 6px;")
        self.original_pixmap = QPixmap()
        self._retry_done = False
        self._cache_dir = cache_dir
        self._threadpool = threadpool
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._load_thumb()

    def _load_thumb(self):
        if self._threadpool is None:
            return
        try:
            w = ThumbnailLoader(
                [self.filepath], key=self.filepath,
                is_single=True, cache_dir=self._cache_dir)
            w.signals.result.connect(self._on_thumb)
            self._threadpool.start(w)
        except Exception as e:
            logger.debug(f"DuplicateThumbnail load failed: {e}")

    def _on_thumb(self, qimg, _):
        try:
            if not qimg.isNull():
                self.original_pixmap = QPixmap.fromImage(qimg)
                # NB-39: video play overlay — same style as
                # GalleryListWidget._on_thumb. Without it users could
                # not tell video from image in Duplicates view until
                # they opened the file. Overlay is baked into
                # original_pixmap (400x400 from ThumbnailLoader);
                # _update_pixmap scales it down with icon_size.
                _ext = os.path.splitext(self.filepath)[1].lower()
                if _ext in VIDEO_EXTS:
                    _w = self.original_pixmap.width()
                    _h = self.original_pixmap.height()
                    _r = max(_w, _h) // 8
                    if _r > 0:
                        p = QPainter(self.original_pixmap)
                        p.setRenderHint(QPainter.RenderHint.Antialiasing)
                        c = QPoint(_w // 2, _h // 2)
                        p.setBrush(QColor(0, 0, 0, 160))
                        p.setPen(Qt.PenStyle.NoPen)
                        p.drawEllipse(c, _r, _r)
                        p.setBrush(QColor(255, 255, 255))
                        p.drawPolygon(QPolygonF([
                            QPointF(c.x() - _r // 3, c.y() - _r // 2),
                            QPointF(c.x() - _r // 3, c.y() + _r // 2),
                            QPointF(c.x() + _r // 2, c.y()),
                        ]))
                        p.end()
                self._update_pixmap()
        except RuntimeError:
            pass

    def _update_pixmap(self):
        try:
            if not self.original_pixmap.isNull():
                pix = self.original_pixmap.scaled(self.icon_size, self.icon_size,
                                                  Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation)
                self.setPixmap(pix)
        except RuntimeError:
            pass

    def set_icon_size(self, size):
        self.icon_size = size
        # (re-apply ): C++ object may already be deleted
        # (deleteLater queued) while Ctrl+wheel iterates cards.
        try:
            self.setFixedSize(size, size)
        except RuntimeError:
            return
        self._update_pixmap()

    def set_selected(self, selected):
        self.is_selected = selected
        border = f"2px solid {COLOR_PRIMARY}" if selected else f"1px solid {COLOR_BORDER}"
        self.setStyleSheet(f"background-color: {COLOR_BG}; border: {border}; border-radius: 6px;")


class DuplicateGroupWidget(QFrame):
    def __init__(self, group_name, files, cache_dir, thumb_size, gallery, threadpool=None):
        super().__init__()
        self.files = files
        self.thumb_size = thumb_size
        self.setStyleSheet("QFrame { background-color: transparent; border: none; }")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.lbl = QLabel(group_name)
        self.lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: bold; font-size: 13px;")
        self.lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.lbl)
        self.inner_grid = QGridLayout()
        self.inner_grid.setSpacing(10)
        self.thumbs = []
        for i, f in enumerate(files):
            thumb = DuplicateThumbnail(f, cache_dir, thumb_size, threadpool=threadpool)
            self.inner_grid.addWidget(thumb, i // 3, i % 3)
            self.thumbs.append(thumb)
        self.inner_grid.setColumnStretch(3, 1)
        layout.addLayout(self.inner_grid)
        self.update_size()

    def update_size(self):
        cols = min(3, len(self.thumbs)) or 1
        w = cols * self.thumb_size + (cols - 1) * 10
        rows = (len(self.thumbs) + 2) // 3
        h = rows * self.thumb_size + (rows - 1) * 10 + 30
        self.setFixedSize(w, h)

    def set_thumb_size(self, size):
        self.thumb_size = size
        # (re-apply ): group widget may be deleted mid-iteration
        try:
            for th in self.thumbs:
                th.set_icon_size(size)
            self.update_size()
        except RuntimeError:
            pass


class DuplicateInnerWidget(QWidget):
    def __init__(self, gallery):
        super().__init__()
        self.gallery = gallery
        self.setMouseTracking(True)

    def mousePressEvent(self, e): self.gallery.on_mouse_press(e)
    def mouseMoveEvent(self, e): self.gallery.on_mouse_move(e)
    def mouseReleaseEvent(self, e): self.gallery.on_mouse_release(e)
    def mouseDoubleClickEvent(self, e): self.gallery.on_mouse_double_click(e)
    def contextMenuEvent(self, e): self.gallery.on_context_menu(e)


class DuplicateGalleryWidget(QScrollArea):
    open_image = Signal(str, list)
    context_menu_requested = Signal(QPoint, str)

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.inner = DuplicateInnerWidget(self)
        self.grid = QGridLayout(self.inner)
        self.grid.setSpacing(15)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.setWidget(self.inner)
        self.cards = []
        self.thumbnails = []
        self.thumb_size = 150
        self.groups = []
        self.cache_dir = None
        self.cols = 2
        self.rubberBand = QRubberBand(QRubberBand.Shape.Rectangle, self.inner)
        self.origin = QPoint()
        self.is_selecting = False
        # own threadpool 
        self.threadpool = QThreadPool()
        self.threadpool.setMaxThreadCount(4)

    def get_thumb_at(self, pos):
        for t in self.thumbnails:
            try:
                tr_ = QRect(t.mapTo(self.inner, QPoint(0, 0)), t.size())
                if tr_.contains(pos):
                    return t
            except RuntimeError:
                continue
        return None

    def on_mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # QMouseEvent.pos() is deprecated in Qt6
            pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
            clicked = self.get_thumb_at(pos)
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if clicked:
                if not ctrl:
                    for th in self.thumbnails:
                        th.set_selected(False)
                clicked.set_selected(True)
            else:
                if not ctrl:
                    for th in self.thumbnails:
                        th.set_selected(False)
            self.origin = pos
            self.rubberBand.setGeometry(QRect(self.origin, QSize()))
            self.rubberBand.show()
            self.is_selecting = True

    def on_mouse_move(self, event):
        if self.is_selecting:
            _pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
            rect = QRect(self.origin, _pos).normalized()
            self.rubberBand.setGeometry(rect)
            for th in self.thumbnails:
                try:
                    tr_ = QRect(th.mapTo(self.inner, QPoint(0, 0)), th.size())
                    if rect.intersects(tr_):
                        th.set_selected(True)
                except RuntimeError:
                    continue

    def on_mouse_release(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.is_selecting:
            self.is_selecting = False
            self.rubberBand.hide()

    def on_mouse_double_click(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
            clicked = self.get_thumb_at(pos)
            if clicked:
                all_files = [th.filepath for th in self.thumbnails]
                self.open_image.emit(clicked.filepath, all_files)

    def on_context_menu(self, event):
        _pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        clicked = self.get_thumb_at(_pos)
        if clicked:
            if not clicked.is_selected:
                for th in self.thumbnails:
                    th.set_selected(False)
                clicked.set_selected(True)
            self.context_menu_requested.emit(self.inner.mapToGlobal(_pos), clicked.filepath)

    def set_groups(self, groups, cache_dir, cols=2):
        self.groups = groups
        self.cache_dir = cache_dir
        self.cols = max(1, min(4, cols))
        self._clear()
        for i in range(10):
            self.grid.setColumnStretch(i, 0)
        if self.cols == 1:
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 0)
            self.grid.setColumnStretch(2, 1)
            start_col = 1
        else:
            for i in range(self.cols):
                self.grid.setColumnStretch(i, 1)
            start_col = 0
        cur_row = 0
        cur_col = start_col
        for name, files in groups:
            span = self.cols if len(files) > 3 else 1
            if cur_col + span > self.cols + start_col:
                cur_row += 1
                cur_col = start_col
            card = DuplicateGroupWidget(name, files, cache_dir, self.thumb_size, self, threadpool=self.threadpool)
            self.grid.addWidget(card, cur_row, cur_col, 1, span, Qt.AlignmentFlag.AlignHCenter)
            self.cards.append(card)
            cur_col += span
        self.thumbnails = [th for card in self.cards for th in card.thumbs]
        self.grid.setRowStretch(cur_row + 1, 1)

    def wheelEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            d = event.angleDelta().y()
            self.thumb_size = min(400, self.thumb_size + 20) if d > 0 else max(100, self.thumb_size - 20)
            self.inner.setUpdatesEnabled(False)
            for card in self.cards:
                card.set_thumb_size(self.thumb_size)
            self.grid.invalidate()
            self.inner.adjustSize()
            self.inner.setUpdatesEnabled(True)
            event.accept()
        else:
            super().wheelEvent(event)

    def get_selected_files(self):
        return [th.filepath for th in self.thumbnails if th.is_selected]

    def select_all(self):
        for th in self.thumbnails:
            th.set_selected(True)

    def _clear(self):
        for c in self.cards:
            c.deleteLater()
        self.cards.clear()
        self.thumbnails.clear()
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()


class GalleryWidget(QWidget):
    go_back = Signal()
    open_image = Signal(str, list)
    files_moved_externally = Signal(list)  # forward from list_widget
    assign_category = Signal(list)
    remove_from_event = Signal(list)
    delete_duplicates = Signal()
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(10)

        self.btn_back = QPushButton(t("btn_back"))
        self.btn_back.clicked.connect(self.go_back.emit)
        self.lbl_title = QLabel(t("gallery_title"))
        self.lbl_title.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {COLOR_TEXT};")

        self.list_widget = GalleryListWidget()
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.list_widget.customContextMenuRequested.connect(self.show_context_menu)
        self.list_widget.files_moved_externally.connect(
            self.files_moved_externally.emit)  # 

        self.dup_gallery = DuplicateGalleryWidget()
        self.dup_gallery.open_image.connect(lambda fp, fs: self.open_image.emit(fp, fs))
        self.dup_gallery.context_menu_requested.connect(self.show_dup_context_menu)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list_widget)
        self.stack.addWidget(self.dup_gallery)

        self.btn_scroll_top = QPushButton(t("scroll_top"))
        self.btn_scroll_top.setFixedSize(32, 32)
        self.btn_scroll_top.clicked.connect(self.scroll_to_top)
        self.btn_scroll_bottom = QPushButton(t("scroll_bottom"))
        self.btn_scroll_bottom.setFixedSize(32, 32)
        self.btn_scroll_bottom.clicked.connect(self.scroll_to_bottom)

        self.combo_sort = QComboBox()
        self.combo_sort.addItems([t("sort_name"), t("sort_date"),
                                  t("sort_photos_first"), t("sort_videos_first")])
        self.combo_sort.currentIndexChanged.connect(lambda _: self.refresh_view())

        top.addWidget(self.btn_back)
        top.addSpacing(12)
        top.addWidget(self.lbl_title)
        top.addStretch()
        self.lbl_sort = QLabel(t("sort_label"))
        top.addWidget(self.lbl_sort)
        top.addWidget(self.combo_sort)
        top.addSpacing(5)
        top.addWidget(self.btn_scroll_top)
        top.addWidget(self.btn_scroll_bottom)
        top.addSpacing(15)

        self.btn_undo = QPushButton(t("btn_undo"))
        self.btn_undo.clicked.connect(self.undo_requested.emit)
        self.btn_redo = QPushButton(t("btn_redo"))
        self.btn_redo.clicked.connect(self.redo_requested.emit)

        self.btn_cut = QPushButton(t("btn_cut"))
        self.btn_copy = QPushButton(t("btn_copy"))
        self.btn_delete = QPushButton(t("btn_delete"))
        self.btn_delete_dups = QPushButton(t("btn_delete_dups"))
        self.btn_delete_dups.clicked.connect(self.delete_duplicates.emit)
        self.btn_delete_dups.hide()

        top.addWidget(self.btn_undo)
        top.addWidget(self.btn_redo)
        top.addWidget(self.btn_delete_dups)
        top.addWidget(self.btn_cut)
        top.addWidget(self.btn_copy)
        top.addWidget(self.btn_delete)

        layout.addLayout(top)
        layout.addWidget(self.stack)

        self.is_dups_mode = False
        self.current_files = []
        self.raw_grouped_data = {}
        self.raw_duplicates = frozenset()
        # Ctrl+A moved to MainWindow._on_select_all_action
        # (QAction ApplicationShortcut). F10 guard ("only when
        # gallery is the active stack") preserved in the dispatch
        # via the stack==4 check. Do not re-add a QShortcut here --
        # it would cause Qt shortcut ambiguity.

    def scroll_to_top(self):
        if self.stack.currentIndex() == 1:
            self.dup_gallery.verticalScrollBar().setValue(0)
        else:
            self.list_widget.scrollToTop()

    def scroll_to_bottom(self):
        if self.stack.currentIndex() == 1:
            self.dup_gallery.verticalScrollBar().setValue(self.dup_gallery.verticalScrollBar().maximum())
        else:
            self.list_widget.scrollToBottom()

    def set_duplicate_groups(self, groups, cache_dir, cols=2):
        self.is_dups_mode = True
        self.stack.setCurrentIndex(1)
        self.btn_delete_dups.show()
        self.btn_back.hide()
        self.lbl_title.setText(t("gallery_duplicates_title", n=len(groups)))
        self.dup_gallery.set_groups(groups, cache_dir, cols)

    def retranslate(self):
        self.btn_back.setText(t("btn_back"))
        self.lbl_sort.setText(t("sort_label"))
        for i, k in enumerate(["sort_name", "sort_date",
                                "sort_photos_first", "sort_videos_first"]):
            self.combo_sort.setItemText(i, t(k))
        self.btn_undo.setText(t("btn_undo"))
        self.btn_redo.setText(t("btn_redo"))
        self.btn_cut.setText(t("btn_cut"))
        self.btn_copy.setText(t("btn_copy"))
        self.btn_delete.setText(t("btn_delete"))
        self.btn_delete_dups.setText(t("btn_delete_dups"))
        self.btn_scroll_top.setText(t("scroll_top"))
        self.btn_scroll_bottom.setText(t("scroll_bottom"))

    def update_theme(self):
        self.lbl_title.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {COLOR_TEXT};")
        self.list_widget.update_theme()
        for b in (self.btn_delete, self.btn_delete_dups):
            b.setStyleSheet(
                f"QPushButton {{ background-color: {COLOR_DANGER}; color: white; "
                f"border: 1px solid {COLOR_DANGER}; border-radius: 8px; padding: 8px 14px; "
                f"font-weight: 600; font-size: 13px; }} "
                f"QPushButton:hover {{ background-color: {COLOR_DANGER_DK}; }}")

    def set_grouped_files(self, title, grouped_data, duplicates_set, cache_dir):
        self.lbl_title.setText(title)
        self.raw_grouped_data = grouped_data
        self.raw_duplicates = frozenset(duplicates_set)
        self.list_widget.cache_dir = cache_dir
        self.is_dups_mode = False
        self.stack.setCurrentIndex(0)
        self.btn_delete_dups.hide()
        self.btn_back.show()
        self.refresh_view()

    def get_files_to_delete_as_dups(self):
        # keep one per group 
        if self.stack.currentIndex() == 1:
            selected = self.dup_gallery.get_selected_files()
            if selected:
                selected_set = set(selected)
                to_delete = []
                for card in self.dup_gallery.cards:
                    card_files = set(card.files)
                    in_card = card_files & selected_set
                    if not in_card:
                        continue
                    if in_card == card_files and len(card_files) > 1:
                        sorted_files = self._sorted_files(
                            card.files, self.combo_sort.currentIndex())
                        keep = sorted_files[0]
                        to_delete.extend([f for f in in_card if f != keep])
                    elif len(card_files) > 1:
                        to_delete.extend(in_card)
                    # 1-file card guard — skip silently
                return to_delete
            to_delete = []
            for card in self.dup_gallery.cards:
                sorted_files = self._sorted_files(card.files, self.combo_sort.currentIndex())
                if len(sorted_files) > 1:
                    to_delete.extend(sorted_files[1:])
            return to_delete
        selected = [it.data(ROLE_FILEPATH) for it in self.list_widget.selectedItems()
                    if it.data(ROLE_FILEPATH)]
        if selected:
            return selected
        to_delete = []
        for files in self.raw_grouped_data.values():
            sorted_files = self._sorted_files(files, self.combo_sort.currentIndex())
            if len(sorted_files) > 1:
                to_delete.extend(sorted_files[1:])
        return to_delete

    def refresh_view(self):
        self.list_widget.clear_all()
        self.current_files = []
        sort_mode = self.combo_sort.currentIndex()
        for title, filepaths in self.raw_grouped_data.items():
            self.list_widget.add_header(title)
            for fp in self._sorted_files(filepaths, sort_mode):
                self.current_files.append(fp)
                is_dup = fp in self.raw_duplicates
                self.list_widget.add_file(fp, is_dup)
        self.list_widget.start_loading()
        self.list_widget.scrollToTop()
        self.list_widget._update_headers()

    def _sorted_files(self, filepaths, sort_mode):
        files = list(filepaths)
        if sort_mode == 0:
            files.sort(key=lambda f: os.path.basename(f).lower())
        elif sort_mode == 1:
            pairs = []
            for f in files:
                try:
                    mt = os.path.getmtime(f)
                except OSError:
                    mt = 0
                pairs.append((f, mt))
            pairs.sort(key=lambda p: p[1])
            files = [p[0] for p in pairs]
        elif sort_mode == 2:
            files.sort(key=lambda f: (f.lower().endswith(VIDEO_EXTS), os.path.basename(f).lower()))
        elif sort_mode == 3:
            files.sort(key=lambda f: (not f.lower().endswith(VIDEO_EXTS), os.path.basename(f).lower()))
        return files

    def get_selected_items(self):
        if self.stack.currentIndex() == 1:
            items = []
            for fp in self.dup_gallery.get_selected_files():
                item = QListWidgetItem()
                item.setData(ROLE_FILEPATH, fp)
                items.append(item)
            return items
        return [it for it in self.list_widget.selectedItems() if it.data(ROLE_FILEPATH) is not None]

    def on_item_double_clicked(self, item):
        fp = item.data(ROLE_FILEPATH)
        if not fp:
            return
        ext = os.path.splitext(fp)[1].lower()
        if ext in VIDEO_EXTS:
            QDesktopServices.openUrl(QUrl.fromLocalFile(fp))
        else:
            self.open_image.emit(fp, self.current_files)

    def show_dup_context_menu(self, global_pos, filepath):
        menu = QMenu(self)
        a_open = menu.addAction(t("menu_open"))
        a_show = menu.addAction(t("menu_show_in_folder"))
        menu.addSeparator()
        a_ev = menu.addAction(t("menu_assign_event"))
        a_rm_ev = menu.addAction(t("menu_remove_from_event"))
        menu.addSeparator()
        a_copy = menu.addAction(t("menu_copy"))
        a_cut = menu.addAction(t("menu_cut"))
        a_del = menu.addAction(t("menu_delete"))
        act = menu.exec(global_pos)
        if act == a_open:
            all_files = [th.filepath for th in self.dup_gallery.thumbnails]
            self.open_image.emit(filepath, all_files)
        elif act == a_show:
            show_in_explorer(filepath)
        elif act == a_ev:
            files = self.dup_gallery.get_selected_files() or [filepath]
            self.assign_category.emit(files)
        elif act == a_rm_ev:
            files = self.dup_gallery.get_selected_files() or [filepath]
            self.remove_from_event.emit(files)
        elif act == a_copy:
            self.btn_copy.click()
        elif act == a_cut:
            self.btn_cut.click()
        elif act == a_del:
            self.btn_delete.click()

    def show_context_menu(self, pos):
        if self.stack.currentIndex() == 1:
            return
        item = self.list_widget.itemAt(pos)
        if not item or item.data(ROLE_FILEPATH) is None:
            return
        # (re-apply ): standard file-manager behaviour —
        # right-click on unselected item makes it the sole selection.
        # Without this, menu actions (Delete/Copy/Cut via
        # get_selected_items()) acted on the PREVIOUS selection —
        # wrong file could be deleted.
        if not item.isSelected():
            self.list_widget.clearSelection()
            item.setSelected(True)
        menu = QMenu(self)
        a_open = menu.addAction(t("menu_open"))
        a_show = menu.addAction(t("menu_show_in_folder"))
        menu.addSeparator()
        a_ev = menu.addAction(t("menu_assign_event"))
        a_rm_ev = menu.addAction(t("menu_remove_from_event"))
        menu.addSeparator()
        a_copy = menu.addAction(t("menu_copy"))
        a_cut = menu.addAction(t("menu_cut"))
        a_del = menu.addAction(t("menu_delete"))
        fp = item.data(ROLE_FILEPATH)
        act = menu.exec(self.list_widget.mapToGlobal(pos))
        if act == a_open:
            ext = os.path.splitext(fp)[1].lower()
            if ext in VIDEO_EXTS:
                QDesktopServices.openUrl(QUrl.fromLocalFile(fp))
            else:
                self.open_image.emit(fp, self.current_files)
        elif act == a_show:
            show_in_explorer(fp)
        elif act == a_ev:
            self.assign_category.emit([it.data(ROLE_FILEPATH) for it in self.get_selected_items()])
        elif act == a_rm_ev:
            self.remove_from_event.emit([it.data(ROLE_FILEPATH) for it in self.get_selected_items()])
        elif act == a_copy:
            self.btn_copy.click()
        elif act == a_cut:
            self.btn_cut.click()
        elif act == a_del:
            self.btn_delete.click()


# ============================================================
#                     ASSIGN EVENT DIALOG
# ============================================================
class AssignEventDialog(QDialog):
    def __init__(self, category_names, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("title_assign_event"))
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        if category_names:
            layout.addWidget(QLabel(t("label_existing_events")))
            self.combo = QComboBox()
            self.combo.addItems(category_names)
            self.combo.setStyleSheet("padding: 8px; font-size: 14px; min-height: 28px;")
            layout.addWidget(self.combo)
            layout.addSpacing(6)
        layout.addWidget(QLabel(t("label_new_event")))
        self.input = QLineEdit()
        self.input.setPlaceholderText(t("placeholder_new_event"))
        self.input.setStyleSheet("padding: 8px; font-size: 14px;")
        layout.addWidget(self.input)
        hint = QLabel(t("hint_assign_event"))
        hint.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        btns = QDialogButtonBox()
        btns.addButton(t("dialog_ok"), QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton(t("dialog_cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_name(self):
        new = self.input.text().strip()
        if new:
            return new
        if hasattr(self, "combo") and self.combo.currentText():
            return self.combo.currentText().strip()
        return ""


# ============================================================
#                     IMAGE VIEWER
# ============================================================
class ViewerGraphicsView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_filepath = None
        self.drag_start_pos = None

    def wheelEvent(self, event):
        # wheel zoom on QGraphicsView
        if self.scene() and self.scene().items():
            factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
            self.scale(factor, factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.MouseButton.LeftButton
                and self.current_filepath and os.path.exists(self.current_filepath)
                and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)):
            if self.drag_start_pos is not None:
                if (event.position().toPoint() - self.drag_start_pos).manhattanLength() > QApplication.startDragDistance():
                    mime = QMimeData()
                    mime.setUrls([QUrl.fromLocalFile(self.current_filepath)])
                    drag = QDrag(self)
                    drag.setMimeData(mime)
                    drag.exec(Qt.DropAction.CopyAction)
                    self.drag_start_pos = None
                    return
        super().mouseMoveEvent(event)


class ImageViewer(QWidget):
    go_back = Signal()
    file_deleted = Signal(str, str)
    file_rotated = Signal(str)
    assign_event = Signal(str)
    remove_from_event = Signal(str)
    copy_to_clipboard = Signal(str)  # sync with MainWindow
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.top_bar = QWidget()
        top = QHBoxLayout(self.top_bar)
        top.setContentsMargins(20, 12, 20, 12)

        self.btn_undo = QPushButton(t("btn_undo"))
        self.btn_undo.clicked.connect(self.undo_requested.emit)
        self.btn_redo = QPushButton(t("btn_redo"))
        self.btn_redo.clicked.connect(self.redo_requested.emit)
        self.btn_close = QPushButton(t("btn_close"))
        self.btn_close.clicked.connect(self.go_back.emit)
        self.btn_rotate_left = QPushButton(t("btn_rotate_left"))
        self.btn_rotate_right = QPushButton(t("btn_rotate_right"))
        self.btn_event = QPushButton(t("btn_event"))
        self.btn_copy = QPushButton(t("btn_copy"))
        self.btn_delete = QPushButton(t("btn_delete"))
        self.btn_rotate_left.clicked.connect(lambda: self.rotate_image(90))
        self.btn_rotate_right.clicked.connect(lambda: self.rotate_image(-90))
        self.btn_event.clicked.connect(self._emit_assign_event)
        self.btn_copy.clicked.connect(self.copy_current)
        self.btn_delete.clicked.connect(self.delete_current)
        self.lbl_title = QLabel("")
        self.lbl_counter = QLabel("")

        top.addWidget(self.btn_undo)
        top.addWidget(self.btn_redo)
        top.addSpacing(10)
        top.addWidget(self.btn_close)
        top.addSpacing(16)
        top.addWidget(self.btn_rotate_left)
        top.addWidget(self.btn_rotate_right)
        top.addWidget(self.btn_event)
        top.addWidget(self.btn_copy)
        top.addWidget(self.btn_delete)
        top.addSpacing(16)
        top.addWidget(self.lbl_title)
        top.addStretch()
        top.addWidget(self.lbl_counter)

        self.view = ViewerGraphicsView()
        self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scene_ = QGraphicsScene()
        self.view.setScene(self.scene_)
        self.pixmap_item = None

        layout.addWidget(self.top_bar)
        layout.addWidget(self.view)

        self.current_index = -1
        self.files = []
        # NB-24: saved QTransform to re-apply on next/prev (zoom/pan
        # preservation). Cleared on load_image (new gallery) and after
        # being applied once. None = fitInView path.
        self._saved_transform = None
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(
            lambda pos: self.show_context_menu(self.view.mapTo(self, pos)))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # QShortcut -> QAction refactor. Widget-context actions
        # created by rebind_hotkeys(config) -- called from
        # MainWindow.setup_shortcuts after config is loaded.
        # NOTE: delete (Del) and copy (Ctrl+C) are handled by
        # MainWindow ApplicationShortcut dispatch -- see
        # _on_delete_action / _on_copy_action. They are NOT
        # registered here to avoid Qt shortcut ambiguity.
        self._actions = {}
        self.update_theme()

    def retranslate(self):
        self.btn_undo.setText(t("btn_undo"))
        self.btn_redo.setText(t("btn_redo"))
        self.btn_close.setText(t("btn_close"))
        self.btn_rotate_left.setText(t("btn_rotate_left"))
        self.btn_rotate_right.setText(t("btn_rotate_right"))
        self.btn_event.setText(t("btn_event"))
        self.btn_copy.setText(t("btn_copy"))
        self.btn_delete.setText(t("btn_delete"))

    def update_theme(self):
        self.top_bar.setStyleSheet(f"background: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER};")
        btn_style = (f"QPushButton {{ background-color: {COLOR_SURFACE}; color: {COLOR_TEXT}; "
                     f"border: 1px solid {COLOR_MUTED}; padding: 8px 16px; border-radius: 6px; font-weight: 600; }} "
                     f"QPushButton:hover {{ background-color: {COLOR_PRIMARY_LT}; border-color: {COLOR_PRIMARY}; }}")
        btn_danger = (f"QPushButton {{ background-color: {COLOR_DANGER}; color: white; border: none; "
                      f"padding: 8px 16px; border-radius: 6px; font-weight: 600; }} "
                      f"QPushButton:hover {{ background-color: {COLOR_DANGER_DK}; }}")
        for b in (self.btn_close, self.btn_rotate_left, self.btn_rotate_right,
                  self.btn_event, self.btn_copy, self.btn_undo, self.btn_redo):
            b.setStyleSheet(btn_style)
        self.btn_delete.setStyleSheet(btn_danger)
        self.view.setStyleSheet(f"background-color: {COLOR_BG}; border: none;")
        self.lbl_title.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 14px;")
        self.lbl_counter.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 13px;")

    def rebind_hotkeys(self, config):
        """widget-context hotkeys. Create actions on first
        call, then only setShortcut on subsequent calls (live rebind).
        Only widget-ctx actions live here: assign_event (Ctrl+E),
        prev/next (Left/Right), close_viewer (Esc).
        """
        if not hasattr(self, "_actions"):
            self._actions = {}
        specs = (
            ("assign_event", self._emit_assign_event),
            ("prev_image",   self.prev_image),
            ("next_image",   self.next_image),
            ("close_viewer", self.go_back.emit),
        )
        for aid, slot in specs:
            key = _resolved_hotkey(config, aid)
            if aid not in self._actions:
                act = QAction(self)
                act.setShortcutContext(
                    Qt.ShortcutContext.WidgetWithChildrenShortcut)
                act.triggered.connect(slot)
                self.addAction(act)
                self._actions[aid] = act
            self._actions[aid].setShortcut(
                QKeySequence(key) if key else QKeySequence()
            )

    def _emit_assign_event(self):
        if 0 <= self.current_index < len(self.files):
            self.assign_event.emit(self.files[self.current_index])

    def show_context_menu(self, pos):
        menu = QMenu(self)
        a_rl = menu.addAction(t("menu_rotate_left"))
        a_rr = menu.addAction(t("menu_rotate_right"))
        menu.addSeparator()
        a_ev = menu.addAction(t("menu_assign_event_sc"))
        a_rm_ev = menu.addAction(t("menu_remove_from_event"))
        menu.addSeparator()
        a_show = menu.addAction(t("menu_show_in_folder"))
        a_copy = menu.addAction(t("menu_copy"))
        a_del = menu.addAction(t("menu_delete"))
        act = menu.exec(self.mapToGlobal(pos))
        if act == a_rl:
            self.rotate_image(90)
        elif act == a_rr:
            self.rotate_image(-90)
        elif act == a_ev:
            self._emit_assign_event()
        elif act == a_rm_ev:
            if 0 <= self.current_index < len(self.files):
                self.remove_from_event.emit(self.files[self.current_index])
        elif act == a_show:
            if 0 <= self.current_index < len(self.files):
                show_in_explorer(self.files[self.current_index])
        elif act == a_copy:
            self.copy_current()
        elif act == a_del:
            self.delete_current()

    def load_image(self, filepath, file_list):
        self.files = [f for f in file_list
                      if os.path.splitext(f)[1].lower() not in VIDEO_EXTS and os.path.exists(f)]
        self._saved_transform = None  # NB-24: new gallery — no zoom carryover
        self.current_index = -1
        self.pixmap_item = None
        self.scene_.clear()
        if filepath in self.files:
            self.current_index = self.files.index(filepath)
            self.update_view()
        else:
            self.lbl_title.setText(t("file_unavailable"))
            self.lbl_counter.setText("")
        self.setFocus()

    def update_view(self):
        if not (0 <= self.current_index < len(self.files)):
            return
        fp = self.files[self.current_index]
        self.view.current_filepath = fp
        self.scene_.clear()
        self.pixmap_item = None
        reader = QImageReader(fp)
        reader.setAutoTransform(True)
        img = reader.read()
        if img.isNull():
            pixmap = QPixmap(fp)
        else:
            pixmap = QPixmap.fromImage(img)
        # QImageReader can't decode HEIC without Qt plugin — PIL fallback
        if pixmap.isNull():
            try:
                with Image.open(fp) as raw:
                    pil_img = ImageOps.exif_transpose(raw).convert("RGB")
                data = pil_img.tobytes("raw", "RGB")
                qimg = QImage(data, pil_img.width, pil_img.height,
                              pil_img.width * 3,
                              QImage.Format.Format_RGB888).copy()
                pixmap = QPixmap.fromImage(qimg)
            except Exception as e:
                logger.debug(f"viewer PIL fallback failed: {e}")
                pixmap = QPixmap(fp)  # last resort (may still fail)
        if pixmap.isNull():
            self.lbl_title.setText(t("cannot_load", name=os.path.basename(fp)))
            return
        self.pixmap_item = self.scene_.addPixmap(pixmap)
        self.scene_.setSceneRect(self.pixmap_item.boundingRect())
        # NB-24: reuse last transform when navigating (next/prev).
        # _saved_transform is set only by next_image/prev_image before
        # they call update_view; any other caller (load_image, rotate,
        # delete) leaves it None and gets fitInView.
        if self._saved_transform is not None:
            try:
                self.view.setTransform(self._saved_transform)
            except RuntimeError:
                pass
            self._saved_transform = None
        else:
            self.view.fitInView(self.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
            # deferred fit — widget may not be shown yet
            QTimer.singleShot(0, self._fit_current)
            QTimer.singleShot(50, self._fit_current)
        self.view.setFocus()
        self.lbl_title.setText(os.path.basename(fp))
        self.lbl_counter.setText(f"{self.current_index + 1} / {len(self.files)}")

    def _fit_current(self):
        # deferred fit helper — safe on deleted item / late timer
        if not self.pixmap_item:
            return
        try:
            self.view.fitInView(self.pixmap_item,
                                Qt.AspectRatioMode.KeepAspectRatio)
        except RuntimeError:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.pixmap_item:
            # guard — pixmap_item C++ side may be gone
            try:
                self.view.fitInView(self.pixmap_item,
                                    Qt.AspectRatioMode.KeepAspectRatio)
            except RuntimeError:
                self.pixmap_item = None

    # wheelEvent removed; handled in ViewerGraphicsView

    def rotate_image(self, angle):
        if not (0 <= self.current_index < len(self.files)):
            return
        # (U5a): F17 safe-media-add invariant -- rotate
        # rewrites a file on disk, must be blocked during scan.
        _mw = self.window()
        if hasattr(_mw, "_guard_scan") and _mw._guard_scan():
            return
        fp = self.files[self.current_index]
        self.scene_.clear()
        self.pixmap_item = None
        try:
            st = os.stat(fp)
            ext = os.path.splitext(fp)[1].lower()
            # capture EXIF before rotation; reset Orientation only
            _orig_exif = None
            try:
                with Image.open(fp) as _src:
                    _orig_exif = _src.getexif()
            except Exception:
                _orig_exif = None
            with Image.open(fp) as img:
                # NB-85: refuse multi-frame TIFF/HEIC. Saving only the
                # first frame via temp+os.replace irrecoverably destroys
                # remaining pages (real data-loss for scanned documents,
                # burst HEIC, fax TIFFs).
                _nframes = getattr(img, "n_frames", 1)
                if _nframes > 1:
                    raise RuntimeError(
                        f"Cannot rotate multi-frame {ext} "
                        f"({_nframes} frames) safely")
                img = ImageOps.exif_transpose(img)
                rotated = img.rotate(angle, expand=True)

                # Preserve original format so we don't turn PNG into JPEG-data-with-.png-extension
                if ext in (".jpg", ".jpeg"):
                    save_format = "JPEG"
                    to_save = rotated.convert("RGB") if rotated.mode != "RGB" else rotated
                    save_kwargs = {"quality": 95}
                elif ext == ".png":
                    save_format = "PNG"
                    to_save = rotated
                    save_kwargs = {}
                elif ext == ".webp":
                    save_format = "WEBP"
                    to_save = rotated
                    save_kwargs = {"quality": 95}
                elif ext in (".heic", ".heif"):
                    # never silently convert HEIC to JPEG — data loss
                    if not HEIF_SUPPORT:
                        raise RuntimeError(t("heic_unavailable"))
                    save_format = "HEIF"
                    to_save = rotated
                    save_kwargs = {}
                    # NB-45: pillow-heif >= 0.11 accepts quality=
                    # for HEIF saves. Older versions ignore unknown
                    # kwargs (Pillow forwards them, the plugin
                    # ignores). Best-effort set it; without this,
                    # HEIC rotates recompress at HEVC default
                    # quality -- silent visible degradation.
                    try:
                        import pillow_heif as _ph
                        _ph_ver = tuple(
                            int(p) for p in _ph.__version__.split(".")[:2]
                        )
                        if _ph_ver >= (0, 11):
                            save_kwargs["quality"] = 95
                    except Exception:
                        pass
                elif ext == ".bmp":
                    save_format = "BMP"
                    to_save = rotated
                    save_kwargs = {}
                elif ext == ".tiff":
                    save_format = "TIFF"
                    to_save = rotated
                    save_kwargs = {}
                else:
                    save_format = "JPEG"
                    to_save = rotated.convert("RGB")
                    save_kwargs = {"quality": 95}

            # reattach EXIF with Orientation reset; preserve Exif sub-IFD
            # (DateTimeOriginal 36867 lives in 0x8769, not IFD0)
            if save_format in ("JPEG", "WEBP", "TIFF", "HEIF"):
                try:
                    _exif = Image.Exif()
                    if _orig_exif:
                        # copy IFD0 tags
                        for _k, _v in _orig_exif.items():
                            if _k == 0x8769:
                                continue  # handled below
                            if _k == 0x0112:
                                _exif[_k] = 1
                            else:
                                _exif[_k] = _v
                        # copy Exif sub-IFD (dates, exposure, etc.)
                        # cast to dict for Pillow <9.2 compat;
                        # without this some Pillow versions silently drop
                        # DateTimeOriginal (regression from patch 10).
                        try:
                            _sub = _orig_exif.get_ifd(0x8769)
                            if _sub:
                                try:
                                    _exif[0x8769] = dict(_sub)
                                except (TypeError, AttributeError):
                                    logger.warning(
                                        "rotate: Pillow too old to write "
                                        "Exif sub-IFD — DateTimeOriginal "
                                        "may be lost on this file")
                        except Exception as _e:
                            logger.warning(
                                f"rotate: sub-ifd read failed: {_e}")
                        if 0x0112 not in _exif:
                            _exif[0x0112] = 1
                    else:
                        _exif[0x0112] = 1
                    save_kwargs["exif"] = _exif.tobytes()
                except Exception as _e:
                    logger.debug(f"rotate: exif reattach failed: {_e}")

            dir_name = os.path.dirname(fp) or "."  # empty dir -> CWD
            fd, temp_path = tempfile.mkstemp(suffix=ext, dir=dir_name)
            os.close(fd)
            try:
                to_save.save(temp_path, format=save_format, **save_kwargs)
                os.utime(temp_path, (st.st_atime, st.st_mtime))
                # NB-86: Windows os.replace fails on read-only target
                # (PermissionError). Ensure target writable during the
                # replace, restore exact mode after. Without this,
                # rotate on any read-only file silently fails on Windows.
                _orig_mode = st.st_mode
                try:
                    try:
                        os.chmod(fp, _orig_mode | stat.S_IWRITE)
                    except OSError:
                        pass
                    os.replace(temp_path, fp)
                finally:
                    try:
                        os.chmod(fp, _orig_mode)
                    except OSError:
                        pass
            except Exception as e:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                raise e
            # NB-74: os.utime restores mtime, but st_mtime_ns can
            # drift by a few ns on NTFS/ext4 -> get_cache_path() in
            # on_file_rotated_viewer computes a NEW key and misses
            # the OLD .jpg in cache_dir, which lingers forever.
            # Delete the old-key cache file directly here.
            try:
                _mw = self.window()
                _cd = getattr(_mw, "config", {}).get("cache_dir")
                if _cd:
                    _old_key = hashlib.sha1(
                        f"{fp}|{st.st_mtime_ns}|{st.st_size}".encode()
                    ).hexdigest()
                    _old_cache = Path(_cd) / f"{_old_key}.jpg"
                    if _old_cache.exists():
                        try:
                            _old_cache.unlink()
                        except OSError:
                            pass
            except Exception:
                pass
            self.file_rotated.emit(fp)
            self.update_view()
        except Exception as e:
            QMessageBox.warning(self, t("error_title"), t("error_rotate", err=str(e)))
            self.update_view()

    def delete_current(self):
        if not (0 <= self.current_index < len(self.files)):
            return
        # F17 symmetry: btn_delete and context menu call this
        # method directly (NOT via MainWindow._on_delete_action),
        # so those two paths bypassed the scan protection.
        # rotate_image in the same class already does this check.
        _mw = self.window()
        if hasattr(_mw, "_guard_scan") and _mw._guard_scan():
            return
        # NB-12: hold MainWindow._busy across the whole confirm+move
        # window. QMessageBox.question spins a nested event loop, so
        # _watch_cut_files (1.5s timer) can fire inside the loop and
        # mutate self.files between the click and the pop. Setting
        # _busy makes _watch_cut_files no-op for the duration. The
        # scan-part was already guarded by I50; this adds the
        # busy-part. Ctrl+Z during the modal is blocked (documented).
        _busy_set = False
        if hasattr(_mw, "_busy"):
            if _mw._busy:
                return
            _mw._busy = True
            _busy_set = True
        try:
            fp = self.files[self.current_index]
            # don't clear scene before confirm — white flash on cancel
            reply = QMessageBox.question(
                self, t("confirm_delete"), t("confirm_delete_file"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                defaultButton=QMessageBox.StandardButton.Yes)  # undoable op
            if reply == QMessageBox.StandardButton.Yes:
                self.scene_.clear()
                self.pixmap_item = None
                ok, trash_path, err = move_to_trash(fp)
                if ok:
                    self.file_deleted.emit(fp, trash_path or "")
                    self.files.pop(self.current_index)
                    if not self.files:
                        self.go_back.emit()
                    else:
                        if self.current_index >= len(self.files):
                            self.current_index = len(self.files) - 1
                        self.update_view()
                else:
                    QMessageBox.warning(self, t("error_title"), t("error_delete", err=str(err)))
                    self.update_view()
            else:
                self.update_view()
        finally:
            if _busy_set:
                _mw._busy = False

    def copy_current(self):
        if not (0 <= self.current_index < len(self.files)):
            return
        fp = self.files[self.current_index]
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(fp)])
        QApplication.clipboard().setMimeData(mime)
        self.lbl_title.setText(f"{t('btn_copy')}: {os.path.basename(fp)}")
        self.copy_to_clipboard.emit(fp)  # sync internal clipboard

    def next_image(self):
        if self.current_index < len(self.files) - 1:
            # NB-24: snapshot transform before swap
            try:
                self._saved_transform = self.view.transform()
            except RuntimeError:
                self._saved_transform = None
            self.current_index += 1
            self.update_view()

    def prev_image(self):
        if self.current_index > 0:
            # NB-24: snapshot transform before swap
            try:
                self._saved_transform = self.view.transform()
            except RuntimeError:
                self._saved_transform = None
            self.current_index -= 1
            self.update_view()


# ============================================================
#                     SCAN PROGRESS DIALOG ()
# ============================================================
class ScanProgressDialog(QDialog):
    """modal dialog for large scans (safe media add).

    Blocks the main window while ScannerThread runs. Prevents the
    QTimer.singleShot race that used to lose _scanned_files (NB-26).

    Cancel aborts the scan. Esc / ✕ also aborts — the dialog does NOT
    close itself; MainWindow closes it programmatically after the
    thread has stopped and _scanned_files has been cleared.
    """
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("dialog_scanning_title"))
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlags(
            self.windowFlags()
            & ~Qt.WindowType.WindowCloseButtonHint
            & ~Qt.WindowType.WindowContextHelpButtonHint
        )
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        self.lbl = QLabel(t("dialog_scanning_body"))
        self.lbl.setWordWrap(True)
        layout.addWidget(self.lbl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # indeterminate until first progress
        self.bar.setTextVisible(True)
        layout.addWidget(self.bar)
        self.btn_cancel = QPushButton(t("toolbar_cancel"))
        self.btn_cancel.clicked.connect(self._on_cancel)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.btn_cancel)
        layout.addLayout(row)
        self._cancelled = False
        self._force_close = False  # programmatic close flag

    def set_progress(self, cur, total):
        try:
            if total > 0 and self.bar.maximum() != total:
                self.bar.setRange(0, total)
                self.bar.setFormat("%v / %m")
            if total > 0:
                self.bar.setValue(cur)
        except RuntimeError:
            pass

    def _on_cancel(self):
        if self._cancelled:
            return
        self._cancelled = True
        try:
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.setText(t("status_stopping_scan"))
        except RuntimeError:
            pass
        self.cancel_requested.emit()

    def closeEvent(self, event):
        # programmatic close (from _close_scan_dialog) must
        # actually accept — previously event.ignore() was
        # unconditional and dialog hung until deleteLater.
        if getattr(self, "_force_close", False):
            event.accept()
            return
        # ✕ / Esc / Alt+F4 → cancel scan, do NOT self-close
        if not self._cancelled:
            self._on_cancel()
        event.ignore()

    def reject(self):
        # (P2): QDialog::keyPressEvent sends Esc to reject(),
        # NOT closeEvent(). Without this override, Esc would just
        # hide the dialog (done(Rejected)) and leave the scan
        # running with _scan_in_progress=True and no cancel request.
        # User thinks the scan was cancelled — it was not.
        if not self._cancelled:
            self._on_cancel()
        # do NOT call super().reject() — let MainWindow close it
        # programmatically after the thread stops.


# ============================================================
#                     SETTINGS
# ============================================================
class SettingsWidget(QWidget):
    priorities_changed = Signal(list)
    cache_settings_changed = Signal(dict)
    cache_cleared = Signal()
    language_load_requested = Signal()
    language_export_requested = Signal()
    language_reset_requested = Signal()
    calendar_export_requested = Signal()
    calendar_import_requested = Signal()
    data_find_missing_requested = Signal()  # 
    data_restore_backup_requested = Signal()  # 
    # hotkey editor signals
    hotkey_changed = Signal(str, str)          # (action_id, portable_seq)
    hotkey_profile_changed = Signal(str)       # "auto"|"windows"|"darwin"|"linux"
    hotkey_reset_requested = Signal()

    def __init__(self, current_priority, cache_config):
        super().__init__()
        # keep live reference to MainWindow.config so conflict
        # detection and profile rendering always read current state
        # (same dict object as MainWindow.config).
        self._cache_cfg = cache_config
        self._hk_edits = {}
        self._hk_labels = {}
        self._btn_hk_reset = None
        # wrap content in QScrollArea to prevent layout squishing
        # when the number of settings grows (e.g. hotkey editor).
        _outer = QVBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QFrame.Shape.NoFrame)
        _content = QWidget()
        layout = QVBoxLayout(_content)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(14)

        self.title = QLabel(t("settings_title"))
        self.title.setStyleSheet(f"font-size: 26px; font-weight: 700; color: {COLOR_TEXT};")
        self.desc = QLabel(t("settings_priority_desc"))
        self.desc.setStyleSheet(f"color: {COLOR_MUTED};")
        self.desc.setWordWrap(True)

        self.list_widget = QListWidget()
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        for prio in current_priority:
            self._add_priority_item(prio)
        self.list_widget.model().rowsMoved.connect(self.save_priorities)

        fmt_layout = QHBoxLayout()
        btn_add_fmt = QPushButton(t("settings_add_format"))
        btn_add_fmt.clicked.connect(self.add_custom_format)
        btn_del_fmt = QPushButton(t("settings_remove_format"))
        btn_del_fmt.clicked.connect(self.remove_custom_format)
        fmt_layout.addWidget(btn_add_fmt)
        fmt_layout.addWidget(btn_del_fmt)

        layout.addWidget(self.title)
        layout.addWidget(self.desc)
        layout.addWidget(self.list_widget)
        layout.addLayout(fmt_layout)

        # ---- cache box
        self.cache_box = QFrame()
        self.cache_box.setObjectName("settings_box")
        cb = QVBoxLayout(self.cache_box)
        cb.setContentsMargins(16, 14, 16, 14)
        cb.addWidget(QLabel(t("settings_cache_box")))
        path_l = QHBoxLayout()
        self.lbl_cache_path = QLabel(str(cache_config.get("cache_dir", "")))
        self.lbl_cache_path.setStyleSheet(f"color: {COLOR_MUTED};")
        btn_change = QPushButton(t("settings_cache_change"))
        btn_change.clicked.connect(self.change_cache_path)
        path_l.addWidget(QLabel(t("settings_cache_path")))
        path_l.addWidget(self.lbl_cache_path)
        path_l.addWidget(btn_change)
        auto_l = QHBoxLayout()
        self.chk_auto = QCheckBox(t("settings_cache_auto"))
        self.chk_auto.setChecked(cache_config.get("auto_clean_enabled", True))
        self.spin_days = QSpinBox()
        self.spin_days.setRange(1, 365)
        self.spin_days.setValue(cache_config.get("auto_clean_days", 7))
        self.spin_days.setSuffix(t("settings_cache_days"))
        auto_l.addWidget(self.chk_auto)
        auto_l.addWidget(self.spin_days)
        auto_l.addStretch()
        self.chk_auto.toggled.connect(self.emit_cache_settings)
        self.spin_days.valueChanged.connect(self.emit_cache_settings)
        self.lbl_cache_size = QLabel()
        self.update_cache_size(cache_config)
        btn_clear = QPushButton(t("settings_cache_clear"))
        btn_clear.clicked.connect(self.force_clear_cache)
        cb.addLayout(path_l)
        cb.addLayout(auto_l)
        cb.addWidget(self.lbl_cache_size)
        cb.addWidget(btn_clear)

        # ---- display box
        self.view_box = QFrame()
        self.view_box.setObjectName("settings_box")
        vb = QVBoxLayout(self.view_box)
        vb.setContentsMargins(16, 14, 16, 14)
        vb.setSpacing(10)
        vb.addWidget(QLabel(t("settings_display_box")))

        dr = QHBoxLayout()
        dr.addWidget(QLabel(t("settings_dups_per_row")))
        self.combo_dups = QComboBox()
        for i in (1, 2, 3, 4):
            self.combo_dups.addItem(str(i), i)
        cur_v = cache_config.get("dups_per_row", 2)
        idx_ = self.combo_dups.findData(cur_v)
        if idx_ < 0:
            idx_ = 1
        self.combo_dups.setCurrentIndex(idx_)
        self.combo_dups.currentIndexChanged.connect(self.emit_cache_settings)
        dr.addWidget(self.combo_dups)
        dr.addStretch()
        vb.addLayout(dr)

        ul = QHBoxLayout()
        ul.addWidget(QLabel(t("settings_undo_limit")))
        self.spin_undo = QSpinBox()
        self.spin_undo.setRange(1, 200)
        self.spin_undo.setValue(cache_config.get("undo_limit", 30))
        self.spin_undo.valueChanged.connect(self.emit_cache_settings)
        ul.addWidget(self.spin_undo)
        ul.addStretch()
        vb.addLayout(ul)

        # safe delete mode — default 'No' above N files
        sl = QHBoxLayout()
        self.chk_safe_delete = QCheckBox(t("settings_safe_delete"))
        _sd_enabled = cache_config.get("safe_delete_enabled", True)
        # Only explicit False disables; None/0/corrupt → treated as ON
        self.chk_safe_delete.setChecked(_sd_enabled is not False)
        try:
            _sd_th = int(cache_config.get("safe_delete_threshold", 50))
        except (TypeError, ValueError):
            _sd_th = 50
        _sd_th = max(1, min(10000, _sd_th))
        self.spin_safe_delete = QSpinBox()
        self.spin_safe_delete.setRange(1, 10000)
        self.spin_safe_delete.setValue(_sd_th)
        self.spin_safe_delete.setEnabled(self.chk_safe_delete.isChecked())
        self.chk_safe_delete.toggled.connect(self.spin_safe_delete.setEnabled)
        self.chk_safe_delete.toggled.connect(self.emit_cache_settings)
        self.spin_safe_delete.valueChanged.connect(self.emit_cache_settings)
        sl.addWidget(self.chk_safe_delete)
        sl.addWidget(self.spin_safe_delete)
        sl.addWidget(QLabel(t("settings_safe_files")))
        sl.addStretch()
        vb.addLayout(sl)

        # safe media add — block UI for large scans
        sm = QHBoxLayout()
        self.chk_safe_media_add = QCheckBox(t("settings_safe_media_add"))
        _sma = cache_config.get("safe_media_add_enabled", True)
        self.chk_safe_media_add.setChecked(_sma is not False)
        try:
            _sma_th = int(cache_config.get("safe_media_add_threshold", 50))
        except (TypeError, ValueError):
            _sma_th = 50
        _sma_th = max(10, min(10000, _sma_th))
        self.spin_safe_media = QSpinBox()
        self.spin_safe_media.setRange(10, 10000)
        self.spin_safe_media.setValue(_sma_th)
        self.spin_safe_media.setEnabled(self.chk_safe_media_add.isChecked())
        self.chk_safe_media_add.toggled.connect(
            self.spin_safe_media.setEnabled)
        self.chk_safe_media_add.toggled.connect(self.emit_cache_settings)
        self.spin_safe_media.valueChanged.connect(self.emit_cache_settings)
        sm.addWidget(self.chk_safe_media_add)
        sm.addWidget(self.spin_safe_media)
        sm.addWidget(QLabel(t("settings_safe_media_files")))
        sm.addStretch()
        vb.addLayout(sm)

        tl = QHBoxLayout()
        tl.addWidget(QLabel(t("settings_theme")))
        self.combo_theme = QComboBox()
        self.combo_theme.addItem(t("settings_theme_light"), "light")
        self.combo_theme.addItem(t("settings_theme_dark"), "dark")
        cur_theme = cache_config.get("theme", "light")
        for i in range(self.combo_theme.count()):
            if self.combo_theme.itemData(i) == cur_theme:
                self.combo_theme.setCurrentIndex(i)
                break
        self.combo_theme.currentIndexChanged.connect(self.emit_cache_settings)
        tl.addWidget(self.combo_theme)
        tl.addStretch()
        vb.addLayout(tl)

        # ---- language box
        self.lang_box = QFrame()
        self.lang_box.setObjectName("settings_box")
        lb = QVBoxLayout(self.lang_box)
        lb.setContentsMargins(16, 14, 16, 14)
        lb.setSpacing(8)
        lb.addWidget(QLabel(t("settings_lang_box")))
        self.lbl_lang = QLabel(t("settings_lang_current", name=tr.name))
        self.lbl_lang.setStyleSheet(f"color: {COLOR_MUTED};")
        lb.addWidget(self.lbl_lang)
        lb_row = QHBoxLayout()
        btn_load_lang = QPushButton(t("settings_lang_load"))
        btn_load_lang.clicked.connect(self.language_load_requested.emit)
        btn_export_lang = QPushButton(t("settings_lang_export"))
        btn_export_lang.clicked.connect(self.language_export_requested.emit)
        btn_reset_lang = QPushButton(t("settings_lang_reset"))
        btn_reset_lang.clicked.connect(self.language_reset_requested.emit)
        lb_row.addWidget(btn_load_lang)
        lb_row.addWidget(btn_export_lang)
        lb_row.addWidget(btn_reset_lang)
        lb_row.addStretch()
        lb.addLayout(lb_row)

        # ---- share box
        self.share_box = QFrame()
        self.share_box.setObjectName("settings_box")
        sb = QVBoxLayout(self.share_box)
        sb.setContentsMargins(16, 14, 16, 14)
        sb.setSpacing(8)
        sb.addWidget(QLabel(t("settings_share_box")))
        hint = QLabel(t("settings_share_hint"))
        hint.setStyleSheet(f"color: {COLOR_MUTED};")
        hint.setWordWrap(True)
        sb.addWidget(hint)
        sb_row = QHBoxLayout()
        btn_exp_cal = QPushButton(t("settings_share_export"))
        btn_exp_cal.clicked.connect(self.calendar_export_requested.emit)
        btn_imp_cal = QPushButton(t("settings_share_import"))
        btn_imp_cal.clicked.connect(self.calendar_import_requested.emit)
        sb_row.addWidget(btn_exp_cal)
        sb_row.addWidget(btn_imp_cal)
        sb_row.addStretch()
        sb.addLayout(sb_row)

        # ---- data safety box
        self.data_box = QFrame()
        self.data_box.setObjectName("settings_box")
        db = QVBoxLayout(self.data_box)
        db.setContentsMargins(16, 14, 16, 14)
        db.setSpacing(8)
        db.addWidget(QLabel(t("settings_data_box")))
        dhint = QLabel(t("settings_data_hint"))
        dhint.setStyleSheet(f"color: {COLOR_MUTED};")
        dhint.setWordWrap(True)
        db.addWidget(dhint)
        # configurable trash folder + retention (min 7d)
        db.addWidget(QLabel(t("settings_trash_box")))
        _thint = QLabel(t("settings_trash_hint"))
        _thint.setStyleSheet(f"color: {COLOR_MUTED};")
        _thint.setWordWrap(True)
        db.addWidget(_thint)
        _tb = QHBoxLayout()
        self.lbl_trash_path = QLabel(
            str(cache_config.get("trash_dir", str(TRASH_DIR))))
        self.lbl_trash_path.setStyleSheet(f"color: {COLOR_MUTED};")
        _btn_trash = QPushButton(t("settings_trash_change"))
        _btn_trash.clicked.connect(self.change_trash_path)
        _tb.addWidget(QLabel(t("settings_trash_path")))
        _tb.addWidget(self.lbl_trash_path)
        _tb.addWidget(_btn_trash)
        db.addLayout(_tb)
        _tr = QHBoxLayout()
        _tr.addWidget(QLabel(t("settings_trash_retention")))
        self.spin_trash_days = QSpinBox()
        # Q3: min 7 days - idiot-proof guard
        self.spin_trash_days.setRange(7, 365)
        try:
            _trd = int(cache_config.get("trash_retention_days", 7))
        except (TypeError, ValueError):
            _trd = 7
        self.spin_trash_days.setValue(max(7, min(365, _trd)))
        self.spin_trash_days.setSuffix(t("settings_cache_days"))
        self.spin_trash_days.valueChanged.connect(self.emit_cache_settings)
        _tr.addWidget(self.spin_trash_days)
        _tr.addStretch()
        db.addLayout(_tr)
        db_row = QHBoxLayout()
        btn_find = QPushButton(t("settings_find_missing"))
        btn_find.clicked.connect(self.data_find_missing_requested.emit)
        btn_restore = QPushButton(t("settings_restore_backup"))
        btn_restore.clicked.connect(self.data_restore_backup_requested.emit)
        db_row.addWidget(btn_find)
        db_row.addWidget(btn_restore)
        db_row.addStretch()
        db.addLayout(db_row)

        # ---- hotkey editor box
        self.hk_box = QFrame()
        self.hk_box.setObjectName("settings_box")
        hk = QVBoxLayout(self.hk_box)
        hk.setContentsMargins(16, 14, 16, 14)
        hk.setSpacing(8)
        self.lbl_hk_title = QLabel(t("hk_section_title"))
        self.lbl_hk_title.setStyleSheet(
            f"color: {COLOR_TEXT}; font-weight: 600; font-size: 14px;")
        hk.addWidget(self.lbl_hk_title)
        self.lbl_hk_hint = QLabel(t("hk_section_hint"))
        self.lbl_hk_hint.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 11px;")
        self.lbl_hk_hint.setWordWrap(True)
        hk.addWidget(self.lbl_hk_hint)
        # profile combo
        _prof_row = QHBoxLayout()
        self.lbl_hk_profile = QLabel(t("hk_profile_label"))
        self.combo_hk_profile = QComboBox()
        self.combo_hk_profile.addItem(
            t("hk_profile_auto", os=_platform_key().capitalize()), "auto")
        self.combo_hk_profile.addItem("Windows", "windows")
        self.combo_hk_profile.addItem("macOS", "darwin")
        self.combo_hk_profile.addItem("Linux", "linux")
        _cur_prof = cache_config.get("hotkey_profile", "auto")
        if _cur_prof not in ("auto", "windows", "darwin", "linux"):
            _cur_prof = "auto"
        for _i in range(self.combo_hk_profile.count()):
            if self.combo_hk_profile.itemData(_i) == _cur_prof:
                self.combo_hk_profile.setCurrentIndex(_i)
                break
        self.combo_hk_profile.currentIndexChanged.connect(
            self._on_hk_profile_changed)
        _prof_row.addWidget(self.lbl_hk_profile)
        _prof_row.addWidget(self.combo_hk_profile)
        _prof_row.addStretch()
        hk.addLayout(_prof_row)
        # sub-section header: Global
        self._hk_global_header = QLabel(t("hk_btn_global"))
        self._hk_global_header.setStyleSheet(
            f"color: {COLOR_MUTED}; font-weight: 600; margin-top: 6px;")
        hk.addWidget(self._hk_global_header)

        def _make_hk_row(_aid):
            _row = QHBoxLayout()
            _lbl = QLabel(t(HOTKEY_ACTIONS[_aid]["label"]))
            _lbl.setMinimumWidth(180)
            _edt = QKeySequenceEdit()
            # (C6): setMaximumSequenceLength added in
            # Qt 6.5. On PySide6 6.0-6.4 (README says 6.0+) the
            # attribute is missing -> AttributeError at startup
            # when Settings tab constructs. Guard for compatibility.
            if hasattr(_edt, "setMaximumSequenceLength"):
                _edt.setMaximumSequenceLength(1)
            _edt.setToolTip("Click to change shortcut")
            _edt.setFixedWidth(220)  # fits Ctrl+Shift+Alt+Key
            _edt.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            _v = _resolved_hotkey(cache_config, _aid)
            _edt.setKeySequence(QKeySequence(_v) if _v else QKeySequence())
            _edt.editingFinished.connect(
                lambda _a=_aid, _e=_edt: self._on_hk_changed(
                    _a, _e.keySequence()))
            _btn = QPushButton("\u2715")
            _btn.setFixedWidth(30)
            _btn.setCursor(Qt.CursorShape.PointingHandCursor)
            _btn.setStyleSheet(f"QPushButton {{ color: {COLOR_DANGER}; font-weight: bold; "
                               f"border: 1px solid {COLOR_BORDER}; border-radius: 4px; }} "
                               f"QPushButton:hover {{ background: {COLOR_HOVER}; border-color: {COLOR_DANGER}; }}")
            _btn.setToolTip(t("hk_disable"))
            _btn.clicked.connect(lambda _=False, _a=_aid: self._on_hk_clear(_a))
            _row.addWidget(_lbl)
            _row.addWidget(_edt)
            _row.addWidget(_btn)
            _row.addStretch()  # keep widgets left-aligned
            hk.addLayout(_row)
            self._hk_edits[_aid] = _edt
            self._hk_labels[_aid] = _lbl

        for _aid in ("undo", "redo", "redo_alt", "copy", "cut",
                     "paste", "delete", "select_all", "remove_event"):
            _make_hk_row(_aid)

        # sub-section header: gallery / viewer
        self._hk_gallery_header = QLabel(t("hk_btn_gallery"))
        self._hk_gallery_header.setStyleSheet(
            f"color: {COLOR_MUTED}; font-weight: 600; margin-top: 6px;")
        hk.addWidget(self._hk_gallery_header)

        for _aid in ("assign_event", "prev_image", "next_image",
                     "close_viewer"):
            _make_hk_row(_aid)

        self._btn_hk_reset = QPushButton(t("hk_reset_all"))
        self._btn_hk_reset.clicked.connect(self._on_hk_reset)
        hk.addWidget(self._btn_hk_reset)

        layout.addWidget(self.cache_box)
        layout.addWidget(self.view_box)
        layout.addWidget(self.hk_box)       # 
        layout.addWidget(self.lang_box)
        layout.addWidget(self.share_box)
        layout.addWidget(self.data_box)
        layout.addStretch()
        _scroll.setWidget(_content)
        _outer.addWidget(_scroll)
        self.update_theme()

    def retranslate(self):
        """Rebuild priority list labels after a language change."""
        prios = []
        for i in range(self.list_widget.count()):
            prios.append(self.list_widget.item(i).data(ROLE_FILEPATH))
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for p in prios:
            self._add_priority_item(p)
        self.list_widget.blockSignals(False)
        self.update_language_label()
        # re-label hotkey editor widgets. currentIndex of
        # profile combo preserved; only item 0 (Auto) is localized
        # (Windows/macOS/Linux are brand names, not translated).
        try:
            self.lbl_hk_title.setText(t("hk_section_title"))
            self.lbl_hk_hint.setText(t("hk_section_hint"))
            self.lbl_hk_profile.setText(t("hk_profile_label"))
            self._hk_global_header.setText(t("hk_btn_global"))
            self._hk_gallery_header.setText(t("hk_btn_gallery"))
            for _aid, _lbl in self._hk_labels.items():
                _lbl.setText(t(HOTKEY_ACTIONS[_aid]["label"]))
            self.combo_hk_profile.setItemText(
                0, t("hk_profile_auto", os=_platform_key().capitalize()))
            if self._btn_hk_reset is not None:
                self._btn_hk_reset.setText(t("hk_reset_all"))
        except (AttributeError, RuntimeError):
            pass

    def _add_priority_item(self, prio):
        if prio == PRIO_CTIME and platform.system() == "Linux":
            return  # creation time unsupported on Linux
        if prio == PRIO_FOLDER:
            name = t("prio_folder")
        elif prio == PRIO_EXIF:
            name = t("prio_exif")
        elif prio == PRIO_CTIME:
            name = t("prio_ctime")
        elif prio == PRIO_MTIME:
            name = t("prio_mtime")
        elif str(prio).startswith("custom:"):
            name = t("prio_custom", fmt=str(prio).split(":", 1)[1])
        else:
            name = str(prio)
        item = QListWidgetItem(name)
        item.setData(ROLE_FILEPATH, prio)
        self.list_widget.addItem(item)

    def add_custom_format(self):
        text, ok = QInputDialog.getText(self, t("title_new_format"), t("label_format_hint"))
        if ok and text.strip():
            fmt = text.strip()
            # NB-102: reject formats with no Y/M/D tokens. Previously
            # "ABC" compiled to a literal regex that never matched —
            # user saw 0 files with no diagnostic.
            if not any(_c in fmt for _c in ("Y", "M", "D")):
                msg_warning(self, t("error_title"),
                            t("error_invalid_format"))
                return
            self._add_priority_item(f"custom:{fmt}")
            self.save_priorities()

    def remove_custom_format(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        if str(item.data(ROLE_FILEPATH)).startswith("custom:"):
            self.list_widget.takeItem(self.list_widget.row(item))
            self.save_priorities()

    # ---- hotkey editor handlers
    def _on_hk_changed(self, action_id, ks):
        """Save new portable sequence. Empty string = disabled.
        Conflict: if this sequence is already bound to a different
        action (via _resolved_hotkey of current config), confirm
        before applying. Cancel restores previous displayed value.
        """
        portable = ks.toString()
        current = _resolved_hotkey(self._cache_cfg, action_id)
        if portable == current:
            return  # no-op (editingFinished fires on focus-out too)
        if portable:
            # NB-83: only same-context actions can conflict. Esc for
            # close_viewer (widget-ctx) cannot clash with an app-ctx
            # action on Esc — Qt shortcut contexts are disjoint
            # namespaces at runtime. Previously every action was
            # scanned regardless of ctx, producing false conflicts.
            _my_ctx = HOTKEY_ACTIONS[action_id].get("ctx", "app")
            conflict_aid = None
            for other in HOTKEY_ACTIONS:
                if other == action_id:
                    continue
                if HOTKEY_ACTIONS[other].get("ctx", "app") != _my_ctx:
                    continue
                if _resolved_hotkey(self._cache_cfg, other) == portable:
                    conflict_aid = other
                    break
            if conflict_aid is not None:
                _reply = QMessageBox.question(
                    self, t("hk_section_title"),
                    t("hk_conflict",
                      a=t(HOTKEY_ACTIONS[action_id]["label"]),
                      b=t(HOTKEY_ACTIONS[conflict_aid]["label"])),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    defaultButton=QMessageBox.StandardButton.No)
                if _reply != QMessageBox.StandardButton.Yes:
                    edt = self._hk_edits.get(action_id)
                    if edt is not None:
                        edt.blockSignals(True)
                        edt.setKeySequence(
                            QKeySequence(current) if current
                            else QKeySequence())
                        edt.blockSignals(False)
                    return
        self.hotkey_changed.emit(action_id, portable)

    def _on_hk_clear(self, action_id):
        edt = self._hk_edits.get(action_id)
        if edt is None:
            return
        edt.blockSignals(True)
        edt.setKeySequence(QKeySequence())
        edt.blockSignals(False)
        self.hotkey_changed.emit(action_id, "")

    def _on_hk_reset(self):
        # Reset UI to platform defaults for current (auto) profile
        for aid, edt in self._hk_edits.items():
            _d = _default_hotkey(aid)  # auto profile
            edt.blockSignals(True)
            edt.setKeySequence(QKeySequence(_d) if _d else QKeySequence())
            edt.blockSignals(False)
        # Reset combo to auto
        self.combo_hk_profile.blockSignals(True)
        for i in range(self.combo_hk_profile.count()):
            if self.combo_hk_profile.itemData(i) == "auto":
                self.combo_hk_profile.setCurrentIndex(i)
                break
        self.combo_hk_profile.blockSignals(False)
        self.hotkey_reset_requested.emit()

    def _on_hk_profile_changed(self, _idx):
        profile = self.combo_hk_profile.currentData()
        self.hotkey_profile_changed.emit(profile)

    def update_theme(self):
        self.title.setStyleSheet(f"font-size: 26px; font-weight: 700; color: {COLOR_TEXT};")
        self.desc.setStyleSheet(f"color: {COLOR_MUTED};")
        # refresh inline-styled labels too
        try:
            self.lbl_cache_path.setStyleSheet(f"color: {COLOR_MUTED};")
            self.lbl_cache_size.setStyleSheet(f"color: {COLOR_MUTED};")
            self.lbl_lang.setStyleSheet(f"color: {COLOR_MUTED};")
        except AttributeError:
            pass
        self.list_widget.setStyleSheet(
            f"QListWidget {{ font-size: 15px; border: 1px solid {COLOR_BORDER}; border-radius: 10px; "
            f"padding: 6px; background: {COLOR_SURFACE}; }} "
            f"QListWidget::item {{ padding: 14px; border-bottom: 1px solid {COLOR_HOVER}; "
            f"border-radius: 6px; color: {COLOR_TEXT}; }} "
            f"QListWidget::item:selected {{ background: {COLOR_PRIMARY_LT}; color: {COLOR_TEXT}; }}")
        # QKeySequenceEdit theme. QKeySequenceEdit is not a
        # QLineEdit subclass -- style the widget and its inner
        # QLineEdit separately for both light and dark theme.
        try:
            _hk_qss = (
                f"QKeySequenceEdit {{ border: none; background: transparent; padding: 0; }} "
                f"QKeySequenceEdit QLineEdit {{ "
                f"background-color: {COLOR_BG}; "
                f"border: 1px solid {COLOR_MUTED}; "
                f"border-radius: 4px; "
                f"padding: 4px 8px; "
                f"color: {COLOR_TEXT}; "
                f"font-weight: bold; }} "
                f"QKeySequenceEdit QLineEdit:hover {{ "
                f"border-color: {COLOR_PRIMARY}; "
                f"background-color: {COLOR_SURFACE}; }} "
                f"QKeySequenceEdit QLineEdit:focus {{ "
                f"border: 2px solid {COLOR_PRIMARY}; "
                f"background-color: {COLOR_SURFACE}; }}"
            )
            for _edt in self._hk_edits.values():
                _edt.setStyleSheet(_hk_qss)
        except (AttributeError, RuntimeError):
            pass

    def update_language_label(self):
        self.lbl_lang.setText(t("settings_lang_current", name=tr.name))

    def emit_cache_settings(self):
        self.cache_settings_changed.emit({
            "auto_clean_enabled": self.chk_auto.isChecked(),
            "auto_clean_days": self.spin_days.value(),
            "dups_per_row": self.combo_dups.currentData(),
            "undo_limit": self.spin_undo.value(),
            "theme": self.combo_theme.currentData(),
            "safe_delete_enabled": self.chk_safe_delete.isChecked(),   # 
            "safe_delete_threshold": self.spin_safe_delete.value(),    # 
            "trash_retention_days": self.spin_trash_days.value(),      # 
            "safe_media_add_enabled": self.chk_safe_media_add.isChecked(),    # 
            "safe_media_add_threshold": self.spin_safe_media.value(),         # 
        })

    def change_cache_path(self):
        folder = QFileDialog.getExistingDirectory(self, t("settings_cache_change"))
        if not folder:
            return
        # warn if chosen folder has foreign *.jpg. Pattern
        # filtering already protects them, but user should know
        # what they are doing.
        try:
            _cache_re = re.compile(r"^(q_)?[0-9a-f]{40}[.]jpg$")
            _foreign = [
                f for f in Path(folder).glob("*.jpg")
                if not _cache_re.match(f.name)
            ]
            if _foreign:
                _reply = QMessageBox.question(
                    self, t("title_cache_warning"),
                    t("warn_cache_folder", n=len(_foreign)),
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    defaultButton=QMessageBox.StandardButton.No)
                if _reply != QMessageBox.StandardButton.Yes:
                    return
        except Exception as _e:
            logger.debug(f"cache-folder check failed: {_e}")
        self.lbl_cache_path.setText(folder)
        # NB-139: refresh "Used: N MB" so the label matches the new folder.
        self.update_cache_size({"cache_dir": folder})
        self.cache_settings_changed.emit({"cache_dir": folder})

    def change_trash_path(self):
        """pick a trash folder. Validation lives in MainWindow
        (set_trash_dir); on rejection, MainWindow rolls the label back."""
        folder = QFileDialog.getExistingDirectory(
            self, t("settings_trash_change"),
            self.lbl_trash_path.text())
        if folder:
            self.lbl_trash_path.setText(folder)
            self.cache_settings_changed.emit({"trash_dir": folder})

    def update_cache_size(self, cache_config):
        self.lbl_cache_size.setText(t("settings_cache_size",
                                      size=get_cache_size_mb(cache_config.get("cache_dir", ""))))

    def force_clear_cache(self):
        cache_dir = self.lbl_cache_path.text()
        n = clear_cache(cache_dir)
        self.update_cache_size({"cache_dir": cache_dir})
        self.cache_cleared.emit()
        QMessageBox.information(self, t("info_done"), t("info_cache_cleared", n=n))

    def save_priorities(self):
        prios = [self.list_widget.item(i).data(ROLE_FILEPATH) for i in range(self.list_widget.count())]
        self.priorities_changed.emit(prios)


class HelpWidget(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        self.browser = QTextBrowser()
        layout.addWidget(self.browser)
        self.update_theme()

    def update_theme(self):
        self.browser.setStyleSheet(
            f"border: none; font-size: 15px; background-color: {COLOR_BG}; color: {COLOR_TEXT};")
        sections = [("help_import", "help_import_body"), ("help_views", "help_views_body"),
                    ("help_gallery", "help_gallery_body"), ("help_viewer", "help_viewer_body"),
                    ("help_dups", "help_dups_body"), ("help_undo", "help_undo_body"),
                    ("help_themes", "help_themes_body")]
        html = f'<h1 style="color:{COLOR_TEXT};">{t("help_title")}</h1>'
        _mod = tr._current.get("modifier_key", "Ctrl")
        _del = tr._current.get("delete_key", "Del")
        for head, body in sections:
            try:
                _body = t(body).format(modifier=_mod, delete=_del)
            except (KeyError, IndexError, ValueError):
                _body = t(body)
            html += f'<h3 style="color:{COLOR_TEXT};">{t(head)}</h3><p>{_body}</p>'
        self.browser.setHtml(html)


class CalendarManagerWidget(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        # NB-25: cache event keys to skip list rebuilds
        self._cached_events = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        self.title = QLabel(t("manager_title"))
        layout.addWidget(self.title)

        self.tabs = QTabWidget()
        cal_tab = QWidget()
        cal_layout = QVBoxLayout(cal_tab)
        self.combo_cal = QComboBox()
        self.combo_cal.setMinimumWidth(280)
        self.combo_cal.setStyleSheet("padding: 10px; font-size: 15px;")
        self.combo_cal.currentTextChanged.connect(self.main_window.on_cal_changed)
        btn_row = QHBoxLayout()
        for label, slot in [
            (t("manager_new"), self.main_window.create_calendar),
            (t("manager_rename"), self.main_window.rename_calendar),
            (t("manager_load"), self.main_window.load_external_calendar),
            (t("manager_save"), self.main_window.export_calendar),
            (t("manager_clear"), self.main_window.clear_calendar),
            (t("manager_delete_cal"), self.main_window.delete_calendar),
        ]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            btn_row.addWidget(b)
        btn_row.addStretch()
        cal_layout.addWidget(QLabel(t("manager_active_cal")))
        cal_layout.addWidget(self.combo_cal)
        cal_layout.addLayout(btn_row)
        cal_layout.addStretch()

        ev_tab = QWidget()
        ev_layout = QVBoxLayout(ev_tab)
        self.list_events = QListWidget()
        btn_ev = QHBoxLayout()
        for label, slot in [
            (t("manager_add"), self.main_window.create_event),
            (t("manager_rename_ev"), self.main_window.rename_event),
            (t("manager_delete"), self.main_window.delete_event),
        ]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            btn_ev.addWidget(b)
        ev_layout.addWidget(QLabel(t("manager_events_list")))
        ev_layout.addWidget(self.list_events)
        ev_layout.addLayout(btn_ev)

        self.tabs.addTab(cal_tab, t("manager_tab_cal"))
        self.tabs.addTab(ev_tab, t("manager_tab_ev"))
        layout.addWidget(self.tabs)
        self.update_theme()

    def update_theme(self):
        self.title.setStyleSheet(
            f"font-size: 26px; font-weight: 700; color: {COLOR_TEXT}; margin-bottom: 10px;")
        self.tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: 1px solid {COLOR_BORDER}; border-radius: 8px; "
            f"background: {COLOR_SURFACE}; }} "
            f"QTabBar::tab {{ padding: 10px 20px; background: {COLOR_HOVER}; "
            f"border-top-left-radius: 8px; border-top-right-radius: 8px; color: {COLOR_TEXT}; }} "
            f"QTabBar::tab:selected {{ background: {COLOR_SURFACE}; font-weight: bold; color: {COLOR_PRIMARY}; }}")
        # style list_events explicitly. Without this, QListWidget
        # falls back to default palette Base color (white in Fusion
        # dark theme, since setPalette is never called) — appeared as
        # a white box inside the grey Manage → Events panel.
        try:
            self.list_events.setStyleSheet(
                f"QListWidget {{ font-size: 15px; "
                f"border: 1px solid {COLOR_BORDER}; border-radius: 10px; "
                f"padding: 6px; background: {COLOR_SURFACE}; }} "
                f"QListWidget::item {{ padding: 14px; "
                f"border-bottom: 1px solid {COLOR_HOVER}; "
                f"border-radius: 6px; color: {COLOR_TEXT}; }} "
                f"QListWidget::item:selected {{ "
                f"background: {COLOR_PRIMARY_LT}; color: {COLOR_TEXT}; }}")
        except AttributeError:
            pass

    def update_combo(self, calendars, current_cal):
        self.combo_cal.blockSignals(True)
        self.combo_cal.clear()
        self.combo_cal.addItems(calendars.keys())
        self.combo_cal.setCurrentText(current_cal)
        self.combo_cal.blockSignals(False)
        # NB-25: rebuild the events list only when the key set changed.
        # Previously ran on every calendar switch / dup-rescan — O(events)
        # work with no visible change.
        _events_key = tuple(self.main_window.categories.keys())
        if _events_key != self._cached_events:
            self._cached_events = _events_key
            self.list_events.clear()
            for ev in _events_key:
                self.list_events.addItem(ev)


# ============================================================
#                     MAIN WINDOW
# ============================================================
def _dispatch_forward_text(op):
    """(U1): forward copy/cut/select_all/delete to a
    focused text widget, if any.

    QAction with ApplicationShortcut context swallows Ctrl+C/X/A
    and Del before they reach the focused QLineEdit / QTextEdit.
    Our dispatchers must therefore re-emit the intent to the
    widget, otherwise text editing in dialogs / Settings / Help
    stops working. Returns True if the operation was forwarded
    (caller must not run file-operation logic).
    """
    fw = QApplication.focusWidget()
    if fw is None:
        return False
    if op == "copy" and isinstance(
            fw, (QLineEdit, QTextEdit, QPlainTextEdit, QTextBrowser)):
        try:
            fw.copy()
        except Exception:
            pass
        return True
    if op == "cut" and isinstance(
            fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
        try:
            fw.cut()
        except Exception:
            pass
        return True
    if op == "select_all" and isinstance(
            fw, (QLineEdit, QTextEdit, QPlainTextEdit, QTextBrowser)):
        try:
            fw.selectAll()
        except Exception:
            pass
        return True
    if op == "delete" and isinstance(
            fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
        # QTextEdit / QPlainTextEdit have no direct del_() method.
        # Post a synthetic key event back to the widget -- Qt will
        # deliver it after our handler returns, so the character
        # is deleted normally by the widget.
        try:
            from PySide6.QtGui import QKeyEvent
            from PySide6.QtCore import QEvent
            # NB-118: resolve the actual hotkey from config instead
            # of hardcoding per-platform. On macOS with a
            # platform_defaults override, or a user-rebound delete
            # to Backspace on Windows, the physically-pressed key
            # matches what we forward.
            _key = None
            try:
                _aw = QApplication.activeWindow()
                if _aw is not None and hasattr(_aw, "config"):
                    _seq = _resolved_hotkey(_aw.config, "delete")
                    if _seq and "backspace" in _seq.lower():
                        _key = Qt.Key.Key_Backspace
                    elif _seq:
                        _key = Qt.Key.Key_Delete
            except Exception:
                pass
            if _key is None:
                _key = (Qt.Key.Key_Backspace
                        if platform.system() == "Darwin"
                        else Qt.Key.Key_Delete)
            _ev = QKeyEvent(
                QEvent.Type.KeyPress, _key,
                Qt.KeyboardModifier.NoModifier)
            QApplication.postEvent(fw, _ev)
        except Exception:
            pass
        return True
    return False


def _busy_guard(method):
    """block concurrent destructive operations via self._busy flag."""
    @functools.wraps(method)  # preserves __name__
    def wrapper(self, *args, **kwargs):
        if getattr(self, "_busy", False):
            try:
                self.statusBar().showMessage(t("status_busy"), 2000)
            except Exception:
                pass
            return None
        # enforce invariant "_busy never True during scan"
        # BEFORE setting _busy. Without this, calling a @_busy_guard
        # method during a scan briefly sets _busy=True — scan_finished
        # sees it and defers via QTimer.singleShot(200) (the very
        # pattern architecturally eliminated). In practice the
        # method returns immediately via its own _guard_scan, but the
        # micro-window exists. Checking here closes it.
        if getattr(self, "_scan_in_progress", False):
            try:
                self.statusBar().showMessage(t("status_wait_scan"), 2000)
            except Exception:
                pass
            return None
        self._busy = True
        try:
            return method(self, *args, **kwargs)
        finally:
            self._busy = False
    return wrapper


def _busy_guard_text_aware(method):
    """NB-79: like _busy_guard but bypasses the guard when a text
    widget has focus. undo/redo/paste_files have their own
    forward-to-widget logic for QLineEdit/QTextEdit and must not be
    blocked during scans — text editing is unrelated to file ops.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit, QTextBrowser)):
            return method(self, *args, **kwargs)
        if getattr(self, "_busy", False):
            try:
                self.statusBar().showMessage(t("status_busy"), 2000)
            except Exception:
                pass
            return None
        if getattr(self, "_scan_in_progress", False):
            try:
                self.statusBar().showMessage(t("status_wait_scan"), 2000)
            except Exception:
                pass
            return None
        self._busy = True
        try:
            return method(self, *args, **kwargs)
        finally:
            self._busy = False
    return wrapper


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(self._window_title())
        self.setMinimumSize(1000, 620)
        self.resize(1300, 860)
        self.setAcceptDrops(True)

        self.calendars = {"Default": {}}
        self.current_cal_name = "Default"
        self.duplicates = {"Default": set()}
        self.categories = {}
        self.last_dups = None
        self.last_dups_groups = []

        self.priority = list(DEFAULT_PRIORITY)
        self.json_path = str(DATA_DIR / "calendar_data.json")

        self.history = []
        self.undo_actions = []
        self.redo_stack = []
        self.redo_actions = []  # 
        self.undo_limit = 30
        self.config = {
            "cache_dir": str(DEFAULT_CACHE_DIR),
            "auto_clean_enabled": True,
            "auto_clean_days": 7,
            "dups_per_row": 2,
            "theme": "light",
            "undo_limit": 30,
            "safe_delete_enabled": True,    # 
            "safe_delete_threshold": 50,    # 
            "trash_dir": str(TRASH_DIR),    # 
            "trash_retention_days": 7,      # 
            "safe_media_add_enabled": True,     # 
            "safe_media_add_threshold": 50,     # 
        }
        self.clipboard_files = []
        self.clipboard_action = None
        self.current_gallery_context = None
        self.scan_thread = None
        self.dup_thread = None
        self._scanned_files = {}
        self._scan_cal_name = None
        self._scan_cancelled = False
        self._dup_cal_name = None
        self._closing_threads = []
        self._dups_stale = False  # tracks rotate-in-viewer
        self._busy = False  # reentrancy guard
        self._actions = {}  # QAction registry for hotkey rebind
        self._scan_in_progress = False   # block destructive ops
        self._scan_dialog = None         # ScanProgressDialog ref
        self._last_view_index = 0  # last successfully applied view
        self._gallery_needs_rebuild = False  # rebuild only after delete in viewer
        self._cut_watch_timer = QTimer(self)  # detect external cut+paste
        self._cut_watch_timer.setInterval(1500)
        self._cut_watch_timer.timeout.connect(self._watch_cut_files)
        self._cut_missing_streak = {}  # {filepath: N consecutive missing}
        self._cut_confirmation_ticks = 3  # 3 × 1.5s = 4.5s before purge
        self.was_in_dups = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._do_save_json)
        self._last_saved_count = -1  # cached count for save backup check
        # split counters so calendar and category losses trigger
        # backup independently (regression: merging both into one
        # total diluted the calendar-only threshold).
        self._last_saved_cal_count = -1
        self._last_saved_cat_count = -1

        self.load_config()
        self.config["undo_limit"] = self.undo_limit
        # apply the configured trash folder. On failure (folder
        # removed/renamed), silently fall back to the default; TRASH_DIR
        # stays as-is and the user sees a status message.
        _td = self.config.get("trash_dir")
        if _td:
            # rollback config on init failure. Otherwise the
            # UI shows the old (invalid) path while TRASH_DIR is at
            # default — user thinks trash is somewhere else.
            if not set_trash_dir(_td):
                logger.warning(
                    f"configured trash_dir invalid: {_td!r}, "
                    f"falling back to {TRASH_DIR}")
                self.config["trash_dir"] = str(TRASH_DIR)
        # (C2): sanitize theme. Corrupted config
        # with "theme": [] or {} crashes apply_theme --
        # "name not in THEMES" on unhashable raises TypeError
        # at startup (apply_theme is called from __init__).
        _theme = self.config.get("theme", "light")
        if not isinstance(_theme, str) or _theme not in THEMES:
            self.config["theme"] = "light"
        apply_theme(self.config["theme"])

        self.setup_ui()
        self.apply_styles()
        self.setup_shortcuts()
        self._build_menubar()
        self.load_last_session()
        QTimer.singleShot(2000, self.check_auto_clean)
        self._update_undo_buttons()
        # beta marker on non-Windows platforms
        if BETA_PLATFORM:
            logger.warning(
                "BETA platform: only Windows is tested by the maintainer "
                f"(detected {platform.system()})"
            )
            QTimer.singleShot(500, lambda: self.statusBar().showMessage(
                t("beta_status_msg"), 10000
            ))

    # ---- config
    def load_config(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    logger.warning(
                        f"load_config: top-level JSON is "
                        f"{type(data).__name__}, resetting")
                    data = {}
                # (P1): "cache": null (or non-dict) previously
                # raised in self.config.update(None) -- outer except
                # swallowed it, all settings reset to defaults, and
                # next save_config overwrote the file. Retention 365
                # reverted to 7 (old trash could purge) + all user
                # prefs lost. Validate type first.
                _cache_cfg = data.get("cache", {})
                if not isinstance(_cache_cfg, dict):
                    logger.warning(
                        f"load_config: cache is "
                        f"{type(_cache_cfg).__name__}, resetting")
                    _cache_cfg = {}
                self.config.update(_cache_cfg)
                # (P0): cache_dir MUST be a non-empty absolute dir.
                # Empty string -> Path(".") -> _AutoCleanWorker would
                # glob("*.jpg") in CWD and unlink user photos. Same for
                # clear_cache / get_cache_path. Reject "." too (would
                # also hit CWD through Path("")).
                _cd = self.config.get("cache_dir")
                try:
                    _cd_ok = (
                        isinstance(_cd, str)
                        and _cd.strip() not in ("", ".")
                        and Path(_cd).is_absolute()
                    )
                    # (P0): also reject root; resolve()
                    if _cd_ok:
                        _cp = Path(_cd.strip())
                        try:
                            _cp = _cp.resolve()
                        except Exception:
                            pass
                        if _cp.parent == _cp:
                            _cd_ok = False
                        elif _cp.exists() and not _cp.is_dir():
                            _cd_ok = False
                except Exception:
                    _cd_ok = False
                if not _cd_ok:
                    logger.warning(
                        f"load_config: invalid cache_dir {_cd!r}, "
                        f"falling back to default")
                    self.config["cache_dir"] = str(DEFAULT_CACHE_DIR)
                # isolate undo_limit parse. Previously a bad value
                # (e.g. "abc") raised ValueError, jumped to outer except,
                # and SKIPPED the entire sanitize block below →
                # dups_per_row="abc" stayed, then crashed in
                # open_duplicates_gallery with ValueError.
                try:
                    _ul = int(data.get("undo_limit",
                                       _cache_cfg.get("undo_limit", 30)))
                except (TypeError, ValueError):
                    _ul = 30
                self.undo_limit = max(1, min(200, _ul))  # clamp
                # (NB-32): sanitize numeric and bool config fields.
                # - numeric: corrupted values (e.g. dups_per_row="abc")
                #   would crash open_duplicates_gallery with ValueError.
                # - bool: JSON 0 / "0" / "false" was treated as ENABLED
                #   because `is not False` only catches Python False
                #   (0 is not False in Python). Only explicit false-y
                #   values disable; anything else -> True (safe default).
                def _clampi(key, default, lo, hi):
                    try:
                        _v = int(self.config.get(key, default))
                    except (TypeError, ValueError):
                        _v = default
                    self.config[key] = max(lo, min(hi, _v))
                _clampi("auto_clean_days", 7, 1, 365)
                _clampi("dups_per_row", 2, 1, 4)
                _clampi("safe_delete_threshold", 50, 1, 10000)
                _clampi("safe_media_add_threshold", 50, 10, 10000)
                _clampi("trash_retention_days", 7, 7, 365)
                # (C1+C3): sanitize hotkeys and
                # hotkey_profile. Corrupted config with
                # "hotkeys": null / [] / non-dict values would
                # crash at startup in _resolved_hotkey or in
                # QKeySequence(). Same class as //
                # cache_dir/trash_dir sanitize.
                _hk = self.config.get("hotkeys")
                if not isinstance(_hk, dict):
                    self.config["hotkeys"] = {}
                else:
                    self.config["hotkeys"] = {
                        _k: _v for _k, _v in _hk.items()
                        if isinstance(_k, str)
                        and isinstance(_v, str)
                    }
                _hp = self.config.get("hotkey_profile", "auto")
                if _hp not in ("auto", "windows", "darwin", "linux"):
                    self.config["hotkey_profile"] = "auto"
                for _bk in ("safe_delete_enabled",
                            "safe_media_add_enabled",
                            "auto_clean_enabled"):
                    _bv = self.config.get(_bk, True)
                    # NB-78: case-insensitive string check. "False" /
                    # "FALSE" / "No" / "Off" now correctly disable;
                    # previously only exact "false"/"False" matched.
                    if _bv is False:
                        self.config[_bk] = False
                    elif isinstance(_bv, str):
                        self.config[_bk] = _bv.strip().lower() not in (
                            "false", "0", "no", "off")
                    elif _bv == 0:
                        self.config[_bk] = False
                    else:
                        self.config[_bk] = True
            except Exception as e:
                logger.warning(f"load_config failed: {e}")

    def save_config(self):
        try:
            self.config["undo_limit"] = self.undo_limit
            _atomic_write_json(CONFIG_FILE,
                               {"cache": self.config, "undo_limit": self.undo_limit})
        except Exception:
            logger.exception("save_config failed")

    def check_auto_clean(self):
        # (NB-33): cache and trash cleanups are independent.
        # Before: auto_clean_enabled=False disabled BOTH — turning off
        # cache auto-clean silently stopped trash retention too,
        # contradicting D10 ("independent from cache retention").
        # Now: cache_days=0 tells worker to skip cache; trash_days is
        # always positive (7..365). Trash cleanup runs regardless.
        try:
            trash_days = int(self.config.get("trash_retention_days", 7))
        except (TypeError, ValueError):
            trash_days = 7
        trash_days = max(7, min(365, trash_days))
        if self.config.get("auto_clean_enabled", True):
            try:
                days = int(self.config.get("auto_clean_days", 7))
            except (TypeError, ValueError):
                days = 7
            days = max(1, min(365, days))
        else:
            days = 0  # signal _AutoCleanWorker: skip cache
        worker = _AutoCleanWorker(self.config["cache_dir"], days, trash_days)
        QThreadPool.globalInstance().start(worker)

    # ---- UI
    def setup_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_widget.setAcceptDrops(False)
        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        nav_bar = QWidget()
        nav_bar.setObjectName("nav_bar")
        nav_layout = QHBoxLayout(nav_bar)
        nav_layout.setContentsMargins(20, 12, 20, 12)
        nav_layout.setSpacing(6)
        self.brand = QLabel(t("brand"))
        self.btn_nav_cal = QPushButton(t("nav_calendar"))
        self.btn_nav_mgr = QPushButton(t("nav_manage"))
        self.btn_nav_set = QPushButton(t("nav_settings"))
        self.btn_nav_help = QPushButton(t("nav_help"))
        for b in (self.btn_nav_cal, self.btn_nav_mgr, self.btn_nav_set, self.btn_nav_help):
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setMinimumWidth(110)
        self.btn_nav_cal.setChecked(True)
        self.btn_nav_cal.clicked.connect(lambda: self.switch_tab(0))
        self.btn_nav_mgr.clicked.connect(lambda: self.switch_tab(1))
        self.btn_nav_set.clicked.connect(lambda: self.switch_tab(2))
        self.btn_nav_help.clicked.connect(lambda: self.switch_tab(3))
        nav_layout.addWidget(self.brand)
        # donation + social buttons
        self.social_container = QWidget()
        social_layout = QHBoxLayout(self.social_container)
        social_layout.setContentsMargins(0, 0, 0, 0)
        social_layout.setSpacing(6)

        def _grad_btn(text, tooltip, url, c1, c2, tc="#ffffff"):
            b = QPushButton(text)
            b.setToolTip(tooltip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                                stop:0 {c1}, stop:1 {c2});
                    color: {tc};
                    border: none;
                    border-radius: 12px;
                    padding: 4px 10px;
                    font-size: 11px;
                    font-weight: 600;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                                stop:0 {c2}, stop:1 {c1});
                }}
            """)
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
            return b

        btn_coffee = _grad_btn("☕", "Buy me a coffee",
                               "https://app.lava.top/products/c3372120-4da5-4016-9cf8-37872efcfd7d",
                               "#f6b26b", "#ffd966", "#333333")
        btn_cat = _grad_btn("🐈", "Feed my cat",
                            "https://app.lava.top/products/ae4d67de-490a-4c41-a52e-971f431a0ead",
                            "#b4a7d6", "#e6b8af", "#333333")
        btn_rocket = _grad_btn("🚀", "Support the author",
                               "https://app.lava.top/products/a9db29ed-7abf-46cb-a69d-0239b6b16aa5",
                               "#6fa8dc", "#9fc5e8", "#333333")
        btn_tg = _grad_btn("✈️", "Telegram",
                           "https://t.me/+lgzAbsvkPi9lYjIy",
                           "#0088cc", "#00aced")
        btn_lava = _grad_btn("🌋", "Lava.top",
                             "https://app.lava.top/tinomakesdev?tabId=subscriptions",
                             "#ff5e3a", "#ff2a68")

        social_layout.addWidget(btn_coffee)
        social_layout.addWidget(btn_cat)
        social_layout.addWidget(btn_rocket)
        social_layout.addSpacing(10)
        social_layout.addWidget(btn_tg)
        social_layout.addWidget(btn_lava)

        nav_layout.addWidget(self.social_container)
        nav_layout.addSpacing(10)
        # About button in nav bar — visible entry point. The
        # menubar Help→About also exists, but on Windows the menubar
        # sits inside the window frame and is easy to miss.
        self.btn_nav_about = QPushButton(t("nav_about"))
        self.btn_nav_about.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_nav_about.setMinimumWidth(90)
        self.btn_nav_about.clicked.connect(self._show_about_dialog)
        nav_layout.addWidget(self.btn_nav_cal)
        nav_layout.addWidget(self.btn_nav_mgr)
        nav_layout.addWidget(self.btn_nav_set)
        nav_layout.addWidget(self.btn_nav_help)
        nav_layout.addWidget(self.btn_nav_about)
        nav_layout.addStretch()
        layout.addWidget(nav_bar)

        self.cal_toolbar_widget = QWidget()
        self.cal_toolbar_widget.setObjectName("cal_toolbar")
        cal_tb = QHBoxLayout(self.cal_toolbar_widget)
        cal_tb.setContentsMargins(20, 10, 20, 10)
        cal_tb.setSpacing(10)

        self.btn_import = QPushButton(t("toolbar_add_folder"))
        self.btn_import.setObjectName("primary")
        self.btn_import.clicked.connect(self.import_folder)

        self.combo_view_mode = QComboBox()
        self.combo_view_mode.addItems([
            t("toolbar_view_days"), t("toolbar_view_months"),
            t("toolbar_view_years"), t("toolbar_view_events"),
            t("toolbar_view_duplicates"),
        ])
        # NB-49: default view = Years (index 2) on first run;
        # else restore config["last_view_mode"] if set to 0..3
        # (4 = Duplicates is never persisted - requires explicit scan).
        _initial_vm = self.config.get("last_view_mode", 2)
        if not isinstance(_initial_vm, int) or not (0 <= _initial_vm <= 3):
            _initial_vm = 2
        self.combo_view_mode.blockSignals(True)
        self.combo_view_mode.setCurrentIndex(_initial_vm)
        self.combo_view_mode.blockSignals(False)
        self._last_view_index = _initial_vm
        self.combo_view_mode.currentIndexChanged.connect(self.change_view_mode)

        self.btn_rescan_dups = QPushButton(t("toolbar_rescan"))
        self.btn_rescan_dups.clicked.connect(self.force_rescan_dups)
        self.btn_rescan_dups.hide()

        self.btn_cancel_scan = QPushButton(t("toolbar_cancel"))
        self.btn_cancel_scan.clicked.connect(self.cancel_scan)
        self.btn_cancel_scan.hide()

        self.btn_paste = QPushButton(t("toolbar_paste"))
        self.btn_paste.clicked.connect(self.paste_files)
        self.btn_paste.setEnabled(False)

        cal_tb.addWidget(self.btn_import)
        cal_tb.addWidget(self.combo_view_mode)
        cal_tb.addWidget(self.btn_rescan_dups)
        cal_tb.addWidget(self.btn_cancel_scan)
        cal_tb.addStretch()
        cal_tb.addWidget(self.btn_paste)
        layout.addWidget(self.cal_toolbar_widget)

        self.stack = QStackedWidget()

        self.calendar_view = CalendarView()
        self.calendar_view.item_selected.connect(self.open_gallery)

        self.manager_view = CalendarManagerWidget(self)

        self.settings_view = SettingsWidget(self.priority, self.config)
        self.settings_view.priorities_changed.connect(self.update_priority)
        self.settings_view.cache_settings_changed.connect(self.on_cache_settings_changed)
        self.settings_view.language_load_requested.connect(self.load_language_dialog)
        self.settings_view.language_export_requested.connect(self.export_language_dialog)
        self.settings_view.language_reset_requested.connect(self.reset_language)
        self.settings_view.calendar_export_requested.connect(self.export_calendar)
        self.settings_view.calendar_import_requested.connect(self.load_external_calendar)
        self.settings_view.data_find_missing_requested.connect(self.find_missing_files)
        self.settings_view.data_restore_backup_requested.connect(self.restore_from_backup)
        # hotkey editor signals
        self.settings_view.hotkey_changed.connect(self.on_hotkey_changed)
        self.settings_view.hotkey_profile_changed.connect(
            self.on_hotkey_profile_changed)
        self.settings_view.hotkey_reset_requested.connect(
            self.on_hotkey_reset_requested)

        self.help_view = HelpWidget()

        self.gallery_view = GalleryWidget()
        self.gallery_view.go_back.connect(self.show_calendar_from_gallery)
        self.gallery_view.open_image.connect(self.open_full_image)
        self.gallery_view.assign_category.connect(self.assign_category_dialog)
        self.gallery_view.remove_from_event.connect(self.remove_from_event)
        self.gallery_view.delete_duplicates.connect(self.on_gallery_delete_dups)
        self.gallery_view.undo_requested.connect(self.undo)
        self.gallery_view.redo_requested.connect(self.redo)
        self.gallery_view.btn_delete.clicked.connect(self.delete_selected_files)
        self.gallery_view.btn_copy.clicked.connect(self.copy_selected_files)
        self.gallery_view.btn_cut.clicked.connect(self.cut_selected_files)
        self.gallery_view.files_moved_externally.connect(
            self._on_files_moved_externally)  # 

        self.image_viewer = ImageViewer()
        self.image_viewer.go_back.connect(self._on_viewer_back)  # viewer back handler 
        self.image_viewer.file_deleted.connect(self.on_file_deleted_viewer)
        self.image_viewer.file_rotated.connect(self.on_file_rotated_viewer)
        self.image_viewer.assign_event.connect(lambda fp: self.assign_category_dialog([fp]))
        self.image_viewer.remove_from_event.connect(lambda fp: self.remove_from_event([fp]))
        self.image_viewer.copy_to_clipboard.connect(self._on_viewer_copy)
        self.image_viewer.undo_requested.connect(self.undo)
        self.image_viewer.redo_requested.connect(self.redo)

        self.stack.addWidget(self.calendar_view)   # 0
        self.stack.addWidget(self.manager_view)    # 1
        self.stack.addWidget(self.settings_view)   # 2
        self.stack.addWidget(self.help_view)       # 3
        self.stack.addWidget(self.gallery_view)    # 4
        self.stack.addWidget(self.image_viewer)    # 5
        layout.addWidget(self.stack)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v / %m")
        self.progress_bar.setFixedWidth(300)
        self.progress_bar.setMinimumHeight(18)
        self.progress_bar.setMaximumHeight(20)
        self.progress_bar.hide()
        self.statusBar().addPermanentWidget(self.progress_bar)
        self.statusBar().showMessage(t("status_ready"))
        if not HEIF_SUPPORT:
            self.statusBar().showMessage(t("heic_unavailable"), 10000)

        tr.language_changed.connect(self.retranslate_ui)

    def retranslate_ui(self):
        self.setWindowTitle(self._window_title())
        self.brand.setText(t("brand"))
        self.btn_nav_cal.setText(t("nav_calendar"))
        self.btn_nav_mgr.setText(t("nav_manage"))
        self.btn_nav_set.setText(t("nav_settings"))
        self.btn_nav_help.setText(t("nav_help"))
        if hasattr(self, "btn_nav_about"):
            self.btn_nav_about.setText(t("nav_about"))
        self.btn_import.setText(t("toolbar_add_folder"))
        self.btn_rescan_dups.setText(t("toolbar_rescan"))
        self.btn_cancel_scan.setText(t("toolbar_cancel"))
        self.btn_paste.setText(t("toolbar_paste"))
        idx = self.combo_view_mode.currentIndex()
        self.combo_view_mode.blockSignals(True)
        self.combo_view_mode.clear()
        self.combo_view_mode.addItems([
            t("toolbar_view_days"), t("toolbar_view_months"),
            t("toolbar_view_years"), t("toolbar_view_events"),
            t("toolbar_view_duplicates"),
        ])
        self.combo_view_mode.setCurrentIndex(idx)
        self.combo_view_mode.blockSignals(False)
        self._rebuild_translatable_widgets()
        self.help_view.update_theme()
        # retranslate image/gallery 
        self.gallery_view.retranslate()
        self.image_viewer.retranslate()
        # retranslate respects tab 
        cur = self.stack.currentIndex()
        if cur == 0:
            if idx != 4:
                self.change_view_mode(idx)
            elif self.last_dups is not None:
                # user was in Duplicates — keep them there. Previously
                # fell back to Days regardless. If last_dups is a dict we
                # can rebuild dups gallery; if None (never scanned) it
                # still shows Days safely.
                self.combo_view_mode.blockSignals(True)
                self.combo_view_mode.setCurrentIndex(4)
                self.combo_view_mode.blockSignals(False)
                self.change_view_mode(4)
            else:
                # no dup cache — cannot stay in Duplicates; fall back
                self.combo_view_mode.blockSignals(True)
                self.combo_view_mode.setCurrentIndex(0)
                self.combo_view_mode.blockSignals(False)
                self.change_view_mode(0)
        elif cur == 4:
            self.gallery_view.update_theme()
            if self.gallery_view.is_dups_mode and self.last_dups_groups:
                cols = int(self.config.get("dups_per_row", 2))
                self.gallery_view.dup_gallery.set_groups(
                    self.last_dups_groups, self.config["cache_dir"], cols)
            elif (self.current_gallery_context
                  and self.current_gallery_context != "ALL_DUPLICATES"):
                # rebuild gallery title
                mode = ["days", "months", "years", "events", "duplicates"][
                    self.combo_view_mode.currentIndex()]
                if mode != "duplicates":
                    self.open_gallery(self.current_gallery_context, mode)
        self._build_menubar()  # re-localize menubar

    def _rebuild_translatable_widgets(self):
        """Recreate Settings and Manager widgets so all labels re-localize."""
        saved_idx = self.stack.currentIndex()

        idx = self.stack.indexOf(self.settings_view)
        if idx >= 0:
            self.stack.removeWidget(self.settings_view)
        # NB-80: no-arg disconnect() tore down ALL signal connections
        # on the widget before deletion. deleteLater() already handles
        # C++-side cleanup; blockSignals avoids queued emissions during
        # the deferred destruction without clobbering internal wiring
        # the widget may have set up in its constructor.
        try:
            self.settings_view.blockSignals(True)
        except RuntimeError:
            pass
        self.settings_view.deleteLater()
        self.settings_view = SettingsWidget(self.priority, self.config)
        self.settings_view.priorities_changed.connect(self.update_priority)
        self.settings_view.cache_settings_changed.connect(self.on_cache_settings_changed)
        self.settings_view.language_load_requested.connect(self.load_language_dialog)
        self.settings_view.language_export_requested.connect(self.export_language_dialog)
        self.settings_view.language_reset_requested.connect(self.reset_language)
        self.settings_view.calendar_export_requested.connect(self.export_calendar)
        self.settings_view.calendar_import_requested.connect(self.load_external_calendar)
        self.settings_view.data_find_missing_requested.connect(self.find_missing_files)
        self.settings_view.data_restore_backup_requested.connect(self.restore_from_backup)
        # hotkey editor signals (must re-connect on every rebuild)
        self.settings_view.hotkey_changed.connect(self.on_hotkey_changed)
        self.settings_view.hotkey_profile_changed.connect(
            self.on_hotkey_profile_changed)
        self.settings_view.hotkey_reset_requested.connect(
            self.on_hotkey_reset_requested)
        if idx >= 0:
            self.stack.insertWidget(idx, self.settings_view)
        else:
            # NB-06: rare — settings_view was already detached
            # (double-fired language_changed or deleteLater race).
            # NB-27: insert BEFORE help_view, not at stack.count().
            # Restores canonical order calendar(0)/manager(1)/
            # settings(2)/help(3)/gallery(4)/viewer(5) -- without
            # this, subsequent switch_tab / retranslate_ui /
            # change_view_mode (hardcoded indices) render wrong widget.
            _anchor = self.stack.indexOf(self.help_view)
            if _anchor < 0:
                _anchor = self.stack.count()
                logger.warning(
                    f"_rebuild: help_view missing too; "
                    f"inserting settings_view at end (idx={_anchor})")
            self.stack.insertWidget(_anchor, self.settings_view)
            logger.warning(
                f"_rebuild: settings_view detached, "
                f"re-inserted before help_view (idx={_anchor})")

        idx = self.stack.indexOf(self.manager_view)
        if idx >= 0:
            self.stack.removeWidget(self.manager_view)
        # NB-80: see settings_view above.
        try:
            self.manager_view.blockSignals(True)
        except RuntimeError:
            pass
        self.manager_view.deleteLater()
        self.manager_view = CalendarManagerWidget(self)
        if idx >= 0:
            self.stack.insertWidget(idx, self.manager_view)
        else:
            # NB-06 + NB-27: insert BEFORE settings_view (which was
            # already handled by NB-27a above, so it is guaranteed
            # to be present and correctly positioned before help_view).
            # Falls back to end only if even settings_view is missing.
            _anchor = self.stack.indexOf(self.settings_view)
            if _anchor < 0:
                _anchor = self.stack.count()
                logger.warning(
                    f"_rebuild: settings_view missing too; "
                    f"inserting manager_view at end (idx={_anchor})")
            self.stack.insertWidget(_anchor, self.manager_view)
            logger.warning(
                f"_rebuild: manager_view detached, "
                f"re-inserted before settings_view (idx={_anchor})")
        self.manager_view.update_combo(self.calendars, self.current_cal_name)

        self.stack.setCurrentIndex(saved_idx)

    def apply_styles(self):
        # NB-73: derive CSS font-family from FONT_FAMILY (platform
        # system font). Previously 'Segoe UI' was hardcoded first in
        # QSS, which overrides FONT_FAMILY on macOS/Linux — two
        # different fonts in one window.
        if FONT_FAMILY:
            if FONT_FAMILY.startswith(".") or " " in FONT_FAMILY:
                _css_font = f"'{FONT_FAMILY}'"
            else:
                _css_font = FONT_FAMILY
        else:
            _css_font = "'Segoe UI', 'Roboto', 'Helvetica Neue'"
        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {COLOR_BG}; }}
            QStackedWidget {{ background-color: {COLOR_BG}; }}
            QScrollArea {{ background-color: {COLOR_BG}; border: none; }}
            QScrollArea > QWidget > QWidget {{ background-color: {COLOR_BG}; }}
            QMessageBox {{ background-color: {COLOR_SURFACE}; }}
            QDialog {{ background-color: {COLOR_SURFACE}; }}
            /* font stack — derived from FONT_FAMILY */
            QWidget {{ font-family: {_css_font}, sans-serif; color: {COLOR_TEXT}; }}

            QWidget#nav_bar {{ background-color: {COLOR_SURFACE}; border-bottom: 1px solid {COLOR_BORDER}; }}
            QWidget#nav_bar QPushButton {{
                background-color: transparent; border: none; border-radius: 8px;
                padding: 8px 18px; font-size: 13px; font-weight: 500; color: {COLOR_MUTED};
            }}
            QWidget#nav_bar QPushButton:hover {{ background-color: {COLOR_HOVER}; color: {COLOR_TEXT}; }}
            QWidget#nav_bar QPushButton:checked {{ background-color: {COLOR_PRIMARY_LT}; color: {COLOR_TEXT}; font-weight: 600; }}

            QWidget#cal_toolbar {{ background-color: {COLOR_BG}; border-bottom: 1px solid {COLOR_BORDER}; }}
            QFrame#settings_box {{ background-color: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; border-radius: 10px; }}
            QWidget#settings_view {{ background-color: {COLOR_BG}; }}

            QPushButton {{ background-color: {COLOR_SURFACE}; color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER}; border-radius: 8px; padding: 8px 14px; font-size: 13px; font-weight: 500; }}
            QPushButton:hover {{ background-color: {COLOR_HOVER}; }}
            QPushButton:disabled {{ background-color: {COLOR_HOVER}; color: {COLOR_MUTED}; border: 1px solid {COLOR_BORDER}; }}

            QPushButton#primary {{ background-color: {COLOR_PRIMARY_DK}; color: #ffffff; border: 1px solid {COLOR_PRIMARY_DK}; font-weight: 600; }}
            QPushButton#primary:hover {{ background-color: {COLOR_PRIMARY}; border-color: {COLOR_PRIMARY}; color: #1a1a1a; }}

            QLineEdit, QSpinBox, QDoubleSpinBox, QAbstractSpinBox, QKeySequenceEdit,
            QKeySequenceEdit QLineEdit {{
                color: {COLOR_TEXT}; background-color: {COLOR_SURFACE};
                border: 1px solid {COLOR_BORDER}; border-radius: 4px; padding: 4px;
            }}
            QComboBox {{ color: {COLOR_TEXT}; background-color: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; border-radius: 8px; padding: 5px 11px; min-height: 20px; }}
            QComboBox:hover {{ border-color: {COLOR_PRIMARY}; }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox QAbstractItemView {{ color: {COLOR_TEXT}; background-color: {COLOR_SURFACE}; border: 1px solid {COLOR_PRIMARY}; border-radius: 6px; padding: 4px; selection-background-color: {COLOR_PRIMARY_LT}; selection-color: {COLOR_TEXT}; }}

            QLabel {{ color: {COLOR_TEXT}; }}
            QProgressBar {{ border: 1px solid {COLOR_BORDER}; border-radius: 6px; text-align: center; background-color: {COLOR_SURFACE}; min-height: 18px; max-height: 20px; color: {COLOR_TEXT}; font-size: 11px; }}
            QProgressBar::chunk {{ background-color: {COLOR_PRIMARY}; border-radius: 5px; }}

            QMenu {{ background-color: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; border-radius: 8px; padding: 4px; }}
            QMenu::item {{ padding: 8px 20px; border-radius: 4px; color: {COLOR_TEXT}; }}
            QMenu::item:selected {{ background-color: {COLOR_PRIMARY_LT}; color: {COLOR_TEXT}; }}
            QMenu::separator {{ height: 1px; background-color: {COLOR_BORDER}; margin: 4px 8px; }}

            QToolTip {{ background-color: {COLOR_TEXT}; color: {COLOR_TOOLTIP_TEXT}; border: none; border-radius: 4px; padding: 4px 8px; }}
            QStatusBar {{ background-color: {COLOR_SURFACE}; border-top: 1px solid {COLOR_BORDER}; color: {COLOR_MUTED}; }}

            QScrollBar:vertical {{ border: none; background-color: {COLOR_HOVER}; width: 10px; margin: 0; }}
            QScrollBar::handle:vertical {{ background-color: {COLOR_SCROLL_HANDLE}; min-height: 24px; border-radius: 5px; }}
            QScrollBar::handle:vertical:hover {{ background-color: {COLOR_SCROLL_HANDLE_HOVER}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar:horizontal {{ border: none; background-color: {COLOR_HOVER}; height: 10px; margin: 0; }}
            QScrollBar::handle:horizontal {{ background-color: {COLOR_SCROLL_HANDLE}; min-width: 24px; border-radius: 5px; }}
            QScrollBar::handle:horizontal:hover {{ background-color: {COLOR_SCROLL_HANDLE_HOVER}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        """)
        self.brand.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {COLOR_TEXT}; margin-right: 10px;")

    # ---- settings / theme
    # ---- hotkey handlers (called from SettingsWidget signals)
    def on_hotkey_changed(self, action_id, seq):
        """Persist a single binding. Empty seq = explicit disable."""
        if not hasattr(self, "_actions") or not self._actions:
            # Defensive: setup_shortcuts not yet called. Not expected.
            return
        self.config.setdefault("hotkeys", {})[action_id] = seq
        self.save_config()
        self.rebind_hotkeys()

    def on_hotkey_profile_changed(self, profile):
        """Switch platform profile. Explicit per-action overrides are
        preserved (they live in config['hotkeys'] and take priority in
        _resolved_hotkey). Only non-overridden actions pick up the new
        profile default.
        """
        if profile not in ("auto", "windows", "darwin", "linux"):
            return
        self.config["hotkey_profile"] = profile
        self.save_config()
        self.rebind_hotkeys()
        # Refresh Settings QKeySequenceEdit values from resolved config.
        # The SettingsWidget itself does NOT rebuild (priority-sync
        # invariant -- do not touch settings_view lifecycle here).
        try:
            for aid, edt in self.settings_view._hk_edits.items():
                v = _resolved_hotkey(self.config, aid)
                edt.blockSignals(True)
                edt.setKeySequence(QKeySequence(v) if v else QKeySequence())
                edt.blockSignals(False)
        except (AttributeError, RuntimeError):
            pass

    def on_hotkey_reset_requested(self):
        """Wipe all per-action overrides; return to auto profile.
        SettingsWidget has already updated its own QKeySequenceEdit
        fields to platform defaults (see _on_hk_reset).
        """
        self.config.pop("hotkeys", None)
        self.config["hotkey_profile"] = "auto"
        self.save_config()
        self.rebind_hotkeys()

    def on_cache_settings_changed(self, new_settings):
        old_theme = self.config.get("theme", "light")
        if "undo_limit" in new_settings:
            self.undo_limit = int(new_settings["undo_limit"])
        # validate trash_dir BEFORE config.update. If the setter
        # fails, drop the key; the old value in config is preserved.
        # (P0): validate cache_dir symmetric to load_config.
        # Without this, UI-setted root ("C:\\") bypasses the load
        # guard — next save writes root into config, and any
        # clear_cache/auto_clean wipes *.jpg in the drive root.
        if "cache_dir" in new_settings:
            _new_cd = new_settings.get("cache_dir")
            _cd_ok2 = True
            try:
                _cd_s = str(_new_cd) if _new_cd is not None else ""
                if (not _cd_s or _cd_s.strip() in ("", ".")
                        or not Path(_cd_s).is_absolute()):
                    _cd_ok2 = False
                else:
                    _cp2 = Path(_cd_s)
                    # (P1): resolve() for .. / symlink
                    try:
                        _cp2 = _cp2.resolve()
                    except Exception:
                        pass
                    if _cp2.parent == _cp2:
                        _cd_ok2 = False
                    elif _cp2.exists() and not _cp2.is_dir():
                        _cd_ok2 = False
            except Exception:
                _cd_ok2 = False
            if not _cd_ok2:
                _ns = dict(new_settings)
                _ns.pop("cache_dir", None)
                new_settings = _ns
                try:
                    self.statusBar().showMessage(
                        f"Cache folder rejected: {_new_cd}", 5000)
                    self.settings_view.lbl_cache_path.setText(
                        str(self.config.get("cache_dir", "")))
                except Exception:
                    pass
        if "trash_dir" in new_settings:
            _new_td = new_settings.get("trash_dir")
            # confirm before switching trash location. The folder
            # will receive deleted files and auto-cleanup will run
            # inside it (prefixed files only — see clean_trash).
            # Only prompt when the path actually changes.
            _path_changing = True
            try:
                _path_changing = (
                    Path(str(_new_td)).resolve()
                    != TRASH_DIR.resolve()
                )
            except Exception:
                _path_changing = True
            if _path_changing:
                try:
                    _reply = QMessageBox.question(
                        self, t("title_trash_warning"),
                        t("warn_trash_folder"),
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.No,
                        defaultButton=QMessageBox.StandardButton.No)
                except Exception:
                    _reply = QMessageBox.StandardButton.No
                if _reply != QMessageBox.StandardButton.Yes:
                    # user cancelled — drop the key, roll back label
                    # no early return — continue applying OTHER
                    # settings from the same emit (theme, dups_per_row,
                    # etc.). Previously this return silently lost
                    # those. Currently harmless because change_trash
                    # path emits only {trash_dir}, but fragile.
                    _ns = dict(new_settings)
                    _ns.pop("trash_dir", None)
                    new_settings = _ns
                    try:
                        self.settings_view.lbl_trash_path.setText(
                            str(TRASH_DIR))
                    except Exception:
                        pass
            if "trash_dir" in new_settings and not set_trash_dir(_new_td):
                _ns = dict(new_settings)
                _ns.pop("trash_dir", None)
                new_settings = _ns
                try:
                    self.statusBar().showMessage(
                        f"Trash folder rejected: {_new_td}", 5000)
                except Exception:
                    pass
                try:
                    self.settings_view.lbl_trash_path.setText(str(TRASH_DIR))
                except Exception:
                    pass
        self.config.update(new_settings)
        self.save_config()

        needs_dup_refresh = False
        theme_changed = "theme" in new_settings and new_settings["theme"] != old_theme

        if theme_changed:
            apply_theme(new_settings["theme"])
            self.apply_styles()
            self.calendar_view.update_theme()
            self.gallery_view.update_theme()
            self.settings_view.update_theme()
            self.help_view.update_theme()
            self.manager_view.update_theme()
            self.image_viewer.update_theme()
            if self.stack.currentIndex() == 0:
                self.change_view_mode(self.combo_view_mode.currentIndex())
            elif self.stack.currentIndex() == 4:
                if self.gallery_view.is_dups_mode:
                    needs_dup_refresh = True
                else:
                    self.gallery_view.refresh_view()

        if "dups_per_row" in new_settings and self.last_dups:
            if self.stack.currentIndex() == 4 and self.gallery_view.is_dups_mode:
                needs_dup_refresh = True

        if needs_dup_refresh and self.last_dups_groups:
            cols = int(self.config.get("dups_per_row", 2))
            self.gallery_view.dup_gallery.set_groups(
                self.last_dups_groups, self.config["cache_dir"], cols)

    # ---- language
    def load_language_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, t("title_language"), "", "JSON (*.json)")
        if not path:
            return
        try:
            tr.load_from_file(path)
            self.config["language_file"] = path
            self.save_config()
            self.settings_view.update_language_label()
        except Exception as e:
            QMessageBox.critical(self, t("error_title"), f"{e}")

    def export_language_dialog(self):
        path, _ = QFileDialog.getSaveFileName(
            self, t("title_language"), "language_template.json", "JSON (*.json)")
        if not path:
            return
        try:
            tr.export_to_file(path)
            QMessageBox.information(self, t("info_done"),
                                    f"{path}")
        except Exception as e:
            QMessageBox.critical(self, t("error_title"), f"{e}")

    def reset_language(self):
        tr.reset_to_default()
        self.settings_view.update_language_label()
        self.config.pop("language_file", None)
        self.save_config()

    # ---- scan guards / dialog
    def _guard_scan(self):
        """return True if a scan is in progress (caller aborts).
        Used by all destructive ops — during a scan _busy must stay
        False so scan_finished never defers (NB-26 race eliminated).
        """
        if getattr(self, "_scan_in_progress", False):
            try:
                self.statusBar().showMessage(t("status_wait_scan"), 2000)
            except Exception:
                pass
            return True
        return False

    def _should_show_scan_dialog(self, total):
        if total <= 0:
            return False
        if not self.config.get("safe_media_add_enabled", True):
            return False
        try:
            th = int(self.config.get("safe_media_add_threshold", 50))
        except (TypeError, ValueError):
            th = 50
        th = max(10, min(10000, th))
        return total > th

    def _open_scan_dialog(self, total):
        if self._scan_dialog is not None:
            return
        try:
            dlg = ScanProgressDialog(self)
            dlg.cancel_requested.connect(self.cancel_scan)
            dlg.set_progress(0, total)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            self._scan_dialog = dlg
        except Exception as e:
            logger.warning(f"_open_scan_dialog failed: {e}")
            self._scan_dialog = None

    def _close_scan_dialog(self):
        if self._scan_dialog is None:
            return
        try:
            # mark _cancelled BEFORE close() so closeEvent does
            # not emit cancel_requested.
            # also set _force_close flag; ScanProgressDialog
            # .closeEvent checks it and calls event.accept() — without
            # it, close() was ignored (event.ignore() unconditional)
            # and dialog stayed visible until deleteLater ran.
            self._scan_dialog._cancelled = True
            self._scan_dialog._force_close = True
            self._scan_dialog.close()
            self._scan_dialog.deleteLater()
        except RuntimeError:
            pass
        self._scan_dialog = None

    def _end_scan(self):
        """called from every scan_finished / cancel_scan exit path."""
        self._close_scan_dialog()
        self._scan_in_progress = False

    # ---- undo / redo
    def _update_undo_buttons(self):
        has_undo = len(self.history) > 0
        has_redo = len(self.redo_stack) > 0
        if hasattr(self.gallery_view, "btn_undo"):
            self.gallery_view.btn_undo.setEnabled(has_undo)
        if hasattr(self.gallery_view, "btn_redo"):
            self.gallery_view.btn_redo.setEnabled(has_redo)
        if hasattr(self.image_viewer, "btn_undo"):
            self.image_viewer.btn_undo.setEnabled(has_undo)
        if hasattr(self.image_viewer, "btn_redo"):
            self.image_viewer.btn_redo.setEnabled(has_redo)

    def _snapshot_state(self):
        """Compact state dict for undo/redo snapshots."""
        return {
            "calendars": copy.deepcopy(self.calendars),
            "duplicates": copy.deepcopy(self.duplicates),
            "categories": copy.deepcopy(self.categories),
            "current_cal_name": self.current_cal_name,
        }

    def save_state_for_undo(self, action=None):
        state = self._snapshot_state()  # use _snapshot_state (save)
        self.history.append(state)
        self.undo_actions.append(action)
        if len(self.history) > self.undo_limit:
            self.history.pop(0)
            if self.undo_actions:
                self.undo_actions.pop(0)
        self.redo_stack.clear()
        self.redo_actions.clear()  # redo_actions.clear() 
        self._update_undo_buttons()

    @_busy_guard_text_aware  # undo — text-forward + file undo
    def undo(self):
        # if a text-input widget has focus, forward to native undo
        fw = QApplication.focusWidget()
        focus_name = fw.__class__.__name__ if fw is not None else None
        logger.info(
            f"undo: entering (history={len(self.history)}, "
            f"busy={self._busy}, focus={focus_name})")
        # proper isinstance check. Previously used hasattr(setText),
        # but QPushButton also has setText — so after clicking any toolbar
        # button (btn_delete, btn_copy...), Ctrl+Z was silently blocked
        # until user clicked back on the list. Now: only genuine text-input
        # widgets forward to local undo; spin/combo are no-op; buttons and
        # other widgets fall through to file undo.
        if fw is not None and isinstance(
                fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
            try:
                if fw.isUndoAvailable():
                    logger.info(f"undo: forwarded to text widget ({focus_name})")
                    fw.undo()
                else:
                    logger.info(f"undo: focus in text widget ({focus_name}), "
                                f"local undo empty — no-op")
            except Exception as e:
                logger.debug(f"undo: text forward failed: {e}")
            return
        if fw is not None and isinstance(
                fw, (QAbstractSpinBox, QComboBox)):
            logger.info(f"undo: focus on input control "
                        f"({fw.__class__.__name__}), ignored")
            return
        # (P2): QKeySequenceEdit focus guard. QKeySequenceEdit
        # is not a QLineEdit -- without this, Ctrl+Z while editing a
        # hotkey could fall through to file undo.
        if fw is not None and isinstance(fw, QKeySequenceEdit):
            logger.info("undo: focus on QKeySequenceEdit, ignored")
            return
        # NB-15: modal dialog open + focus on non-text widget
        # (e.g. OK button) -- Ctrl+Z must not trigger file undo.
        # Text focus was already forwarded above (local undo).
        _mw = QApplication.activeModalWidget()
        if _mw is not None and _mw is not self:
            logger.info(
                f"undo: ignored - modal "
                f"{_mw.__class__.__name__} is open")
            return
        if self._guard_scan():  # 
            return
        # file undo applies only in Calendar (0), Gallery (4), Viewer (5).
        # In Manager / Settings / Help — ignore (they have local widgets).
        # Prevents accidental file rollback from Settings spinners etc.
        if self.stack.currentIndex() in (1, 2, 3):
            logger.info(
                f"undo: ignored — stack={self.stack.currentIndex()} "
                f"(file undo only in calendar/gallery/viewer)")
            return
        if not self.history:
            logger.info("undo: history empty, nothing to undo")
            self.statusBar().showMessage(t("status_nothing_undo"), 2000)
            return
        was_gallery = self.stack.currentIndex() == 4
        was_dups = was_gallery and self.gallery_view.is_dups_mode
        scroll = self.gallery_view.list_widget.verticalScrollBar().value() if (was_gallery and not self.gallery_view.is_dups_mode) else 0
        dup_scroll = self.gallery_view.dup_gallery.verticalScrollBar().value() if (was_gallery and self.gallery_view.is_dups_mode) else 0

        current_state = self._snapshot_state()  # use _snapshot_state (undo)
        self.redo_stack.append(current_state)
        # remember for redo 
        self.redo_actions.append(self.undo_actions[-1] if self.undo_actions else None)

        prev_state = self.history.pop()
        action = self.undo_actions.pop() if self.undo_actions else None
        restore_failed = []
        # correct undo order:
        #   1. moved — un-move returns source files, empties destination
        #   2. trash — restore deleted (no path conflict with moved)
        #   3. overwritten — dest is now free, restore original from trash
        #   4. created — left on disk (undo-of-copy limitation)
        # Previous order (trash → overwritten → moved) failed for paste-move
        # with overwrite: overwritten tried to restore B into dest while A
        # was still there, leaving B stuck in trash.
        def _purge_path_from_state(state, path):
            for cal in state["calendars"].values():
                for d in list(cal.keys()):
                    if path in cal[d]:
                        cal[d].remove(path)
                        if not cal[d]:
                            del cal[d]
            for cat in state["categories"].values():
                if path in cat:
                    cat.remove(path)
            # NB-63: also clear from the duplicates
            # snapshot. The NB-53 helper purged calendars and
            # categories, but left the path in the duplicates
            # set -- a later open_gallery could apply a stale
            # warning marker to a foreign file at that path.
            for dup_set in state.get("duplicates", {}).values():
                if isinstance(dup_set, set):
                    dup_set.discard(path)

        if action and action.get("moved"):
            for src, dst in action["moved"]:
                if os.path.exists(dst) and not os.path.exists(src):
                    try:
                        os.makedirs(os.path.dirname(src) or ".", exist_ok=True)
                        shutil.move(dst, src)
                    except Exception as e:
                        restore_failed.append((src, str(e)))
                        _purge_path_from_state(prev_state, src)
                elif os.path.exists(dst) and os.path.exists(src):
                    logger.warning(f"undo: src {src} occupied by foreign file, purging from snapshot")
                    _purge_path_from_state(prev_state, src)
        if action and action.get("trash"):
            for tp, orig in action["trash"]:
                if tp and os.path.exists(tp):
                    ok, err = restore_from_trash(tp, orig)
                    if not ok:
                        restore_failed.append((orig, str(err)))
                        # (C4): NB-53 analog for the
                        # trash branch. If restore failed
                        # because orig is occupied by a foreign
                        # file, purge the path from prev_state --
                        # otherwise the restored calendar points
                        # at a file the user never had.
                        _purge_path_from_state(prev_state, orig)
        if action and action.get("overwritten"):
            created_set = set(action.get("created", []))
            _new_overwritten = []
            for tp, orig in action["overwritten"]:
                new_tp = tp
                # Copy+overwrite: created copy at orig, must remove it.
                # Move+overwrite: `moved` above already emptied orig.
                if orig in created_set and os.path.exists(orig):
                    # trash instead of os.remove — recoverable
                    ok_t, trashed, _ = move_to_trash(orig)
                    if not ok_t:
                        restore_failed.append((orig, "trash failed"))
                        continue
                    new_tp = trashed
                if tp and os.path.exists(tp):
                    ok, err = restore_from_trash(tp, orig)
                    if not ok:
                        restore_failed.append((orig, str(err)))
                        # (C4b): same as trash branch --
                        # purge path from prev_state if restore
                        # failed due to occupied destination.
                        _purge_path_from_state(prev_state, orig)
                _new_overwritten.append((new_tp, orig))
            action["overwritten"] = _new_overwritten
        if action and action.get("created"):
            # (NB-21): exclude overwritten destinations — those
            # were already moved to trash by the "overwritten" branch
            # above (or are on disk as part of that flow). Log only
            # truly leftover copies; otherwise the message was wrong
            # for the copy+overwrite case.
            _overwritten_dests = {o for _, o in action.get("overwritten", [])}
            _actually_created = [
                c for c in action["created"] if c not in _overwritten_dests]
            if _actually_created:
                logger.info(
                    f"undo copy-paste: {len(_actually_created)} file(s) left "
                    f"on disk (cannot safely redo deletion)")

        self.calendars = prev_state["calendars"]
        self.duplicates = prev_state["duplicates"]
        self.categories = prev_state["categories"]
        prev_cal = prev_state.get("current_cal_name", self.current_cal_name)
        if prev_cal in self.calendars:
            self.current_cal_name = prev_cal
        else:
            self.current_cal_name = next(iter(self.calendars))

        # always reset dup cache after undo — snapshot does not include it
        self.last_dups = None
        self.last_dups_groups = []
        self._dups_stale = False

        # NB-48: signal refresh_all_views() to keep user in
        # Duplicates view instead of silently dropping to Days.
        # Stale cards are cleared; user sees "Press Rescan" hint.
        if was_dups:
            self._restore_dups_after_op = True

        self.refresh_all_views()
        if was_gallery and self.stack.currentIndex() == 4:
            if self.gallery_view.is_dups_mode:
                QTimer.singleShot(50, lambda: self.gallery_view.dup_gallery.verticalScrollBar().setValue(dup_scroll))
            else:
                QTimer.singleShot(50, lambda: self.gallery_view.list_widget.verticalScrollBar().setValue(scroll))
        self._update_undo_buttons()
        if restore_failed:
            msg_warning(self, t("error_title"),
                        t("undo_restore_failed", n=len(restore_failed)))
        self.statusBar().showMessage(t("status_undo_done"), 2000)

    @_busy_guard_text_aware  # redo — text-forward + file redo
    def redo(self):
        # forward to native redo if text input has focus
        fw = QApplication.focusWidget()
        # same isinstance fix as undo
        if fw is not None and isinstance(
                fw, (QLineEdit, QTextEdit, QPlainTextEdit)):
            try:
                if fw.isRedoAvailable():
                    fw.redo()
            except Exception:
                pass
            return
        if fw is not None and isinstance(
                fw, (QAbstractSpinBox, QComboBox)):
            logger.info(f"redo: focus on input control "
                        f"({fw.__class__.__name__}), ignored")
            return
        # (P2): QKeySequenceEdit focus guard (same as undo).
        if fw is not None and isinstance(fw, QKeySequenceEdit):
            logger.info("redo: focus on QKeySequenceEdit, ignored")
            return
        # NB-15: same guard as undo -- modal + non-text focus
        # must not trigger file redo.
        _mw = QApplication.activeModalWidget()
        if _mw is not None and _mw is not self:
            logger.info(
                f"redo: ignored - modal "
                f"{_mw.__class__.__name__} is open")
            return
        if self._guard_scan():  # 
            return
        # same scope as undo
        if self.stack.currentIndex() in (1, 2, 3):
            logger.info(
                f"redo: ignored — stack={self.stack.currentIndex()}")
            return
        if not self.redo_stack:
            self.statusBar().showMessage(t("status_nothing_redo"), 2000)
            return
        # redo cannot correctly replay paste-with-overwrite — undo's
        # "overwritten" bookkeeping is not symmetric with redo, so a silent
        # replay would desync calendar (points to "new" file at dest) from
        # disk (still contains old file). Instead of guessing, skip this
        # redo step with a clear message. Pure copy-paste (created without
        # overwritten) is still redoable: undo leaves the copy on disk (B1)
        # and redo only restores the calendar entry. See KNOWN_LIMITATIONS B2.
        _next_ra = self.redo_actions[-1] if self.redo_actions else None
        if _next_ra and _next_ra.get("overwritten"):
            logger.warning(
                "redo: refusing to replay paste-with-overwrite "
                "(KNOWN_LIMITATIONS B2); skipping this redo step")
            self.redo_stack.pop()
            self.redo_actions.pop()
            self._update_undo_buttons()
            self.statusBar().showMessage(
                t("status_redo_skipped_overwrite"), 5000)
            return
        was_gallery = self.stack.currentIndex() == 4
        was_dups = was_gallery and self.gallery_view.is_dups_mode
        scroll = self.gallery_view.list_widget.verticalScrollBar().value() if (was_gallery and not self.gallery_view.is_dups_mode) else 0
        dup_scroll = self.gallery_view.dup_gallery.verticalScrollBar().value() if (was_gallery and self.gallery_view.is_dups_mode) else 0

        current_state = self._snapshot_state()  # use _snapshot_state (redo)
        next_state = self.redo_stack.pop()
        # apply move redo first 
        redo_action = self.redo_actions.pop() if self.redo_actions else None
        redo_moved_pairs = []
        redo_conflicts = []  # redo conflicts list
        if redo_action and redo_action.get("moved"):
            for src, dst in redo_action["moved"]:
                src_exists = os.path.exists(src)
                dst_exists = os.path.exists(dst)
                if dst_exists and not src_exists:
                    # already at destination — treat as done
                    redo_moved_pairs.append((src, dst))
                elif dst_exists and src_exists:
                    # dst occupied — skip, don't trash src
                    logger.warning(f"redo move conflict: {dst} occupied, skip {src}")
                    redo_conflicts.append((src, dst))
                elif src_exists:
                    try:
                        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                        shutil.move(src, dst)
                        redo_moved_pairs.append((src, dst))
                    except Exception as e:
                        logger.warning(f"redo move failed: {src} -> {dst}: {e}")
                # else both missing — nothing to recover

        # CRITICAL: do NOT compute to_trash from set-diff.
        # Old logic: cur_files - next_files → move_to_trash(fp) for each.
        # Bug: user cleared calendar (files stay on disk), undo restored
        # them, redo (Ctrl+Y) would move ALL files to TRASH_DIR — silent
        # physical deletion, contradicting the "Files remain" promise of
        # clear_calendar / delete_calendar / import / restore dialogs.
        #
        # Correct: redo replays ONLY recorded actions.
        #   - "moved": handled above (un-move/re-move pair)
        #   - "trash": files undone from trash must go back to trash
        #   - everything else: no disk changes
        trashed_now = []
        if redo_action and redo_action.get("trash"):
            for _tp, _orig in redo_action["trash"]:
                # (BUG-B): only re-trash if undo ACTUALLY restored.
                # If _tp (trash slot) still exists, undo's
                # restore_from_trash() failed — likely because _orig was
                # occupied by an external file. Re-trashing _orig would
                # then purge that foreign file, which the user never
                # deleted via this action.
                if (os.path.exists(_orig)
                        and not os.path.exists(_tp)):
                    ok, new_tp, _ = move_to_trash(_orig)
                    if ok and new_tp:
                        trashed_now.append((new_tp, _orig))

        self.history.append(current_state)
        new_undo_action = {"trash": trashed_now}
        if redo_moved_pairs:
            new_undo_action["moved"] = redo_moved_pairs
        self.undo_actions.append(new_undo_action)

        self.calendars = next_state["calendars"]
        self.duplicates = next_state["duplicates"]
        self.categories = next_state["categories"]
        # on redo conflict, keep BOTH files in calendars.
        # dst is in next_state already. src is on disk but missing from
        # next_state — previously we purged dst too, orphaning both.
        # Leave dst in place; src stays on disk (visible on next rescan).
        next_cal = next_state.get("current_cal_name", self.current_cal_name)
        if next_cal in self.calendars:
            self.current_cal_name = next_cal
        else:
            self.current_cal_name = next(iter(self.calendars))

        # always reset dup cache after redo
        self.last_dups = None
        self.last_dups_groups = []
        self._dups_stale = False

        # NB-48: same Duplicates-preservation flag as undo.
        if was_dups:
            self._restore_dups_after_op = True

        self.refresh_all_views()
        if was_gallery and self.stack.currentIndex() == 4:
            if self.gallery_view.is_dups_mode:
                QTimer.singleShot(50, lambda: self.gallery_view.dup_gallery.verticalScrollBar().setValue(dup_scroll))
            else:
                QTimer.singleShot(50, lambda: self.gallery_view.list_widget.verticalScrollBar().setValue(scroll))
        self._update_undo_buttons()
        # redo conflict status
        if redo_conflicts:
            # aggregate conflict log
            logger.warning(
                f"redo: {len(redo_conflicts)} move conflict(s) skipped: "
                + ", ".join(f"{src}->{dst}" for src, dst in redo_conflicts[:5])  # log src->dst
                + (" ..." if len(redo_conflicts) > 5 else ""))
            self.statusBar().showMessage(
                t("status_redo_conflicts", n=len(redo_conflicts)), 5000)
        else:
            self.statusBar().showMessage(t("status_redo_done"), 2000)

    def refresh_all_views(self):
        if self.current_cal_name not in self.calendars:
            self.current_cal_name = next(iter(self.calendars))
        if self.stack.currentIndex() == 4:
            if self.current_gallery_context == "ALL_DUPLICATES" or self.combo_view_mode.currentIndex() == 4:
                if self.last_dups:
                    self.open_duplicates_gallery()
                elif getattr(self, "_restore_dups_after_op", False):
                    # NB-48: undo/redo in Duplicates. Do NOT
                    # silently drop to Days. Stay in Duplicates view,
                    # clear stale cards, prompt user to Rescan.
                    self._restore_dups_after_op = False
                    self.combo_view_mode.blockSignals(True)
                    self.combo_view_mode.setCurrentIndex(4)
                    self.combo_view_mode.blockSignals(False)
                    self._last_view_index = 4
                    self.was_in_dups = True
                    self.stack.setCurrentIndex(4)
                    self.cal_toolbar_widget.show()
                    try:
                        self.gallery_view.dup_gallery._clear()
                        self.gallery_view.cards = []
                        self.gallery_view.thumbnails = []
                        self.gallery_view.is_dups_mode = True
                        self.gallery_view.stack.setCurrentIndex(1)
                        self.gallery_view.btn_delete_dups.hide()
                        self.gallery_view.btn_back.hide()
                        self.gallery_view.lbl_title.setText(
                            t("dups_rescan_needed"))
                    except Exception:
                        logger.exception(
                            "refresh_all_views: dups clear failed")
                else:
                    self.show_calendar_from_gallery()
            else:
                mode = ["days", "months", "years", "events", "duplicates"][self.combo_view_mode.currentIndex()]
                if mode != "duplicates" and self.current_gallery_context:
                    self.open_gallery(self.current_gallery_context, mode)
                else:
                    self.change_view_mode(self.combo_view_mode.currentIndex())
        elif self.stack.currentIndex() == 5:
            # NB-84a: any caller of refresh_all_views (undo, redo,
            # find_missing_files, remove_from_event, restore_from_backup)
            # has modified underlying state, so the gallery snapshot
            # (raw_grouped_data + list_widget) is now stale. Force the
            # rebuild flag; _on_viewer_back rebuilds from
            # self.calendars (source of truth) on exit.
            self._gallery_needs_rebuild = True
            # clamp idx 
            idx_v = self.image_viewer.current_index
            if 0 <= idx_v < len(self.image_viewer.files):
                fp = self.image_viewer.files[idx_v]
            else:
                fp = ""
            all_files = []
            if self.gallery_view.is_dups_mode:
                for card in self.gallery_view.dup_gallery.cards:
                    all_files.extend(card.files)
            else:
                for g, files in self.gallery_view.raw_grouped_data.items():
                    all_files.extend(files)
            # viewer fp must be in all_files
            if fp and fp in all_files and os.path.exists(fp):
                # NB-84b: do NOT clear _gallery_needs_rebuild here.
                # `all_files` is derived from gallery_view.raw_grouped_data
                # (a stale snapshot), not from self.calendars, so "fp is
                # valid" cannot prove the gallery is in sync. Leaving the
                # flag True lets _on_viewer_back rebuild from
                # self.calendars on exit. Without this, Ctrl+Z while in
                # viewer left the restored file invisible in the gallery
                # until manual refresh / reopen.
                self.image_viewer.load_image(fp, all_files)
            else:
                # avoid self-recursion — go directly to gallery/calendar
                self._gallery_needs_rebuild = False  # prevent re-trigger
                self.stack.setCurrentIndex(4)
                if self.last_dups:
                    self.open_duplicates_gallery()
                elif self.current_gallery_context:
                    _m = ["days", "months", "years", "events", "duplicates"][
                        self.combo_view_mode.currentIndex()]
                    if _m != "duplicates":
                        self.open_gallery(self.current_gallery_context, _m)
                    else:
                        self.show_calendar_from_gallery()
                else:
                    self.show_calendar_from_gallery()
            self._update_undo_buttons()
            self.save_json()  # save in stack5 
            return
        elif self.stack.currentIndex() == 0:
            self.change_view_mode(self.combo_view_mode.currentIndex())
        # on Settings/Manager/Help — just refresh data, don't jump tabs
        # update undo after refresh 
        self._update_undo_buttons()
        self.save_json()

    def change_view_mode(self, index):
        # block entry to Duplicates view during main-scan
        # (parallel dup-scan + main-scan → I/O thrash + unreliable
        # file set for dup-scan). Other view modes pass through.
        if index == 4 and self._guard_scan():
            # rollback combo. Without this, the combobox showed
            # "Duplicates" while stack stayed on the previous widget —
            # silent desync between UI and state.
            try:
                self.combo_view_mode.blockSignals(True)
                self.combo_view_mode.setCurrentIndex(self._last_view_index)
                self.combo_view_mode.blockSignals(False)
            except Exception:
                pass
            return
        # capture prev view
        prev_index = self._last_view_index
        if self.combo_view_mode.currentIndex() != index:
            self.combo_view_mode.blockSignals(True)
            self.combo_view_mode.setCurrentIndex(index)
            self.combo_view_mode.blockSignals(False)
        self._last_view_index = index
        # NB-49: remember non-Duplicates view in config.
        # Persisted via save_config() at close / settings change.
        if index != 4:
            try:
                self.config["last_view_mode"] = index
            except Exception:
                pass
        modes = ["days", "months", "years", "events", "duplicates"]
        mode = modes[index]
        if hasattr(self, "btn_rescan_dups"):
            self.btn_rescan_dups.setVisible(mode == "duplicates")

        if mode == "duplicates":
            # busy rollback to previous view
            if getattr(self, "_busy", False):
                self.statusBar().showMessage(t("status_busy"), 2000)
                self.combo_view_mode.blockSignals(True)
                self.combo_view_mode.setCurrentIndex(prev_index)
                self.combo_view_mode.blockSignals(False)
                self._last_view_index = prev_index
                return
            filepaths = set()
            for _d, files in self.calendars.get(self.current_cal_name, {}).items():
                filepaths.update(files)
            if not filepaths:
                # update_data with empty clears scene + shows empty state
                self.calendar_view.update_data({}, {}, "days",
                                                self.config["cache_dir"])
                self.stack.setCurrentIndex(0)
                self.combo_view_mode.blockSignals(True)
                self.combo_view_mode.setCurrentIndex(0)
                self.combo_view_mode.blockSignals(False)
                self._last_view_index = 0
                # NB-14: currentIndexChanged was blocked, so
                # change_view_mode(0) did not run — rescan button
                # stayed visible in Days view. Hide explicitly.
                if hasattr(self, "btn_rescan_dups"):
                    self.btn_rescan_dups.hide()
                return
            if self.last_dups is not None:  # last_dups is not None 
                if self.last_dups:
                    self.open_duplicates_gallery()
                else:
                    # no-dup silent (change_view_mode)
                    self.statusBar().showMessage(t("info_dups_none"), 4000)
                    self.combo_view_mode.blockSignals(True)
                    self.combo_view_mode.setCurrentIndex(0)
                    self.combo_view_mode.blockSignals(False)
                    self.change_view_mode(0)
                return
            if self.dup_thread and self.dup_thread.isRunning():
                _old = self.dup_thread
                self.dup_thread = None
                _old.stop()
                if _old.isRunning():
                    # bounded wait — never block UI forever
                    logger.warning("old dup thread didn't stop in 5s, "
                                   "waiting 10s more")
                    if not _old.wait(10000):
                        logger.error("dup thread won't die, terminating")
                        _old.terminate()
                        _old.wait(2000)
            self.stack.setCurrentIndex(4)
            self.gallery_view.lbl_title.setText(t("status_analyzing"))
            self.gallery_view.list_widget.clear_all()
            self.gallery_view.btn_delete_dups.hide()
            self.gallery_view.btn_back.hide()
            self._update_undo_buttons()
            self.statusBar().showMessage(t("status_analyzing"))
            self.progress_bar.setRange(0, 0)
            self.progress_bar.show()
            # capture dup cal 
            self._dup_cal_name = self.current_cal_name
            self.dup_thread = DuplicateScanThread(filepaths)
            _thread = self.dup_thread
            self.dup_thread.progress.connect(self.update_progress)
            # capture thread reference for stale check
            self.dup_thread.scan_done.connect(
                lambda d, t=_thread: self.on_duplicates_found(d, t))
            self.dup_thread.start()
        else:
            self.stack.setCurrentIndex(0)
            self.cal_toolbar_widget.show()
            self.calendar_view.update_data(
                self.calendars.get(self.current_cal_name, {}),
                self.categories, mode, self.config["cache_dir"])

    def on_duplicates_found(self, dups, thread=None):
        # re-entrancy guard (same as scan_finished — queued signal
        # from QThread::scan_done can fire inside _process_ui)
        if getattr(self, "_busy", False):
            logger.info("on_duplicates_found: busy, deferring 200ms")
            QTimer.singleShot(
                200, lambda: self.on_duplicates_found(dups, thread))
            return
        # ignore stale thread result
        if thread is not None and thread is not self.dup_thread:
            logger.info("on_duplicates_found: stale thread result, ignored")
            try:
                thread.deleteLater()
            except Exception:
                pass
            return
        # write to captured cal 
        # if the source calendar was deleted mid-scan, DISCARD results.
        # Old fallback wrote duplicates from a deleted calendar into the
        # current one — user could then delete non-duplicate files via
        # "Delete duplicates".
        orig_cal = getattr(self, "_dup_cal_name", None)
        if orig_cal and orig_cal not in self.calendars:
            logger.info(
                f"on_duplicates_found: calendar {orig_cal!r} no longer exists, "
                f"discarding results")
            self._dup_cal_name = None
            self.progress_bar.hide()
            self.progress_bar.setRange(0, 100)
            return
        target_cal = orig_cal or self.current_cal_name
        self._dup_cal_name = None
        # (P1): defensive close. If dialog somehow was opened
        # (bug or race), dup-scan completion MUST NOT leave it up —
        # Cancel would not stop dup-scan and user is stuck.
        self._close_scan_dialog()
        # clear stale after rescan
        self._dups_stale = False
        # last_dups guard — only store if the scan matches current cal
        if target_cal == self.current_cal_name:
            self.last_dups = dups
        else:
            self.last_dups = None
        self.duplicates[target_cal] = (
            {fp for g in dups.values() for fp in g} if dups else set()
        )
        self.save_json()
        self.progress_bar.hide()
        self.progress_bar.setRange(0, 100)
        if not dups:
            # no-dup silent (on_duplicates_found)
            self.statusBar().showMessage(t("info_dups_none"), 4000)
            if (self.stack.currentIndex() == 4
                    and self.combo_view_mode.currentIndex() == 4
                    and target_cal == self.current_cal_name):
                self.combo_view_mode.blockSignals(True)
                self.combo_view_mode.setCurrentIndex(0)
                self.combo_view_mode.blockSignals(False)
                self.change_view_mode(0)
            return
        if (self.stack.currentIndex() == 4
                and self.combo_view_mode.currentIndex() == 4
                and target_cal == self.current_cal_name):
            self.open_duplicates_gallery()
        elif dups:
            self.statusBar().showMessage(t("info_dups_found", n=len(dups)), 5000)

    def open_duplicates_gallery(self):
        self._last_view_index = 4
        groups = []
        if not self.last_dups:
            return
        for i, (h, files) in enumerate(self.last_dups.items()):
            existing = [f for f in files if os.path.exists(f)]
            if len(existing) > 1:
                groups.append((f"{i + 1}", existing))
        if not groups:
            # reset state so combo doesn't stay on "Duplicates"
            self.last_dups = None
            self.last_dups_groups = []
            self._last_view_index = 0
            self.combo_view_mode.blockSignals(True)
            self.combo_view_mode.setCurrentIndex(0)
            self.combo_view_mode.blockSignals(False)
            self.stack.setCurrentIndex(0)
            self.cal_toolbar_widget.show()
            self.change_view_mode(0)
            return
        self.last_dups_groups = groups
        self.current_gallery_context = "ALL_DUPLICATES"
        cols = int(self.config.get("dups_per_row", 2))
        self.gallery_view.set_duplicate_groups(groups, self.config["cache_dir"], cols)
        self.stack.setCurrentIndex(4)
        self.cal_toolbar_widget.show()
        self._update_undo_buttons()

    def _sync_duplicates_from_last_dups(self):
        """NB-70: after a delete, keep self.duplicates[cal] consistent
        with self.last_dups. Otherwise the surviving file in each
        former dup group lingers in self.duplicates, persisted to
        disk — after restart, open_gallery applies a stale warning
        marker to files that no longer have a duplicate."""
        cal = self.current_cal_name
        if cal not in self.duplicates:
            return
        # NB-91: only sync when fresh scan data exists. If last_dups
        # is None (fresh session — duplicates loaded from JSON but no
        # scan yet), do NOT clobber persisted state. Previously the
        # first cut-paste in Explorer during a fresh session wiped
        # all ⚠️ markers until manual Rescan.
        # Note: last_dups == {} means "scan ran, zero dups" — that IS
        # a valid signal and we DO want to reset to empty.
        if self.last_dups is None:
            return
        _files = set()
        for g in self.last_dups.values():
            for fp in g:
                if os.path.exists(fp):
                    _files.add(fp)
        self.duplicates[cal] = _files

    def _get_remaining_dups(self):
        if not self.last_dups:
            return {}
        result = {}
        for h, files in self.last_dups.items():
            existing = [f for f in files if os.path.exists(f)]
            if len(existing) > 1:
                result[h] = existing
        return result

    @_busy_guard  # busy dups-delete
    def on_gallery_delete_dups(self):
        if self._guard_scan():  # 
            return
        to_delete = self.gallery_view.get_files_to_delete_as_dups()
        if not to_delete:
            QMessageBox.information(self, t("info_done"), t("error_no_dups_group"))
            return
        # safe-delete-mode aware default (see _delete_default_button).
        # hard-coded Yes-for-1/No-for-N; threshold now configurable.
        _default_btn = self._delete_default_button(len(to_delete))
        # NB-41: honest message for partial selection. Dialog
        # previously always said "exactly one file per group will
        # remain", but when user selects only SOME files in a card
        # (dup mode), get_files_to_delete_as_dups deletes exactly
        # those -- other files in the group remain. Message now
        # reflects that.
        _has_selection = False
        try:
            if self.gallery_view.is_dups_mode:
                _has_selection = bool(
                    self.gallery_view.dup_gallery.get_selected_files())
            else:
                _has_selection = bool(
                    self.gallery_view.list_widget.selectedItems())
        except Exception:
            _has_selection = False
        if _has_selection:
            _confirm_text = t("confirm_delete_dups_partial",
                              n=len(to_delete))
        else:
            _confirm_text = t("confirm_delete_dups", n=len(to_delete))
        reply = QMessageBox.question(
            self, t("confirm_delete"),
            _confirm_text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            defaultButton=_default_btn)
        if reply != QMessageBox.StandardButton.Yes:
            logger.info(f"on_gallery_delete_dups cancelled (reply={reply!r})")
            return
        n = len(to_delete)
        if n > 1:
            self.progress_bar.setRange(0, n)
            self.progress_bar.setFormat("%v / %m")
            self.progress_bar.show()
            _process_ui()  # 
        self.save_state_for_undo()
        # cal_data removed 
        trashed = []
        failed = 0
        successful = []
        for i, fp in enumerate(to_delete):
            self.gallery_view.list_widget.remove_item_by_filepath(fp)
            success, tp, err = move_to_trash(fp)
            if success:
                if tp:
                    trashed.append((tp, fp))
                successful.append(fp)
            else:
                failed += 1
            if n > 1 and i % 3 == 0:
                self.progress_bar.setValue(i + 1)
                _process_ui()  # 
        # bulk purge dups 
        self._purge_paths_bulk(successful)
        if n > 1:
            self.progress_bar.hide()
        if self.undo_actions:
            self.undo_actions[-1] = {"trash": trashed}
        self.last_dups = self._get_remaining_dups()
        self._sync_duplicates_from_last_dups()  # NB-70
        self.last_dups_groups = []  # force rebuild
        self.save_json()
        if failed:
            QMessageBox.warning(self, t("error_title"),
                                t("error_partial_delete", n=failed))
        if self.last_dups:
            self.open_duplicates_gallery()
        else:
            # clear groups after all dups deleted 
            self.last_dups_groups = []
            QMessageBox.information(self, t("info_done"), t("info_dups_all_gone"))
            self.show_calendar_from_gallery()

    def force_rescan_dups(self):
        self._dups_stale = False  # clear stale on rescan
        self.last_dups = None
        self.last_dups_groups = []  # clear groups too
        if self.dup_thread and self.dup_thread.isRunning():
            _old = self.dup_thread
            self.dup_thread = None
            _old.stop()
            if _old.isRunning():
                logger.warning("force_rescan: dup thread didn't stop, "
                               "waiting 10s more")
                if not _old.wait(10000):
                    logger.error("dup thread won't die, terminating")
                    _old.terminate()
                    _old.wait(2000)
        self.change_view_mode(self.combo_view_mode.currentIndex())

    # ---- shortcuts / drag & drop
    def setup_shortcuts(self):
        # QShortcut -> QAction refactor. Actions are stored in
        # self._actions (dict) so the Python wrapper cannot be GC'd --
        # same invariant, just moved from closure to registry.
        # Live-rebind: see rebind_hotkeys().
        self._actions = {}
        self._bind_app_action("undo",         self.undo)
        self._bind_app_action("redo",         self.redo)
        self._bind_app_action("redo_alt",     self.redo)
        self._bind_app_action("copy",         self._on_copy_action)
        self._bind_app_action("cut",          self.cut_selected_files)
        self._bind_app_action("paste",        self.paste_files)
        self._bind_app_action("delete",       self._on_delete_action)
        self._bind_app_action("select_all",   self._on_select_all_action)
        self._bind_app_action("remove_event", self.remove_selected_from_event)
        self.image_viewer.rebind_hotkeys(self.config)

    def _bind_app_action(self, action_id, slot):
        """create or replace an ApplicationShortcut QAction.
        Stored in self._actions[action_id]. Shortcut resolved from
        config via _resolved_hotkey.
        """
        key = _resolved_hotkey(self.config, action_id)
        # (U2a): setText -- the same QAction is later
        # added to the Edit menu; without text those
        # entries showed as blank lines.
        _label = t(HOTKEY_ACTIONS[action_id]["label"])
        act = QAction(_label, self)
        if key:
            act.setShortcut(QKeySequence(key))
        # (U2a): always set ApplicationShortcut context,
        # even if key is empty (rebind_hotkeys() later sets
        # a shortcut but would leave WindowShortcut default).
        act.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act.triggered.connect(slot)
        self.addAction(act)
        self._actions[action_id] = act

    def rebind_hotkeys(self):
        """re-apply shortcuts from config without restart.
        Actions are NOT recreated -- only setShortcut is updated, so
        menubar references (self._actions) stay valid.
        """
        for aid, act in self._actions.items():
            key = _resolved_hotkey(self.config, aid)
            act.setShortcut(QKeySequence(key) if key else QKeySequence())
        self.image_viewer.rebind_hotkeys(self.config)

    def _on_copy_action(self):
        """dispatch Ctrl+C by current stack widget.
        Gallery (4) -> copy_selected_files; Viewer (5) -> copy_current.
        Other stacks -> no-op (no toast; keep quiet).
        (U1a): forward to text widgets and modal guard.
        ApplicationShortcut would otherwise swallow Ctrl+C in
        QLineEdit / QTextEdit / QPlainTextEdit / QTextBrowser.
        """
        if _dispatch_forward_text("copy"):
            return
        _mw = QApplication.activeModalWidget()
        if _mw is not None and _mw is not self:
            return
        idx = self.stack.currentIndex()
        if idx == 4:
            self.copy_selected_files()
        elif idx == 5:
            self.image_viewer.copy_current()

    def _on_delete_action(self):
        """dispatch Del by current stack widget.
        Gallery (4) -> delete_selected_files (has @_busy_guard +
        _guard_scan internally); Viewer (5) -> delete_current.
        (U1b): text-forward + modal guard.
        (U5b): _guard_scan on stack 5 too (previously
        Viewer bypassed F17 scan protection).
        """
        if _dispatch_forward_text("delete"):
            return
        _mw = QApplication.activeModalWidget()
        if _mw is not None and _mw is not self:
            return
        idx = self.stack.currentIndex()
        if idx == 4:
            self.delete_selected_files()
        elif idx == 5:
            if self._guard_scan():
                return
            self.image_viewer.delete_current()

    def _on_select_all_action(self):
        """Ctrl+A dispatch. Replaces the QShortcut on
        GalleryWidget. CRITICAL: the stack==4 guard reproduces the
        (NB-05) F10 fix -- without it, Ctrl+A in Settings/Help
        would silently select hidden gallery items. Do not remove
        this check.
        (U1c): text-forward + modal guard.
        """
        if _dispatch_forward_text("select_all"):
            return
        _mw = QApplication.activeModalWidget()
        if _mw is not None and _mw is not self:
            return
        if self.stack.currentIndex() != 4:
            return
        if self.gallery_view.is_dups_mode:
            self.gallery_view.dup_gallery.select_all()
        else:
            self.gallery_view.list_widget.selectAll()

    # ---- platform / menubar helpers
    def _window_title(self):
        base = t("app_title")
        if BETA_PLATFORM:
            return base + " " + t("beta_title_suffix")
        return base

    def _build_menubar(self):
        """basic menubar (File / Edit / Settings / Help).

        Edit items reference the SAME QAction objects from
        self._actions (created by setup_shortcuts) -- no ambiguity,
        no double registration. Shortcuts render in the menu
        automatically and live-update on rebind_hotkeys().

        File / Settings / Help items are still local QAction
        objects (Ctrl+O, Ctrl+Q, Ctrl+,, About), kept alive via
        self._menubar_action_refs against PySide6 GC (pattern).
        """
        # NB-69: delete old menubar QActions before recreating them.
        # parent=self, mb.clear() only detaches from the menu — the
        # old QActions stay alive and keep their shortcuts
        # (Ctrl+O, Ctrl+Q, Ctrl+,), so a second _build_menubar on
        # retranslate_ui yields ambiguous shortcuts. Clear shortcut
        # immediately, then deleteLater (C++ cleanup deferred).
        for _old in getattr(self, "_menubar_action_refs", []):
            try:
                _old.setShortcut(QKeySequence())
            except RuntimeError:
                pass
            try:
                _old.deleteLater()
            except RuntimeError:
                pass
        self._menubar_action_refs = []
        # (U2b): refresh labels on retranslate
        for _aid, _act in self._actions.items():
            try:
                _act.setText(t(HOTKEY_ACTIONS[_aid]["label"]))
            except Exception:
                pass
        mb = self.menuBar()
        mb.clear()

        def _mk(menu, label, slot, shortcut=None):
            a = QAction(label, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.triggered.connect(slot)
            menu.addAction(a)
            self._menubar_action_refs.append(a)
            return a

        # File
        m_file = mb.addMenu(t("menu_file"))
        _mk(m_file, t("toolbar_add_folder"), self.import_folder, "Ctrl+O")
        m_file.addSeparator()
        _mk(m_file, t("menu_quit"), self.close, "Ctrl+Q")

        # Edit -- use shared QActions from self._actions ().
        # Shortcuts rendered by Qt automatically.
        m_edit = mb.addMenu(t("menu_edit"))
        for aid in ("undo", "redo"):
            if aid in self._actions:
                m_edit.addAction(self._actions[aid])
        m_edit.addSeparator()
        for aid in ("copy", "cut", "paste", "delete", "select_all"):
            if aid in self._actions:
                m_edit.addAction(self._actions[aid])

        # Settings
        m_settings = mb.addMenu(t("menu_settings"))
        _mk(m_settings, t("menu_preferences"),
            lambda: self.switch_tab(2), "Ctrl+,")

        # Help
        m_help = mb.addMenu(t("menu_help"))
        _mk(m_help, t("menu_about"), self._show_about_dialog)

    def _show_about_dialog(self):
        # Enriched About — includes runtime versions so bug reports
        # contain useful diagnostic info without extra questions.
        import platform as _plat
        try:
            import PIL as _pil
            _pil_ver = _pil.__version__
        except Exception:
            _pil_ver = "n/a"
        try:
            import PySide6 as _qt
            _qt_ver = _qt.__version__
        except Exception:
            _qt_ver = "n/a"
        try:
            import cv2 as _cv
            _cv_ver = _cv.__version__
        except Exception:
            _cv_ver = "n/a"
        try:
            import pillow_heif as _ph
            _heif = f"{_ph.__version__} (HEIC enabled)"
        except Exception:
            _heif = "not installed (HEIC disabled)"
        _details = (
            f"Python {sys.version.split()[0]}\n"
            f"PySide6 {_qt_ver}\n"
            f"Pillow {_pil_ver}\n"
            f"OpenCV {_cv_ver}\n"
            f"pillow-heif: {_heif}\n"
            f"Platform: {_plat.system()} {_plat.release()} "
            f"({_plat.machine()})"
        )
        text = t("about_text",
                 version=QApplication.applicationVersion(),
                 details=_details)
        if BETA_PLATFORM:
            text += "\n\n" + t("beta_about_note")
        QMessageBox.about(self, t("app_title"), text)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and event.source() is None:
            event.acceptProposedAction()

    def dropEvent(self, event):
        if event.source() is not None:
            return
        # dropEvent busy
        if getattr(self, "_busy", False):
            self.statusBar().showMessage(t("status_busy"), 2000)
            return
        if self.scan_thread and self.scan_thread.isRunning():
            QMessageBox.warning(self, t("title_scan_running"), t("warn_scan_running"))
            return
        urls = event.mimeData().urls()
        if urls:
            # NB-140: only trigger a scan for media-file drops; a random
            # .txt / .pdf dropped on the window would otherwise scan the
            # whole parent folder. Multi-URL: first *usable* entry wins
            # (single scan at a time — start_scanning guards duplicates).
            _started = False
            for url in urls:
                if not url.isLocalFile():
                    continue
                path = url.toLocalFile()
                if os.path.isdir(path):
                    self.start_scanning(path)
                    _started = True
                    break
                if os.path.isfile(path) and path.lower().endswith(VALID_EXTS_TUPLE):
                    self.start_scanning(os.path.dirname(path))
                    _started = True
                    break
            if _started:
                event.acceptProposedAction()
            else:
                self.statusBar().showMessage(
                    "Drop a folder or a media file to scan", 3000)

    # ---- viewer callbacks
    def _on_viewer_back(self):
        # rebuild dup gallery if needed (files may have been deleted/rotated in viewer)
        if self.stack.currentIndex() != 5:
            return
        if self.current_gallery_context == "ALL_DUPLICATES":
            # clear stale rebuild flag — dups path rebuilds independently
            self._gallery_needs_rebuild = False
            # rescan on stale — rotation changed hashes, need fresh scan
            if getattr(self, "_dups_stale", False):
                self._dups_stale = False
                self.last_dups = None
                self.last_dups_groups = []
                self.stack.setCurrentIndex(4)
                self.combo_view_mode.blockSignals(True)
                self.combo_view_mode.setCurrentIndex(4)
                self.combo_view_mode.blockSignals(False)
                self.change_view_mode(4)
                return
            remaining = self._get_remaining_dups()
            if remaining:
                self.last_dups = remaining
                self.open_duplicates_gallery()
                return
            # all dups gone
            self.last_dups = None
            self.last_dups_groups = []
            self.current_gallery_context = None
            self.stack.setCurrentIndex(0)
            self.combo_view_mode.blockSignals(True)
            self.combo_view_mode.setCurrentIndex(0)
            self.combo_view_mode.blockSignals(False)
            self.change_view_mode(0)
            return
        # rebuild only if needed (delete in viewer)
        if self._gallery_needs_rebuild:
            self._gallery_needs_rebuild = False
            if self.current_gallery_context:
                _mode = ["days", "months", "years", "events", "duplicates"][
                    self.combo_view_mode.currentIndex()]
                if _mode != "duplicates":
                    scroll = self.gallery_view.list_widget.verticalScrollBar().value()
                    self.open_gallery(self.current_gallery_context, _mode)
                    QTimer.singleShot(50, lambda: self.gallery_view.list_widget.verticalScrollBar().setValue(scroll))
                    return
        self.stack.setCurrentIndex(4)

    def on_file_rotated_viewer(self, filepath):
        # invalidate thumb cache on rotate — mtime preserved, so key may match
        try:
            cache_path = get_cache_path(filepath, self.config["cache_dir"])
            if cache_path and cache_path.exists():
                cache_path.unlink(missing_ok=True)
        except OSError:
            pass
        # quad-cache keyed on mtime_ns (preserved on rotate) — won't
        # self-invalidate. Clear q_*.jpg off the UI thread.
        _invalidate_quad_cache_async(self.config.get("cache_dir"))
        item = self.gallery_view.list_widget._file_to_item.get(filepath)
        if item:
            item.setIcon(QIcon())
            self.gallery_view.list_widget.update_thumbnail_for_file(filepath)
        # NB-97: rotate may break a dup group (hash changes).
        # Remove every member of any last_dups group containing
        # filepath from self.duplicates[cal]. Survivors stay in the
        # set otherwise and keep ⚠️ marker until next Rescan.
        if self.last_dups:
            for _h, _grp in list(self.last_dups.items()):
                if filepath in _grp:
                    _cal_dups = self.duplicates.get(
                        self.current_cal_name, set())
                    for _fp in _grp:
                        _cal_dups.discard(_fp)
                    self.last_dups.pop(_h, None)
                    break
        # reset last_dups on rotate — any return path triggers rescan
        self._dups_stale = True
        self.last_dups = None
        self.save_json()

    def on_file_deleted_viewer(self, filepath, trash_path=None):
        # don't reset stale on delete — _on_viewer_back must rescan after rotate
        self.save_state_for_undo({"trash": [(trash_path, filepath)] if trash_path else []})
        # NB-97: deleted file may have been in a dup group; the
        # survivor(s) would keep ⚠️ marker until next Rescan.
        # Remove the whole group from self.duplicates[cal] here.
        if self.last_dups:
            for _h, _grp in list(self.last_dups.items()):
                if filepath in _grp:
                    _cal_dups = self.duplicates.get(
                        self.current_cal_name, set())
                    for _fp in _grp:
                        _cal_dups.discard(_fp)
                    self.last_dups.pop(_h, None)
                    break
        self._purge_path_everywhere(filepath)
        self.save_json()
        self.gallery_view.list_widget.remove_item_by_filepath(filepath)
        # stay in viewer — do not pull user out of fullscreen viewer
        if self.stack.currentIndex() == 5:
            if self.current_gallery_context == "ALL_DUPLICATES" and self.last_dups:
                self.last_dups = self._get_remaining_dups()
            else:
                # always flag rebuild — previously skipped when
                # _dups_stale=True, but that left a ghost item in
                # raw_grouped_data visible after sort change.
                # _on_viewer_back dup-path uses change_view_mode(4) which
                # rebuilds anyway, so this doesn't cause double work.
                self._gallery_needs_rebuild = True
                self.last_dups = None
                self.last_dups_groups = []
            return
        if self.current_gallery_context == "ALL_DUPLICATES":
            self.last_dups = self._get_remaining_dups()
            if self.last_dups:
                self.open_duplicates_gallery()
            else:
                self.stack.setCurrentIndex(0)
                self.combo_view_mode.blockSignals(True)
                self.combo_view_mode.setCurrentIndex(0)
                self.combo_view_mode.blockSignals(False)
                self.change_view_mode(0)
        else:
            # clear last_dups too 
            self.last_dups = None
            self.last_dups_groups = []
            if self.current_gallery_context:
                mode = ["days", "months", "years", "events", "duplicates"][self.combo_view_mode.currentIndex()]
                self.open_gallery(self.current_gallery_context, mode)

    @_busy_guard
    def find_missing_files(self):
        """manual cleanup of files that no longer exist on disk.
        Non-destructive by default: shows preview, requires confirmation.
        """
        if self._guard_scan():  # 
            return
        # progress feedback. Previously: os.path.exists scan on a
        # large library (10k+ files on HDD / network drive) froze the
        # window for several seconds with zero UI indication — user
        # thought the app hung. Now: count total upfront (O(lists),
        # no I/O), show determinate progress bar, spin event loop
        # every 200 files to let it paint.
        try:
            _total = sum(len(files) for cal in self.calendars.values()
                         for files in cal.values())
            _total += sum(len(files) for files in self.categories.values())
        except Exception:
            _total = 0
        if _total == 0:
            msg_info(self, t("info_done"), t("info_no_missing_files"))
            return
        self.progress_bar.setRange(0, _total)
        self.progress_bar.setFormat("%v / %m")
        self.progress_bar.show()
        self.statusBar().showMessage(t("status_finding_missing"))
        _process_ui()
        _processed = 0
        report = {}
        for cal_name, cal_data in self.calendars.items():
            missing = []
            for date, files in cal_data.items():
                for f in files:
                    if not os.path.exists(f):
                        missing.append(f)
                    _processed += 1
                    if _processed % 200 == 0:
                        self.progress_bar.setValue(_processed)
                        _process_ui()
            if missing:
                report[cal_name] = missing
        # also scan categories
        cat_missing = []
        for cat_files in self.categories.values():
            for f in cat_files:
                if not os.path.exists(f):
                    cat_missing.append(f)
                _processed += 1
                if _processed % 200 == 0:
                    self.progress_bar.setValue(_processed)
                    _process_ui()
        self.progress_bar.setValue(_total)
        # sentinel key avoids collision with a calendar named "Categories"
        if cat_missing:
            report["__categories__"] = cat_missing
        if not report:
            self.progress_bar.hide()
            self.progress_bar.setRange(0, 100)
            self.statusBar().clearMessage()
            msg_info(self, t("info_done"), t("info_no_missing_files"))
            return
        # found some — hide progress before confirm dialog
        self.progress_bar.hide()
        self.progress_bar.setRange(0, 100)
        self.statusBar().clearMessage()
        total = sum(len(m) for m in report.values())
        lines = [f"  • {'Categories' if cal == '__categories__' else cal}: "
                 f"{len(missing)}"
                 for cal, missing in report.items()]
        msg = t("confirm_clean_missing", total=total) + "\n\n" + "\n".join(lines)
        reply = QMessageBox.question(
            self, t("title_clean_missing"), msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        # re-check existence right before purge — drive may have
        # reconnected while the dialog was open.
        # (P1): also require parent accessible. If external drive
        # disconnected between dialog and purge, os.path.exists False
        # for EVERY file → all_missing = all files → calendar purged.
        # Undo exists but user may not realize what happened.
        all_missing = []
        for missing in report.values():
            for f in missing:
                if os.path.exists(f):
                    continue
                _p = os.path.dirname(f) or "."
                try:
                    _p_ok = (os.path.isdir(_p)
                             and os.access(_p, os.R_OK))
                except OSError:
                    _p_ok = False
                if _p_ok:
                    all_missing.append(f)
        if not all_missing:
            msg_info(self, t("info_done"), t("info_no_missing_files"))
            return
        self.save_state_for_undo()
        # use real removed count (deduplicated by set inside purge)
        removed = self._purge_specific_paths(all_missing)
        self.last_dups = None
        self.last_dups_groups = []
        self._dups_stale = False
        self.save_json()
        self.refresh_all_views()
        self.statusBar().showMessage(
            t("status_cleaned_missing", n=removed), 5000)

    @_busy_guard
    def restore_from_backup(self):
        """list available backups and restore one."""
        if self._guard_scan():  # 
            return
        if not BACKUP_DIR.exists():
            msg_info(self, t("info_done"), t("info_no_backups"))
            return
        backups = sorted(BACKUP_DIR.glob("calendar_data_*.json"), reverse=True)
        if not backups:
            msg_info(self, t("info_done"), t("info_no_backups"))
            return
        items = []
        for b in backups[:20]:
            try:
                count = _count_files_in_json(str(b))
                size_kb = b.stat().st_size // 1024
                items.append(f"{b.stem} — {count} files, {size_kb} KB")
            except Exception:
                items.append(b.stem)
        choice, ok = QInputDialog.getItem(
            self, t("title_restore_backup"),
            t("label_restore_backup"), items, 0, False)
        if not ok or not choice:
            return
        try:
            idx = items.index(choice)
        except ValueError:
            return
        backup_path = str(backups[idx])
        try:
            with open(backup_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            msg_error(self, t("error_title"), str(e))
            return
        if not isinstance(data, dict):
            msg_error(self, t("error_title"),
                      "Invalid backup format: expected JSON object")
            return
        reply = QMessageBox.question(
            self, t("confirm_overwrite"),
            t("confirm_restore_body", name=os.path.basename(backup_path)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        # validate types BEFORE mutating instance state
        _cal = data.get("calendars", {"Default": {}})
        _dups = data.get("duplicates", {})
        _cats = data.get("categories", {})
        if not isinstance(_cal, dict):
            msg_error(self, t("error_title"), "Invalid 'calendars' in backup")
            return
        if not isinstance(_dups, dict):
            msg_error(self, t("error_title"), "Invalid 'duplicates' in backup")
            return
        if not isinstance(_cats, dict):
            msg_error(self, t("error_title"), "Invalid 'categories' in backup")
            return
        # (BUG-A): validate ALL values BEFORE mutating self.*
        # fields. Previously self.calendars = _cal was assigned first;
        # then set(None) on a bad duplicates value raised TypeError →
        # calendars were already replaced, duplicates/categories still
        # old → closeEvent force-saved inconsistent state to disk.
        _dups_converted = {}
        for _dk, _dv in _dups.items():
            try:
                _dups_converted[_dk] = set(_dv) if _dv is not None else set()
            except (TypeError, ValueError):
                msg_error(self, t("error_title"),
                          f"Invalid 'duplicates' value for {_dk!r} "
                          f"in backup (expected list, got "
                          f"{type(_dv).__name__})")
                return
        self.save_state_for_undo()
        try:
            self.calendars = _cal
            self.duplicates = _dups_converted
            self.categories = _cats
            # (NB-03) + (NB-28): isinstance(list) AND element
            # whitelist. [1, 2, 3] passed isinstance but yielded 0 files.
            self.priority = _sanitize_priority_list(
                data.get("priority", DEFAULT_PRIORITY))
            # NB-64: filter PRIO_CTIME on Linux, same
            # as load_last_session and SettingsWidget.
            # _add_priority_item. A Windows-made backup would
            # otherwise restore a priority list containing
            # PRIO_CTIME, which yields 0 files on Linux
            # (st_ctime = metadata change time, not creation).
            if platform.system() == "Linux":
                self.priority = [p for p in self.priority
                                 if p != PRIO_CTIME]
                # (U4b): same fallback as load_last_session
                if not self.priority:
                    self.priority = [p for p in DEFAULT_PRIORITY
                                     if p != PRIO_CTIME]
            if not self.calendars:
                self.calendars = {"Default": {}}
            if self.current_cal_name not in self.calendars:
                self.current_cal_name = next(iter(self.calendars))
            self.duplicates.setdefault(self.current_cal_name, set())
            self._sanitize_calendars()
            # re-validate current_cal_name after sanitize (same
            # reason as load_last_session). Otherwise subsequent save
            # writes invalid current_cal and update_combo may crash on
            # non-str keys.
            if (not isinstance(self.current_cal_name, str)
                    or self.current_cal_name not in self.calendars):
                if self.calendars:
                    self.current_cal_name = next(iter(self.calendars))
                else:
                    self.calendars = {"Default": {}}
                    self.current_cal_name = "Default"
            self.duplicates.setdefault(self.current_cal_name, set())
            self.last_dups = None
            self.last_dups_groups = []
            self._dups_stale = False
            self.update_combo()
            # priority may have changed — recreate settings view so
            # the priority list reflects the restored data
            try:
                self._rebuild_translatable_widgets()
            except Exception as e:
                logger.warning(f"restore: settings rebuild failed: {e}")
            # force=True — we're inside @_busy_guard, normal path defers.
            # NB-75: skip_backup_dialog=False so the NB-36 dialog fires
            # if the post-restore backup fails — restore is destructive.
            self._save_timer.stop()
            saved = self._do_save_json(force=True, skip_backup_dialog=False)
            # NB-88: change_view_mode(4) sees _busy=True (we are still
            # inside @_busy_guard) and rolls back combo without
            # switching stack — leaving stale dup cards from pre-restore
            # state on screen. Defer mode application until after
            # _busy_guard exits.
            _target_view = self.combo_view_mode.currentIndex()
            QTimer.singleShot(
                0, lambda: self.change_view_mode(_target_view))
            if saved is not True:
                msg_error(self, t("error_title"), t("error_save_failed"))
                return
            msg_info(self, t("info_done"), t("info_restore_done"))
        except Exception as e:
            msg_error(self, t("error_title"), str(e))

    def _sanitize_calendars(self):
        """Remove invalid keys/values from all calendars. Returns count removed."""
        # NB-116: dead date_re removed — validation goes through
        # datetime.strptime + roundtrip (see _dt below). G8 flagged.
        removed = 0
        for cal_name in list(self.calendars.keys()):
            # (P1 crash): non-str calendar keys. Corrupted
            # JSON {"calendars": {123: {...}}} passes isinstance
            # checks but crashes later in update_combo ->
            # combo_cal.addItems(calendars.keys()) with TypeError
            # (Qt expects Sequence[str]). Drop them at sanitize
            # time -- update_combo runs right after load_last_session.
            if not isinstance(cal_name, str):
                logger.warning(
                    f"Dropping non-str calendar name {cal_name!r}")
                try:
                    self.calendars.pop(cal_name, None)
                except Exception:
                    pass
                removed += 1
                continue
            cal_data = self.calendars[cal_name]
            if not isinstance(cal_data, dict):
                logger.warning(f"Calendar {cal_name!r} is not a dict, resetting")
                self.calendars[cal_name] = {}
                removed += 1
                continue
            clean = {}
            for k, v in cal_data.items():
                if not isinstance(k, str):
                    logger.warning(f"Dropping non-str key {k!r} in calendar {cal_name!r}")
                    removed += 1
                    continue
                # validate via strptime (regex passes "2024-99-99")
                # (NB-38): roundtrip check. strptime accepts
                # non-canonical forms like "2024-1-1"; draw_days /
                # draw_months filter by ^\d{4}-\d{2}-\d{2}$, so such
                # entries would silently be invisible in the calendar.
                try:
                    _dt = datetime.datetime.strptime(k, "%Y-%m-%d")
                    if _dt.strftime("%Y-%m-%d") != k:
                        raise ValueError(f"non-canonical date {k!r}")
                except (ValueError, TypeError):
                    logger.warning(f"Dropping invalid date {k!r} in calendar {cal_name!r}")
                    removed += 1
                    continue
                if not isinstance(v, list):
                    logger.warning(f"Dropping non-list value for {k!r}")
                    removed += 1
                    continue
                files = [f for f in v if isinstance(f, str)]
                if len(files) < len(v):
                    removed += len(v) - len(files)  # count invalid entries
                if files:
                    clean[k] = files
            self.calendars[cal_name] = clean
            # sanitize dups 
            valid_files = {f for files in clean.values() for f in files}
            # duplicates may be non-set from corrupted JSON
            # NB-81: set(_dup_v) on unhashable items (list-of-lists from
            # a manually edited JSON) raised TypeError outside any
            # try/except. Wrap; fallback to empty set with a warning.
            # No data loss — surviving dup paths are re-derived from
            # calendars[cal_name] which is already validated above.
            _dup_v = self.duplicates.get(cal_name)
            if isinstance(_dup_v, set):
                self.duplicates[cal_name] = _dup_v & valid_files
            elif isinstance(_dup_v, (list, tuple)):
                try:
                    _as_set = set(_dup_v)
                    self.duplicates[cal_name] = _as_set & valid_files
                except (TypeError, ValueError) as _de:
                    logger.warning(
                        f"sanitize: dup {cal_name!r} contains "
                        f"unhashable items ({_de}), resetting")
                    self.duplicates[cal_name] = set()
            elif cal_name in self.duplicates:
                logger.warning(
                    f"sanitize: dup {cal_name!r} is "
                    f"{type(_dup_v).__name__}, resetting")
                self.duplicates[cal_name] = set()
        # remove duplicates entries for deleted calendars
        self.duplicates = {k: v for k, v in self.duplicates.items()
                           if k in self.calendars}
        # sanitize categories — null/bad values crash Events view
        try:
            _clean_cats = {}
            _cat_removed = 0
            for _cname, _cfiles in list(self.categories.items()):
                if not isinstance(_cname, str):
                    _cat_removed += 1
                    continue
                if not isinstance(_cfiles, list):
                    logger.warning(
                        f"sanitize: category {_cname!r} is "
                        f"{type(_cfiles).__name__}, dropping")
                    _cat_removed += 1
                    continue
                _valid = [f for f in _cfiles if isinstance(f, str)]
                if len(_valid) < len(_cfiles):
                    _cat_removed += len(_cfiles) - len(_valid)
                # (P2): preserve EMPTY events. Previously
                # `if _valid: _clean_cats[...]` dropped empty lists,
                # so a user-created "New event" without any files
                # was silently deleted on next launch.
                # Empty list is valid state — filtering for display
                # happens in draw_events (F5), not here.
                _clean_cats[_cname] = _valid
            self.categories = _clean_cats
            removed += _cat_removed
        except Exception as e:
            # (NB-01): NEVER wipe all categories on exception.
            # A partial failure mid-loop used to leave `self.categories = {}`
            # user's entire event list gone, only recoverable from backup.
            # Better: leave as-is (some invalid entries, but 99%+ valid),
            # then best-effort per-item cleanup. Worst case: stale entries
            # remain, which is far less harmful than wiping.
            logger.warning(
                f"sanitize: categories fix failed: {e}; "
                f"leaving as-is to avoid data loss")
            try:
                for _cname in list(self.categories.keys()):
                    _cv = self.categories.get(_cname)
                    if (not isinstance(_cname, str)
                            or not isinstance(_cv, list)):
                        self.categories.pop(_cname, None)
                    else:
                        self.categories[_cname] = [
                            f for f in _cv if isinstance(f, str)]
            except Exception:
                pass  # leave as-is; partial > wiped
        if removed:
            logger.info(f"Sanitized {removed} invalid entries")
        return removed

    def _purge_paths_bulk(self, paths):
        """Bulk remove paths from all calendars, duplicates and categories."""
        to_remove = set(paths)
        if not to_remove:
            return
        for cal_data in self.calendars.values():
            for d in list(cal_data.keys()):
                cal_data[d] = [f for f in cal_data[d] if f not in to_remove]
                if not cal_data[d]:
                    del cal_data[d]
        for cat_files in self.categories.values():
            cat_files[:] = [f for f in cat_files if f not in to_remove]
        # dup clear in bulk 
        for dup_set in self.duplicates.values():
            dup_set -= to_remove

    def _purge_path_everywhere(self, filepath):
        """Remove a path from every calendar, duplicates set and category. Fix 1.25."""
        for cal_name, cal_data in self.calendars.items():
            for d_str in list(cal_data.keys()):
                if filepath in cal_data[d_str]:
                    cal_data[d_str].remove(filepath)
                    if not cal_data[d_str]:
                        del cal_data[d_str]
            # no phantom set 
            if cal_name in self.duplicates:
                self.duplicates[cal_name].discard(filepath)
        # drop list in purge — dict not mutated, only inner lists
        # NB-106: full removal via comprehension. list.remove removes
        # only the first occurrence; a corrupted JSON with duplicate
        # paths would leave a stale entry.
        for cat_files in self.categories.values():
            if filepath in cat_files:
                cat_files[:] = [f for f in cat_files if f != filepath]

    def show_calendar_from_gallery(self):
        # combo first — reset was_in_dups to prevent stale dup view
        was_dups = (self.combo_view_mode.currentIndex() == 4)
        self.was_in_dups = False
        if was_dups:
            self.combo_view_mode.blockSignals(True)
            self.combo_view_mode.setCurrentIndex(0)
            self.combo_view_mode.blockSignals(False)
        self.stack.setCurrentIndex(0)
        self.switch_tab(0)

    # ---- calendar CRUD
    def update_combo(self):
        self.manager_view.update_combo(self.calendars, self.current_cal_name)

    def on_cal_changed(self, name):
        # NB-92: guard calendar switch mid-scan, same as NB-55 for
        # create/rename/delete. Without this, current_cal_name changes
        # while ScannerThread writes to captured _scan_cal_name ->
        # user sees new (empty) calendar after merge, files landed
        # in the old one.
        if self._guard_scan():
            try:
                cb = self.manager_view.combo_cal
                cb.blockSignals(True)
                cb.setCurrentText(self.current_cal_name)
                cb.blockSignals(False)
            except Exception:
                pass
            return
        # on_cal_changed no tab jump
        if name and name in self.calendars and name != self.current_cal_name:
            self.current_cal_name = name
            self.duplicates.setdefault(self.current_cal_name, set())
            self.last_dups = None
            self.last_dups_groups = []  # clear groups on cal change 
            self.save_json()
            # Don't force a tab switch: user may be on Manage or Settings.
            # When they click Calendar, switch_tab(0) will refresh the view.

    def create_calendar(self):
        # NB-55: match rename/clear/delete_calendar, which all guard
        # against mid-scan CRUD. Without this, current_cal_name changes
        # while scan writes to captured _scan_cal_name -> user sees
        # empty new calendar after merge (files landed in old one).
        if self._guard_scan():
            return
        name, ok = QInputDialog.getText(self, t("title_new_calendar"), t("label_calendar_name"))
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self.calendars:
            # create_cal conflict 
            msg_warning(self, t("error_title"), t("error_name_exists", name=name))
            self.statusBar().showMessage(t("error_name_exists", name=name), 3000)  # status after conflict cal 
            return
        self.save_state_for_undo()
        self.calendars[name] = {}
        self.duplicates[name] = set()
        self.current_cal_name = name
        self.last_dups = None
        self.update_combo()
        self.change_view_mode(self.combo_view_mode.currentIndex())
        self.save_json()

    def rename_calendar(self):
        if self._guard_scan():  # 
            return
        old = self.current_cal_name
        new, ok = QInputDialog.getText(
            self, t("title_rename_calendar"),
            t("label_rename_calendar", old=old), text=old)
        if not ok:
            return
        # strip early 
        new = new.strip()
        if not new or new == old:
            return
        if new in self.calendars:
            # rename_cal conflict 
            msg_warning(self, t("error_title"), t("error_name_exists", name=new))
            self.statusBar().showMessage(t("error_name_exists", name=new), 3000)  # status after conflict rename cal 
            return
        self.save_state_for_undo()
        self.calendars[new] = self.calendars.pop(old)
        # defensive — old may be missing from duplicates if loaded
        # from corrupted/legacy JSON where _sanitize didn't fill it
        self.duplicates[new] = self.duplicates.pop(old, set())
        self.current_cal_name = new
        self.update_combo()
        self.save_json()

    def export_calendar(self):
        if not self.calendars[self.current_cal_name]:
            # NB-101: silent no-op was confusing. Tell the user.
            msg_info(self, t("info_done"), t("error_calendar_empty"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, t("title_save_calendar"),
            f"{self.current_cal_name}.json", "JSON (*.json)")
        if path:
            try:
                _atomic_write_json(
                    path, self.calendars[self.current_cal_name])
                self.statusBar().showMessage(
                    t("info_done"), 2000)
            except Exception as e:
                logger.error(f"export_calendar failed: {e}")
                msg_error(self, t("error_title"),
                          t("error_save_failed"))

    def load_external_calendar(self):
        if self._guard_scan():  # 
            return
        path, _ = QFileDialog.getOpenFileName(
            self, t("title_import_calendar"), "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # If user pointed at the app's own state file, unwrap it
            if isinstance(data, dict) and "calendars" in data and isinstance(data["calendars"], dict):
                cals = data["calendars"]
                if len(cals) == 1:
                    data = next(iter(cals.values()))
                else:
                    items = list(cals.keys())
                    choice, ok = QInputDialog.getItem(
                        self, t("title_import_calendar"),
                        t("import_pick_calendar"),
                        items, 0, False)
                    if not ok or not choice:
                        return
                    data = cals[choice]

            if not isinstance(data, dict):
                QMessageBox.warning(self, t("error_title"),
                                    t("import_invalid_format"))
                return

            date_re = re.compile(r'^\d{4}-\d{2}-\d{2}$')
            clean_data = {}
            bad = 0
            for k, v in data.items():
                if isinstance(k, str) and date_re.match(k) and isinstance(v, list):
                    clean_data[k] = [f for f in v if isinstance(f, str)]
                else:
                    bad += 1
            if bad:
                logger.warning(f"Import: skipped {bad} invalid entries")
            if not clean_data:
                QMessageBox.warning(self, t("error_title"),
                                    t("import_no_valid_dates", n=bad))
                return

            name, ok = QInputDialog.getText(
                self, t("title_import_calendar"), t("label_import_calendar"),
                text=os.path.basename(path).replace(".json", ""))
            if ok and name.strip():
                name = name.strip()
                # confirm overwrite of existing calendar
                if name in self.calendars:
                    reply = QMessageBox.question(
                        self, t("confirm_overwrite"),
                        t("confirm_overwrite_body", name=name),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                self.save_state_for_undo()
                self.calendars[name] = clean_data
                self.duplicates[name.strip()] = set()
                self.current_cal_name = name.strip()
                self.last_dups = None
                self.update_combo()
                self._sanitize_calendars()  # validate imported dates
                self.change_view_mode(self.combo_view_mode.currentIndex())
                self.save_json()
                if bad:
                    self.statusBar().showMessage(
                        t("import_partial_success", n=bad), 5000)
        except Exception as e:
            QMessageBox.critical(self, t("error_title"), t("error_load", err=str(e)))

    def clear_calendar(self):
        if self._guard_scan():  # 
            return
        if not self.calendars[self.current_cal_name]:
            return
        reply = QMessageBox.question(self, t("confirm_delete"),
                                     t("confirm_clear_cal"),
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        # default No (safe)
        if reply == QMessageBox.StandardButton.Yes:
            self.save_state_for_undo()
            self.calendars[self.current_cal_name] = {}
            self.duplicates[self.current_cal_name] = set()
            self.last_dups = None
            self.change_view_mode(self.combo_view_mode.currentIndex())
            self.save_json()

    def delete_calendar(self):
        if self._guard_scan():  # 
            return
        name = self.current_cal_name
        if len(self.calendars) <= 1:
            msg_warning(self, t("error_title"), t("error_cant_delete_last_cal"))
            return
        reply = QMessageBox.question(
            self, t("confirm_delete"),
            t("confirm_delete_cal", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.save_state_for_undo()
        self.calendars.pop(name, None)
        self.duplicates.pop(name, None)
        self.current_cal_name = next(iter(self.calendars))
        self.last_dups = None
        self.last_dups_groups = []
        self.update_combo()
        self.change_view_mode(self.combo_view_mode.currentIndex())
        self.save_json()

    # ---- events
    def create_event(self):
        name, ok = QInputDialog.getText(self, t("title_new_event"), t("label_event_name"))
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self.categories:
            # create_ev conflict 
            msg_warning(self, t("error_title"), t("error_name_exists", name=name))
            self.statusBar().showMessage(t("error_name_exists", name=name), 3000)  # status after conflict ev 
            return
        self.save_state_for_undo()
        self.categories[name] = []
        self.save_json()
        self.manager_view.update_combo(self.calendars, self.current_cal_name)

    def rename_event(self):
        item = self.manager_view.list_events.currentItem()
        if not item:
            return
        old = item.text()
        new, ok = QInputDialog.getText(
            self, t("title_rename_event"), t("label_event_new_name"), text=old)
        if not ok or not new.strip():
            return
        new = new.strip()
        if new == old:
            return
        if new in self.categories:
            # rename_ev conflict 
            msg_warning(self, t("error_title"), t("error_name_exists", name=new))
            self.statusBar().showMessage(t("error_name_exists", name=new), 3000)  # status after conflict rename ev 
            return
        self.save_state_for_undo()
        files = self.categories.pop(old)
        self.categories[new] = files
        self.save_json()
        self.manager_view.update_combo(self.calendars, self.current_cal_name)

    def delete_event(self):
        item = self.manager_view.list_events.currentItem()
        if not item:
            return
        name = item.text()
        reply = QMessageBox.question(
            self, t("confirm_delete"), t("confirm_delete_event", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.save_state_for_undo()
            if name in self.categories:
                del self.categories[name]
            self.save_json()
            self.manager_view.update_combo(self.calendars, self.current_cal_name)

    def assign_category_dialog(self, files):
        if not files:
            return
        dialog = AssignEventDialog(list(self.categories.keys()), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.get_name()
        if not name:
            return
        self.save_state_for_undo()
        if name not in self.categories:
            self.categories[name] = []
        for f in files:
            if f not in self.categories[name]:
                self.categories[name].append(f)
        self.save_json()
        self.manager_view.update_combo(self.calendars, self.current_cal_name)
        self.statusBar().showMessage(t("status_files_added", name=name), 3000)

    def remove_selected_from_event(self):
        files = []
        if self.stack.currentIndex() == 4:
            items = self.gallery_view.get_selected_items()
            files = [it.data(ROLE_FILEPATH) for it in items]
        elif self.stack.currentIndex() == 5:
            if 0 <= self.image_viewer.current_index < len(self.image_viewer.files):
                files = [self.image_viewer.files[self.image_viewer.current_index]]
        if files:
            self.remove_from_event(files)

    def remove_from_event(self, files):
        if not files:
            return
        self.save_state_for_undo()
        removed = 0
        for cat_files in self.categories.values():
            for f in files:
                if f in cat_files:
                    cat_files.remove(f)
                    removed += 1
        self.save_json()
        self.refresh_all_views()
        if removed:
            self.statusBar().showMessage(t("status_removed_from_event", n=removed), 3000)

    # ---- tabs
    def switch_tab(self, index):
        self.btn_nav_cal.setChecked(index == 0)
        self.btn_nav_mgr.setChecked(index == 1)
        self.btn_nav_set.setChecked(index == 2)
        self.btn_nav_help.setChecked(index == 3)
        if index == 0:
            # no dup-scan from nav 
            if self.was_in_dups or self.combo_view_mode.currentIndex() == 4:
                if self.last_dups:
                    self.change_view_mode(4)
                else:
                    self.was_in_dups = False
                    # clear groups in switch_tab fallback 
                    self.last_dups_groups = []
                    self.combo_view_mode.blockSignals(True)
                    self.combo_view_mode.setCurrentIndex(0)
                    self.combo_view_mode.blockSignals(False)
                    self.stack.setCurrentIndex(0)
                    self.cal_toolbar_widget.show()
                    self.change_view_mode(0)
            else:
                self.stack.setCurrentIndex(0)
                self.cal_toolbar_widget.show()
                self.change_view_mode(self.combo_view_mode.currentIndex())
        elif index == 1:
            self.was_in_dups = (self.stack.currentIndex() == 4 and self.gallery_view.is_dups_mode)
            self.stack.setCurrentIndex(1)
            self.cal_toolbar_widget.hide()
            self.manager_view.update_combo(self.calendars, self.current_cal_name)
        elif index == 2:
            self.was_in_dups = (self.stack.currentIndex() == 4 and self.gallery_view.is_dups_mode)
            self.stack.setCurrentIndex(2)
            self.cal_toolbar_widget.hide()
        elif index == 3:
            self.was_in_dups = (self.stack.currentIndex() == 4 and self.gallery_view.is_dups_mode)
            self.stack.setCurrentIndex(3)
            self.cal_toolbar_widget.hide()

    def update_priority(self, new_priority):
        self.priority = new_priority
        self.save_json()

    # ---- scanning
    def import_folder(self):
        if self._guard_scan():  # (covers scan_thread.isRunning too)
            return
        # import_folder _busy
        if getattr(self, "_busy", False):
            self.statusBar().showMessage(t("status_busy"), 2000)
            return
        folder = QFileDialog.getExistingDirectory(self, t("toolbar_add_folder"))
        if folder:
            self.start_scanning(folder)

    def cancel_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self._scan_cancelled = True
            # NB-47: show "Stopping..." BEFORE blocking stop().
            # stop() waits up to 5s; message previously appeared
            # only AFTER wait() returned -- on network drives / AV
            # the OS could show "Not Responding" during that gap.
            self.statusBar().showMessage(
                t("status_stopping_scan"), 3000)
            _process_ui()
            stopped = self.scan_thread.stop()  # wait timeout check 
            if not stopped:
                return  # let finished-handler clean up
            self.scan_thread = None
            self._scanned_files.clear()
            self._end_scan()  # 
            self.btn_import.setEnabled(True)
            self.btn_cancel_scan.hide()
            self.progress_bar.hide()
            self.statusBar().showMessage(t("status_ready"), 3000)

    def start_scanning(self, folder):
        if self.scan_thread and self.scan_thread.isRunning():
            return
        # (NB-37): refuse to start a new scan while dup-scan runs.
        # Both use update_progress; on_duplicates_found could apply
        # stale last_dups; also I/O thrash on HDD.
        if self.dup_thread and self.dup_thread.isRunning():
            self.statusBar().showMessage(t("status_busy"), 2000)
            return
        # set flag BEFORE thread starts — destructive ops are
        # blocked from the very first second, no race window.
        self._scan_in_progress = True
        # (NB-35): guard setup. If ScannerThread()/start() raises
        # (rare, but possible on misbehaving platform), _scan_in_progress
        # must be cleared or destructive ops are blocked until restart.
        try:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setFormat(t("status_counting_files"))  # counting files localized 
            self.progress_bar.show()
            self.btn_import.setEnabled(False)
            self.btn_cancel_scan.show()
            self.statusBar().showMessage(t("status_counting_files"))
            # reset was_in_dups 
            self.was_in_dups = False
            # reset combo before switch_tab 
            self.combo_view_mode.blockSignals(True)
            self.combo_view_mode.setCurrentIndex(0)
            self.combo_view_mode.blockSignals(False)
            try:
                self.switch_tab(0)
            except Exception:
                logger.exception("switch_tab failed during scan start")
            self._scanned_files.clear()
            self._scan_cal_name = self.current_cal_name
            self._scan_cancelled = False  # reset scan flags 
            self.last_dups = None
            self.scan_thread = ScannerThread(folder, self.priority)
            _thread = self.scan_thread
            self.scan_thread.progress.connect(self.update_progress)
            self.scan_thread.file_found.connect(self.add_file_to_data)
            # capture thread reference for stale check
            self.scan_thread.finished.connect(
                lambda t=_thread: self.scan_finished(t))
            self.scan_thread.start()
        except Exception:
            logger.exception("start_scanning failed")
            self._scan_in_progress = False
            self.btn_import.setEnabled(True)
            self.btn_cancel_scan.hide()
            self.progress_bar.hide()
            self.scan_thread = None
            self.statusBar().showMessage(t("status_ready"), 3000)
            # no raise — exception in slot goes to Qt
            # excepthook; in --noconsole may cause abort. Cleanup
            # already done, nothing useful to propagate.
            return

    def update_progress(self, cur, total):
        # NB-96: do not clobber a progress bar owned by an active
        # file operation (delete / paste / restore). Dup-scan runs
        # concurrently with delete; without this guard its emissions
        # reset the bar that delete just set.
        if getattr(self, "_busy", False):
            return
        # (P1): ScanProgressDialog is for MAIN scan only.
        # update_progress is connected to BOTH ScannerThread.progress
        # and DuplicateScanThread.progress. Without the
        # _scan_in_progress guard, dup-scan (which runs while
        # _scan_in_progress == False) would open the modal dialog,
        # and Cancel would not stop it (cancel_scan only handles
        # self.scan_thread). That is a modal deadlock — regression.
        if (self._scan_in_progress
                and total > 0
                and self._scan_dialog is None):
            if self._should_show_scan_dialog(total):
                self._open_scan_dialog(total)
        if self._scan_dialog is not None:
            self._scan_dialog.set_progress(cur, total)
        if total > 0:
            # NB-09: dup-scan now reports a monotonic total
            # (sizing count + hashing count). Sizing phase already
            # set maximum to the sizing-only total; hashing phase
            # sends a larger total. Re-set range whenever the
            # incoming total exceeds the current maximum, so the
            # bar keeps climbing instead of appearing stalled.
            if (self.progress_bar.maximum() == 0
                    or total > self.progress_bar.maximum()):
                self.progress_bar.setRange(0, total)
                self.progress_bar.setFormat("%v / %m")
            self.progress_bar.setValue(cur)
            self.statusBar().showMessage(t("status_scanning", cur=cur, total=total))

    def add_file_to_data(self, filepath, date_str):
        self._scanned_files[filepath] = date_str

    def scan_finished(self, thread=None):
        # re-entrancy guard — _process_ui inside delete/paste does NOT
        # block queued signals from worker threads. If scan finished while
        # user is mid-operation, defer until _busy clears, otherwise
        # save_state_for_undo() corrupts undo history (undo_actions[-1] will
        # be overwritten by the caller after this returns).
        if getattr(self, "_busy", False):
            logger.info("scan_finished: busy, deferring 200ms")
            QTimer.singleShot(200, lambda: self.scan_finished(thread))
            return
        # ignore stale thread result
        if thread is not None and thread is not self.scan_thread:
            logger.info("scan_finished: stale thread result, ignored")
            try:
                thread.deleteLater()
            except Exception:
                pass
            return
        if self._scan_cancelled:
            self._scan_cancelled = False
            self._scan_cal_name = None
            self._scanned_files.clear()
            self.btn_import.setEnabled(True)
            self.btn_cancel_scan.hide()
            self.progress_bar.hide()
            self.scan_thread = None
            self._end_scan()  # 
            return
        # scan into captured calendar
        target_name = getattr(self, "_scan_cal_name", self.current_cal_name)
        if target_name not in self.calendars:
            # log fallback 
            logger.warning(
                f"scan_finished: target calendar {target_name!r} no longer exists, "
                f"falling back to {self.current_cal_name!r}")
            target_name = self.current_cal_name
        if target_name not in self.calendars:
            logger.error("scan_finished: no valid calendar, discarding scan results")
            self._scanned_files.clear()
            self._scan_cal_name = None
            self.btn_import.setEnabled(True)
            self.btn_cancel_scan.hide()
            self.progress_bar.hide()
            self.scan_thread = None
            self._end_scan()  # 
            return
        # NB-17: save undo state ONLY when we have a valid target.
        # Before: empty undo entry was pushed when target calendar had
        # been deleted mid-scan (harmless but noisy — see KNOWN_LIM. B4).
        # (NB-17b): also skip when _scanned_files is empty — a scan
        # that found nothing would otherwise push an undo step that
        # Ctrl+Z silently rolls back (user thinks undo is broken).
        try:
            if self._scanned_files:
                self.save_state_for_undo()
            cal_data = self.calendars[target_name]
            for fp in self._scanned_files:
                for d in list(cal_data.keys()):
                    if fp in cal_data[d]:
                        cal_data[d].remove(fp)
                        if not cal_data[d]:
                            del cal_data[d]
            for fp, ds in self._scanned_files.items():
                if not os.path.exists(fp):
                    continue
                cal_data.setdefault(ds, [])
                if fp not in cal_data[ds]:
                    cal_data[ds].append(fp)

            # NB-62: reset dup cache BEFORE view refresh
            # below. open_gallery() reads last_dups and would
            # otherwise apply stale warning markers to freshly
            # scanned files (regression: this reset lived
            # in the finally block, which runs after the try
            # block, so open_gallery saw the old last_dups).
            self.last_dups = None
            self.last_dups_groups = []
            self._dups_stale = False
            
            cur_stack = self.stack.currentIndex()
            if cur_stack == 0:
                self.change_view_mode(self.combo_view_mode.currentIndex())
            elif cur_stack == 4 and self.current_gallery_context:
                _m = ["days", "months", "years", "events", "duplicates"][
                    self.combo_view_mode.currentIndex()]
                if _m != "duplicates":
                    self.open_gallery(self.current_gallery_context, _m)
            self.save_json()
            total = sum(len(v) for v in cal_data.values())
            self.statusBar().showMessage(t("status_found", total=total, dups=0), 5000)
        except Exception as e:
            logger.exception("scan_finished: merge failed")
        finally:
            self._scanned_files.clear()
            self._scan_cal_name = None
            self.btn_import.setEnabled(True)
            self.btn_cancel_scan.hide()
            self.progress_bar.hide()
            self.progress_bar.setRange(0, 100)
            self.scan_thread = None
            self._end_scan()

    def _watch_cut_files(self):
        """poll for external cut+paste.

        Improvements over previous versions:
          - skips ticks while _busy (paste/delete running)
          - tracks partial moves: removes only disappeared files from
            clipboard, keeps watching the rest
          - uses _purge_specific_paths (safe — only confirmed-missing)
          - rebuilds view from any tab (not only gallery)
          - clear status message on partial moves
        """
        if getattr(self, "_busy", False):
            return  # let current operation finish
        # also skip during scan. _purge_specific_paths + view
        # rebuild mid-scan causes needless I/O; scan_finished will
        # re-run os.path.exists filter on merge (). Skip cleanly.
        if getattr(self, "_scan_in_progress", False):
            return
        if self.clipboard_action != "move" or not self.clipboard_files:
            self._cut_watch_timer.stop()
            return
        # distinguish "moved" from "drive disconnected"
        def _file_confirmed_missing(fp):
            if os.path.exists(fp):
                return False
            parent = os.path.dirname(fp) or "."
            try:
                return os.path.isdir(parent) and os.access(parent, os.R_OK)
            except OSError:
                return False
        # reset streak for files that reappeared
        for _fp in list(self._cut_missing_streak.keys()):
            if os.path.exists(_fp):
                self._cut_missing_streak.pop(_fp, None)
        missing_now = [fp for fp in self.clipboard_files
                       if _file_confirmed_missing(fp)]
        if not missing_now:
            return
        # require N consecutive ticks before purge (AV/backup defense)
        confirmed_missing = []
        for fp in missing_now:
            self._cut_missing_streak[fp] = (
                self._cut_missing_streak.get(fp, 0) + 1)
            if self._cut_missing_streak[fp] >= self._cut_confirmation_ticks:
                confirmed_missing.append(fp)
        if not confirmed_missing:
            return
        logger.info(
            f"_watch_cut_files: {len(confirmed_missing)}/"
            f"{len(self.clipboard_files)} cut file(s) confirmed moved "
            f"(>= {self._cut_confirmation_ticks} ticks)")
        # keep remaining files under watch
        _removed_set = set(confirmed_missing)
        self.clipboard_files = [fp for fp in self.clipboard_files
                                if fp not in _removed_set]
        if not self.clipboard_files:
            self.clipboard_action = None
            self.btn_paste.setEnabled(False)
            self._cut_watch_timer.stop()
        # purge only the confirmed-missing files
        # capture remaining BEFORE clearing last_dups
        # (pre-existing bug from patch 7 — same as _on_files_moved_externally)
        # purge confirmed_missing (>=3 ticks), not raw missing
        remaining_dups = self._get_remaining_dups()
        removed = self._purge_specific_paths(confirmed_missing)
        # pop streak entries for purged files. Otherwise
        # _cut_missing_streak retains every path that was ever
        # confirmed-missing, growing unbounded over long sessions.
        for _fp in confirmed_missing:
            self._cut_missing_streak.pop(_fp, None)
        # always proceed — even if removed==0, categories/dups may
        # have been touched and UI needs refreshing.
        self.last_dups = remaining_dups if remaining_dups else None
        self._sync_duplicates_from_last_dups()  # NB-70
        self.last_dups_groups = []
        self._dups_stale = False
        self.save_json()
        # rebuild current view
        if self.stack.currentIndex() == 4:
            # NB-98: state already purged+saved; a failure in the
            # rebuild would leave the UI stale until next tick.
            # Log instead of crashing the Qt slot.
            try:
                scroll = 0
                dup_scroll = 0
                if self.gallery_view.is_dups_mode:
                    dup_scroll = self.gallery_view.dup_gallery.verticalScrollBar().value()
                else:
                    scroll = self.gallery_view.list_widget.verticalScrollBar().value()
                if self.current_gallery_context == "ALL_DUPLICATES":
                    if remaining_dups:
                        self.open_duplicates_gallery()
                        QTimer.singleShot(50, lambda: self.gallery_view.dup_gallery
                                          .verticalScrollBar().setValue(dup_scroll))
                    else:
                        self.show_calendar_from_gallery()
                elif self.current_gallery_context:
                    mode = ["days", "months", "years", "events", "duplicates"][
                        self.combo_view_mode.currentIndex()]
                    if mode != "duplicates":
                        self.open_gallery(self.current_gallery_context, mode)
                        QTimer.singleShot(50, lambda: self.gallery_view.list_widget
                                          .verticalScrollBar().setValue(scroll))
            except Exception:
                logger.exception("_watch_cut_files: view rebuild failed")
        elif self.stack.currentIndex() == 0:
            self.change_view_mode(self.combo_view_mode.currentIndex())
        elif self.stack.currentIndex() == 5:
            # NB-58: filter image_viewer.files to drop confirmed-
            # missing paths, then clamp/re-locate current_index.
            # Without this, external move of non-current cut files
            # leaves stale entries in viewer: next/prev hit 'Could
            # not load' for files already purged from calendar.
            _missing_set = set(confirmed_missing)
            _viewer_files_changed = False
            if _missing_set:
                _old_cur = self.image_viewer.current_index
                _old_fp = (self.image_viewer.files[_old_cur]
                           if 0 <= _old_cur < len(self.image_viewer.files)
                           else None)
                _new_files = [f for f in self.image_viewer.files
                              if f not in _missing_set]
                if len(_new_files) != len(self.image_viewer.files):
                    self.image_viewer.files = _new_files
                    _viewer_files_changed = True
                    if _old_fp and _old_fp in _new_files:
                        self.image_viewer.current_index = _new_files.index(_old_fp)
                    elif _new_files:
                        self.image_viewer.current_index = min(
                            _old_cur, len(_new_files) - 1)
                    else:
                        self.image_viewer.current_index = -1
            cur_idx = self.image_viewer.current_index
            cur_fp = None
            if 0 <= cur_idx < len(self.image_viewer.files):
                cur_fp = self.image_viewer.files[cur_idx]
            # NB-66: viewer had files, all moved externally.
            # update_view() is a no-op when index = -1, so the
            # viewer would hang on the last removed pixmap. Back
            # out first.
            if not self.image_viewer.files:
                self._gallery_needs_rebuild = True
                self._on_viewer_back()
            elif cur_fp and not os.path.exists(cur_fp):
                self._gallery_needs_rebuild = True
                self._on_viewer_back()
            elif _viewer_files_changed or cur_fp:
                # NB-67: flag gallery rebuild — raw_grouped_data
                # snapshot is not updated by purge, so a non-current
                # file moved externally leaves a stale tile.
                self._gallery_needs_rebuild = True
                self.image_viewer.update_view()
            else:
                self._gallery_needs_rebuild = True
                self._on_viewer_back()
        # use len(missing) — actual files moved; `removed` counts
        # calendar entries, which differs if same file was in two places.
        moved_count = len(confirmed_missing)
        if self.clipboard_files:
            self.statusBar().showMessage(
                t("status_cut_partial",
                  moved=moved_count, left=len(self.clipboard_files)), 5000)
        else:
            self.statusBar().showMessage(
                t("status_cut_done", n=moved_count), 5000)

    def _purge_specific_paths(self, paths):
        """remove known-dead paths from ALL calendars, categories, dups.

        Safe: only touches paths you explicitly pass. No scan, no O(n).
        Used by _watch_cut_files after confirmed external moves.
        """
        to_remove = set(paths)
        if not to_remove:
            return 0
        removed = 0
        for cal_name, cal_data in self.calendars.items():
            for date in list(cal_data.keys()):
                files = cal_data[date]
                kept = [f for f in files if f not in to_remove]
                if len(kept) < len(files):
                    removed += len(files) - len(kept)
                if kept:
                    cal_data[date] = kept
                else:
                    del cal_data[date]
        for cat_files in self.categories.values():
            before = len(cat_files)
            cat_files[:] = [f for f in cat_files if f not in to_remove]
            removed += before - len(cat_files)
        for dup_set in self.duplicates.values():
            dup_set -= to_remove
        return removed

    # removed _purge_missing_files_in_current_cal (dead code)

    # ---- gallery open
    def open_gallery(self, key, mode):
        # NO auto-purge. Previously this called
        # _purge_missing_files_in_current_cal() on every open, which wiped
        # the entire calendar when the external drive was disconnected.
        # Missing files are now cleaned only via Settings -> Find missing files
        # (explicit user action, with preview and confirmation).
        # External cut+paste is handled safely via _watch_cut_files using
        # _purge_specific_paths (only confirmed-missing paths).
        cal_data = self.calendars[self.current_cal_name]
        self.current_gallery_context = key
        # dups from last_dups 
        if self.last_dups:
            dups = {fp for group in self.last_dups.values() for fp in group}
        else:
            # NB-46: fall back to persisted self.duplicates.
            # After app restart last_dups is None (runtime cache),
            # but self.duplicates is loaded from JSON -- otherwise
            # the gallery showed no dup-marks until user manually
            # opened Duplicates view. dups is only used for the
            # warning marker, so low risk.
            _persisted = self.duplicates.get(
                self.current_cal_name, set())
            dups = set(_persisted) if _persisted else set()
        grouped = {}
        title = ""
        if mode == "days":
            grouped = {key: cal_data.get(key, [])}
            title = key
        elif mode == "months":
            matching = sorted(
                ((d, files) for d, files in cal_data.items() if d.startswith(key)),
                key=lambda x: x[0])
            for ds, files in matching:
                d = datetime.datetime.strptime(ds, "%Y-%m-%d")
                grouped[f"{d.day} {month_name(d.month)}"] = files
            y, m = map(int, key.split("-"))
            title = f"{month_name(m)} {y}"
        elif mode == "years":
            month_groups = {}
            for ds, files in cal_data.items():
                if ds.startswith(key):
                    month_groups.setdefault(ds[:7], []).extend(files)
            for ms in sorted(month_groups.keys()):
                y, m = map(int, ms.split("-"))
                grouped[f"{month_name(m)} {y}"] = month_groups[ms]
            title = f"{key}"
        elif mode == "events":
            grouped = {key: self.categories.get(key, [])}
            title = key
        total = sum(len(v) for v in grouped.values())
        dup_in = sum(1 for files in grouped.values() for f in files if f in dups)
        title_str = f"{title} — {total}"
        if dup_in:
            title_str += f" ⚠️ {dup_in}"
        self.gallery_view.set_grouped_files(
            title_str, grouped, dups, self.config["cache_dir"])
        self.stack.setCurrentIndex(4)
        self.cal_toolbar_widget.hide()
        self._update_undo_buttons()

    def open_full_image(self, filepath, files):
        # video to system player 
        ext = os.path.splitext(filepath)[1].lower()
        if ext in VIDEO_EXTS:
            QDesktopServices.openUrl(QUrl.fromLocalFile(filepath))
            return
        self.image_viewer.load_image(filepath, files)
        self.stack.setCurrentIndex(5)
        self.image_viewer.setFocus()
        self._update_undo_buttons()

    def _on_viewer_copy(self, filepath):
        """viewer copy also populates internal clipboard."""
        # always stop cut-watch — intent changed to copy
        self._cut_watch_timer.stop()
        if filepath and os.path.exists(filepath):
            self.clipboard_files = [filepath]
            self.clipboard_action = "copy"
            self.btn_paste.setEnabled(True)

    def _on_files_moved_externally(self, filepaths, _attempt=0):
        # (P1): regression of _guard_scan() previously
        # returned early, dropping the signal. But OS ALREADY moved
        # the files — calendar was left with stale paths until manual
        # rescan / Find missing files. This is the exact class of bug
        # NB-08 fixed for _busy. Defer instead of drop.
        # Idempotent: _on_files_moved_externally_impl uses set purge
        # and idempotent clipboard filter.
        # cap retries at 60 (~30 seconds). If scan never finishes
        # or _busy never clears, log once and give up instead of
        # spamming forever.
        _CAP = 60
        if getattr(self, "_scan_in_progress", False):
            if _attempt >= _CAP:
                logger.warning(
                    "drag move: scan never finished, giving up")
                return
            logger.info(
                "drag move: scan in progress, deferring 500ms")
            QTimer.singleShot(
                500, lambda: self._on_files_moved_externally(
                    filepaths, _attempt + 1))
            return
        """drag-Move from gallery to Explorer purged source files.

        (NB-08): replaced @_busy_guard with defer-on-busy. The OS has
        already moved the files; silently dropping the signal under
        _busy=True left stale calendar entries until manual rescan /
        Find missing files. Deferred retry is idempotent (purge uses set,
        clipboard filter is idempotent).
        """
        if getattr(self, "_busy", False):
            if _attempt >= _CAP:
                logger.warning(
                    "drag move: busy never cleared, giving up")
                return
            logger.info("drag move: busy, deferring 500ms")
            QTimer.singleShot(
                500, lambda: self._on_files_moved_externally(
                    filepaths, _attempt + 1))
            return
        if not filepaths:
            return
        self._busy = True
        try:
            self._on_files_moved_externally_impl(filepaths)
        finally:
            self._busy = False

    def _on_files_moved_externally_impl(self, filepaths):
        # stop cut-watch and drop moved paths from internal clipboard
        self._cut_watch_timer.stop()
        self.clipboard_files = [f for f in self.clipboard_files
                                if f not in filepaths]
        if not self.clipboard_files:
            self.clipboard_action = None
            self.btn_paste.setEnabled(False)
        elif self.clipboard_action == "move":
            # some files still in clipboard — resume external-move watch
            self._cut_watch_timer.start()
        # (P1 data-loss-adjacent): parent-dir accessibility
        # check. If external drive disconnected after drag-move,
        # os.path.exists() returns False for ALL files -> purge
        # wipes calendar entries though files remain on the
        # (offline) drive. Same class as G32/G33. Undo
        # snapshot is not created here, so Ctrl+Z cannot recover.
        missing = []
        for f in filepaths:
            if os.path.exists(f):
                continue
            _p = os.path.dirname(f) or "."
            try:
                _p_ok = (os.path.isdir(_p)
                         and os.access(_p, os.R_OK))
            except OSError:
                _p_ok = False
            if _p_ok:
                missing.append(f)
        if not missing:
            return
        logger.info(
            f"drag move: {len(missing)} file(s) moved externally")
        # capture remaining dups from the OLD last_dups BEFORE clearing
        remaining_dups = self._get_remaining_dups()
        removed = self._purge_specific_paths(missing)
        # use None (not {}) when empty — change_view_mode(4) relies on
        # `last_dups is not None` to know a scan was already done
        self.last_dups = remaining_dups if remaining_dups else None
        self._sync_duplicates_from_last_dups()  # NB-70
        self.last_dups_groups = []
        self._dups_stale = False  # groups already correct (hashes unchanged)
        if removed:
            self.save_json()
        # rebuild whichever view is active
        if self.stack.currentIndex() == 4:
            in_dups = (self.gallery_view.is_dups_mode
                       or self.current_gallery_context == "ALL_DUPLICATES")
            if in_dups:
                if remaining_dups:
                    self.open_duplicates_gallery()
                else:
                    self.current_gallery_context = None
                    self.show_calendar_from_gallery()
            elif self.current_gallery_context:
                mode = ["days", "months", "years", "events", "duplicates"][
                    self.combo_view_mode.currentIndex()]
                if mode != "duplicates":
                    scroll = self.gallery_view.list_widget.verticalScrollBar().value()
                    self.open_gallery(self.current_gallery_context, mode)
                    QTimer.singleShot(50, lambda: self.gallery_view.list_widget
                                      .verticalScrollBar().setValue(scroll))
        self.statusBar().showMessage(
            t("status_move_done", n=len(missing)), 5000)

    # ---- delete / copy / cut / paste

    def _delete_default_button(self, count):
        """default button for delete-confirm dialogs.

        Safe delete mode ON (default):
            count <= threshold → Yes  (batch, undoable in one step)
            count >  threshold → No   (Ctrl+A / shift-range guard)
        Safe delete mode OFF (user opted out):
            always Yes  (pre-behavior, user accepts the risk)

        Threshold configurable via Settings ('safe_delete_threshold',
        default 50). Corrupted config values fall back to safe defaults;
        this method never raises.
        """
        _sd = self.config.get("safe_delete_enabled", True)
        if _sd is False:  # only explicit Python False disables
            return QMessageBox.StandardButton.Yes
        try:
            _th = int(self.config.get("safe_delete_threshold", 50))
        except (TypeError, ValueError):
            _th = 50
        _th = max(1, min(10000, _th))
        return (QMessageBox.StandardButton.Yes if count <= _th
                else QMessageBox.StandardButton.No)

    @_busy_guard  # busy delete
    def delete_selected_files(self):
        if self._guard_scan():  # 
            return
        logger.info(
            f"delete_selected_files: entering "
            f"(stack={self.stack.currentIndex()}, busy={self._busy})")
        if self.stack.currentIndex() != 4:
            logger.warning(
                f"delete_selected_files: wrong stack "
                f"({self.stack.currentIndex()}), aborting")
            return
        items = self.gallery_view.get_selected_items()
        logger.info(f"delete_selected_files: {len(items)} item(s) selected")
        if not items:
            self.statusBar().showMessage(t("status_no_selection"), 3000)
            return
        # safe-delete-mode aware default. Previously hard-coded
        # Yes-for-1/No-for-N. Threshold now configurable in Settings
        # (default: Yes up to 50, No above — Ctrl+A guard). User can
        # disable safe mode entirely (checkbox off) to restore pre-safe-mode behavior.
        _default_btn = self._delete_default_button(len(items))
        reply = QMessageBox.question(
            self, t("confirm_delete"), t("confirm_delete_files", n=len(items)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            defaultButton=_default_btn)
        if reply != QMessageBox.StandardButton.Yes:
            logger.info(f"delete_selected_files cancelled (reply={reply!r})")
            self.statusBar().showMessage(t("status_ready"), 1500)
            return
        logger.info(f"delete_selected_files: proceeding with {len(items)} item(s)")
        n = len(items)
        if n > 1:
            self.progress_bar.setRange(0, n)
            self.progress_bar.setFormat("%v / %m")
            self.progress_bar.show()
            _process_ui()  # 
        scroll = self.gallery_view.list_widget.verticalScrollBar().value()
        dup_scroll = self.gallery_view.dup_gallery.verticalScrollBar().value() if hasattr(self.gallery_view, "dup_gallery") else 0
        self.save_state_for_undo()
        trashed = []
        failed = 0
        successful = []
        for i, item in enumerate(items):
            fp = item.data(ROLE_FILEPATH)
            # skip list in dup — list_widget is empty in dup view
            if not self.gallery_view.is_dups_mode:
                self.gallery_view.list_widget.remove_item_by_filepath(fp)
            success, tp, err = move_to_trash(fp)
            if success:
                if tp:
                    trashed.append((tp, fp))
                successful.append(fp)
            else:
                failed += 1
            if n > 1 and i % 3 == 0:
                self.progress_bar.setValue(i + 1)
                _process_ui()  # 
        # bulk purge delete 
        self._purge_paths_bulk(successful)
        if n > 1:
            self.progress_bar.hide()
        if self.undo_actions:
            self.undo_actions[-1] = {"trash": trashed}
        if failed:
            QMessageBox.warning(self, t("error_title"), t("error_partial_delete", n=failed))
        logger.info(f"delete_selected_files done: ok={len(successful)}, failed={failed}")
        self.save_json()
        if self.current_gallery_context == "ALL_DUPLICATES":
            remaining = self._get_remaining_dups()
            # NB-59: sync last_dups with post-delete state so
            # open_duplicates_gallery uses fresh data. Mirror
            # on_gallery_delete_dups, which sets this explicitly.
            # None (not {}) when empty -- change_view_mode(4) relies
            # on `last_dups is not None` to know a scan ran.
            self.last_dups = remaining or None
            self._sync_duplicates_from_last_dups()  # NB-70
            if remaining:
                self.open_duplicates_gallery()
                QTimer.singleShot(50, lambda: self.gallery_view.dup_gallery.verticalScrollBar().setValue(dup_scroll))
            else:
                # clear groups on empty 
                self.last_dups = None
                self.last_dups_groups = []
                QMessageBox.information(self, t("info_done"), t("info_no_more_dups"))
                self.stack.setCurrentIndex(0)
                self.combo_view_mode.blockSignals(True)
                self.combo_view_mode.setCurrentIndex(0)
                self.combo_view_mode.blockSignals(False)
                self.change_view_mode(0)
        else:
            # invalidate last_dups non-dup 
            self.last_dups = None
            self.last_dups_groups = []
            if self.stack.currentIndex() == 4 and self.current_gallery_context:
                mode = ["days", "months", "years", "events", "duplicates"][self.combo_view_mode.currentIndex()]
                self.open_gallery(self.current_gallery_context, mode)
                QTimer.singleShot(50, lambda: self.gallery_view.list_widget.verticalScrollBar().setValue(scroll))
            else:
                self.change_view_mode(self.combo_view_mode.currentIndex())

    def copy_selected_files(self):
        if self.stack.currentIndex() != 4:
            return
        items = self.gallery_view.get_selected_items()
        if not items:
            logger.info("copy_selected_files: no selection")
            self.statusBar().showMessage(t("status_no_selection"), 2000)
            return
        files = [it.data(ROLE_FILEPATH) for it in items
                 if it.data(ROLE_FILEPATH) and os.path.exists(it.data(ROLE_FILEPATH))]
        if not files:
            logger.info("copy_selected_files: selection has no existing files")
            self.statusBar().showMessage(t("status_no_selection"), 2000)
            return
        self.clipboard_files = files
        self.clipboard_action = "copy"
        self.btn_paste.setEnabled(True)
        # clear streak — previous move-state entries never match
        # this copy set and would accumulate forever (memory leak).
        self._cut_missing_streak.clear()
        # put into SYSTEM clipboard so Explorer can paste
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(fp) for fp in self.clipboard_files])
        QApplication.clipboard().setMimeData(mime)
        logger.info(f"copy_selected_files: {len(self.clipboard_files)} file(s)")
        self._cut_watch_timer.stop()  # not a move, nothing to watch
        self.statusBar().showMessage(t("status_copied", n=len(self.clipboard_files)), 3000)

    def cut_selected_files(self):
        # (U1d): text-forward + modal guard.
        if _dispatch_forward_text("cut"):
            return
        _mw = QApplication.activeModalWidget()
        if _mw is not None and _mw is not self:
            return
        if self.stack.currentIndex() != 4:
            return
        items = self.gallery_view.get_selected_items()
        if not items:
            logger.info("cut_selected_files: no selection")
            self.statusBar().showMessage(t("status_no_selection"), 2000)
            return
        files = [it.data(ROLE_FILEPATH) for it in items
                 if it.data(ROLE_FILEPATH) and os.path.exists(it.data(ROLE_FILEPATH))]
        if not files:
            logger.info("cut_selected_files: selection has no existing files")
            self.statusBar().showMessage(t("status_no_selection"), 2000)
            return
        self.clipboard_files = files
        self.clipboard_action = "move"
        self.btn_paste.setEnabled(True)
        # clear old streak (fresh cut, fresh detection)
        self._cut_missing_streak.clear()
        # put into SYSTEM clipboard with Move intent (Windows Explorer "Cut")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(fp) for fp in self.clipboard_files])
        try:
            # Preferred DropEffect: DROPEFFECT_MOVE = 2 (little-endian DWORD)
            mime.setData("Preferred DropEffect", b"\x02\x00\x00\x00")
        except Exception:
            pass
        QApplication.clipboard().setMimeData(mime)
        logger.info(f"cut_selected_files: {len(self.clipboard_files)} file(s)")
        # start watching for external move (Explorer Ctrl+V elsewhere)
        self._cut_watch_timer.start()
        self.statusBar().showMessage(t("status_cut", n=len(self.clipboard_files)), 3000)

    @_busy_guard_text_aware  # busy paste — text-forward + file paste
    def paste_files(self):
        # if text input focused — forward to native paste
        fw = QApplication.focusWidget()
        if fw is not None and hasattr(fw, "canPaste") and hasattr(fw, "paste"):
            try:
                if fw.canPaste():
                    fw.paste()
                    return
            except Exception:
                pass
        if self._guard_scan():  # 
            return
        # require gallery 
        if self.stack.currentIndex() != 4:
            self.statusBar().showMessage(t("status_paste_gallery_only"), 2000)
            return
        # forbid paste in dups-view (no meaningful target)
        if self.current_gallery_context == "ALL_DUPLICATES":
            self.statusBar().showMessage(t("status_paste_dups_forbidden"), 2000)
            return
        if not self.clipboard_files or not self.current_gallery_context:
            return
        # defensive — clip board state must be valid
        if self.clipboard_action not in ("copy", "move"):
            logger.warning(
                f"paste_files: unknown clipboard_action "
                f"{self.clipboard_action!r}, aborting")
            return
        # forbid paste in months/years — no single target folder
        # (current_gallery_context = "2024-03" or "2024" is not a date key).
        _paste_mode = ["days", "months", "years", "events", "duplicates"][
            self.combo_view_mode.currentIndex()]
        if _paste_mode in ("months", "years"):
            self.statusBar().showMessage(t("status_paste_gallery_only"), 2000)
            return
        # internal paste — stop external-move watcher
        self._cut_watch_timer.stop()
        # determine target dir first — avoid phantom undo step
        cal_data = self.calendars[self.current_cal_name]
        target_dir = ""
        mode_for_paste = ["days", "months", "years", "events", "duplicates"][
            self.combo_view_mode.currentIndex()]
        if mode_for_paste == "events":
            # event context is event name, not date
            ev_files = self.categories.get(self.current_gallery_context, [])
            if not ev_files:
                self.statusBar().showMessage(t("status_paste_empty_event"), 3000)
                return
            target_dir = os.path.dirname(ev_files[0])
        elif self.current_gallery_context in cal_data and cal_data[self.current_gallery_context]:
            target_dir = os.path.dirname(cal_data[self.current_gallery_context][0])
        elif self.current_gallery_context == "ALL_DUPLICATES" or not target_dir:
            target_dir = os.path.dirname(self.clipboard_files[0]) if self.clipboard_files else ""
        if not target_dir:
            return
        # pre-scan paste — skip if nothing to do (no phantom undo step)
        has_work = False
        for _fp in self.clipboard_files:
            if not os.path.exists(_fp):
                continue
            _dest = os.path.join(target_dir, os.path.basename(_fp))
            if os.path.abspath(_dest) != os.path.abspath(_fp):
                has_work = True
                break
        if not has_work:
            # no-op paste feedback 
            self.statusBar().showMessage(t("status_nothing_to_paste"), 2000)
            if self.clipboard_files:
                self.btn_paste.setEnabled(False)
                self.clipboard_files = []
            return
        n = len(self.clipboard_files)
        if n > 1:
            self.progress_bar.setRange(0, n)
            self.progress_bar.setFormat("%v / %m")
            self.progress_bar.show()
            _process_ui()  # 
        self.save_state_for_undo()
        success_count = 0
        moved = []       # src->dest for move
        created = []     # dest paths for copy (for undo)
        remaining = []   # files user skipped or that failed
        overwritten = [] # (trash_path, orig_path) of replaced dest
        for idx, fp in enumerate(self.clipboard_files):
            if n > 1 and idx % 3 == 0:
                self.progress_bar.setValue(idx + 1)
                _process_ui()  # 
            if not os.path.exists(fp):
                continue  # gone, drop silently
            dest = os.path.join(target_dir, os.path.basename(fp))
            # samefile catches hardlinks + case-insensitive FS
            _is_same = False
            try:
                if os.path.exists(dest):
                    _is_same = os.path.samefile(dest, fp)
                else:
                    _is_same = os.path.abspath(dest) == os.path.abspath(fp)
            except (OSError, ValueError):
                _is_same = os.path.abspath(dest) == os.path.abspath(fp)
            if _is_same:
                success_count += 1
                continue
            if os.path.exists(dest):
                reply = QMessageBox.question(
                    self, t("confirm_overwrite"),
                    t("confirm_overwrite_body", name=os.path.basename(dest)),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    remaining.append(fp)
                    continue
                # preserve overwritten file for undo BEFORE copy/move
                # destroys it. If trash fails (locked, permission), skip the
                # whole paste for this file — otherwise copy2/move would
                # silently overwrite dest and lose original content forever.
                ok_trash, tp, _ = move_to_trash(dest)
                if not ok_trash:
                    logger.warning(
                        f"paste: could not trash {dest}, skipping to "
                        f"avoid overwriting without backup")
                    remaining.append(fp)
                    continue
                # tp can be None if file already gone — no undo needed
                if tp:
                    overwritten.append((tp, dest))
            _dest_existed_before = os.path.exists(dest)
            try:
                if self.clipboard_action == "copy":
                    shutil.copy2(fp, dest)
                    created.append(dest)  # track for undo
                elif self.clipboard_action == "move":
                    shutil.move(fp, dest)
                    # (P0): record BEFORE purge. If purge
                    # raises, undo still has the pair -- otherwise
                    # cleanup in except deletes the only copy.
                    moved.append((fp, dest))
                    self._purge_path_everywhere(fp)
                date_obj = get_file_date(dest, os.path.basename(target_dir), self.priority)
                if date_obj is None:
                    date_obj = datetime.date.today()
                dest_date = date_obj.strftime("%Y-%m-%d")
                # dedupe path across OTHER dates (preserve categories)
                for _d in list(cal_data.keys()):
                    if _d != dest_date and dest in cal_data[_d]:
                        cal_data[_d].remove(dest)
                        if not cal_data[_d]:
                            del cal_data[_d]
                cal_data.setdefault(dest_date, [])
                if dest not in cal_data[dest_date]:
                    cal_data[dest_date].append(dest)
                # (P2): events mode — the file was copied/moved
                # INTO the event folder, but self.categories was not
                # updated. open_gallery("events") rebuilds from
                # categories, so the user saw "nothing happened".
                # For copy: add dest to event. For move:
                # _purge_path_everywhere above already removed the
                # OLD path; add dest to the SAME event so membership
                # survives the move-in-place (A6 still applies to
                # cross-calendar moves).
                if mode_for_paste == "events":
                    _ev = self.current_gallery_context
                    if _ev and _ev != "ALL_DUPLICATES":
                        _evlist = self.categories.setdefault(_ev, [])
                        if dest not in _evlist:
                            _evlist.append(dest)
                success_count += 1
            except Exception as e:
                QMessageBox.warning(self, t("error_title"),
                                    t("error_paste", path=fp, err=str(e)))
                # cleanup partial dest if we created it (and
                # it didn't exist before). shutil.copy2 leaves a
                # truncated file on disk if I/O fails mid-write —
                # orphan garbage that undo cannot restore (no
                # entry in "created"), and blocks undo of overwrite
                # (destination occupied).
                # (P0): only clean for copy. For move, dest
                # is the ONLY copy (fp is gone) -- removing it
                # destroys the file. Skip, leave moved file.
                if (self.clipboard_action == "copy"
                        and not _dest_existed_before
                        and os.path.exists(dest)):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                # NB-52: for move, dest is the only copy on disk.
                # Main-body exception fired after shutil.move but
                # before calendar update -> phantom calendar (fp in
                # calendar but file no longer there, dest orphaned).
                # Purge fp and add dest with fallback today-date so
                # calendar matches disk. Best-effort: if this also
                # raises, user will pick it up on next rescan.
                # NB-90: only fixup when move ACTUALLY removed src.
                # shutil.move on cross-FS may fail mid-copy — dest is
                # created, src still present. Previously we purged
                # valid src from calendar and installed a possibly-
                # corrupt dest (data-loss-adjacent).
                if (self.clipboard_action == "move"
                        and os.path.exists(dest)
                        and not os.path.exists(fp)):
                    try:
                        for _dd in list(cal_data.keys()):
                            if fp in cal_data[_dd]:
                                cal_data[_dd].remove(fp)
                                if not cal_data[_dd]:
                                    del cal_data[_dd]
                        _today = datetime.date.today().strftime("%Y-%m-%d")
                        for _dd in list(cal_data.keys()):
                            if _dd != _today and dest in cal_data[_dd]:
                                cal_data[_dd].remove(dest)
                                if not cal_data[_dd]:
                                    del cal_data[_dd]
                        cal_data.setdefault(_today, [])
                        if dest not in cal_data[_today]:
                            cal_data[_today].append(dest)
                    except Exception as _e2:
                        logger.warning(
                            f"paste: post-move calendar fixup failed: {_e2}")
                # NB-72: for move-action, if shutil.move already
                # succeeded, fp no longer exists on disk. Re-adding it
                # to `remaining` yields a dead path in the clipboard
                # and a misleading "kept in clipboard: M" message.
                if self.clipboard_action == "copy" or not os.path.exists(dest):
                    remaining.append(fp)  # keep for retry
        # record action for undo
        _action = {}
        if moved:
            _action["moved"] = moved
        if created:
            _action["created"] = created
        if overwritten:
            _action["overwritten"] = overwritten
        if _action and self.undo_actions:
            self.undo_actions[-1] = _action
        self.last_dups = None
        self.last_dups_groups = []
        # NB-71: progress bar was shown above for n > 1; hide here.
        if n > 1:
            self.progress_bar.hide()
        self.save_json()
        # NB-22: skip change_view_mode when opening a gallery directly —
        # it caused a brief calendar->gallery flash. open_gallery rebuilds
        # the target view itself.
        mode = ["days", "months", "years", "events", "duplicates"][self.combo_view_mode.currentIndex()]
        if self.current_gallery_context and mode != "duplicates":
            self.open_gallery(self.current_gallery_context, mode)
        else:
            self.change_view_mode(self.combo_view_mode.currentIndex())
        # retain failed/skipped in clipboard
        self.clipboard_files = remaining
        self.btn_paste.setEnabled(bool(remaining))
        # if cut and some files remain unprocessed, resume watching
        # for external moves so user can still Ctrl+V them in Explorer.
        if remaining and self.clipboard_action == "move":
            self._cut_watch_timer.start()
        if remaining:
            self.statusBar().showMessage(
                t("status_paste_partial", n=success_count, left=len(remaining)), 4000)

    # ---- persistence
    def save_json(self):
        """Debounced: restart 500ms timer. Real write happens in _do_save_json."""
        self._save_timer.start()

    def _ask_backup_fail_save_anyway(self):
        """(NB-36): confirm destructive save without backup.

        Returns True if user wants to save anyway (accepting that the
        previous on-disk state will be overwritten without a backup
        copy). Returns False to cancel this save attempt -- data in
        memory stays, on-disk state preserved, user can free disk
        space / fix permissions and retry.
        """
        # (C7): if QMessageBox itself raises (Qt state
        # corruption, closing window), do NOT proceed with save.
        # Outer except in _do_save_json previously swallowed the
        # exception and the save continued without any prompt.
        try:
            reply = QMessageBox.question(
                self, t("backup_fail_title"), t("backup_fail_body"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                defaultButton=QMessageBox.StandardButton.No)
        except Exception:
            logger.exception(
                "backup-fail dialog failed, cancelling save")
            return False
        return reply == QMessageBox.StandardButton.Yes

    def _do_save_json(self, force=False, skip_backup_dialog=None):
        """returns True=ok, False=failed-or-cancelled, None=deferred.

        force=True bypasses the busy check (used by closeEvent and
        restore_from_backup -- operations that must save synchronously).
        skip_backup_dialog=True bypasses the NB-36 backup-fail dialog.
        (C5): force and skip_backup_dialog are now separate.
        Previously force=True silently skipped the backup-fail dialog
        even in closeEvent -- user pressed "No" twice and still got
        a destructive save without backup. closeEvent now calls with
        skip_backup_dialog=False so the dialog fires when needed.
        Default behaviour when skip_backup_dialog is None:
        fall back to force (backward compatible for other callers).
        """
        if skip_backup_dialog is None:
            skip_backup_dialog = force
        if not force and getattr(self, "_busy", False):
            # don't save mid-file-operation; final save at end of op
            self._save_timer.start()
            return None
        try:
            new_state = {
                "calendars": self.calendars,
                "duplicates": {k: list(v) for k, v in self.duplicates.items()},
                "categories": self.categories,
                "priority": self.priority,
                "current_cal": self.current_cal_name,
            }
            new_count = _count_files_in_state(new_state)
            # backup before destructive save, but NEVER block the save.
            # None from _backup_calendar_data = no source file (OK).
            # False = actual backup failure — warn, then save anyway.
            _backup_failed = False
            _new_cal = -1  # split counters, init before try
            _new_cat = -1
            try:
                old_count = getattr(self, "_last_saved_count", -1)
                if old_count < 0:
                    old_count = _count_files_in_json(self.json_path)
                # if old JSON exists but is unreadable
                # (_count_files_in_json returned -1), back it up
                # BEFORE overwriting — user may recover parts of it
                # manually even though _atomic_write_json will
                # replace it wholesale.
                if (old_count < 0
                        and os.path.exists(self.json_path)):
                    # (P1 data-loss-adjacent): _backup_calendar_data
                    # catches its own exceptions and returns False on
                    # failure -- the outer try/except never fired, and
                    # the return value was ignored. Result: corrupt
                    # (but potentially recoverable) old JSON silently
                    # overwritten by _atomic_write_json with no
                    # backup. Now check return value and flag
                    # _backup_failed so at least the user is warned
                    # (status bar) before data is gone.
                    _cb = _backup_calendar_data(
                        self.json_path,
                        reason="old file unreadable, backing up")
                    if _cb is False:
                        _backup_failed = True
                        logger.error(
                            "corrupt-old-JSON backup failed, "
                            "saving anyway to avoid total loss")
                        # NB-93: same NB-36 dialog as normal backup
                        # failure. Previously only logged — user could
                        # not intervene before destructive overwrite of
                        # a corrupt-but-possibly-recoverable old JSON.
                        if not skip_backup_dialog:
                            if not self._ask_backup_fail_save_anyway():
                                logger.warning(
                                    "User cancelled save "
                                    "(corrupt old file, backup failed)")
                                self.statusBar().showMessage(
                                    t("backup_fail_cancelled"), 5000)
                                return False
                            logger.warning(
                                "User accepted save without backup "
                                "(corrupt old file)")
                # split counters so calendar-only or category-only
                # drops >5% fire backup even when total drop <5% (
                # regression: merging both into one count diluted each
                # individual signal). Scenario: 100 cal + 100 cat;
                # deleting 6 cal files (6% cal drop) is only 3% total
                # drop -> did not fire backup.
                _new_cal, _new_cat = _count_files_in_state_split(new_state)
                _old_cal = getattr(self, "_last_saved_cal_count", -1)
                _old_cat = getattr(self, "_last_saved_cat_count", -1)
                if _old_cal < 0 or _old_cat < 0:
                    _oc, _ot = _count_files_in_json_split(self.json_path)
                    if _oc >= 0:
                        _old_cal = _oc
                    if _ot >= 0:
                        _old_cat = _ot
                _needs_backup = False
                # NB-110: require old_count >= _MIN_BACKUP_THRESHOLD.
                # Previously 5 -> 4 (20% drop, 1 file) fired a backup,
                # spamming BACKUP_KEEP slots on tiny libraries.
                if (old_count >= _MIN_BACKUP_THRESHOLD
                        and new_count >= 0
                        and (new_count == 0
                             or new_count < old_count * 0.95)):
                    _needs_backup = True
                if (_old_cal >= _MIN_BACKUP_THRESHOLD
                        and _new_cal >= 0
                        and (_new_cal == 0
                             or _new_cal < _old_cal * 0.95)):
                    _needs_backup = True
                if (_old_cat >= _MIN_BACKUP_THRESHOLD
                        and _new_cat >= 0
                        and (_new_cat == 0
                             or _new_cat < _old_cat * 0.95)):
                    _needs_backup = True
                if _needs_backup:
                    backup_result = _backup_calendar_data(
                        self.json_path,
                        reason=f"counts {old_count}->{new_count} "
                               f"cal {_old_cal}->{_new_cal} "
                               f"cat {_old_cat}->{_new_cat}")
                    if backup_result is False:
                        _backup_failed = True
                        logger.error("Backup failed")
                        # NB-36: destructive save + backup
                        # failed = old state overwritten without any
                        # backup. Ask user before proceeding. Skip the
                        # dialog when force=True -- closeEvent /
                        # restore_from_backup already committed via
                        # their own confirm dialogs.
                        if not skip_backup_dialog:
                            if not self._ask_backup_fail_save_anyway():
                                logger.warning(
                                    "User cancelled save "
                                    "(backup failed)")
                                self.statusBar().showMessage(
                                    t("backup_fail_cancelled"), 5000)
                                return False
                            logger.warning(
                                "User accepted save without backup")
            except Exception as e:
                logger.debug(f"backup check skipped: {e}")
            try:
                _atomic_write_json(self.json_path, new_state)
                # (P1): do NOT update _last_saved_count when
                # backup failed. Otherwise next save sees
                # new_count == old_count and skips threshold check
                # -- backup is never retried. Extends NB-36.
                if not _backup_failed:
                    self._last_saved_count = new_count
                    # also update split counters on success so
                    # next save compares against fresh baselines.
                    if _new_cal >= 0:
                        self._last_saved_cal_count = _new_cal
                    if _new_cat >= 0:
                        self._last_saved_cat_count = _new_cat
                else:
                    self.statusBar().showMessage(
                        t("warn_backup_failed"), 5000)
                return True
            except Exception as e:
                logger.error(f"save_json write failed: {e}")
                try:
                    _msg = t("error_save_failed")
                    if _backup_failed:
                        _msg += " / " + t("warn_backup_failed")
                    self.statusBar().showMessage(_msg, 5000)
                except Exception:
                    pass
                return False
        except Exception:
            logger.exception("save_json failed")
            return False

    def load_last_session(self):
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                # (P1): atomic pre-validate. Previously self.calendars
                # was assigned FIRST, then duplicates comprehension could
                # raise (e.g. {"Default": null} -> set(None) TypeError),
                # and categories would be left at __init__ default {}. The
                # next save_json() would then write empty categories to
                # disk — losing all event assignments. Mirror (BUG-A)
                # pattern: validate everything, then commit.
                _raw_cals = saved.get("calendars", {"Default": {}})
                if not isinstance(_raw_cals, dict):
                    _raw_cals = {"Default": {}}
                _raw_dups = saved.get("duplicates", {})
                _dups_ok = {}
                if isinstance(_raw_dups, dict):
                    for _dk, _dv in _raw_dups.items():
                        try:
                            _dups_ok[_dk] = set(_dv) if _dv is not None else set()
                        except (TypeError, ValueError):
                            logger.warning(
                                f"load: bad duplicates value for {_dk!r} "
                                f"({type(_dv).__name__}), using empty set")
                            _dups_ok[_dk] = set()
                else:
                    logger.warning(
                        f"load: duplicates is "
                        f"{type(_raw_dups).__name__}, resetting to empty")
                _raw_cats = saved.get("categories", {})
                if not isinstance(_raw_cats, dict):
                    logger.warning(
                        f"load: categories is "
                        f"{type(_raw_cats).__name__}, resetting to empty")
                    _raw_cats = {}
                _raw_prio = saved.get("priority", DEFAULT_PRIORITY)
                _raw_cur = saved.get("current_cal", "")
                # All validated — commit.
                self.calendars = _raw_cals
                self.duplicates = _dups_ok
                self.categories = _raw_cats
                self.priority = _raw_prio
                self.current_cal_name = _raw_cur
            except Exception as e:
                logger.warning(f"load_last_session failed: {e}")
        # validate types BEFORE _sanitize_calendars() — a corrupted
        # JSON with "calendars": [...] or "categories": null would crash
        # at .keys() call outside try/except in _sanitize.
        if not isinstance(self.calendars, dict):
            logger.warning(
                f"load: calendars is {type(self.calendars).__name__}, "
                f"resetting to empty")
            self.calendars = {"Default": {}}
        if not isinstance(self.duplicates, dict):
            logger.warning(
                f"load: duplicates is {type(self.duplicates).__name__}, "
                f"resetting to empty")
            self.duplicates = {}
        if not isinstance(self.categories, dict):
            logger.warning(
                f"load: categories is {type(self.categories).__name__}, "
                f"resetting to empty")
            self.categories = {}
        # (NB-28): sanitize priority ELEMENTS, not just list-ness.
        # A corrupted JSON like "priority": [1, 2, 3] previously passed
        # isinstance(list) and yielded 0 files on next scan.
        self.priority = _sanitize_priority_list(self.priority)
        # (P0): PRIO_CTIME is unsupported on Linux (st_ctime = metadata change,
        # not creation). Filter it out and notify user if it was present.
        if platform.system() == "Linux":
            _had_ctime = PRIO_CTIME in self.priority
            self.priority = [p for p in self.priority if p != PRIO_CTIME]
            # (U4a): if priority was ["ctime"] only, the
            # filter leaves an empty list -> get_file_date returns
            # None for every file -> "0 files" scan. Fall back.
            if not self.priority:
                self.priority = [p for p in DEFAULT_PRIORITY
                                 if p != PRIO_CTIME]
            if _had_ctime:
                QTimer.singleShot(100, lambda: self.statusBar().showMessage(
                    "File creation date (PRIO_CTIME) is unavailable on Linux", 5000))
        if not self.calendars:
            self.calendars = {"Default": {}}
        if (not self.current_cal_name
                or not isinstance(self.current_cal_name, str)
                or self.current_cal_name not in self.calendars):
            self.current_cal_name = next(iter(self.calendars))
        removed = self._sanitize_calendars()
        if removed:
            self.save_json()
            QTimer.singleShot(100, lambda: msg_warning(
                self, t("data_cleanup_title"),
                t("data_cleanup_body", n=removed)))
        # re-validate current_cal_name AFTER sanitize. 
        # dropped non-str calendar names but the pre-sanitize fallback
        # could pick one (next(iter(calendars)) returns int key if
        # present), and the old `not in calendars: calendars[...] = {}
        # ` block would re-add it, crashing update_combo -> addItems.
        # Scenario unreachable via plain JSON (keys always str) but
        # the fix closes the loop.
        if (not isinstance(self.current_cal_name, str)
                or self.current_cal_name not in self.calendars):
            if self.calendars:
                self.current_cal_name = next(iter(self.calendars))
            else:
                self.calendars = {"Default": {}}
                self.current_cal_name = "Default"
        if self.current_cal_name not in self.calendars:
            self.calendars[self.current_cal_name] = {}
        if self.current_cal_name not in self.duplicates:
            self.duplicates[self.current_cal_name] = set()
        # restore language file if configured
        lang_file = self.config.get("language_file")
        # (P2): validate type -- corrupted config with
        # "language_file": 123 would crash os.path.exists(123)
        # at startup (outside try block).
        if not isinstance(lang_file, str):
            lang_file = None
        if lang_file and os.path.exists(lang_file):
            try:
                tr.load_from_file(lang_file)
                self.settings_view.update_language_label()
            except Exception:
                pass
        self.update_combo()
        # (P1): sync Settings priority UI with loaded priority.
        # SettingsWidget created in setup_ui with default priority;
        # load_last_session changes self.priority but not the UI list.
        # Without sync, first user action on the list fires
        # save_priorities() with DEFAULT order -- overwrites custom
        # priority silently. User prefs lost.
        try:
            _lw = self.settings_view.list_widget
            _lw.blockSignals(True)
            try:
                _lw.clear()
                for _p in self.priority:
                    self.settings_view._add_priority_item(_p)
            finally:
                # NB-89: blockSignals(False) MUST run even if a
                # priority item raises. Otherwise rowsMoved ->
                # save_priorities stays disconnected forever, and
                # drag-n-drop in priority list silently no-ops.
                _lw.blockSignals(False)
        except Exception as _e:
            logger.warning(
                f"load_last_session: settings sync failed: {_e}")
        # read from disk (-1 if no file). Previously summed in-memory
        # count, which made first save think the file existed with 0 files.
        # initialize split counters too — first save compares
        # against on-disk baselines for both calendars and categories.
        _init_cal, _init_cat = _count_files_in_json_split(self.json_path)
        self._last_saved_cal_count = _init_cal if _init_cal >= 0 else 0
        self._last_saved_cat_count = _init_cat if _init_cat >= 0 else 0
        self._last_saved_count = _count_files_in_json(self.json_path)
        # NB-49: apply saved view mode (already set in setup_ui,
        # but this call also triggers the actual view render).
        self.change_view_mode(self.combo_view_mode.currentIndex())

    def closeEvent(self, event):
        # busy close guard — don't destroy window during file operation
        if getattr(self, "_busy", False):
            try:
                self.statusBar().showMessage(t("status_busy"), 3000)
            except Exception:
                pass
            event.ignore()
            return
        # stop cut-watch BEFORE any dialog — QMessageBox.exec spins
        # a nested event loop, letting the timer fire mid-close (purge +
        # rebuild racing with the final save).
        try:
            self._cut_watch_timer.stop()
        except Exception:
            pass
        # flush debounced save 
        self._save_timer.stop()
        # NB-135: no backup-fail dialog here; the final force=True save
        # below is the single source of truth for NB-36.
        save_ok = self._do_save_json(skip_backup_dialog=True) is not False
        # if save failed, warn user before close
        if save_ok:
            reply = QMessageBox.question(
                self, t("title_exit"), t("confirm_exit"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        else:
            reply = QMessageBox.question(
                self, t("title_exit"),
                t("error_save_failed") + "\n\n" + t("confirm_exit_anyway"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            # config save only when actually closing
            self.save_config()
            # short timeouts 
            self.statusBar().showMessage(t("status_closing"), 5000)
            # bounded waits — never freeze the window forever
            if self.scan_thread:
                self._scan_cancelled = True
                _th = self.scan_thread
                self.scan_thread = None
                _th.stop()  # waits up to 5s
                if _th.isRunning():
                    logger.warning("closeEvent: scan thread alive, "
                                   "waiting 10s more")
                    if not _th.wait(10000):
                        logger.error("scan thread won't die, terminating")
                        _th.terminate()
                        _th.wait(2000)
            if self.dup_thread:
                _th = self.dup_thread
                self.dup_thread = None
                _th.stop()
                if _th.isRunning():
                    logger.warning("closeEvent: dup thread alive, "
                                   "waiting 10s more")
                    if not _th.wait(10000):
                        logger.error("dup thread won't die, terminating")
                        _th.terminate()
                        _th.wait(2000)
            self._cut_watch_timer.stop()  # stop polling timer
            for pool in (
                self.calendar_view.threadpool,
                self.gallery_view.list_widget.threadpool,
                self.gallery_view.dup_gallery.threadpool,
            ):
                pool.clear()
                pool.waitForDone(1500)
            QThreadPool.globalInstance().clear()
            QThreadPool.globalInstance().waitForDone(2000)
            self._end_scan()  # - close the dialog if still open
            # final save after all threads stopped — scan_finished may
            # have added files during the confirmation dialog's nested event
            # loop, and the debounced save timer was stopped before dialog.
            self._save_timer.stop()
            # check final save. If it fails (disk lost right
            # at close), user must see at least a warning before
            # the window is destroyed.
            # (C5c): force=True bypasses _busy (we already
            # checked it at the top), but skip_backup_dialog=False
            # so NB-36 dialog fires if backup fails again -- user
            # gets a final chance to cancel the destructive save.
            _final_ok = self._do_save_json(
                force=True, skip_backup_dialog=False)
            if _final_ok is False:
                try:
                    logger.error(
                        "closeEvent: final save failed")
                    msg_error(self, t("error_title"),
                              t("error_save_failed"))
                except Exception:
                    pass
                # NB-87: do not force-close on save failure. User must
                # be able to fix disk-full / permissions and retry.
                # NB-133: restore UI state we tore down above so the
                # window is usable again (btn_import was disabled and
                # thread pools already drained).
                try:
                    self.btn_import.setEnabled(True)
                    self.btn_cancel_scan.hide()
                    self.progress_bar.hide()
                    self.progress_bar.setRange(0, 100)
                    self.statusBar().showMessage(
                        t("error_save_failed"), 5000)
                    if (self.clipboard_action == "move"
                            and self.clipboard_files):
                        self._cut_watch_timer.start()
                except Exception:
                    logger.exception("closeEvent: UI restore failed")
                event.ignore()
                return
            event.accept()
        else:
            # (NB-27): resume cut-watch if it was active before close.
            # Otherwise user's "cancel close" silently disables
            # external-move detection for the rest of the session:
            # subsequent Explorer Ctrl+V would leave stale calendar
            # entries until manual rescan / Find missing files.
            if (self.clipboard_action == "move"
                    and self.clipboard_files):
                try:
                    self._cut_watch_timer.start()
                except Exception:
                    pass
            event.ignore()


# ============================================================
#                        ENTRY POINT
# ============================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    # --- Фикс иконки для панели задач Windows ---
    import ctypes
    try:
        myappid = 'TinoMakesDev.MemoryCalendar.1.0' 
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass
    
    # --- Загрузка иконки ---
    from PySide6.QtGui import QIcon
    if hasattr(sys, '_MEIPASS'):
        # Если запущено как .exe
        _icon_path = os.path.join(sys._MEIPASS, "iconDone.ico")
    else:
        # Если запущено как .py скрипт
        _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iconDone.ico")
        
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))
    # -------------------------------------------

    # OS integration metadata
    app.setApplicationName("Memory Calendar")
    app.setApplicationDisplayName("Memory Calendar")
    app.setOrganizationName("MemoryCalendar")
    app.setApplicationVersion("1.0")
    
    if platform.system() == "Darwin":
        tr._current["modifier_key"] = "Cmd"
        tr._current["delete_key"] = "Backspace"
    else:
        tr._current["modifier_key"] = "Ctrl"
        tr._current["delete_key"] = "Del"
        
    try:
        font = QFont.systemFont()
        font.setPointSize(10)
    except Exception:
        font = QFont()
    app.setFont(font)
    
    window = MainWindow()
    window.show()
    sys.exit(app.exec())