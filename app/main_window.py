from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import storage
from .worker import QueueWorker

AUDIO_EXTENSIONS = (
    ".m4a", ".mp3", ".wav", ".mp4", ".mov", ".mkv", ".webm", ".ogg", ".oga",
    ".flac", ".wma", ".aac", ".mpeg", ".mpga",
)

STATUS_WAITING = "ממתין"
STATUS_RUNNING = "מתמלל..."
STATUS_DONE = "הושלם"
STATUS_ERROR = "שגיאה"
STATUS_CANCELLED = "בוטל"

COL_NAME, COL_DURATION, COL_STATUS, COL_PROGRESS, COL_ACTIONS = range(5)


def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}TB"


def unique_output_path(source_path: str, output_folder: str) -> str:
    src = Path(source_path)
    folder = Path(output_folder) if output_folder else src.parent
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{src.stem}_transcript"
    candidate = folder / f"{base}.txt"
    n = 1
    while candidate.exists():
        candidate = folder / f"{base}_{n}.txt"
        n += 1
    return str(candidate)


class QueueTab(QWidget):
    def __init__(self, main_window: "MainWindow"):
        super().__init__()
        self.main_window = main_window
        self.jobs: dict[str, dict] = {}  # job_id -> {source_path, output_path, row}
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.add_btn = QPushButton("+ הוסף קבצים")
        self.add_btn.clicked.connect(self.pick_files)
        self.start_btn = QPushButton("▶ התחל תמלול")
        self.start_btn.clicked.connect(self.start_queue)
        self.stop_btn = QPushButton("⏹ עצור")
        self.stop_btn.clicked.connect(self.stop_queue)
        self.stop_btn.setEnabled(False)
        self.clear_btn = QPushButton("נקה תור")
        self.clear_btn.clicked.connect(self.clear_finished)
        toolbar.addWidget(self.add_btn)
        toolbar.addWidget(self.start_btn)
        toolbar.addWidget(self.stop_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(self.clear_btn)
        layout.addLayout(toolbar)

        hint = QLabel("גררו לכאן קבצי אודיו/וידאו, או השתמשו בכפתור “הוסף קבצים”")
        hint.setObjectName("hint")
        layout.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["קובץ", "גודל", "סטטוס", "התקדמות", "פעולות"])
        self.table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_PROGRESS, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_PROGRESS, 160)
        self.table.setColumnWidth(COL_ACTIONS, 190)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(110)
        self.log.setObjectName("log")
        layout.addWidget(self.log)

        self.thread: QThread | None = None
        self.worker: QueueWorker | None = None

    # --- drag & drop -----------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        self.add_files(paths)

    # --- adding files ------------------------------------------------------
    def pick_files(self):
        settings = storage.load_settings()
        start_dir = settings.get("last_open_dir") or str(Path.home())
        filt = "קבצי אודיו/וידאו (*" + " *".join(AUDIO_EXTENSIONS) + ");;כל הקבצים (*)"
        paths, _ = QFileDialog.getOpenFileNames(self, "בחרו קבצים לתמלול", start_dir, filt)
        if paths:
            settings["last_open_dir"] = str(Path(paths[0]).parent)
            storage.save_settings(settings)
        self.add_files(paths)

    def add_files(self, paths: list[str]):
        added = 0
        for p in paths:
            if not p or not os.path.isfile(p):
                continue
            if Path(p).suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            self._add_row(p)
            added += 1
        if added == 0 and paths:
            QMessageBox.information(self, "אין קבצים מתאימים",
                                     "לא נמצאו קבצי אודיו/וידאו נתמכים ברשימה שנבחרה.")

    def _add_row(self, source_path: str):
        job_id = str(uuid.uuid4())
        row = self.table.rowCount()
        self.table.insertRow(row)

        name_item = QTableWidgetItem(Path(source_path).name)
        name_item.setToolTip(source_path)
        self.table.setItem(row, COL_NAME, name_item)

        try:
            size = os.path.getsize(source_path)
        except OSError:
            size = 0
        self.table.setItem(row, COL_DURATION, QTableWidgetItem(human_size(size)))

        self.table.setItem(row, COL_STATUS, QTableWidgetItem(STATUS_WAITING))

        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(0)
        self.table.setCellWidget(row, COL_PROGRESS, progress)

        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        remove_btn = QPushButton("הסר")
        remove_btn.clicked.connect(lambda: self._remove_job(job_id))
        actions_layout.addWidget(remove_btn)
        self.table.setCellWidget(row, COL_ACTIONS, actions)

        self.jobs[job_id] = {"source_path": source_path, "row": row}

    def _remove_job(self, job_id: str):
        job = self.jobs.get(job_id)
        if not job:
            return
        row = job["row"]
        self.table.removeRow(row)
        del self.jobs[job_id]
        for other in self.jobs.values():
            if other["row"] > row:
                other["row"] -= 1

    def clear_finished(self):
        to_remove = [jid for jid, j in self.jobs.items()
                     if self.table.item(j["row"], COL_STATUS).text() in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED)]
        for jid in to_remove:
            self._remove_job(jid)

    def _row_for(self, job_id: str) -> int | None:
        job = self.jobs.get(job_id)
        return job["row"] if job else None

    # --- running the queue --------------------------------------------------
    def start_queue(self):
        api_key = storage.get_api_key()
        if not api_key:
            QMessageBox.warning(self, "חסר מפתח API",
                                 "יש להזין מפתח API של OpenAI במסך ההגדרות לפני תמלול.")
            self.main_window.tabs.setCurrentWidget(self.main_window.settings_tab)
            return

        pending = [jid for jid, j in self.jobs.items()
                   if self.table.item(j["row"], COL_STATUS).text() == STATUS_WAITING]
        if not pending:
            QMessageBox.information(self, "אין מה לתמלל", "אין קבצים ממתינים בתור.")
            return

        settings = storage.load_settings()
        job_list = []
        for jid in pending:
            source_path = self.jobs[jid]["source_path"]
            output_path = unique_output_path(source_path, settings.get("output_folder", ""))
            self.jobs[jid]["output_path"] = output_path
            job_list.append({"id": jid, "source_path": source_path, "output_path": output_path})

        self.add_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._log("מתחיל תמלול תור...")

        self.thread = QThread()
        self.worker = QueueWorker()
        self.worker.set_jobs(job_list)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.job_started.connect(self._on_job_started)
        self.worker.job_progress.connect(self._on_job_progress)
        self.worker.job_done.connect(self._on_job_done)
        self.worker.job_failed.connect(self._on_job_failed)
        self.worker.job_cancelled.connect(self._on_job_cancelled)
        self.worker.queue_finished.connect(self._on_queue_finished)
        self.worker.queue_finished.connect(self.thread.quit)
        self.thread.start()

    def stop_queue(self):
        if self.worker:
            self.worker.request_stop()
            self._log("מבקש לעצור את התור לאחר סיום החלק הנוכחי...")
            self.stop_btn.setEnabled(False)

    def _on_job_started(self, job_id: str):
        row = self._row_for(job_id)
        if row is None:
            return
        self.table.item(row, COL_STATUS).setText(STATUS_RUNNING)
        self._log(f"מתחיל: {Path(self.jobs[job_id]['source_path']).name}")

    def _on_job_progress(self, job_id: str, message: str, fraction: float):
        row = self._row_for(job_id)
        if row is None:
            return
        bar: QProgressBar = self.table.cellWidget(row, COL_PROGRESS)
        bar.setValue(int(fraction * 100))
        bar.setFormat(message)

    def _on_job_done(self, job_id: str, output_path: str, char_count: int):
        row = self._row_for(job_id)
        job = self.jobs.get(job_id)
        if row is not None:
            self.table.item(row, COL_STATUS).setText(STATUS_DONE)
            bar: QProgressBar = self.table.cellWidget(row, COL_PROGRESS)
            bar.setValue(100)
            bar.setFormat("הושלם")
            self._add_result_actions(row, output_path)
        if job:
            self._log(f"הושלם: {Path(job['source_path']).name} → {output_path}")
            storage.add_history_item(storage.HistoryItem.new(
                source_path=job["source_path"], output_path=output_path,
                duration_sec=0.0, status="done", chars=char_count,
            ))
            self.main_window.history_tab.reload()

    def _on_job_failed(self, job_id: str, error_message: str):
        row = self._row_for(job_id)
        job = self.jobs.get(job_id)
        if row is not None:
            self.table.item(row, COL_STATUS).setText(STATUS_ERROR)
            self.table.item(row, COL_STATUS).setToolTip(error_message)
            bar: QProgressBar = self.table.cellWidget(row, COL_PROGRESS)
            bar.setFormat("שגיאה")
        if job:
            self._log(f"שגיאה ב-{Path(job['source_path']).name}: {error_message}")
            storage.add_history_item(storage.HistoryItem.new(
                source_path=job["source_path"], output_path="",
                duration_sec=0.0, status="error", error=error_message,
            ))
            self.main_window.history_tab.reload()

    def _on_job_cancelled(self, job_id: str):
        row = self._row_for(job_id)
        if row is not None:
            self.table.item(row, COL_STATUS).setText(STATUS_CANCELLED)

    def _on_queue_finished(self):
        self.add_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._log("התור הסתיים.")
        # Mark any still-waiting rows as cancelled if a stop was requested mid-run.
        if self.worker and self.worker._stop_requested:
            for jid, j in self.jobs.items():
                item = self.table.item(j["row"], COL_STATUS)
                if item and item.text() == STATUS_WAITING:
                    item.setText(STATUS_CANCELLED)
        self.worker = None
        self.thread = None

    def _add_result_actions(self, row: int, output_path: str):
        actions = QWidget()
        layout = QHBoxLayout(actions)
        layout.setContentsMargins(0, 0, 0, 0)
        open_btn = QPushButton("פתח קובץ")
        open_btn.clicked.connect(lambda: open_path(output_path))
        folder_btn = QPushButton("פתח תיקייה")
        folder_btn.clicked.connect(lambda: reveal_in_folder(output_path))
        layout.addWidget(open_btn)
        layout.addWidget(folder_btn)
        self.table.setCellWidget(row, COL_ACTIONS, actions)

    def _log(self, text: str):
        self.log.append(text)


def open_path(path: str):
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def reveal_in_folder(path: str):
    folder = str(Path(path).parent)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


class HistoryTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("סינון לפי שם קובץ...")
        self.filter_edit.textChanged.connect(self.reload)
        clear_btn = QPushButton("נקה היסטוריה")
        clear_btn.clicked.connect(self.clear_history)
        toolbar.addWidget(self.filter_edit, 1)
        toolbar.addWidget(clear_btn)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["תאריך", "קובץ מקור", "סטטוס", "תווים", "פעולות"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)

        self.reload()

    def reload(self):
        items = storage.load_history()
        query = self.filter_edit.text().strip().lower()
        if query:
            items = [it for it in items if query in Path(it["source_path"]).name.lower()]

        self.table.setRowCount(0)
        for it in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(it["timestamp"].replace("T", " ")))
            name_item = QTableWidgetItem(Path(it["source_path"]).name)
            name_item.setToolTip(it["source_path"])
            self.table.setItem(row, 1, name_item)
            status_he = "הושלם" if it["status"] == "done" else "שגיאה"
            status_item = QTableWidgetItem(status_he)
            if it["status"] == "error":
                status_item.setToolTip(it.get("error", ""))
            self.table.setItem(row, 2, status_item)
            self.table.setItem(row, 3, QTableWidgetItem(str(it.get("chars", 0))))

            actions = QWidget()
            al = QHBoxLayout(actions)
            al.setContentsMargins(0, 0, 0, 0)
            if it["status"] == "done" and it.get("output_path"):
                out_path = it["output_path"]
                open_btn = QPushButton("פתח קובץ")
                open_btn.clicked.connect(lambda _, p=out_path: open_path(p))
                folder_btn = QPushButton("פתח תיקייה")
                folder_btn.clicked.connect(lambda _, p=out_path: reveal_in_folder(p))
                al.addWidget(open_btn)
                al.addWidget(folder_btn)
            self.table.setCellWidget(row, 4, actions)

    def clear_history(self):
        reply = QMessageBox.question(self, "נקה היסטוריה", "למחוק את כל ההיסטוריה? (הקבצים עצמם לא יימחקו)")
        if reply == QMessageBox.Yes:
            storage.clear_history()
            self.reload()


class SettingsTab(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = storage.load_settings()

        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        api_row = QHBoxLayout()
        self.api_key_edit = QLineEdit(storage.get_api_key())
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        show_btn = QPushButton("הצג")
        show_btn.setCheckable(True)
        show_btn.toggled.connect(
            lambda checked: self.api_key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        )
        import_btn = QPushButton("ייבוא מקובץ...")
        import_btn.clicked.connect(self.import_from_file)
        api_row.addWidget(self.api_key_edit, 1)
        api_row.addWidget(show_btn)
        api_row.addWidget(import_btn)
        form.addRow("מפתח API של OpenAI", api_row)

        self.org_edit = QLineEdit(self.settings.get("organization_id", ""))
        self.org_edit.setPlaceholderText("org-... (אופציונלי)")
        form.addRow("מזהה ארגון", self.org_edit)

        self.project_edit = QLineEdit(self.settings.get("project_id", ""))
        self.project_edit.setPlaceholderText("proj_... (אופציונלי)")
        form.addRow("מזהה פרויקט", self.project_edit)

        self.model_combo = QComboBox()
        self.model_combo.addItems(storage.MODEL_OPTIONS)
        if self.settings.get("model") in storage.MODEL_OPTIONS:
            self.model_combo.setCurrentText(self.settings["model"])
        form.addRow("מודל תמלול", self.model_combo)

        self.language_combo = QComboBox()
        for code, label in storage.LANGUAGE_OPTIONS:
            self.language_combo.addItem(label, code)
        current_lang = self.settings.get("language", "")
        idx = self.language_combo.findData(current_lang)
        if idx >= 0:
            self.language_combo.setCurrentIndex(idx)
        form.addRow("שפת ההקלטה", self.language_combo)

        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(1, 60)
        self.chunk_spin.setSuffix(" דקות")
        self.chunk_spin.setValue(int(self.settings.get("chunk_minutes", 15) or 15))
        form.addRow("אורך חלק (לקבצים ארוכים)", self.chunk_spin)

        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(self.settings.get("output_folder", ""))
        self.output_edit.setPlaceholderText("ריק = אותה תיקייה כמו קובץ המקור")
        browse_btn = QPushButton("עיון...")
        browse_btn.clicked.connect(self.pick_output_folder)
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(browse_btn)
        form.addRow("תיקיית פלט", out_row)

        outer.addLayout(form)

        save_btn = QPushButton("שמור הגדרות")
        save_btn.clicked.connect(self.save)
        outer.addWidget(save_btn, alignment=Qt.AlignLeft)

        note = QLabel(
            "מפתח ה-API נשמר באחסון המאובטח של מערכת ההפעלה (Windows Credential "
            "Manager / macOS Keychain) ולא בקובץ טקסט גלוי."
        )
        note.setWordWrap(True)
        note.setObjectName("hint")
        outer.addWidget(note)
        outer.addStretch(1)

    def pick_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "בחרו תיקיית פלט", self.output_edit.text() or str(Path.home()))
        if folder:
            self.output_edit.setText(folder)

    def import_from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "ייבוא מפתחות מקובץ", str(Path.home()), "קבצי טקסט (*.env *.txt);;כל הקבצים (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            QMessageBox.warning(self, "שגיאה", f"לא ניתן לקרוא את הקובץ: {e}")
            return
        parsed = storage.parse_keys_env(text)
        if parsed["api_key"]:
            self.api_key_edit.setText(parsed["api_key"])
        if parsed["organization_id"]:
            self.org_edit.setText(parsed["organization_id"])
        if parsed["project_id"]:
            self.project_edit.setText(parsed["project_id"])
        if parsed["model"] in storage.MODEL_OPTIONS:
            self.model_combo.setCurrentText(parsed["model"])
        found = [k for k, v in parsed.items() if v]
        if found:
            QMessageBox.information(self, "יובא בהצלחה", "נמצאו ונטענו: " + ", ".join(found))
        else:
            QMessageBox.warning(self, "לא נמצא כלום", "לא זוהו מפתחות בקובץ שנבחר.")

    def save(self):
        storage.set_api_key(self.api_key_edit.text())
        self.settings["organization_id"] = self.org_edit.text().strip()
        self.settings["project_id"] = self.project_edit.text().strip()
        self.settings["model"] = self.model_combo.currentText()
        self.settings["language"] = self.language_combo.currentData()
        self.settings["chunk_minutes"] = self.chunk_spin.value()
        self.settings["output_folder"] = self.output_edit.text().strip()
        storage.save_settings(self.settings)
        QMessageBox.information(self, "נשמר", "ההגדרות נשמרו בהצלחה.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("תמלול AI — TranscribeApp")
        self.resize(980, 640)

        self.tabs = QTabWidget()
        self.queue_tab = QueueTab(self)
        self.history_tab = HistoryTab()
        self.settings_tab = SettingsTab()
        self.tabs.addTab(self.queue_tab, "תור עבודה")
        self.tabs.addTab(self.history_tab, "היסטוריה")
        self.tabs.addTab(self.settings_tab, "הגדרות")
        self.setCentralWidget(self.tabs)

    def closeEvent(self, event):
        if self.queue_tab.thread and self.queue_tab.thread.isRunning():
            reply = QMessageBox.question(
                self, "תמלול פעיל",
                "יש תמלול פעיל. לעצור ולצאת?",
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.queue_tab.stop_queue()
            self.queue_tab.thread.quit()
            self.queue_tab.thread.wait(5000)
        event.accept()
