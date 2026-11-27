"""Modèle de données du projet et persistance JSON.

Un projet est un dossier contenant :

    project.json      état complet (catégories, pistes, ordre, cases cochées)
    project.json.bak  copie de la sauvegarde précédente
    media/            fichiers copiés dans le projet (si l'option est active)

Les chemins des pistes situées dans le dossier du projet sont stockés en
relatif (projet portable) ; les autres sont stockés en absolu (référence).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterable, Optional

PROJECT_FILE = "project.json"
MEDIA_DIR = "media"
FORMAT_VERSION = 1

AUDIO_EXTENSIONS = (
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aif", ".aiff",
)


class ProjectError(Exception):
    """Erreur de lecture/écriture d'un projet."""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _known_fields(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


@dataclass
class Track:
    path: str
    title: str = ""
    id: str = field(default_factory=new_id)
    enabled: bool = True
    duration: float = 0.0
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bitrate: int = 0  # kb/s

    def __post_init__(self) -> None:
        if not self.title:
            self.title = Path(self.path).stem

    @property
    def format_label(self) -> str:
        parts = []
        if self.codec:
            parts.append(self.codec.upper())
        if self.bitrate:
            parts.append(f"{self.bitrate} kb/s")
        if self.sample_rate:
            parts.append(f"{self.sample_rate / 1000:g} kHz")
        if self.channels:
            parts.append({1: "mono", 2: "stéréo"}.get(self.channels, f"{self.channels} can."))
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(**_known_fields(cls, data))


@dataclass
class Category:
    name: str
    id: str = field(default_factory=new_id)
    enabled: bool = True
    tracks: list[Track] = field(default_factory=list)

    def enabled_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.enabled]

    def duration(self, only_enabled: bool = False) -> float:
        tracks = self.enabled_tracks() if only_enabled else self.tracks
        return sum(t.duration for t in tracks)

    def track(self, track_id: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.id == track_id), None)

    def reorder(self, ids: Iterable[str]) -> None:
        by_id = {t.id: t for t in self.tracks}
        ordered = [by_id.pop(i) for i in ids if i in by_id]
        self.tracks = ordered + list(by_id.values())  # rien n'est perdu

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "tracks": [t.to_dict() for t in self.tracks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Category":
        return cls(
            name=str(data.get("name", "Catégorie")),
            id=str(data.get("id") or new_id()),
            enabled=bool(data.get("enabled", True)),
            tracks=[Track.from_dict(t) for t in data.get("tracks", [])],
        )


class Project:
    def __init__(self, root: Path | str, name: str | None = None, copy_files: bool = False):
        self.root = Path(root).expanduser().resolve()
        self.name = name or self.root.name
        self.copy_files = copy_files
        self.categories: list[Category] = []

    # ------------------------------------------------------------------ chemins
    @property
    def file(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def media_dir(self) -> Path:
        return self.root / MEDIA_DIR

    def resolve(self, track: Track) -> Path:
        p = Path(track.path)
        return p if p.is_absolute() else self.root / p

    def stored_path(self, path: Path | str) -> str:
        p = Path(path).expanduser().resolve()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    def is_inside(self, path: Path | str) -> bool:
        try:
            Path(path).resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def _unique_media_path(self, filename: str) -> Path:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        target = self.media_dir / filename
        stem, suffix, i = target.stem, target.suffix, 2
        while target.exists():
            target = self.media_dir / f"{stem} ({i}){suffix}"
            i += 1
        return target

    def import_file(self, src: Path | str) -> str:
        """Retourne le chemin à stocker ; copie le fichier si copy_files est actif."""
        src = Path(src).expanduser().resolve()
        if self.copy_files and not self.is_inside(src):
            target = self._unique_media_path(src.name)
            shutil.copy2(src, target)
            return self.stored_path(target)
        return self.stored_path(src)

    def consolidate(self) -> tuple[int, list[str]]:
        """Copie dans media/ toutes les pistes référencées hors du projet."""
        copied, errors = 0, []
        for cat in self.categories:
            for t in cat.tracks:
                src = self.resolve(t)
                if self.is_inside(src):
                    continue
                if not src.is_file():
                    errors.append(f"{t.title} : fichier introuvable ({src})")
                    continue
                try:
                    target = self._unique_media_path(src.name)
                    shutil.copy2(src, target)
                    t.path = self.stored_path(target)
                    copied += 1
                except OSError as exc:
                    errors.append(f"{t.title} : {exc}")
        return copied, errors

    # --------------------------------------------------------------- catégories
    def category(self, cat_id: str | None) -> Optional[Category]:
        return next((c for c in self.categories if c.id == cat_id), None)

    def unique_category_name(self, base: str = "Nouvelle catégorie") -> str:
        names = {c.name.casefold() for c in self.categories}
        if base.casefold() not in names:
            return base
        i = 2
        while f"{base} {i}".casefold() in names:
            i += 1
        return f"{base} {i}"

    def add_category(self, name: str | None = None, index: int | None = None) -> Category:
        cat = Category(name=name or self.unique_category_name())
        if index is None or index < 0 or index > len(self.categories):
            self.categories.append(cat)
        else:
            self.categories.insert(index, cat)
        return cat

    def remove_category(self, cat_id: str) -> None:
        self.categories = [c for c in self.categories if c.id != cat_id]

    def reorder_categories(self, ids: Iterable[str]) -> None:
        by_id = {c.id: c for c in self.categories}
        ordered = [by_id.pop(i) for i in ids if i in by_id]
        self.categories = ordered + list(by_id.values())

    def find_track(self, track_id: str) -> tuple[Optional[Category], Optional[Track]]:
        for c in self.categories:
            t = c.track(track_id)
            if t:
                return c, t
        return None, None

    def missing_tracks(self) -> list[tuple[Category, Track]]:
        return [(c, t) for c in self.categories for t in c.tracks if not self.resolve(t).is_file()]

    # ------------------------------------------------------------- persistance
    def to_dict(self) -> dict:
        return {
            "format_version": FORMAT_VERSION,
            "name": self.name,
            "copy_files": self.copy_files,
            "categories": [c.to_dict() for c in self.categories],
        }

    def save(self) -> None:
        """Écriture atomique (fichier temporaire + os.replace) avec copie .bak."""
        self.root.mkdir(parents=True, exist_ok=True)
        data = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=".project-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if self.file.exists():
                try:
                    shutil.copy2(self.file, self.file.with_name(PROJECT_FILE + ".bak"))
                except OSError:
                    pass
            os.replace(tmp, self.file)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def create(cls, root: Path | str, name: str | None = None, copy_files: bool = False) -> "Project":
        p = cls(root, name, copy_files)
        if p.file.exists():
            raise ProjectError(f"Un projet existe déjà dans {p.root}")
        p.save()
        return p

    @classmethod
    def load(cls, path: Path | str) -> "Project":
        path = Path(path).expanduser()
        file = path / PROJECT_FILE if path.is_dir() else path
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProjectError(f"Projet introuvable : {file}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"Projet illisible ({file}) : {exc}") from exc
        if not isinstance(data, dict) or "categories" not in data:
            raise ProjectError(f"{file} n'est pas un projet MP3 Compiler valide")
        if int(data.get("format_version", 1)) > FORMAT_VERSION:
            raise ProjectError("Ce projet a été créé par une version plus récente de l'application")
        p = cls(file.parent, data.get("name"), bool(data.get("copy_files", False)))
        p.categories = [Category.from_dict(c) for c in data.get("categories", [])]
        return p
