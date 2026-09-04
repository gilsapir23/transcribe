"""TranscribeApp entry point.

A small cross-platform desktop app that transcribes long audio/video files
via the OpenAI transcription API (splitting large files automatically with
a bundled ffmpeg), with a queue, history, and a settings screen for the API
key (stored in the OS keychain, not in plain text).
"""
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow


def resource_path(rel_path: str) -> Path:
    """Resolve a resource path both when running from source and when
    bundled by PyInstaller (which unpacks data files into sys._MEIPASS)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / rel_path


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("TranscribeApp")
    app.setLayoutDirection(Qt.RightToLeft)

    qss_path = resource_path("resources/style.qss")
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
