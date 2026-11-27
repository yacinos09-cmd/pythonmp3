"""Boîtes de dialogue : options d'export et paramètres (FFmpeg)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPlainTextEdit, QPushButton, QRadioButton,
                               QVBoxLayout, QWidget)

from app.audio import EncodeSettings, check_ffmpeg
from app.export_plan import (PRESETS_PER_CATEGORY, PRESETS_SINGLE, ExportOptions, plan_exports)
from app.models import Category, Project
from app.ui.widgets import format_duration

BITRATES = [128, 160, 192, 256, 320]
SAMPLE_RATES = [44100, 48000]
CUSTOM = "Personnalisé…"


def _combo(items: list[tuple[str, object]], current) -> QComboBox:
    cb = QComboBox()
    for label, value in items:
        cb.addItem(label, value)
    idx = cb.findData(current)
    cb.setCurrentIndex(idx if idx >= 0 else 0)
    return cb


class ExportDialog(QDialog):
    def __init__(self, parent: QWidget, project: Project, current: Category | None,
                 scope: str = "checked"):
        super().__init__(parent)
        self.setWindowTitle("Exporter en MP3")
        self.setMinimumWidth(720)
        self.project, self.current = project, current
        self.s = QSettings()
        s = self.s

        # --- portée
        n_checked = sum(1 for c in project.categories if c.enabled)
        self.rb_checked = QRadioButton(f"Catégories cochées ({n_checked})")
        self.rb_current = QRadioButton(f"Catégorie courante : « {current.name} »" if current
                                       else "Catégorie courante")
        self.rb_current.setEnabled(current is not None)
        (self.rb_current if scope == "current" and current else self.rb_checked).setChecked(True)

        # --- regroupement
        self.rb_per_cat = QRadioButton("Un fichier MP3 par catégorie")
        self.rb_single = QRadioButton("Tout regrouper dans un seul fichier MP3 (dans l'ordre des catégories)")
        (self.rb_single if s.value("export/grouping", "per_category") == "single"
         else self.rb_per_cat).setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_per_cat)
        grp.addButton(self.rb_single)

        # --- sortie
        default_out = str(Path(project.root) / "Exports")
        self.out_edit = QLineEdit(s.value("export/output_dir", default_out))
        browse = QPushButton("Parcourir…")
        browse.clicked.connect(self._browse)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(browse)

        self.preset_combo = QComboBox()
        self.template_edit = QLineEdit()
        tokens = QLabel("Variables : <code>{project}</code> <code>{category}</code> <code>{n}</code> "
                        "<code>{count}</code> <code>{date}</code> — « / » crée un sous-dossier.")
        tokens.setTextFormat(Qt.TextFormat.RichText)
        tokens.setWordWrap(True)
        tokens.setStyleSheet("color: palette(placeholder-text);")

        self.preview = QPlainTextEdit(readOnly=True)
        self.preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.preview.setMaximumHeight(150)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        # --- encodage
        enc = EncodeSettings()
        self.mode_combo = _combo([("Automatique — copie directe sans réencodage si possible", "auto"),
                                  ("Toujours réencoder", "always")],
                                 s.value("export/reencode", enc.reencode))
        self.bitrate_combo = _combo([(f"{b} kb/s", b) for b in BITRATES],
                                    int(s.value("export/bitrate", enc.bitrate)))
        self.rate_combo = _combo([(f"{r} Hz", r) for r in SAMPLE_RATES],
                                 int(s.value("export/sample_rate", enc.sample_rate)))
        self.channels_combo = _combo([("Stéréo", 2), ("Mono", 1)],
                                     int(s.value("export/channels", enc.channels)))
        self.existing_combo = _combo([("Remplacer", "replace"), ("Créer « nom (2).mp3 »", "rename"),
                                      ("Ne pas exporter", "skip")],
                                     s.value("export/on_existing", enc.on_existing))
        self.skip_check = QCheckBox("Ignorer les pistes manquantes ou corrompues "
                                    "(sinon l'export de la catégorie concernée échoue)")
        self.skip_check.setChecked(s.value("export/skip_invalid", True, type=bool))
        self.open_check = QCheckBox("Ouvrir le dossier de sortie à la fin")
        self.open_check.setChecked(s.value("export/open_after", True, type=bool))

        # --- mise en page
        g_scope = QGroupBox("Que faut-il exporter ?")
        l = QVBoxLayout(g_scope)
        for w in (self.rb_checked, self.rb_current):
            l.addWidget(w)
        sc = QButtonGroup(self)
        sc.addButton(self.rb_checked)
        sc.addButton(self.rb_current)
        l.addSpacing(4)
        l.addWidget(self.rb_per_cat)
        l.addWidget(self.rb_single)

        g_out = QGroupBox("Destination et arborescence")
        f = QFormLayout(g_out)
        f.addRow("Dossier de sortie :", out_row)
        f.addRow("Organisation :", self.preset_combo)
        f.addRow("Modèle de chemin :", self.template_edit)
        f.addRow("", tokens)
        f.addRow("Aperçu :", self.preview)

        g_enc = QGroupBox("Encodage")
        f2 = QFormLayout(g_enc)
        f2.addRow("Mode :", self.mode_combo)
        row = QHBoxLayout()
        for w in (self.bitrate_combo, self.rate_combo, self.channels_combo):
            row.addWidget(w)
        row.addStretch()
        f2.addRow("Si réencodage :", row)
        f2.addRow("Fichier existant :", self.existing_combo)
        f2.addRow(self.skip_check)
        f2.addRow(self.open_check)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.export_btn = buttons.addButton("Exporter", QDialogButtonBox.ButtonRole.AcceptRole)
        self.export_btn.setDefault(True)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        for w in (g_scope, g_out, g_enc):
            root.addWidget(w)
        root.addWidget(buttons)

        # --- signaux
        self._fill_presets()
        self.rb_per_cat.toggled.connect(self._fill_presets)
        self.preset_combo.currentIndexChanged.connect(self._preset_chosen)
        self.template_edit.textChanged.connect(self._template_edited)
        for rb in (self.rb_checked, self.rb_current):
            rb.toggled.connect(self._update_preview)
        self.out_edit.textChanged.connect(self._update_preview)
        self._update_preview()

    # ----------------------------------------------------------------- logique
    @property
    def grouping(self) -> str:
        return "single" if self.rb_single.isChecked() else "per_category"

    def _template_key(self) -> str:
        return f"export/template_{self.grouping}"

    def _fill_presets(self) -> None:
        presets = PRESETS_SINGLE if self.grouping == "single" else PRESETS_PER_CATEGORY
        template = self.s.value(self._template_key(), presets[0][1] if self.grouping == "single"
                                else PRESETS_PER_CATEGORY[2][1])
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for label, tpl in presets:
            self.preset_combo.addItem(f"{label}   ({tpl})", tpl)
        self.preset_combo.addItem(CUSTOM, None)
        idx = self.preset_combo.findData(template)
        self.preset_combo.setCurrentIndex(idx if idx >= 0 else self.preset_combo.count() - 1)
        self.preset_combo.blockSignals(False)
        self.template_edit.blockSignals(True)
        self.template_edit.setText(template)
        self.template_edit.blockSignals(False)
        self._update_preview()

    def _preset_chosen(self) -> None:
        tpl = self.preset_combo.currentData()
        if tpl:
            self.template_edit.setText(tpl)
        else:
            self.template_edit.setFocus()

    def _template_edited(self, text: str) -> None:
        idx = self.preset_combo.findData(text)
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(idx if idx >= 0 else self.preset_combo.count() - 1)
        self.preset_combo.blockSignals(False)
        self._update_preview()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Dossier de sortie", self.out_edit.text())
        if d:
            self.out_edit.setText(d)

    def options(self) -> ExportOptions:
        return ExportOptions(
            output_dir=Path(self.out_edit.text().strip() or ".").expanduser(),
            grouping=self.grouping,
            template=self.template_edit.text().strip(),
            scope="current" if self.rb_current.isChecked() else "checked",
            current_category_id=self.current.id if self.current else None,
        )

    def encode_settings(self) -> EncodeSettings:
        return EncodeSettings(
            reencode=self.mode_combo.currentData(),
            bitrate=self.bitrate_combo.currentData(),
            sample_rate=self.rate_combo.currentData(),
            channels=self.channels_combo.currentData(),
            skip_invalid=self.skip_check.isChecked(),
            on_existing=self.existing_combo.currentData(),
        )

    def _update_preview(self) -> None:
        opts = self.options()
        jobs, notes = plan_exports(self.project, opts)
        lines = [f"{opts.output_dir}"]
        for j in jobs:
            rel = j.output.relative_to(opts.output_dir).as_posix()
            lines.append(f"  └ {rel}   [{len(j.tracks)} piste(s), {format_duration(j.estimated_duration)}]")
        if not jobs:
            lines.append("  (rien à exporter)")
        lines += [f"⚠ {n}" for n in notes]
        self.preview.setPlainText("\n".join(lines))
        self.export_btn.setEnabled(bool(jobs) and bool(self.out_edit.text().strip()))

    def _accept(self) -> None:
        s = self.s
        enc = self.encode_settings()
        s.setValue("export/grouping", self.grouping)
        s.setValue(self._template_key(), self.template_edit.text().strip())
        s.setValue("export/output_dir", self.out_edit.text().strip())
        s.setValue("export/reencode", enc.reencode)
        s.setValue("export/bitrate", enc.bitrate)
        s.setValue("export/sample_rate", enc.sample_rate)
        s.setValue("export/channels", enc.channels)
        s.setValue("export/on_existing", enc.on_existing)
        s.setValue("export/skip_invalid", enc.skip_invalid)
        s.setValue("export/open_after", self.open_check.isChecked())
        self.accept()


class SettingsDialog(QDialog):
    """Choix de l'exécutable FFmpeg (vide = détection automatique)."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Paramètres")
        self.setMinimumWidth(560)
        self.s = QSettings()
        self.path_edit = QLineEdit(self.s.value("ffmpeg/path", ""))
        self.path_edit.setPlaceholderText("Détection automatique")
        browse = QPushButton("Parcourir…")
        browse.clicked.connect(self._browse)
        test = QPushButton("Tester")
        test.clicked.connect(self._test)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        row = QHBoxLayout()
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        row.addWidget(test)
        help_ = QLabel("Ordre de recherche : chemin ci-dessus → variable MP3COMPILER_FFMPEG → FFmpeg "
                       "intégré à l'application → dossier vendor/ffmpeg → PATH système → emplacements "
                       "courants (Homebrew, C:\\ffmpeg\\bin…).")
        help_.setWordWrap(True)
        help_.setStyleSheet("color: palette(placeholder-text);")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Exécutable FFmpeg</b>"))
        lay.addLayout(row)
        lay.addWidget(help_)
        lay.addWidget(self.status)
        lay.addStretch()
        lay.addWidget(buttons)
        self._test()

    def _browse(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Choisir l'exécutable FFmpeg")
        if f:
            self.path_edit.setText(f)
            self._test()

    def _test(self) -> None:
        st = check_ffmpeg(self.path_edit.text().strip() or None)
        color = "#2e7d32" if st.ok and st.has_mp3_encoder else "#c62828"
        where = f"<br><small>{st.path}</small>" if st.path else ""
        self.status.setText(f"<span style='color:{color}'>{st.describe()}</span>{where}")

    def _accept(self) -> None:
        self.s.setValue("ffmpeg/path", self.path_edit.text().strip())
        self.accept()
