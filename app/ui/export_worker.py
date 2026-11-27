"""Exécution de l'export dans un thread séparé (l'interface reste réactive)."""
from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QObject, Signal, Slot

from app.audio import AudioError, EncodeSettings, ExportCancelled, ExportJob, Exporter, JobResult
from app.ui.widgets import format_duration


class ExportWorker(QObject):
    progress = Signal(float)              # progression globale 0 → 1
    jobStarted = Signal(int, int, str)    # index (1-based), total, libellé
    log = Signal(str, str)                # niveau, message
    finished = Signal(object)             # list[JobResult]

    def __init__(self, ffmpeg: str, jobs: list[ExportJob], settings: EncodeSettings):
        super().__init__()
        self.ffmpeg, self.jobs, self.settings = ffmpeg, jobs, settings
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        results: list[JobResult] = []
        weights = [max(j.estimated_duration, 1.0) for j in self.jobs]
        total = sum(weights) or 1.0
        done = 0.0
        exporter = Exporter(self.ffmpeg, self.settings,
                            log=lambda lvl, msg: self.log.emit(lvl, msg), cancel=self._cancel)
        n = len(self.jobs)
        for i, job in enumerate(self.jobs):
            if self._cancel.is_set():
                break
            self.jobStarted.emit(i + 1, n, job.title or job.output.name)
            self.log.emit("info", f"▶ [{i + 1}/{n}] « {job.title} » — {len(job.tracks)} piste(s) → {job.output}")
            try:
                res = exporter.export(
                    job, progress=lambda f, base=done, w=weights[i]: self.progress.emit((base + f * w) / total))
                results.append(res)
                if res.status == "ok":
                    extra = f", {len(res.skipped_tracks)} ignorée(s)" if res.skipped_tracks else ""
                    self.log.emit("success", f"✔ {res.output.name} ({format_duration(res.duration)}, "
                                             f"{res.method}{extra})")
            except ExportCancelled:
                results.append(JobResult(job.output, "cancelled", "annulé"))
                self.log.emit("warning", "Export annulé par l'utilisateur.")
                break
            except AudioError as exc:
                results.append(JobResult(job.output, "error", str(exc)))
                self.log.emit("error", f"✘ « {job.title} » : {exc}")
            except Exception as exc:  # erreur inattendue : on continue avec les suivants
                results.append(JobResult(job.output, "error", str(exc)))
                self.log.emit("error", f"✘ « {job.title} » : erreur inattendue {exc!r}")
                self.log.emit("error", traceback.format_exc(limit=3))
            done += weights[i]
            self.progress.emit(done / total)
        self.finished.emit(results)
