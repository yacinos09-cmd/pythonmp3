"""Fenêtre principale : catégories, pistes, pré-écoute, export et journal."""
from __future__ import annotations

import html
import os
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, QStandardPaths, Qt, QThread, QTimer, QUrl, Slot
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSlider,
                               QSplitter, QStyle, QToolButton, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from app import APP_NAME, ORG_NAME, __version__
from app.audio import AudioError, FFmpegStatus, check_ffmpeg, probe
from app.export_plan import plan_exports, sanitize_component
from app.models import AUDIO_EXTENSIONS, PROJECT_FILE, Category, Project, ProjectError, Track
from app.ui.dialogs import ExportDialog, SettingsDialog
from app.ui.export_worker import ExportWorker
from app.ui.widgets import ITEM_FLAGS, ROLE_ID, ReorderTree, format_duration

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    HAS_MULTIMEDIA = True
except ImportError:  # pragma: no cover
    HAS_MULTIMEDIA = False

LOG_COLORS = {"info": None, "success": "#2e7d32", "warning": "#e65100", "error": "#c62828"}
AUDIO_FILTER = "Fichiers audio (" + " ".join(f"*{e}" for e in AUDIO_EXTENSIONS) + ");;MP3 (*.mp3);;Tous les fichiers (*)"
T_NUM, T_TITLE, T_DUR, T_FMT, T_PATH = range(5)
C_NAME, C_COUNT, C_DUR = range(3)


def move_indices(items: list, indices: list[int], delta: int) -> list[int]:
    """Déplace les éléments aux indices donnés de ±1 en bloc ; retourne les nouveaux indices."""
    sel = set(indices)
    order = sorted(indices, reverse=delta > 0)
    for i in order:
        j = i + delta
        if 0 <= j < len(items) and j not in sel:
            items[i], items[j] = items[j], items[i]
            sel.discard(i)
            sel.add(j)
    return sorted(sel)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings()
        self.project: Project | None = None
        self.ffmpeg: FFmpegStatus = FFmpegStatus(None)
        self._populating = False
        self._thread: QThread | None = None
        self._worker: ExportWorker | None = None
        self._last_export_dir: Path | None = None
        self._playing_id: str | None = None

        self._save_timer = QTimer(self, singleShot=True, interval=400)
        self._save_timer.timeout.connect(self.save_project)

        self.setWindowTitle(APP_NAME)
        self.resize(1180, 760)
        self._build_actions()
        self._build_ui()
        self._build_menus()
        self._build_player()
        geo = self.settings.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        QTimer.singleShot(0, self._startup)

    # ================================================================== UI
    def icon(self, sp: QStyle.StandardPixmap) -> QIcon:
        return self.style().standardIcon(sp)

    def _action(self, text, slot, shortcut=None, icon=None, tip=None) -> QAction:
        a = QAction(text, self)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if icon is not None:
            a.setIcon(self.icon(icon))
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        return a

    def _build_actions(self) -> None:
        SP = QStyle.StandardPixmap
        self.act_new = self._action("Nouveau projet…", self.new_project, QKeySequence.StandardKey.New, SP.SP_FileIcon)
        self.act_open = self._action("Ouvrir un projet…", self.open_project, QKeySequence.StandardKey.Open, SP.SP_DialogOpenButton)
        self.act_save = self._action("Enregistrer", self.save_project, QKeySequence.StandardKey.Save, SP.SP_DialogSaveButton,
                                     "Le projet est aussi enregistré automatiquement à chaque modification")
        self.act_reveal = self._action("Ouvrir le dossier du projet", self.reveal_project, icon=SP.SP_DirOpenIcon)
        self.act_copy = self._action("Copier les fichiers importés dans le projet", self.toggle_copy_files)
        self.act_copy.setCheckable(True)
        self.act_consolidate = self._action("Rassembler tous les fichiers dans le projet…", self.consolidate)
        self.act_quit = self._action("Quitter", self.close, QKeySequence.StandardKey.Quit)

        self.act_add_cat = self._action("Nouvelle catégorie", self.add_category, "Ctrl+Shift+N", SP.SP_FileDialogNewFolder)
        self.act_rename_cat = self._action("Renommer la catégorie", self.rename_category, "F2")
        self.act_del_cat = self._action("Supprimer la catégorie", self.delete_category, icon=SP.SP_TrashIcon)
        self.act_export_cat = self._action("Exporter cette catégorie…", lambda: self.export_dialog("current"))

        self.act_add_files = self._action("Ajouter des fichiers…", self.add_files_dialog, "Ctrl+I", SP.SP_FileDialogContentsView)
        self.act_add_folder = self._action("Ajouter un dossier…", self.add_folder_dialog, "Ctrl+Shift+I", SP.SP_DirIcon)
        self.act_export = self._action("Exporter…", lambda: self.export_dialog("checked"), "Ctrl+E", SP.SP_DriveHDIcon,
                                       "Fusionner les catégories cochées en fichiers MP3")
        self.act_settings = self._action("Paramètres / FFmpeg…", self.open_settings, QKeySequence.StandardKey.Preferences,
                                         SP.SP_ComputerIcon)
        self.act_about = self._action(f"À propos de {APP_NAME}", self.about)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&Fichier")
        for a in (self.act_new, self.act_open, self.act_save, None, self.act_reveal, self.act_copy,
                  self.act_consolidate, None, self.act_settings, None, self.act_quit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Catégories")
        for a in (self.act_add_cat, self.act_rename_cat, self.act_del_cat, None, self.act_export_cat):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Pistes")
        for a in (self.act_add_files, self.act_add_folder):
            m.addAction(a)
        m = mb.addMenu("&Export")
        m.addAction(self.act_export)
        m.addAction(self.act_export_cat)
        m = mb.addMenu("&Aide")
        m.addAction(self._action("Vérifier FFmpeg", lambda: (self.refresh_ffmpeg_status(), self.log("info", self.ffmpeg.describe()))))
        m.addAction(self.act_about)

        tb = self.addToolBar("Principal")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        for a in (self.act_new, self.act_open, None, self.act_add_cat, self.act_add_files,
                  self.act_add_folder, None, self.act_export, None, self.act_settings):
            tb.addSeparator() if a is None else tb.addAction(a)

    def _small_btn(self, text, slot, icon=None, tip=None) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        if icon is not None:
            b.setIcon(self.icon(icon))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if text
                                 else Qt.ToolButtonStyle.ToolButtonIconOnly)
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(slot)
        b.setAutoRaise(True)
        return b

    def _build_ui(self) -> None:
        SP = QStyle.StandardPixmap

        # --- panneau catégories
        self.cat_tree = ReorderTree(accept_foreign=True)
        self.cat_tree.setHeaderLabels(["Catégorie", "Pistes", "Durée"])
        self.cat_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        h = self.cat_tree.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(C_NAME, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(C_COUNT, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(C_DUR, QHeaderView.ResizeMode.ResizeToContents)
        self.cat_tree.setToolTip("Cochez les catégories à exporter. Double-cliquez pour renommer.\n"
                                 "Glissez pour réordonner ; déposez des fichiers ou des pistes sur une catégorie.")
        self.cat_tree.currentItemChanged.connect(self._current_category_changed)
        self.cat_tree.itemChanged.connect(self._category_item_changed)
        self.cat_tree.itemDoubleClicked.connect(lambda it, col: self.cat_tree.editItem(it, C_NAME))
        self.cat_tree.orderChanged.connect(self._categories_reordered)
        self.cat_tree.filesDropped.connect(self._files_dropped_on_category)
        self.cat_tree.foreignDropped.connect(self._tracks_dropped_on_category)
        self.cat_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.cat_tree.customContextMenuRequested.connect(self._category_menu)

        cat_btns = QHBoxLayout()
        cat_btns.setSpacing(2)
        cat_btns.addWidget(self._small_btn("Ajouter", self.add_category, SP.SP_FileDialogNewFolder))
        cat_btns.addWidget(self._small_btn("", self.rename_category, SP.SP_FileDialogDetailedView, "Renommer (F2)"))
        cat_btns.addWidget(self._small_btn("", lambda: self.move_category(-1), SP.SP_ArrowUp, "Monter"))
        cat_btns.addWidget(self._small_btn("", lambda: self.move_category(1), SP.SP_ArrowDown, "Descendre"))
        cat_btns.addStretch()
        cat_btns.addWidget(self._small_btn("", self.delete_category, SP.SP_TrashIcon, "Supprimer la catégorie"))

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(6, 6, 3, 6)
        title = QLabel("Catégories")
        title.setStyleSheet("font-weight:600; font-size:14px;")
        ll.addWidget(title)
        ll.addWidget(self.cat_tree, 1)
        ll.addLayout(cat_btns)

        # --- panneau pistes
        self.track_header = QLabel()
        self.track_header.setStyleSheet("font-weight:600; font-size:14px;")
        self.track_tree = ReorderTree()
        self.track_tree.setHeaderLabels(["#", "Titre", "Durée", "Format", "Fichier"])
        h = self.track_tree.header()
        h.setStretchLastSection(True)
        h.setSectionResizeMode(T_NUM, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(T_TITLE, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(T_DUR, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(T_FMT, QHeaderView.ResizeMode.ResizeToContents)
        self.track_tree.setColumnWidth(T_TITLE, 300)
        self.track_tree.setToolTip("Cochez les pistes à inclure dans l'export. Glissez pour réordonner.\n"
                                   "Déposez des fichiers MP3 ou des dossiers ici. Double-clic : écouter.")
        self.track_tree.itemChanged.connect(self._track_item_changed)
        self.track_tree.itemDoubleClicked.connect(lambda it, col: self.play_track(it.data(0, ROLE_ID)))
        self.track_tree.orderChanged.connect(self._tracks_reordered)
        self.track_tree.filesDropped.connect(lambda paths, row: self.add_paths(paths, self.current_category(), row))
        self.track_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.track_tree.customContextMenuRequested.connect(self._track_menu)
        for key, slot in ((QKeySequence(QKeySequence.StandardKey.Delete), self.remove_tracks),
                          (QKeySequence(Qt.Key.Key_Backspace), self.remove_tracks),
                          (QKeySequence(Qt.Key.Key_Return), self.toggle_play)):
            sc = QShortcut(key, self.track_tree)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(slot)

        tr_btns = QHBoxLayout()
        tr_btns.setSpacing(2)
        tr_btns.addWidget(self._small_btn("Ajouter…", self.add_files_dialog, SP.SP_FileDialogContentsView))
        tr_btns.addWidget(self._small_btn("", lambda: self.move_tracks(-1), SP.SP_ArrowUp, "Monter"))
        tr_btns.addWidget(self._small_btn("", lambda: self.move_tracks(1), SP.SP_ArrowDown, "Descendre"))
        tr_btns.addWidget(self._small_btn("Tout cocher", lambda: self.check_all_tracks(True)))
        tr_btns.addWidget(self._small_btn("Tout décocher", lambda: self.check_all_tracks(False)))
        tr_btns.addStretch()
        tr_btns.addWidget(self._small_btn("Retirer", self.remove_tracks, SP.SP_TrashIcon,
                                          "Retirer les pistes sélectionnées (les fichiers ne sont pas supprimés)"))

        # --- lecteur
        self.play_btn = self._small_btn("", self.toggle_play, SP.SP_MediaPlay, "Lecture / pause (Entrée)")
        self.stop_btn = self._small_btn("", self.stop_playback, SP.SP_MediaStop, "Stop")
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 0)
        self.time_label = QLabel("—")
        self.time_label.setMinimumWidth(260)
        player_row = QHBoxLayout()
        player_row.addWidget(self.play_btn)
        player_row.addWidget(self.stop_btn)
        player_row.addWidget(self.seek, 1)
        player_row.addWidget(self.time_label)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(3, 6, 6, 6)
        rl.addWidget(self.track_header)
        rl.addWidget(self.track_tree, 1)
        rl.addLayout(tr_btns)
        rl.addLayout(player_row)

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(left)
        top.addWidget(right)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 3)
        top.setSizes([320, 860])

        # --- journal + progression
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(5000)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("Prêt")
        self.progress.setTextVisible(True)
        self.cancel_btn = QPushButton("Annuler l'export")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_export)
        clear = QPushButton("Effacer le journal")
        clear.clicked.connect(self.log_view.clear)
        prow = QHBoxLayout()
        prow.addWidget(self.progress, 1)
        prow.addWidget(self.cancel_btn)
        prow.addWidget(clear)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(6, 0, 6, 6)
        bl.addLayout(prow)
        bl.addWidget(self.log_view, 1)

        main = QSplitter(Qt.Orientation.Vertical)
        main.addWidget(top)
        main.addWidget(bottom)
        main.setStretchFactor(0, 4)
        main.setStretchFactor(1, 1)
        main.setSizes([560, 180])
        self.setCentralWidget(main)

        self.ffmpeg_label = QLabel()
        self.statusBar().addPermanentWidget(self.ffmpeg_label)

    def _build_player(self) -> None:
        """Le lecteur est créé à la première écoute : un problème de périphérique
        audio ne peut pas empêcher l'application de démarrer."""
        self.player = None
        self._player_ready = False
        if not HAS_MULTIMEDIA or os.environ.get("MP3COMPILER_NO_PLAYER"):
            for w in (self.play_btn, self.stop_btn, self.seek):
                w.setEnabled(False)
            self.time_label.setText("Pré-écoute indisponible")

    def _ensure_player(self) -> bool:
        if self._player_ready:
            return self.player is not None
        self._player_ready = True
        if not HAS_MULTIMEDIA or os.environ.get("MP3COMPILER_NO_PLAYER"):
            return False
        try:
            self.player = QMediaPlayer(self)
            self.audio_out = QAudioOutput(self)
            self.audio_out.setVolume(0.9)
            self.player.setAudioOutput(self.audio_out)
        except Exception as exc:  # pragma: no cover
            self.player = None
            self.log("error", f"Pré-écoute indisponible : {exc}")
            return False
        self.player.positionChanged.connect(self._player_position)
        self.player.durationChanged.connect(self._player_duration)
        self.player.playbackStateChanged.connect(self._player_state)
        self.player.errorOccurred.connect(self._player_error)
        self.seek.sliderMoved.connect(self.player.setPosition)
        return True

    def _player_duration(self, d: int) -> None:
        self.seek.setRange(0, int(d))

    def _player_error(self, err, msg: str) -> None:
        self.log("error", f"Lecture impossible : {msg}")

    # ============================================================ démarrage
    def _startup(self) -> None:
        self.refresh_ffmpeg_status()
        self.log("info", f"{APP_NAME} {__version__} — {self.ffmpeg.describe()}")
        if not self.ffmpeg.ok:
            self.log("warning", "FFmpeg est nécessaire pour l'export. Menu Fichier › Paramètres / FFmpeg…")
        last = self.settings.value("project/last", "")
        if last and (Path(last) / PROJECT_FILE).is_file():
            try:
                self.set_project(Project.load(last))
                return
            except ProjectError as exc:
                self.log("error", str(exc))
        self.set_project(self._default_project())

    def _default_project(self) -> Project:
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation) or str(Path.home())
        root = Path(docs) / APP_NAME / "Mon projet"
        if (root / PROJECT_FILE).is_file():
            try:
                return Project.load(root)
            except ProjectError as exc:
                self.log("error", str(exc))
                root = root.with_name(f"Mon projet {datetime.now():%Y%m%d-%H%M%S}")
        p = Project.create(root, "Mon projet")
        p.add_category("Catégorie 1")
        p.save()
        self.log("info", f"Nouveau projet créé : {root}")
        return p

    def refresh_ffmpeg_status(self) -> None:
        self.ffmpeg = check_ffmpeg(self.settings.value("ffmpeg/path", "") or None)
        ok = self.ffmpeg.ok and self.ffmpeg.has_mp3_encoder
        self.ffmpeg_label.setText(("🟢 " if ok else "🔴 ") + self.ffmpeg.describe())
        self.ffmpeg_label.setToolTip(self.ffmpeg.path or "Configurez FFmpeg dans les paramètres")

    # ============================================================== journal
    def log(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        color = LOG_COLORS.get(level)
        body = html.escape(message).replace("\n", "<br>&nbsp;&nbsp;")
        if color:
            body = f"<span style='color:{color}'>{body}</span>"
        self.log_view.appendHtml(f"<span style='color:gray'>{ts}</span>&nbsp; {body}")

    # =============================================================== projet
    def set_project(self, project: Project) -> None:
        self.stop_playback()
        self.project = project
        self.settings.setValue("project/last", str(project.root))
        self.setWindowTitle(f"{project.name} — {APP_NAME}")
        self.act_copy.setChecked(project.copy_files)
        self.statusBar().showMessage(f"Projet : {project.root}", 5000)
        self.refresh_categories(select_id=project.categories[0].id if project.categories else None)
        missing = project.missing_tracks()
        if missing:
            self.log("warning", f"{len(missing)} fichier(s) introuvable(s) dans ce projet (affichés en rouge).")
        self.log("info", f"Projet ouvert : {project.root}")

    def schedule_save(self) -> None:
        self._save_timer.start()

    @Slot()
    def save_project(self) -> None:
        if not self.project:
            return
        try:
            self.project.save()
            self.statusBar().showMessage("Projet enregistré", 1500)
        except OSError as exc:
            self.log("error", f"Échec de l'enregistrement du projet : {exc}")

    def new_project(self) -> None:
        name, ok = QInputDialog.getText(self, "Nouveau projet", "Nom du projet :", text="Nouveau projet")
        if not ok or not name.strip():
            return
        base = self.project.root.parent if self.project else Path.home()
        parent = QFileDialog.getExistingDirectory(self, "Dossier dans lequel créer le projet", str(base))
        if not parent:
            return
        root = Path(parent) / sanitize_component(name.strip())
        if (root / PROJECT_FILE).exists():
            if QMessageBox.question(self, "Projet existant",
                                    f"Un projet existe déjà dans\n{root}\n\nL'ouvrir ?") == QMessageBox.StandardButton.Yes:
                self._open_path(root)
            return
        copy = QMessageBox.question(
            self, "Fichiers du projet",
            "Copier les fichiers MP3 importés dans le dossier du projet ?\n\n"
            "• Oui : projet autonome et déplaçable (prend de la place disque).\n"
            "• Non : les fichiers sont simplement référencés à leur emplacement actuel.\n\n"
            "Modifiable plus tard dans le menu Fichier.") == QMessageBox.StandardButton.Yes
        self.save_project()
        try:
            p = Project.create(root, name.strip(), copy_files=copy)
            p.add_category("Catégorie 1")
            p.save()
        except (OSError, ProjectError) as exc:
            QMessageBox.critical(self, "Erreur", f"Impossible de créer le projet :\n{exc}")
            return
        self.set_project(p)

    def open_project(self) -> None:
        start = str(self.project.root.parent if self.project else Path.home())
        f, _ = QFileDialog.getOpenFileName(self, "Ouvrir un projet", start,
                                           f"Projet {APP_NAME} ({PROJECT_FILE});;JSON (*.json)")
        if f:
            self._open_path(Path(f))

    def _open_path(self, path: Path) -> None:
        self.save_project()
        try:
            self.set_project(Project.load(path))
        except ProjectError as exc:
            QMessageBox.critical(self, "Erreur", str(exc))

    def reveal_project(self) -> None:
        if self.project:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.root)))

    def toggle_copy_files(self, checked: bool) -> None:
        if self.project:
            self.project.copy_files = checked
            self.schedule_save()
            self.log("info", "Les nouveaux fichiers seront copiés dans le projet." if checked
                     else "Les nouveaux fichiers seront référencés sans copie.")

    def consolidate(self) -> None:
        if not self.project:
            return
        if QMessageBox.question(self, "Rassembler les fichiers",
                                f"Copier tous les fichiers référencés hors du projet dans\n"
                                f"{self.project.media_dir} ?") != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            copied, errors = self.project.consolidate()
        finally:
            QApplication.restoreOverrideCursor()
        for e in errors:
            self.log("warning", e)
        self.log("success", f"{copied} fichier(s) copié(s) dans le projet.")
        self.save_project()
        self.refresh_tracks()

    # ============================================================ catégories
    def current_category(self) -> Category | None:
        it = self.cat_tree.currentItem()
        return self.project.category(it.data(0, ROLE_ID)) if (it and self.project) else None

    def _current_category_changed(self, *_):
        if not self._populating:
            self.refresh_tracks()

    def refresh_categories(self, select_id: str | None = None) -> None:
        if not self.project:
            return
        if select_id is None and (cur := self.current_category()):
            select_id = cur.id
        self._populating = True
        self.cat_tree.clear()
        select_item = None
        for cat in self.project.categories:
            it = QTreeWidgetItem()
            it.setFlags(ITEM_FLAGS | Qt.ItemFlag.ItemIsEditable)
            it.setData(0, ROLE_ID, cat.id)
            it.setText(C_NAME, cat.name)
            it.setCheckState(C_NAME, Qt.CheckState.Checked if cat.enabled else Qt.CheckState.Unchecked)
            it.setTextAlignment(C_COUNT, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            it.setTextAlignment(C_DUR, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._fill_category_stats(it, cat)
            self.cat_tree.addTopLevelItem(it)
            if cat.id == select_id:
                select_item = it
        self._populating = False
        if select_item is None and self.cat_tree.topLevelItemCount():
            select_item = self.cat_tree.topLevelItem(0)
        if select_item:
            self.cat_tree.setCurrentItem(select_item)
        self.refresh_tracks()

    def _fill_category_stats(self, it: QTreeWidgetItem, cat: Category) -> None:
        on = len(cat.enabled_tracks())
        it.setText(C_COUNT, f"{on}/{len(cat.tracks)}" if on != len(cat.tracks) else str(len(cat.tracks)))
        it.setText(C_DUR, format_duration(cat.duration(only_enabled=True)))
        it.setToolTip(C_DUR, f"Durée des pistes cochées (total : {format_duration(cat.duration())})")

    def _update_category_stats(self, cat: Category) -> None:
        it = self.cat_tree.item_by_id(cat.id)
        if it:
            prev, self._populating = self._populating, True
            self._fill_category_stats(it, cat)
            self._populating = prev

    def _category_item_changed(self, it: QTreeWidgetItem, column: int) -> None:
        if self._populating or not self.project:
            return
        cat = self.project.category(it.data(0, ROLE_ID))
        if not cat:
            return
        name = it.text(C_NAME).strip()
        enabled = it.checkState(C_NAME) == Qt.CheckState.Checked
        if not name:
            self._populating = True
            it.setText(C_NAME, cat.name)
            self._populating = False
        elif name != cat.name:
            self.log("info", f"Catégorie renommée : « {cat.name} » → « {name} »")
            cat.name = name
            self.refresh_tracks()
        cat.enabled = enabled
        self.schedule_save()

    def add_category(self) -> None:
        if not self.project:
            return
        cur = self.current_category()
        idx = self.project.categories.index(cur) + 1 if cur else None
        cat = self.project.add_category(index=idx)
        self.refresh_categories(select_id=cat.id)
        self.schedule_save()
        it = self.cat_tree.item_by_id(cat.id)
        if it:
            self.cat_tree.editItem(it, C_NAME)

    def rename_category(self) -> None:
        it = self.cat_tree.currentItem()
        if it:
            self.cat_tree.setFocus()
            self.cat_tree.editItem(it, C_NAME)

    def move_category(self, delta: int) -> None:
        cat = self.current_category()
        if not cat:
            return
        cats = self.project.categories
        move_indices(cats, [cats.index(cat)], delta)
        self.refresh_categories(select_id=cat.id)
        self.schedule_save()

    def delete_category(self) -> None:
        cat = self.current_category()
        if not cat:
            return
        if cat.tracks and QMessageBox.question(
                self, "Supprimer la catégorie",
                f"Supprimer « {cat.name} » et ses {len(cat.tracks)} piste(s) de la liste ?\n"
                "(Les fichiers audio ne sont pas supprimés du disque.)") != QMessageBox.StandardButton.Yes:
            return
        if self._playing_id and cat.track(self._playing_id):
            self.stop_playback()
        idx = self.project.categories.index(cat)
        self.project.remove_category(cat.id)
        cats = self.project.categories
        self.refresh_categories(select_id=cats[min(idx, len(cats) - 1)].id if cats else None)
        self.schedule_save()

    def _categories_reordered(self) -> None:
        if self.project:
            self.project.reorder_categories(self.cat_tree.ids())
            self.refresh_categories()
            self.schedule_save()

    def _target_category(self, row: int) -> Category | None:
        if row < 0 or row >= self.cat_tree.topLevelItemCount():
            return None
        return self.project.category(self.cat_tree.topLevelItem(row).data(0, ROLE_ID))

    def _files_dropped_on_category(self, paths: list, row: int) -> None:
        cat = self._target_category(row) or self.current_category()
        self.add_paths(paths, cat)

    def _tracks_dropped_on_category(self, source, row: int) -> None:
        target = self._target_category(row)
        if source is self.track_tree and target:
            self.move_tracks_to(target)

    def _category_menu(self, pos) -> None:
        m = QMenu(self)
        if self.cat_tree.itemAt(pos):
            for a in (self.act_rename_cat, self.act_export_cat, None, self.act_del_cat):
                m.addSeparator() if a is None else m.addAction(a)
            m.addSeparator()
        m.addAction(self.act_add_cat)
        m.exec(self.cat_tree.viewport().mapToGlobal(pos))

    # ================================================================ pistes
    def refresh_tracks(self, select_ids: list[str] | None = None) -> None:
        cat = self.current_category()
        if select_ids is None:
            select_ids = [i.data(0, ROLE_ID) for i in self.track_tree.selectedItems()]
        prev, self._populating = self._populating, True
        self.track_tree.clear()
        missing_brush = QBrush(QColor("#c62828"))
        warn_icon = self.icon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        if cat:
            for n, t in enumerate(cat.tracks, 1):
                path = self.project.resolve(t)
                it = QTreeWidgetItem()
                it.setFlags(ITEM_FLAGS)
                it.setData(0, ROLE_ID, t.id)
                it.setCheckState(T_NUM, Qt.CheckState.Checked if t.enabled else Qt.CheckState.Unchecked)
                it.setText(T_NUM, f"{n:02d}")
                it.setText(T_TITLE, t.title)
                it.setText(T_DUR, format_duration(t.duration))
                it.setTextAlignment(T_DUR, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                it.setText(T_FMT, t.format_label)
                it.setText(T_PATH, str(path))
                it.setToolTip(T_PATH, str(path))
                if not path.is_file():
                    for c in range(5):
                        it.setForeground(c, missing_brush)
                    it.setIcon(T_TITLE, warn_icon)
                    it.setToolTip(T_TITLE, "Fichier introuvable")
                self.track_tree.addTopLevelItem(it)
                if t.id in select_ids:
                    it.setSelected(True)
            on = cat.enabled_tracks()
            self.track_header.setText(
                f"{cat.name} — {len(cat.tracks)} piste(s) · {format_duration(cat.duration())}"
                + (f"  (cochées : {len(on)} · {format_duration(cat.duration(True))})"
                   if len(on) != len(cat.tracks) else ""))
            self._update_category_stats(cat)
        else:
            self.track_header.setText("Aucune catégorie — créez-en une pour commencer")
        self._populating = prev

    def _track_item_changed(self, it: QTreeWidgetItem, column: int) -> None:
        if self._populating:
            return
        cat = self.current_category()
        t = cat.track(it.data(0, ROLE_ID)) if cat else None
        if t:
            t.enabled = it.checkState(T_NUM) == Qt.CheckState.Checked
            self.refresh_tracks()
            self.schedule_save()

    def selected_tracks(self) -> list[Track]:
        cat = self.current_category()
        if not cat:
            return []
        ids = {i.data(0, ROLE_ID) for i in self.track_tree.selectedItems()}
        return [t for t in cat.tracks if t.id in ids]

    def _tracks_reordered(self) -> None:
        cat = self.current_category()
        if cat:
            cat.reorder(self.track_tree.ids())
            self.refresh_tracks()
            self.schedule_save()

    def move_tracks(self, delta: int) -> None:
        cat = self.current_category()
        rows = self.track_tree.selected_rows()
        if not cat or not rows:
            return
        move_indices(cat.tracks, rows, delta)
        self.refresh_tracks()
        self.schedule_save()

    def move_tracks_to(self, target: Category) -> None:
        cat = self.current_category()
        tracks = self.selected_tracks()
        if not cat or not tracks or target is cat:
            return
        ids = {t.id for t in tracks}
        cat.tracks = [t for t in cat.tracks if t.id not in ids]
        target.tracks.extend(tracks)
        self.log("info", f"{len(tracks)} piste(s) déplacée(s) vers « {target.name} ».")
        self._update_category_stats(target)
        self.refresh_tracks(select_ids=[])
        self.schedule_save()

    def remove_tracks(self) -> None:
        cat = self.current_category()
        tracks = self.selected_tracks()
        if not cat or not tracks:
            return
        ids = {t.id for t in tracks}
        if self._playing_id in ids:
            self.stop_playback()
        first = min(self.track_tree.selected_rows())
        cat.tracks = [t for t in cat.tracks if t.id not in ids]
        nxt = [cat.tracks[min(first, len(cat.tracks) - 1)].id] if cat.tracks else []
        self.refresh_tracks(select_ids=nxt)
        self.schedule_save()

    def check_all_tracks(self, state: bool) -> None:
        cat = self.current_category()
        if cat:
            for t in cat.tracks:
                t.enabled = state
            self.refresh_tracks()
            self.schedule_save()

    def add_files_dialog(self) -> None:
        start = self.settings.value("import/last_dir", str(Path.home()))
        files, _ = QFileDialog.getOpenFileNames(self, "Ajouter des fichiers audio", start, AUDIO_FILTER)
        if files:
            self.settings.setValue("import/last_dir", str(Path(files[0]).parent))
            self.add_paths(files, self.current_category())

    def add_folder_dialog(self) -> None:
        start = self.settings.value("import/last_dir", str(Path.home()))
        d = QFileDialog.getExistingDirectory(self, "Ajouter tous les fichiers audio d'un dossier", start)
        if d:
            self.settings.setValue("import/last_dir", d)
            self.add_paths([d], self.current_category())

    @staticmethod
    def _expand(paths: list[str]) -> list[Path]:
        out: list[Path] = []
        for p in map(Path, paths):
            if p.is_dir():
                out += sorted((f for f in p.rglob("*")
                               if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS),
                              key=lambda f: [s.casefold() for s in f.relative_to(p).parts])
            elif p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                out.append(p)
        return out

    def add_paths(self, paths: list[str], cat: Category | None, row: int = -1) -> None:
        if not self.project:
            return
        files = self._expand(paths)
        if not files:
            self.log("warning", "Aucun fichier audio reconnu dans la sélection.")
            return
        if cat is None:
            cat = self.project.add_category()
            self.refresh_categories(select_id=cat.id)
        new, rejected = [], []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for i, f in enumerate(files):
                if i % 20 == 0:
                    self.statusBar().showMessage(f"Analyse {i + 1}/{len(files)} : {f.name}")
                    QApplication.processEvents()
                try:
                    info = probe(f)
                    stored = self.project.import_file(f)
                except (AudioError, OSError) as exc:
                    rejected.append(f"{f.name} : {exc}")
                    continue
                new.append(Track(path=stored, title=f.stem, duration=info.duration, codec=info.codec,
                                 sample_rate=info.sample_rate, channels=info.channels,
                                 bitrate=info.bitrate))
        finally:
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()
        if row < 0 or row > len(cat.tracks):
            row = len(cat.tracks)
        cat.tracks[row:row] = new
        if new:
            self.log("success", f"{len(new)} piste(s) ajoutée(s) à « {cat.name} »"
                     + (" (copiées dans le projet)" if self.project.copy_files else "") + ".")
        for r in rejected:
            self.log("error", f"Fichier refusé — {r}")
        if rejected:
            QMessageBox.warning(self, "Fichiers refusés",
                                f"{len(rejected)} fichier(s) n'ont pas pu être ajoutés (illisibles ou corrompus) :\n\n"
                                + "\n".join(rejected[:12]) + ("\n…" if len(rejected) > 12 else ""))
        if self.current_category() is cat:
            self.refresh_tracks(select_ids=[t.id for t in new])
        else:
            self._update_category_stats(cat)
        self.schedule_save()

    def _track_menu(self, pos) -> None:
        tracks = self.selected_tracks()
        m = QMenu(self)
        if tracks:
            m.addAction(self.icon(QStyle.StandardPixmap.SP_MediaPlay), "Écouter",
                        lambda: self.play_track(tracks[0].id))
            m.addAction("Afficher dans le dossier",
                        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.resolve(tracks[0]).parent))))
            if len(tracks) == 1:
                m.addAction("Renommer le titre…", lambda: self._rename_track(tracks[0]))
            sub = m.addMenu("Déplacer vers")
            cur = self.current_category()
            for c in self.project.categories:
                if c is not cur:
                    sub.addAction(c.name, lambda c=c: self.move_tracks_to(c))
            sub.setEnabled(len(self.project.categories) > 1)
            m.addSeparator()
            m.addAction(self.icon(QStyle.StandardPixmap.SP_TrashIcon), "Retirer de la liste", self.remove_tracks)
            m.addSeparator()
        m.addAction(self.act_add_files)
        m.addAction(self.act_add_folder)
        m.exec(self.track_tree.viewport().mapToGlobal(pos))

    def _rename_track(self, t: Track) -> None:
        title, ok = QInputDialog.getText(self, "Renommer", "Titre affiché :", text=t.title)
        if ok and title.strip():
            t.title = title.strip()
            self.refresh_tracks()
            self.schedule_save()

    # ============================================================ pré-écoute
    def play_track(self, track_id: str) -> None:
        if not self.project or not self._ensure_player():
            return
        _, t = self.project.find_track(track_id)
        if not t:
            return
        path = self.project.resolve(t)
        if not path.is_file():
            self.log("error", f"Lecture impossible, fichier introuvable : {path}")
            return
        self._playing_id = t.id
        self._playing_title = t.title
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def toggle_play(self) -> None:
        if not self._ensure_player():
            return
        sel = self.selected_tracks()
        state = self.player.playbackState()
        if sel and sel[0].id != self._playing_id:
            self.play_track(sel[0].id)
        elif state == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        elif self._playing_id:
            self.player.play()
        elif sel:
            self.play_track(sel[0].id)

    def stop_playback(self) -> None:
        if getattr(self, "player", None):
            self.player.stop()
            self.player.setSource(QUrl())
        self._playing_id = None
        self.time_label.setText("—")

    def _player_position(self, pos: int) -> None:
        if not self.seek.isSliderDown():
            self.seek.setValue(pos)
        if self._playing_id:
            dur = self.player.duration()
            self.time_label.setText(f"{format_duration(pos / 1000)} / {format_duration(dur / 1000)} — "
                                    f"{self._playing_title}")

    def _player_state(self, state) -> None:
        SP = QStyle.StandardPixmap
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setIcon(self.icon(SP.SP_MediaPause if playing else SP.SP_MediaPlay))

    # ================================================================ export
    def export_dialog(self, scope: str = "checked") -> None:
        if not self.project:
            return
        if self._thread is not None:
            QMessageBox.information(self, "Export en cours", "Un export est déjà en cours.")
            return
        self.refresh_ffmpeg_status()
        if not self.ffmpeg.ok:
            if QMessageBox.question(self, "FFmpeg introuvable",
                                    "FFmpeg est nécessaire pour l'export mais n'a pas été trouvé.\n\n"
                                    "Ouvrir les paramètres pour indiquer son emplacement ?") == QMessageBox.StandardButton.Yes:
                self.open_settings()
            return
        dlg = ExportDialog(self, self.project, self.current_category(), scope)
        if dlg.exec() != ExportDialog.DialogCode.Accepted:
            return
        opts, enc = dlg.options(), dlg.encode_settings()
        if not self.ffmpeg.has_mp3_encoder:
            self.log("warning", "Cet FFmpeg n'a pas libmp3lame : seule la copie directe fonctionnera.")
        jobs, notes = plan_exports(self.project, opts)
        for n in notes:
            self.log("warning", n)
        if not jobs:
            QMessageBox.information(self, "Rien à exporter", "Aucune piste cochée dans la sélection.")
            return
        self._last_export_dir = opts.output_dir
        self._open_after = self.settings.value("export/open_after", True, type=bool)
        self.start_export(jobs, enc)

    def start_export(self, jobs, enc) -> None:
        self.save_project()
        self.log("info", f"Export de {len(jobs)} fichier(s) vers {self._last_export_dir} …")
        self._thread = QThread(self)
        self._worker = ExportWorker(self.ffmpeg.path, jobs, enc)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self.log)
        # Méthodes liées (et non lambdas) : appel en file d'attente dans le thread de l'UI.
        self._worker.progress.connect(self._on_export_progress)
        self._worker.jobStarted.connect(self._on_job_started)
        self._worker.finished.connect(self._export_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._thread_done)
        self.progress.setValue(0)
        self.cancel_btn.setEnabled(True)
        self.act_export.setEnabled(False)
        self.act_export_cat.setEnabled(False)
        self._thread.start()

    @Slot(float)
    def _on_export_progress(self, f: float) -> None:
        self.progress.setValue(int(f * 1000))

    @Slot(int, int, str)
    def _on_job_started(self, i: int, n: int, label: str) -> None:
        self.progress.setFormat(f"[{i}/{n}] {label} — %p%")

    def cancel_export(self) -> None:
        if self._worker:
            self._worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.progress.setFormat("Annulation…")

    def _thread_done(self) -> None:
        self._thread = None
        self._worker = None

    def _export_finished(self, results) -> None:
        self.cancel_btn.setEnabled(False)
        self.act_export.setEnabled(True)
        self.act_export_cat.setEnabled(True)
        ok = [r for r in results if r.status == "ok"]
        err = [r for r in results if r.status == "error"]
        skipped = [r for r in results if r.status == "skipped"]
        cancelled = any(r.status == "cancelled" for r in results)
        tracks_skipped = sum(len(r.skipped_tracks) for r in ok)
        summary = f"{len(ok)} fichier(s) exporté(s)"
        if err:
            summary += f", {len(err)} en erreur"
        if skipped:
            summary += f", {len(skipped)} non exporté(s) (existants)"
        if tracks_skipped:
            summary += f", {tracks_skipped} piste(s) ignorée(s)"
        if cancelled:
            summary += " — annulé"
        self.progress.setFormat(summary)
        if not cancelled:
            self.progress.setValue(1000)
        self.log("error" if err else ("warning" if cancelled or tracks_skipped else "success"), f"Terminé : {summary}.")

        box = QMessageBox(self)
        box.setWindowTitle("Export terminé" if not cancelled else "Export annulé")
        box.setIcon(QMessageBox.Icon.Warning if (err or cancelled) else QMessageBox.Icon.Information)
        details = "\n".join(f"✘ {r.output.name if r.output else '?'} : {r.message}" for r in err)
        box.setText(summary + "." + ("\n\nDétails dans le journal." if err or tracks_skipped else ""))
        if details:
            box.setDetailedText(details)
        open_btn = box.addButton("Ouvrir le dossier", QMessageBox.ButtonRole.ActionRole) if ok else None
        box.addButton(QMessageBox.StandardButton.Close)
        if ok and self._open_after and not err and not cancelled:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_export_dir)))
        box.exec()
        if open_btn is not None and box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_export_dir)))

    # ============================================================== divers
    def open_settings(self) -> None:
        if SettingsDialog(self).exec():
            self.refresh_ffmpeg_status()
            self.log("info", self.ffmpeg.describe() + (f" ({self.ffmpeg.path})" if self.ffmpeg.path else ""))

    def about(self) -> None:
        QMessageBox.about(self, f"À propos de {APP_NAME}",
                          f"<h3>{APP_NAME} {__version__}</h3>"
                          "<p>Organisez vos fichiers MP3 par catégories et compilez-les en un seul fichier.</p>"
                          f"<p>{self.ffmpeg.describe()}<br><small>{self.ffmpeg.path or ''}</small></p>"
                          f"<p><small>Python {sys.version.split()[0]} · Qt via PySide6</small></p>")

    def closeEvent(self, event) -> None:
        if self._thread is not None:
            if QMessageBox.question(self, "Export en cours",
                                    "Un export est en cours. L'annuler et quitter ?") != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.cancel()
            self._thread.quit()
            self._thread.wait(10000)
        self.stop_playback()
        self._save_timer.stop()
        self.save_project()
        self.settings.setValue("window/geometry", self.saveGeometry())
        super().closeEvent(event)


def run() -> int:
    QApplication.setOrganizationName(ORG_NAME)
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationVersion(__version__)
    app = QApplication(sys.argv)
    icon_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "assets" / "icon.png"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    w = MainWindow()
    w.show()
    return app.exec()
