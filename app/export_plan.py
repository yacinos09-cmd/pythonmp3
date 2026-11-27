"""Planification de l'export : quelles catégories, quels fichiers, quels chemins.

Modèle de chemin (relatif au dossier de sortie, « / » = sous-dossier) :

    {project}   nom du projet
    {category}  nom de la catégorie  (« Compilation » en mode fichier unique)
    {n}         numéro de la catégorie dans le projet, sur 2 chiffres (01, 02…)
    {count}     nombre de pistes exportées
    {date}      date du jour (AAAA-MM-JJ)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.audio import AudioInfo, ExportJob, SourceTrack
from app.models import Category, Project

PRESETS_PER_CATEGORY = [
    ("Fichiers à plat", "{category}.mp3"),
    ("Fichiers numérotés à plat", "{n} - {category}.mp3"),
    ("Un sous-dossier par catégorie", "{category}/{category}.mp3"),
    ("Sous-dossiers numérotés", "{n} - {category}/{category}.mp3"),
    ("Dossier projet › sous-dossiers", "{project}/{n} - {category}/{project} - {category}.mp3"),
]
PRESETS_SINGLE = [
    ("Fichier unique", "{project}.mp3"),
    ("Fichier unique daté", "{project} - {date}.mp3"),
    ("Dans un dossier projet", "{project}/{project} - compilation.mp3"),
]
TOKENS = ("project", "category", "n", "count", "date")

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def sanitize_component(name: str, fallback: str = "sans titre") -> str:
    """Rend un nom utilisable comme fichier/dossier sous Windows, macOS et Linux."""
    s = _INVALID.sub("_", str(name)).strip().rstrip(". ")
    if not s:
        s = fallback
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s[:120]


def render_template(template: str, values: dict[str, str]) -> Path:
    template = (template or "").replace("\\", "/").strip()

    def repl(m: re.Match) -> str:
        key = m.group(1)
        return sanitize_component(values[key]) if key in values else m.group(0)

    rendered = re.sub(r"\{(\w+)\}", repl, template)
    parts = [sanitize_component(p) for p in rendered.split("/")
             if p.strip() and p.strip() not in (".", "..")]
    if not parts:
        parts = ["export.mp3"]
    if not parts[-1].lower().endswith(".mp3"):
        parts[-1] += ".mp3"
    return Path(*parts)


@dataclass
class ExportOptions:
    output_dir: Path
    grouping: str = "per_category"     # "per_category" | "single"
    template: str = "{category}/{category}.mp3"
    scope: str = "checked"             # "checked" | "current"
    current_category_id: str | None = None


def _sources(project: Project, cat: Category) -> list[SourceTrack]:
    out = []
    for t in cat.enabled_tracks():
        info = (AudioInfo(t.duration, t.codec, t.sample_rate, t.channels, t.bitrate)
                if t.duration > 0 else None)
        out.append(SourceTrack(project.resolve(t), t.title, info))
    return out


def selected_categories(project: Project, options: ExportOptions) -> list[Category]:
    if options.scope == "current":
        cat = project.category(options.current_category_id)
        return [cat] if cat else []
    return [c for c in project.categories if c.enabled]


def plan_exports(project: Project, options: ExportOptions) -> tuple[list[ExportJob], list[str]]:
    """Retourne (jobs, remarques). Une catégorie sans piste cochée est ignorée."""
    out_dir = Path(options.output_dir)
    date = datetime.now().strftime("%Y-%m-%d")
    cats = selected_categories(project, options)
    jobs: list[ExportJob] = []
    notes: list[str] = []

    if options.grouping == "single":
        tracks = [s for c in cats for s in _sources(project, c)]
        if tracks:
            rel = render_template(options.template, {
                "project": project.name, "category": "Compilation", "n": "01",
                "count": str(len(tracks)), "date": date})
            jobs.append(ExportJob(out_dir / rel, tracks, title=project.name, album=project.name))
        else:
            notes.append("Aucune piste cochée dans les catégories sélectionnées.")
    else:
        for cat in cats:
            idx = project.categories.index(cat) + 1
            tracks = _sources(project, cat)
            if not tracks:
                notes.append(f"« {cat.name} » ignorée : aucune piste cochée.")
                continue
            rel = render_template(options.template, {
                "project": project.name, "category": cat.name, "n": f"{idx:02d}",
                "count": str(len(tracks)), "date": date})
            jobs.append(ExportJob(out_dir / rel, tracks, title=cat.name, album=project.name))

    # Deux catégories peuvent produire le même chemin : on numérote.
    seen: set[str] = set()
    for job in jobs:
        p, i = job.output, 2
        while str(p).casefold() in seen:
            p = job.output.with_name(f"{job.output.stem} ({i}){job.output.suffix}")
            i += 1
        if p != job.output:
            notes.append(f"Nom en double : « {job.output.name} » renommé en « {p.name} ».")
            job.output = p
        seen.add(str(p).casefold())
    return jobs, notes
