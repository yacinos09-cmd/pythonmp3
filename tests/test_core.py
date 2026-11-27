"""Tests du cœur (sans interface). Lancer : python -m pytest -q"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audio import (AudioError, EncodeSettings, ExportJob, Exporter, SourceTrack,  # noqa: E402
                       check_ffmpeg, probe)
from app.export_plan import ExportOptions, plan_exports, render_template, sanitize_component  # noqa: E402
from app.models import Project, Track  # noqa: E402

STATUS = check_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(not (STATUS.ok and STATUS.has_mp3_encoder),
                                  reason="FFmpeg + libmp3lame requis")


def make_tone(path: Path, seconds: float, rate: int = 44100, ch: int = 2, freq: int = 440) -> Path:
    subprocess.run([STATUS.path, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate={rate}",
                    "-ac", str(ch), "-c:a", "libmp3lame", "-b:a", "128k", str(path)], check=True)
    return path


# ------------------------------------------------------------------ modèle
def test_project_roundtrip(tmp_path):
    p = Project.create(tmp_path / "Projet", "Démo")
    c = p.add_category("Intro")
    c.tracks.append(Track(path=str(tmp_path / "a.mp3"), duration=3.5))
    p.add_category("Ambiances")
    p.save()
    q = Project.load(tmp_path / "Projet")
    assert q.name == "Démo"
    assert [c.name for c in q.categories] == ["Intro", "Ambiances"]
    assert q.categories[0].tracks[0].duration == 3.5
    q.reorder_categories([q.categories[1].id, q.categories[0].id])
    assert q.categories[0].name == "Ambiances"


def test_copy_files_relative(tmp_path):
    src = tmp_path / "src.mp3"
    src.write_bytes(b"x")
    p = Project.create(tmp_path / "P", copy_files=True)
    stored = p.import_file(src)
    assert stored == "media/src.mp3"
    assert (p.root / stored).is_file()
    assert p.import_file(src) == "media/src (2).mp3"


# -------------------------------------------------------------- templates
def test_templates():
    v = {"project": "Mon/Projet", "category": "Chapitre: 1", "n": "03", "count": "2", "date": "d"}
    assert render_template("{n} - {category}/{category}.mp3", v).as_posix() == \
        "03 - Chapitre_ 1/Chapitre_ 1.mp3"
    assert render_template("{project}", v).as_posix() == "Mon_Projet.mp3"
    assert render_template("../../{category}", v).as_posix() == "Chapitre_ 1.mp3"
    assert sanitize_component("CON") == "_CON"


def test_plan_dedupes_and_skips(tmp_path):
    p = Project(tmp_path)
    a, b = p.add_category("Spots"), p.add_category("Spots")
    p.add_category("Vide")
    for cat in (a, b):
        cat.tracks.append(Track(path=str(tmp_path / "x.mp3"), duration=1))
    jobs, notes = plan_exports(p, ExportOptions(tmp_path / "out", template="{category}.mp3"))
    assert [j.output.name for j in jobs] == ["Spots.mp3", "Spots (2).mp3"]
    assert any("Vide" in n for n in notes)
    jobs, _ = plan_exports(p, ExportOptions(tmp_path / "out", grouping="single",
                                            template="{project}.mp3"))
    assert len(jobs) == 1 and len(jobs[0].tracks) == 2


# ------------------------------------------------------------------- audio
@needs_ffmpeg
def test_probe_and_corrupt(tmp_path):
    info = probe(make_tone(tmp_path / "a.mp3", 2))
    assert info.codec == "mp3" and abs(info.duration - 2) < 0.15 and info.sample_rate == 44100
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"\x00" * 100)
    with pytest.raises(AudioError):
        probe(bad)


@needs_ffmpeg
def test_export_copy_mode(tmp_path):
    a = make_tone(tmp_path / "a.mp3", 2)
    b = make_tone(tmp_path / "b.mp3", 3, freq=660)
    job = ExportJob(tmp_path / "out" / "cat.mp3", [SourceTrack(a, "A"), SourceTrack(b, "B")], "Cat")
    logs, prog = [], []
    res = Exporter(STATUS.path, EncodeSettings(), log=lambda l, m: logs.append(m)).export(job, prog.append)
    assert res.status == "ok" and res.method == "copie directe"
    assert abs(probe(res.output).duration - 5) < 0.3
    assert prog[-1] == 1.0
    assert not list((tmp_path / "out").glob("*.part"))


@needs_ffmpeg
def test_export_mixed_rates_reencode_and_skip_missing(tmp_path):
    a = make_tone(tmp_path / "a.mp3", 2, rate=44100)
    b = make_tone(tmp_path / "b.mp3", 2, rate=48000, ch=1)
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"\x00" * 100)
    job = ExportJob(tmp_path / "mix.mp3", [SourceTrack(a), SourceTrack(tmp_path / "missing.mp3"),
                                           SourceTrack(bad), SourceTrack(b)])
    s = EncodeSettings(bitrate=320, sample_rate=48000)
    res = Exporter(STATUS.path, s).export(job)
    assert res.method == "réencodage" and len(res.skipped_tracks) == 2
    info = probe(res.output)
    assert info.sample_rate == 48000 and info.channels == 2 and abs(info.duration - 4) < 0.3
    assert 300 <= info.bitrate <= 330

    with pytest.raises(AudioError):
        Exporter(STATUS.path, EncodeSettings(skip_invalid=False)).export(job)


@needs_ffmpeg
def test_existing_file_policies(tmp_path):
    a = make_tone(tmp_path / "a.mp3", 1)
    out = tmp_path / "o.mp3"
    out.write_bytes(b"old")
    job = ExportJob(out, [SourceTrack(a)])
    assert Exporter(STATUS.path, EncodeSettings(on_existing="skip")).export(job).status == "skipped"
    r = Exporter(STATUS.path, EncodeSettings(on_existing="rename")).export(job)
    assert r.output.name == "o (2).mp3" and out.read_bytes() == b"old"
