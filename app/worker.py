"""Background queue processor. Runs on a QThread so the GUI never blocks."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from . import storage
from .transcriber import JobSettings, TranscriptionCancelled, TranscriptionError, transcribe_file


class QueueWorker(QObject):
    job_started = Signal(str)                     # job_id
    job_progress = Signal(str, str, float)         # job_id, message, fraction
    job_done = Signal(str, str, int)               # job_id, output_path, char_count
    job_failed = Signal(str, str)                  # job_id, error_message
    job_cancelled = Signal(str)                    # job_id
    queue_finished = Signal()

    def __init__(self):
        super().__init__()
        self._jobs: list[dict] = []  # {id, source_path, output_path}
        self._stop_requested = False
        self._current_job_id: str | None = None

    def set_jobs(self, jobs: list[dict]):
        self._jobs = list(jobs)

    def request_stop(self):
        self._stop_requested = True

    @Slot()
    def run(self):
        self._stop_requested = False
        settings_dict = storage.load_settings()
        api_key = storage.get_api_key()

        job_settings = JobSettings(
            api_key=api_key,
            organization_id=settings_dict.get("organization_id", ""),
            project_id=settings_dict.get("project_id", ""),
            model=settings_dict.get("model", "whisper-1"),
            language=settings_dict.get("language", ""),
            chunk_minutes=int(settings_dict.get("chunk_minutes", 15) or 15),
        )

        for job in self._jobs:
            if self._stop_requested:
                break
            job_id = job["id"]
            self._current_job_id = job_id
            self.job_started.emit(job_id)

            if not api_key:
                self.job_failed.emit(job_id, "לא הוגדר מפתח API. עברו למסך ההגדרות.")
                continue

            def progress_cb(msg: str, frac: float, jid=job_id):
                self.job_progress.emit(jid, msg, frac)

            def stop_cb():
                return self._stop_requested

            try:
                text = transcribe_file(
                    job["source_path"],
                    job["output_path"],
                    job_settings,
                    on_progress=progress_cb,
                    should_stop=stop_cb,
                )
                self.job_done.emit(job_id, job["output_path"], len(text))
            except TranscriptionCancelled:
                self.job_cancelled.emit(job_id)
                break
            except TranscriptionError as e:
                self.job_failed.emit(job_id, str(e))
            except Exception as e:  # noqa: BLE001 - surface unexpected errors in the UI, don't crash
                self.job_failed.emit(job_id, f"שגיאה לא צפויה: {e}")

        self._current_job_id = None
        self.queue_finished.emit()
