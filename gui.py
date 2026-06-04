import logging
import random
import threading
import webbrowser
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyleFactory,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWidgets import QListView

from app_db import AppDB
from workshop_core import (  # noqa: F401
    get_available_tags,
    roll_random_item,
    harvest_ids,
    clear_cache,
    get_cached_count,
    get_item_details,
    load_ignored_ids,
    save_ignored_ids,
)

logger = logging.getLogger("gui")

DEBUG = False


class _SignalProxy(QWidget):
    tags_loaded = Signal(list)
    roll_result = Signal(object)
    status_message = Signal(str)
    app_count_updated = Signal(int)
    show_details = Signal(object)
    harvest_status = Signal(str)


class DetailsDialog(QDialog):
    _offset = 0

    def __init__(self, details: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(details.get("name", "Workshop Item"))
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setStyleSheet(self._dark_style())
        self._build_ui(details)
        self.setFixedSize(self.sizeHint())
        if parent:
            p = parent.mapToGlobal(QPoint(0, 0))
            off = (DetailsDialog._offset % 500)
            self.move(p.x() + 40 + off, p.y() + 40 + off)
            DetailsDialog._offset += 26
        logger.info("Dialog size: %sx%s", self.width(), self.height())

    def _dark_style(self):
        return """
            QDialog {
                background-color: #1e1e1e;
                color: #d4d4d4;
            }
            QLabel {
                color: #d4d4d4;
                font-size: 12px;
            }
            QLabel#nameLabel {
                font-size: 15px;
                font-weight: bold;
                color: #4fc3f7;
            }
            QLabel#tagLabel {
                background-color: #094771;
                color: #ffffff;
                border-radius: 4px;
                padding: 2px 6px;
                font-size: 11px;
            }
            QPushButton {
                background-color: #3c3c3c;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 16px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #4e4e4e;
            }
            QPushButton#openBtn {
                background-color: #0078d4;
            }
            QPushButton#openBtn:hover {
                background-color: #1a8ae8;
            }
        """

    def _build_ui(self, d: dict):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 16, 16, 16)

        top = QHBoxLayout()
        top.setSpacing(12)

        thumb_url = d.get("thumbnail")
        thumb_label = QLabel()
        thumb_label.setFixedSize(160, 90)
        thumb_label.setAlignment(Qt.AlignCenter)
        thumb_label.setStyleSheet("background-color: #2d2d2d; border-radius: 4px;")
        if thumb_url:
            pixmap = QPixmap()
            if pixmap.loadFromData(self._fetch_image(thumb_url)):
                pixmap = pixmap.scaled(160, 90, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                thumb_label.setPixmap(pixmap)
        top.addWidget(thumb_label)

        info_col = QVBoxLayout()
        info_col.setSpacing(2)

        name_lbl = QLabel(d.get("name", "Unknown"))
        name_lbl.setObjectName("nameLabel")
        name_lbl.setWordWrap(True)
        info_col.addWidget(name_lbl)

        info_col.addWidget(QLabel(f"ID: {d.get('id', '?')}"))
        if d.get("size"):
            info_col.addWidget(QLabel(f"Size: {d['size']}"))
        if d.get("posted"):
            info_col.addWidget(QLabel(f"Posted: {d['posted']}"))
        if d.get("updated"):
            info_col.addWidget(QLabel(f"Updated: {d['updated']}"))

        top.addLayout(info_col, 1)
        layout.addLayout(top)

        if d.get("tags"):
            tag_row = QHBoxLayout()
            tag_row.setSpacing(4)
            tag_row.addWidget(QLabel("Tags:"))
            for tag in d["tags"][:10]:
                lbl = QLabel(tag)
                lbl.setObjectName("tagLabel")
                lbl.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
                tag_row.addWidget(lbl)
            tag_row.addStretch(1)
            layout.addLayout(tag_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        open_btn = QPushButton("Open in Browser")
        open_btn.setObjectName("openBtn")
        open_btn.clicked.connect(lambda: webbrowser.open(d.get("url", "")))
        btn_row.addWidget(open_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        btn_row.addStretch(1)
        layout.addLayout(btn_row)

    def _fetch_image(self, url: str) -> bytes:
        import urllib.request
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read()
        except Exception:
            return b""


class WorkshopRollerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Workshop Randomizer")
        self.resize(985, 520)
        self._setup_theme()

        self.app_db = AppDB()
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._on_search_timer)

        self._api_key_timer = QTimer(self)
        self._api_key_timer.setSingleShot(True)
        self._api_key_timer.setInterval(800)   # debounce — save 0.8 s after last keystroke
        self._api_key_timer.timeout.connect(self._save_api_key)

        self._locked_tags: Optional[list] = None
        self._app_id: Optional[int] = None
        self._seen_ids: set[int] = set()

        self._detail_dialogs: list[DetailsDialog] = []

        self._proxy = _SignalProxy()
        self._proxy.tags_loaded.connect(self._populate_tags)
        self._proxy.roll_result.connect(self._on_roll_result)
        self._proxy.status_message.connect(self.statusBar().showMessage)
        self._proxy.app_count_updated.connect(self._on_app_count_updated)
        self._proxy.show_details.connect(self._show_details_dialog)
        self._proxy.harvest_status.connect(self._on_harvest_status)

        self._build_menu()
        self._build_ui()

        QTimer.singleShot(0, self._initial_setup)

    def _setup_theme(self):
        QApplication.setStyle(QStyleFactory.create("Fusion"))
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e1e;
                color: #d4d4d4;
            }
            QMenuBar {
                background-color: #2d2d2d;
                color: #d4d4d4;
                border-bottom: 1px solid #3c3c3c;
                padding: 2px;
            }
            QMenuBar::item:selected {
                background-color: #094771;
            }
            QMenu {
                background-color: #2d2d2d;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
            }
            QMenu::item:selected {
                background-color: #094771;
            }
            QLabel {
                color: #d4d4d4;
                font-size: 12px;
            }
            QLabel#titleLabel {
                font-size: 13px;
                font-weight: bold;
                color: #e0e0e0;
            }
            QLineEdit, QSpinBox {
                background-color: #2d2d2d;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
            }
            QLineEdit:focus, QSpinBox:focus {
                border-color: #0078d4;
            }
            QLineEdit#resultField {
                background-color: #1a1a1a;
                color: #4fc3f7;
                border: 1px solid #0078d4;
                font-weight: bold;
                selection-background-color: #264f78;
            }
            QLineEdit#lockedField {
                background-color: #2d2d2d;
                color: #ffa726;
                border: 1px solid #ffa726;
                font-weight: bold;
            }
            QListWidget {
                background-color: #252526;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                font-size: 12px;
                outline: none;
            }
            QListWidget::item:selected {
                background-color: #094771;
                color: #ffffff;
            }
            QListWidget::item:hover {
                background-color: #2a2d2e;
            }
            QPushButton {
                background-color: #0e639c;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 6px 16px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1177bb;
            }
            QPushButton:pressed {
                background-color: #094771;
            }
            QPushButton:disabled {
                background-color: #3c3c3c;
                color: #666666;
            }
            QPushButton#loadTagsBtn {
                background-color: #2d8f2d;
            }
            QPushButton#loadTagsBtn:hover {
                background-color: #3aa33a;
            }
            QPushButton#lockButton {
                background-color: #2d8f2d;
            }
            QPushButton#lockButton:hover {
                background-color: #3aa33a;
            }
            QPushButton#resetButton {
                background-color: #8f2d2d;
            }
            QPushButton#resetButton:hover {
                background-color: #a33a3a;
            }
            QPushButton#maxButton {
                background-color: #3c3c3c;
                color: #cccccc;
            }
            QPushButton#maxButton:checked {
                background-color: #ffa726;
                color: #1e1e1e;
            }
            QPushButton#maxButton:hover {
                background-color: #4e4e4e;
            }
            QPushButton#maxButton:checked:hover {
                background-color: #ffb74d;
            }
            QPushButton#refreshBtn {
                background-color: #3c3c3c;
                color: #cccccc;
            }
            QPushButton#refreshBtn:hover {
                background-color: #4e4e4e;
            }
            QPushButton#forgetCacheBtn {
                background-color: #6b3030;
            }
            QPushButton#forgetCacheBtn:hover {
                background-color: #8a3e3e;
            }
            QGroupBox {
                font-size: 12px;
                font-weight: bold;
                color: #e0e0e0;
                border: 1px solid #3c3c3c;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 16px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
            }
            QStatusBar {
                background-color: #007acc;
                color: #ffffff;
                font-size: 11px;
            }
            QScrollBar:vertical {
                background: #1e1e1e;
                width: 10px;
            }
            QScrollBar::handle:vertical {
                background: #424242;
                border-radius: 5px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #555555;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")
        refresh_action = QAction("&Refresh Game IDs", self)
        refresh_action.setShortcut("Ctrl+R")
        refresh_action.triggered.connect(self._on_refresh_game_ids)
        file_menu.addAction(refresh_action)

        file_menu.addSeparator()
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        help_menu = menubar.addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        vbox = QVBoxLayout(central)
        vbox.setSpacing(8)
        vbox.setContentsMargins(12, 8, 12, 8)

        self._build_app_id_section(vbox)
        self._build_tags_section(vbox)
        self._build_pages_section(vbox)
        self._build_action_section(vbox)

    def _build_app_id_section(self, parent: QVBoxLayout):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        vbox = QVBoxLayout(frame)
        vbox.setContentsMargins(8, 6, 8, 6)
        vbox.setSpacing(6)

        # ── Row 1: game search ──────────────────────────────────────────
        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel("Steam App ID:")
        lbl.setObjectName("titleLabel")
        row1.addWidget(lbl)

        self.app_input = QLineEdit()
        self.app_input.setPlaceholderText("Type a game name or ID...")
        self.app_input.setMinimumWidth(280)
        self.app_input.textChanged.connect(self._on_app_text_changed)
        row1.addWidget(self.app_input, 1)

        self.load_tags_btn = QPushButton("Load Tags")
        self.load_tags_btn.setObjectName("loadTagsBtn")
        self.load_tags_btn.setToolTip("Fetch available tags for this game")
        self.load_tags_btn.clicked.connect(self._on_load_tags)
        row1.addWidget(self.load_tags_btn)

        self.refresh_btn = QPushButton("Refresh Game IDs")
        self.refresh_btn.setObjectName("refreshBtn")
        self.refresh_btn.clicked.connect(self._on_refresh_game_ids)
        row1.addWidget(self.refresh_btn)

        vbox.addLayout(row1)

        # ── Row 2: Steam Web API key ─────────────────────────────────────
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)

        api_lbl = QLabel("API Key:")
        api_lbl.setObjectName("titleLabel")
        api_lbl.setToolTip(
            "Free Steam Web API key — enables full cursor-based pagination\n"
            "(up to 100 items/request, unlimited pages).\n"
            "Get one at: steamcommunity.com/dev/apikey"
        )
        row2.addWidget(api_lbl)

        self.api_key_input = QLineEdit()
        self.api_key_input.setPlaceholderText(
            "Paste Steam Web API key for full pagination  "
            "—  get a free key at steamcommunity.com/dev/apikey"
        )
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.textChanged.connect(self._on_api_key_changed)
        row2.addWidget(self.api_key_input, 1)

        self.show_key_btn = QPushButton("Show")
        self.show_key_btn.setObjectName("maxButton")   # reuse the toggle styling
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setFixedWidth(70)
        self.show_key_btn.toggled.connect(self._on_show_key_toggled)
        row2.addWidget(self.show_key_btn)

        self.api_status_lbl = QLabel("● no key  (scraper fallback, 1 page only)")
        self.api_status_lbl.setStyleSheet("color: #888888; font-size: 11px;")
        row2.addWidget(self.api_status_lbl)

        vbox.addLayout(row2)
        parent.addWidget(frame)

    def _build_tags_section(self, parent: QVBoxLayout):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        vbox = QVBoxLayout(frame)
        vbox.setContentsMargins(8, 6, 8, 6)

        lbl = QLabel("Available Tags")
        lbl.setObjectName("titleLabel")
        vbox.addWidget(lbl)

        self.tag_list = QListWidget()
        self.tag_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.tag_list.setWrapping(True)
        self.tag_list.setFlow(QListView.TopToBottom)
        self.tag_list.setSpacing(4)
        self.tag_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tag_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tag_list.setMinimumHeight(300)
        vbox.addWidget(self.tag_list, 1)

        lock_row = QHBoxLayout()

        self.lock_btn = QPushButton("Lock Category")
        self.lock_btn.setObjectName("lockButton")
        self.lock_btn.clicked.connect(self._on_lock_category)
        lock_row.addWidget(self.lock_btn)

        self.locked_field = QLineEdit()
        self.locked_field.setObjectName("lockedField")
        self.locked_field.setReadOnly(True)
        self.locked_field.setPlaceholderText("None — rolling from ALL items")
        lock_row.addWidget(self.locked_field, 1)

        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setObjectName("resetButton")
        self.reset_btn.clicked.connect(self._on_reset_category)
        lock_row.addWidget(self.reset_btn)

        vbox.addLayout(lock_row)

        self.tags_status = QLabel("Click 'Load Tags' after entering an App ID.")
        self.tags_status.setWordWrap(True)
        vbox.addWidget(self.tags_status)

        parent.addWidget(frame)

    def _build_pages_section(self, parent: QVBoxLayout):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        hbox = QHBoxLayout(frame)
        hbox.setContentsMargins(8, 6, 8, 6)

        lbl = QLabel("Pages to fetch:")
        lbl.setObjectName("titleLabel")
        hbox.addWidget(lbl)

        self.pages_spin = QSpinBox()
        self.pages_spin.setRange(1, 99999)
        self.pages_spin.setValue(50)
        self.pages_spin.setSuffix(" pages")
        self.pages_spin.setMinimumWidth(120)
        self.pages_spin.valueChanged.connect(self._on_pages_changed)
        hbox.addWidget(self.pages_spin)

        self.max_btn = QPushButton("MAX")
        self.max_btn.setObjectName("maxButton")
        self.max_btn.setCheckable(True)
        self.max_btn.setToolTip("Fetch ALL available pages until none left")
        self.max_btn.toggled.connect(self._on_max_toggled)
        hbox.addWidget(self.max_btn)

        hbox.addStretch(1)

        self.cache_label = QLabel("Cache: 0 items")
        hbox.addWidget(self.cache_label)

        self.forget_cache_btn = QPushButton("Forget Cache")
        self.forget_cache_btn.setObjectName("forgetCacheBtn")
        self.forget_cache_btn.clicked.connect(self._on_forget_cache)
        hbox.addWidget(self.forget_cache_btn)

        parent.addWidget(frame)

    def _build_action_section(self, parent: QVBoxLayout):
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        hbox = QHBoxLayout(frame)
        hbox.setContentsMargins(8, 6, 8, 6)

        count_lbl = QLabel("Count:")
        count_lbl.setObjectName("titleLabel")
        hbox.addWidget(count_lbl)

        self.roll_count_spin = QSpinBox()
        self.roll_count_spin.setRange(1, 1000)
        self.roll_count_spin.setValue(1)
        self.roll_count_spin.setMinimumWidth(70)
        self.roll_count_spin.setToolTip("How many items to roll (max 1000)")
        hbox.addWidget(self.roll_count_spin)

        self.ignore_seen_cb = QCheckBox("Ignore already rolled")
        self.ignore_seen_cb.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        self.ignore_seen_cb.toggled.connect(self._on_ignore_seen_toggled)
        hbox.addWidget(self.ignore_seen_cb)

        self.save_history_cb = QCheckBox("Save ignored history")
        self.save_history_cb.setStyleSheet("color: #888888; font-size: 11px;")
        self.save_history_cb.setEnabled(False)
        hbox.addWidget(self.save_history_cb)

        self.roll_btn = QPushButton("Roll the Dice!")
        self.roll_btn.setObjectName("rollButton")
        self.roll_btn.clicked.connect(self._on_roll)
        hbox.addWidget(self.roll_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumWidth(180)
        hbox.addWidget(self.progress_bar)

        self.result_field = QLineEdit()
        self.result_field.setObjectName("resultField")
        self.result_field.setReadOnly(True)
        self.result_field.setPlaceholderText("Click Roll to get a random workshop item...")
        hbox.addWidget(self.result_field, 1)

        parent.addWidget(frame)

    def _initial_setup(self):
        # Restore saved API key (silently, no signal noise)
        saved_key = self.app_db.get_setting("steam_api_key") or ""
        if saved_key:
            self.api_key_input.blockSignals(True)
            self.api_key_input.setText(saved_key)
            self.api_key_input.blockSignals(False)
            self._refresh_api_status(saved_key)

        count = self.app_db.get_count()
        if count == 0:
            logger.info("No cached game list. Click 'Refresh Game IDs' to fetch.")
            self._proxy.status_message.emit("No game database. Click Refresh Game IDs.")
        else:
            logger.info("Loaded %s games from local database", count)
            self._proxy.status_message.emit(f"Ready — {count} games cached")

        self._seen_ids = load_ignored_ids()
        if self._seen_ids:
            logger.info("Loaded %d ignored IDs from history", len(self._seen_ids))

    def _on_api_key_changed(self, text: str):
        self._refresh_api_status(text)
        self._api_key_timer.start()

    def _refresh_api_status(self, text: str):
        if text.strip():
            self.api_status_lbl.setText("● key set  (full pagination enabled)")
            self.api_status_lbl.setStyleSheet("color: #4fc3f7; font-size: 11px;")
        else:
            self.api_status_lbl.setText("● no key  (scraper fallback, 1 page only)")
            self.api_status_lbl.setStyleSheet("color: #888888; font-size: 11px;")

    def _on_show_key_toggled(self, checked: bool):
        self.api_key_input.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        self.show_key_btn.setText("Hide" if checked else "Show")

    def _save_api_key(self):
        key = self.api_key_input.text().strip()
        self.app_db.set_setting("steam_api_key", key)
        logger.debug("API key saved (%d chars)", len(key))

    def _on_app_text_changed(self, text: str):
        self._app_id = None
        self._search_timer.start()

    def _on_search_timer(self):
        text = self.app_input.text().strip()
        if not text:
            self.app_input.setCompleter(None)
            return

        results = self.app_db.search(text, limit=20)
        if not results:
            self.app_input.setCompleter(None)
            return

        completer = QCompleter([f"{name} (ID: {aid})" for aid, name in results], self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        completer.activated.connect(self._on_completer_activated)
        self.app_input.setCompleter(completer)
        completer.complete()

    def _on_completer_activated(self, text: str):
        m = __import__("re").search(r"ID:\s*(\d+)", text)
        if m:
            self._app_id = int(m.group(1))
            logger.info("Selected app: %s (%s)", self._app_id, text)
            self._proxy.status_message.emit(f"Selected: {text}")

    def _get_current_app_id(self) -> Optional[int]:
        if self._app_id is not None:
            return self._app_id
        text = self.app_input.text().strip()
        m = __import__("re").search(r"ID:\s*(\d+)", text)
        if m:
            return int(m.group(1))
        if text.isdigit():
            return int(text)
        return None

    def _on_load_tags(self):
        app_id = self._get_current_app_id()
        if app_id is None:
            self._proxy.status_message.emit("Enter a game name or App ID first.")
            return

        self.load_tags_btn.setEnabled(False)
        self.load_tags_btn.setText("Loading...")
        self.tag_list.clear()
        self._locked_tags = None
        self.locked_field.clear()
        self.tags_status.setText("Fetching tags...")

        def worker(aid):
            try:
                tags = get_available_tags(aid)
                items = [(k, v) for k, v in sorted(tags.items())]
                self._proxy.tags_loaded.emit(items)
                self._update_cache_label(aid)
            except Exception as e:
                logger.exception("Failed to load tags for %s", aid)
                self._proxy.status_message.emit(f"Error: {e}")
            finally:
                self.load_tags_btn.setEnabled(True)
                self.load_tags_btn.setText("Load Tags")

        threading.Thread(target=worker, args=(app_id,), daemon=True).start()

    def _populate_tags(self, items: list):
        self.tag_list.clear()
        if not items:
            self.tags_status.setText("No tags found for this game.")
            return
        for raw_tag, display_name in items:
            item = QListWidgetItem(display_name)
            item.setData(Qt.UserRole, raw_tag)
            self.tag_list.addItem(item)
        self.tags_status.setText(f"{len(items)} tags. Select and click 'Lock Category'.")

    def _on_lock_category(self):
        selected = self.tag_list.selectedItems()
        if not selected:
            self._proxy.status_message.emit("Select at least one tag first.")
            return
        raw_tags = [item.data(Qt.UserRole) for item in selected]
        display_parts = [item.text() for item in selected]
        self._locked_tags = raw_tags
        self.locked_field.setText(" | ".join(display_parts))
        logger.info("Locked category: %s", raw_tags)
        self._proxy.status_message.emit(f"Locked: {', '.join(display_parts)}")

    def _on_reset_category(self):
        self._locked_tags = None
        self.locked_field.clear()
        logger.info("Category reset to ALL")
        self._proxy.status_message.emit("Category reset to ALL items")

    def _on_pages_changed(self, value: int):
        if self.max_btn.isChecked():
            self.max_btn.blockSignals(True)
            self.max_btn.setChecked(False)
            self.max_btn.blockSignals(False)

    def _on_max_toggled(self, checked: bool):
        self.pages_spin.setEnabled(not checked)
        if checked:
            self.pages_spin.setStyleSheet("background-color: #2d2d2d; color: #888888; border: 1px solid #ffa726;")
        else:
            self.pages_spin.setStyleSheet("")

    def _on_refresh_game_ids(self):
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Refreshing...")
        self._proxy.status_message.emit("Fetching game list...")

        def worker():
            try:
                count = self.app_db.refresh()
                self._proxy.app_count_updated.emit(count)
            except Exception as e:
                logger.exception("Refresh failed")
            finally:
                self.refresh_btn.setEnabled(True)
                self.refresh_btn.setText("Refresh Game IDs")

        threading.Thread(target=worker, daemon=True).start()

    def _on_app_count_updated(self, count: int):
        logger.info("Game DB updated: %s games", count)
        self._proxy.status_message.emit(f"DB updated: {count} games")

    def _on_forget_cache(self):
        app_id = self._get_current_app_id()
        if app_id is not None:
            clear_cache(app_id)
            logger.info("Cache cleared for app %s", app_id)
        else:
            clear_cache()
            logger.info("All caches cleared")
        self._update_cache_label(app_id)
        self._proxy.status_message.emit("Cache cleared.")

    def _update_cache_label(self, app_id: Optional[int] = None):
        if app_id is None:
            app_id = self._get_current_app_id()
        if app_id is None:
            self.cache_label.setText("Cache: 0 items")
            return
        counts = get_cached_count(app_id)
        total = sum(counts.values())
        self.cache_label.setText(f"Cache: {total} items")

    def _on_harvest_status(self, msg: str):
        self._proxy.status_message.emit(msg)

    def _on_ignore_seen_toggled(self, checked: bool):
        self.save_history_cb.setEnabled(checked)
        if checked:
            self.save_history_cb.setStyleSheet("color: #d4d4d4; font-size: 11px;")
        else:
            self.save_history_cb.setStyleSheet("color: #888888; font-size: 11px;")
            self.save_history_cb.setChecked(False)

    def _on_roll(self):
        app_id = self._get_current_app_id()
        if app_id is None:
            self._proxy.status_message.emit("Enter a valid App ID first.")
            return

        DetailsDialog._offset = 0

        max_pages = None if self.max_btn.isChecked() else self.pages_spin.value()
        tags = self._locked_tags
        count = self.roll_count_spin.value()
        self._detail_app_id = app_id
        use_seen = self.ignore_seen_cb.isChecked()
        save_history = self.save_history_cb.isChecked()
        api_key = self.api_key_input.text().strip() or None

        self.roll_btn.setEnabled(False)
        self.roll_btn.setText("Rolling...")
        self.result_field.clear()
        self._proxy.status_message.emit("Rolling the dice...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)

        def worker(aid, tgs, mp, ct, akey, svh):
            def status(msg):
                self._proxy.harvest_status.emit(msg)
            try:
                logger.info("Rolling: app_id=%s tags=%s max_pages=%s count=%s api=%s",
                            aid, tgs, mp, ct, "key" if akey else "scraper")
                results = []
                excluded: set = set()
                for i in range(ct):
                    combined = excluded | (self._seen_ids if use_seen else set())
                    result = roll_random_item(aid, tags=tgs, max_pages=mp,
                                              exclude_ids=combined or None,
                                              status_callback=status,
                                              api_key=akey)
                    if result is None:
                        break
                    results.append(result)
                    excluded.add(result["id"])
                if svh and results:
                    save_ignored_ids(aid, [r["id"] for r in results])
                self._proxy.roll_result.emit({"items": results, "requested": ct})
                self._update_cache_label(aid)
            except Exception as e:
                logger.exception("Roll failed")
                self._proxy.roll_result.emit({"error": str(e)})

        threading.Thread(target=worker, args=(app_id, tags, max_pages, count, api_key, save_history), daemon=True).start()

    def _on_roll_result(self, msg):
        self.roll_btn.setEnabled(True)
        self.roll_btn.setText("Roll the Dice!")
        self.progress_bar.setVisible(False)

        if msg is None:
            self.result_field.setText("No valid item found.")
            self._proxy.status_message.emit("No items found.")
            return

        if "error" in msg:
            self.result_field.setText(f"Error: {msg['error']}")
            self._proxy.status_message.emit("Roll failed.")
            return

        results = msg["items"]
        if not results:
            self.result_field.setText("No valid item found.")
            self._proxy.status_message.emit("No items found.")
            return

        self._roll_results = results
        self._roll_anim_index = 0
        self._roll_anim_timer = QTimer(self)
        self._roll_anim_timer.setInterval(30)
        self._roll_anim_timer.timeout.connect(self._on_roll_anim_step)
        self._roll_anim_timer.start()

    def _on_roll_anim_step(self):
        results = self._roll_results
        idx = self._roll_anim_index
        item = results[idx % len(results)]
        self.result_field.setText(f"{item['url']}")
        self._roll_anim_index += 1
        if self._roll_anim_index >= 6:
            self._roll_anim_timer.stop()
            self._roll_anim_timer.deleteLater()
            self._roll_anim_timer = None
            self._show_final_result(results)

    def _show_final_result(self, results: list):
        self.roll_btn.setEnabled(True)
        for r in results:
            self._seen_ids.add(r["id"])
        final = results[-1]
        self.result_field.setText(f"{final['url']}")
        self.result_field.selectAll()
        self._proxy.status_message.emit(f"Found {len(results)} item(s). Opening details...")
        logger.info("Fetching details for %s item(s)...", len(results))
        self._open_detail_dialogs(results)

    def _open_detail_dialogs(self, results: list):
        def worker(aid):
            errors = []
            for item in results:
                try:
                    details = get_item_details(item["id"], app_id=aid)
                    if details:
                        self._proxy.show_details.emit(details)
                    else:
                        logger.warning("No details returned for item %s", item["id"])
                        errors.append(str(item["id"]))
                except Exception as e:
                    logger.exception("Failed to fetch details for item %s", item["id"])
                    errors.append(str(item["id"]))
            if errors:
                self._proxy.status_message.emit("Error: Items %s do not exist or are inaccessible" % ", ".join(errors[:5]))
        threading.Thread(target=worker, args=(self._detail_app_id,), daemon=True).start()

    def _show_details_dialog(self, details: dict):
        dlg = DetailsDialog(details, self)
        self._detail_dialogs.append(dlg)
        dlg.show()
        def _on_finished():
            try:
                self._detail_dialogs.remove(dlg)
            except ValueError:
                pass
        dlg.finished.connect(_on_finished)

    def _show_about(self):
        QMessageBox.about(
            self, "About Workshop Randomizer",
            "<h3>Workshop Randomizer v2.0</h3>"
            "<p>Randomly pick a valid Steam Workshop item for any game.</p>"
            "<p>Built with PySide6 & Python 3</p>",
        )


def run_gui():
    import sys

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if DEBUG else logging.WARNING)
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(h)

    app = QApplication(sys.argv)
    app.setApplicationName("Workshop Randomizer")
    app.setOrganizationName("WorkshopTools")

    window = WorkshopRollerWindow()
    window.show()
    sys.exit(app.exec())
