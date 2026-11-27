"""Traitement audio : analyse des fichiers, détection de FFmpeg et export.

Ce module ne dépend pas de Qt : il est utilisable (et testable) seul.

Stratégie d'export d'une liste de pistes vers un MP3 unique :

* **Copie directe** (mode « auto ») — si toutes les pistes sont des MP3 avec la
  même fréquence d'échantillonnage et le même nombre de canaux, les trames MP3
  sont concaténées sans réencodage (demuxer concat + ``-c:a copy``) :
  instantané et sans perte.
* **Réencodage** — sinon (formats/fréquences mixtes, ou réencodage forcé) :
  chaque piste est décodée en PCM WAV temporaire au format cible, puis le tout
  est encodé en une seule passe libmp3lame au débit choisi. Cette méthode est
  sans silence parasite entre les pistes (encodage unique).
* Si la copie directe échoue (fichier atypique), bascule automatique sur le
  réencodage.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

LogFn = Callable[[str, str], None]        # (niveau, message) ; niveau: info|success|warning|error
ProgressFn = Callable[[float], None]      # fraction 0.0 → 1.0

FFMPEG_ENV = "MP3COMPILER_FFMPEG"
DECODE_SHARE = 0.35  # part de la barre de progression consacrée au décodage (réencodage)


class AudioError(Exception):
    """Erreur de lecture d'un fichier ou d'exécution de FFmpeg."""


class ExportCancelled(Exception):
    """L'utilisateur a annulé l'export."""


# --------------------------------------------------------------------- analyse
@dataclass(frozen=True)
class AudioInfo:
    duration: float
    codec: str
    sample_rate: int
    channels: int
    bitrate: int  # kb/s


def probe(path: Path | str) -> AudioInfo:
    """Lit les caractéristiques d'un fichier audio (rapide, via mutagen)."""
    try:
        import mutagen
        from mutagen.mp3 import MP3
    except ImportError as exc:  # pragma: no cover
        raise AudioError("Le module Python « mutagen » est requis") from exc

    p = Path(path)
    if not p.is_file():
        raise AudioError("fichier introuvable")
    try:
        f = mutagen.File(p)
    except Exception as exc:  # mutagen lève des exceptions variées
        raise AudioError(f"fichier illisible ou corrompu ({exc})") from exc
    if f is None or getattr(f, "info", None) is None:
        raise AudioError("format audio non reconnu ou fichier corrompu")

    info = f.info
    if isinstance(f, MP3):
        layer = getattr(info, "layer", 3)
        codec = "mp3" if layer == 3 else f"mp{layer}"
    else:
        codec = type(f).__name__.lower()  # flac, wave, mp4, oggvorbis…
    duration = float(getattr(info, "length", 0.0) or 0.0)
    if duration <= 0:
        raise AudioError("durée nulle : fichier vide ou corrompu")
    return AudioInfo(
        duration=duration,
        codec=codec,
        sample_rate=int(getattr(info, "sample_rate", 0) or 0),
        channels=int(getattr(info, "channels", 0) or 0),
        bitrate=int(getattr(info, "bitrate", 0) or 0) // 1000,
    )


# ---------------------------------------------------------------- FFmpeg : où ?
def _exe(name: str = "ffmpeg") -> str:
    return name + ".exe" if os.name == "nt" else name


def _app_dirs() -> list[Path]:
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)          # bundle PyInstaller
    if meipass:
        dirs.append(Path(meipass))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        dirs.append(exe_dir)
        if sys.platform == "darwin":                  # .app/Contents/MacOS → Resources
            dirs.append(exe_dir.parent / "Resources")
            dirs.append(exe_dir.parent / "Frameworks")
    dirs.append(Path(__file__).resolve().parent.parent)  # racine des sources
    return dirs


def ffmpeg_candidates(custom: str | None = None) -> Iterator[Path]:
    exe = _exe()
    if custom:
        yield Path(custom).expanduser()
    env = os.environ.get(FFMPEG_ENV)
    if env:
        yield Path(env).expanduser()
    for d in _app_dirs():
        yield d / "ffmpeg" / exe
        yield d / "vendor" / "ffmpeg" / exe
        yield d / exe
    found = shutil.which(exe)
    if found:
        yield Path(found)
    # Une .app lancée depuis le Finder n'hérite pas du PATH du terminal.
    if sys.platform == "darwin":
        extra = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
    elif os.name == "nt":
        extra = [r"C:\ffmpeg\bin",
                 os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin"),
                 os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links")]
    else:
        extra = ["/usr/bin", "/usr/local/bin"]
    for e in extra:
        yield Path(e) / exe
    try:
        import imageio_ffmpeg  # type: ignore
        yield Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass


def find_ffmpeg(custom: str | None = None) -> Optional[str]:
    for c in ffmpeg_candidates(custom):
        try:
            if c.is_file() and os.access(c, os.X_OK):
                return str(c)
        except OSError:
            continue
    return None


def _popen_kwargs() -> dict:
    # Évite l'ouverture d'une fenêtre console sous Windows (application --windowed).
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


@dataclass
class FFmpegStatus:
    path: Optional[str]
    version: str = ""
    has_mp3_encoder: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.path) and not self.error

    def describe(self) -> str:
        if not self.path:
            return "FFmpeg introuvable"
        if self.error:
            return f"FFmpeg inutilisable : {self.error}"
        enc = "libmp3lame OK" if self.has_mp3_encoder else "⚠ sans libmp3lame (réencodage impossible)"
        return f"FFmpeg {self.version} — {enc}"


def check_ffmpeg(custom: str | None = None) -> FFmpegStatus:
    path = find_ffmpeg(custom)
    if not path:
        return FFmpegStatus(None, error="introuvable")
    try:
        ver = subprocess.run([path, "-hide_banner", "-version"], capture_output=True, text=True,
                             timeout=15, errors="replace", **_popen_kwargs())
        first = ver.stdout.splitlines()[0] if ver.stdout else ""
        m = re.search(r"version\s+(\S+)", first)
        enc = subprocess.run([path, "-hide_banner", "-encoders"], capture_output=True, text=True,
                             timeout=15, errors="replace", **_popen_kwargs())
        return FFmpegStatus(path, m.group(1) if m else first.strip(), "libmp3lame" in enc.stdout)
    except (OSError, subprocess.SubprocessError) as exc:
        return FFmpegStatus(path, error=str(exc))


# ------------------------------------------------------------- FFmpeg : lancer
def run_ffmpeg(ffmpeg: str, args: list[str], *, duration: float = 0.0,
               progress: ProgressFn | None = None,
               cancel: threading.Event | None = None) -> list[str]:
    """Lance FFmpeg, suit la progression (-progress) et gère l'annulation.

    Retourne les avertissements émis sur stderr ; lève AudioError en cas d'échec.
    """
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
           "-progress", "pipe:1", "-nostats", *args]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", **_popen_kwargs())
    except OSError as exc:
        raise AudioError(f"impossible de lancer FFmpeg : {exc}") from exc

    err_lines: list[str] = []

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                err_lines.append(line)

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                raise ExportCancelled()
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and progress and duration > 0:
                try:
                    progress(max(0.0, min(1.0, int(value) / 1e6 / duration)))
                except ValueError:  # "N/A"
                    pass
        rc = proc.wait()
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        raise
    finally:
        t.join(timeout=5)

    if cancel is not None and cancel.is_set():
        raise ExportCancelled()
    if rc != 0:
        detail = "\n".join(err_lines[-6:]) or f"code de sortie {rc}"
        raise AudioError(f"FFmpeg a échoué : {detail}")
    if progress:
        progress(1.0)
    return err_lines


# ---------------------------------------------------------------------- export
@dataclass
class SourceTrack:
    path: Path
    title: str = ""
    info: Optional[AudioInfo] = None


@dataclass
class ExportJob:
    output: Path
    tracks: list[SourceTrack]
    title: str = ""
    album: str = ""

    @property
    def estimated_duration(self) -> float:
        return sum(t.info.duration for t in self.tracks if t.info)


@dataclass
class EncodeSettings:
    reencode: str = "auto"         # "auto" (copie si possible) | "always"
    bitrate: int = 192             # kb/s (réencodage)
    sample_rate: int = 44100       # Hz (réencodage)
    channels: int = 2              # (réencodage)
    skip_invalid: bool = True      # ignorer pistes manquantes/corrompues
    on_existing: str = "replace"   # "replace" | "rename" | "skip"


@dataclass
class JobResult:
    output: Optional[Path]
    status: str                    # ok | skipped | error | cancelled
    message: str = ""
    method: str = ""
    tracks_used: int = 0
    skipped_tracks: list[str] = field(default_factory=list)
    duration: float = 0.0


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def write_concat_list(paths: list[Path], list_path: Path) -> None:
    with open(list_path, "w", encoding="utf-8") as fh:
        fh.write("ffconcat version 1.0\n")
        for p in paths:
            s = Path(p).resolve().as_posix().replace("'", "'\\''")
            fh.write(f"file '{s}'\n")


class Exporter:
    def __init__(self, ffmpeg: str, settings: EncodeSettings,
                 log: LogFn | None = None, cancel: threading.Event | None = None):
        self.ffmpeg = ffmpeg
        self.settings = settings
        self._log_fn = log or (lambda level, msg: None)
        self.cancel = cancel or threading.Event()

    def log(self, level: str, msg: str) -> None:
        self._log_fn(level, msg)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise ExportCancelled()

    # -- étapes ---------------------------------------------------------------
    def validate(self, job: ExportJob) -> tuple[list[SourceTrack], list[str]]:
        valid, problems = [], []
        for t in job.tracks:
            self._check_cancel()
            try:
                info = probe(t.path)
            except AudioError as exc:
                msg = f"{t.title or t.path.name} : {exc} ({t.path})"
                if not self.settings.skip_invalid:
                    raise AudioError(f"piste invalide — {msg}") from exc
                self.log("warning", f"Piste ignorée — {msg}")
                problems.append(msg)
                continue
            valid.append(SourceTrack(t.path, t.title, info))
        if not valid:
            raise AudioError("aucune piste valide à exporter")
        return valid, problems

    @staticmethod
    def can_stream_copy(tracks: list[SourceTrack]) -> tuple[bool, str]:
        infos = [t.info for t in tracks if t.info]
        if len(infos) != len(tracks):
            return False, "informations audio incomplètes"
        non_mp3 = [t.path.name for t in tracks if t.info and t.info.codec != "mp3"]
        if non_mp3:
            return False, f"fichiers non-MP3 ({', '.join(non_mp3[:3])}…)"
        rates = {i.sample_rate for i in infos}
        if len(rates) > 1:
            return False, f"fréquences d'échantillonnage différentes ({', '.join(map(str, sorted(rates)))} Hz)"
        chans = {i.channels for i in infos}
        if len(chans) > 1:
            return False, "mélange de pistes mono et stéréo"
        return True, ""

    def _metadata_args(self, job: ExportJob) -> list[str]:
        args = ["-map_metadata", "-1", "-id3v2_version", "3"]
        if job.title:
            args += ["-metadata", f"title={job.title}"]
        if job.album:
            args += ["-metadata", f"album={job.album}"]
        return args

    def _concat_copy(self, tracks: list[SourceTrack], out: Path, job: ExportJob,
                     progress: ProgressFn) -> None:
        total = sum(t.info.duration for t in tracks if t.info)
        with tempfile.TemporaryDirectory(prefix="mp3compiler_") as td:
            lst = Path(td) / "list.txt"
            write_concat_list([t.path for t in tracks], lst)
            warnings = run_ffmpeg(self.ffmpeg, [
                "-f", "concat", "-safe", "0", "-i", str(lst),
                "-map", "0:a:0", "-c:a", "copy", *self._metadata_args(job),
                "-f", "mp3", str(out),
            ], duration=total, progress=progress, cancel=self.cancel)
        for w in warnings[-3:]:
            self.log("warning", f"FFmpeg : {w}")

    def _reencode(self, tracks: list[SourceTrack], out: Path, job: ExportJob,
                  progress: ProgressFn) -> list[str]:
        s = self.settings
        skipped: list[str] = []
        total = sum(t.info.duration for t in tracks if t.info) or 1.0
        with tempfile.TemporaryDirectory(prefix="mp3compiler_") as td:
            wavs: list[Path] = []
            used, done = 0.0, 0.0
            for i, t in enumerate(tracks):
                self._check_cancel()
                d = t.info.duration if t.info else 0.0
                wav = Path(td) / f"{i:05d}.wav"
                self.log("info", f"  Décodage {i + 1}/{len(tracks)} : {t.path.name}")
                try:
                    run_ffmpeg(self.ffmpeg, [
                        "-i", str(t.path), "-vn", "-map", "0:a:0",
                        "-ac", str(s.channels), "-ar", str(s.sample_rate),
                        "-c:a", "pcm_s16le", "-rf64", "auto", "-f", "wav", str(wav),
                    ], duration=d, cancel=self.cancel,
                        progress=lambda f, base=done, d=d: progress(DECODE_SHARE * (base + f * d) / total))
                except AudioError as exc:
                    msg = f"{t.title or t.path.name} : {exc}"
                    if not s.skip_invalid:
                        raise AudioError(f"décodage impossible — {msg}") from exc
                    self.log("warning", f"Piste ignorée — {msg}")
                    skipped.append(msg)
                    done += d
                    continue
                wavs.append(wav)
                used += d
                done += d
            if not wavs:
                raise AudioError("aucune piste n'a pu être décodée")
            self._check_cancel()
            self.log("info", f"  Encodage MP3 {s.bitrate} kb/s, {s.sample_rate} Hz…")
            lst = Path(td) / "list.txt"
            write_concat_list(wavs, lst)
            run_ffmpeg(self.ffmpeg, [
                "-f", "concat", "-safe", "0", "-i", str(lst),
                "-c:a", "libmp3lame", "-b:a", f"{s.bitrate}k",
                "-ar", str(s.sample_rate), "-ac", str(s.channels),
                *self._metadata_args(job), "-f", "mp3", str(out),
            ], duration=used, cancel=self.cancel,
                progress=lambda f: progress(DECODE_SHARE + (1 - DECODE_SHARE) * f))
        return skipped

    # -- point d'entrée -------------------------------------------------------
    def export(self, job: ExportJob, progress: ProgressFn | None = None) -> JobResult:
        progress = progress or (lambda f: None)
        out = Path(job.output)
        if out.exists():
            if self.settings.on_existing == "skip":
                self.log("warning", f"Existe déjà, ignoré : {out}")
                return JobResult(out, "skipped", "le fichier existe déjà")
            if self.settings.on_existing == "rename":
                out = unique_path(out)

        tracks, skipped = self.validate(job)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part")
        try:
            if self.settings.reencode == "always":
                ok, reason = False, "réencodage demandé"
            else:
                ok, reason = self.can_stream_copy(tracks)
            if ok:
                self.log("info", "  Concaténation rapide (sans réencodage)…")
                try:
                    self._concat_copy(tracks, tmp, job, progress)
                    method = "copie directe"
                except AudioError as exc:
                    self.log("warning", f"  Copie directe impossible ({exc}) → réencodage")
                    _safe_unlink(tmp)
                    skipped += self._reencode(tracks, tmp, job, progress)
                    method = "réencodage"
            else:
                self.log("info", f"  Réencodage nécessaire : {reason}")
                skipped += self._reencode(tracks, tmp, job, progress)
                method = "réencodage"
            os.replace(tmp, out)
        except BaseException:
            _safe_unlink(tmp)
            raise
        return JobResult(out, "ok", method=method, tracks_used=len(tracks) - len(skipped),
                         skipped_tracks=skipped,
                         duration=sum(t.info.duration for t in tracks if t.info))


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass
