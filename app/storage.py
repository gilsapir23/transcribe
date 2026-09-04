"""Local persistence: settings (JSON) + secret API key (OS keychain) + history (JSON)."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import keyring
from platformdirs import user_data_dir

APP_NAME = "TranscribeApp"
KEYRING_SERVICE = "TranscribeApp"
KEYRING_USER = "openai_api_key"

DEFAULT_SETTINGS = {
    "organization_id": "",
    "project_id": "",
    "model": "whisper-1",
    "language": "he",
    "chunk_minutes": 15,
    "output_folder": "",  # empty = same folder as source file
    "last_open_dir": "",
}

LANGUAGE_OPTIONS = [
    ("", "זיהוי אוטומטי"),
    ("he", "עברית"),
    ("en", "אנגלית"),
    ("ar", "ערבית"),
    ("ru", "רוסית"),
    ("fr", "צרפתית"),
    ("es", "ספרדית"),
    ("de", "גרמנית"),
]

MODEL_OPTIONS = [
    "whisper-1",
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
]


def data_dir() -> Path:
    p = Path(user_data_dir(APP_NAME, appauthor=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_path() -> Path:
    return data_dir() / "settings.json"


def history_path() -> Path:
    return data_dir() / "history.json"


def load_settings() -> dict:
    p = settings_path()
    if not p.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    return merged


def save_settings(settings: dict) -> None:
    settings_path().write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def get_api_key() -> str:
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception:
        return ""


def set_api_key(key: str) -> None:
    key = (key or "").strip()
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass


def parse_keys_env(text: str) -> dict:
    """Best-effort parser for a 'keys.env'-style file people jot OpenAI creds into.

    Recognizes lines like:
        organization id: org-xxx
        open api key:
        sk-proj-xxx
        project id:
        proj_xxx
        model: whisper
    """
    result = {"api_key": "", "organization_id": "", "project_id": "", "model": ""}

    key_match = re.search(r"sk-[A-Za-z0-9_-]{10,}", text)
    if key_match:
        result["api_key"] = key_match.group(0)

    org_match = re.search(r"org-[A-Za-z0-9]{10,}", text)
    if org_match:
        result["organization_id"] = org_match.group(0)

    proj_match = re.search(r"proj_[A-Za-z0-9]{10,}", text)
    if proj_match:
        result["project_id"] = proj_match.group(0)

    model_match = re.search(r"model\s*[:=]\s*([A-Za-z0-9._-]+)", text, re.IGNORECASE)
    if model_match:
        m = model_match.group(1).strip().lower()
        if m in ("whisper", "whisper1"):
            m = "whisper-1"
        result["model"] = m

    return result


@dataclass
class HistoryItem:
    id: str
    timestamp: str
    source_path: str
    output_path: str
    duration_sec: float
    status: str  # "done" | "error"
    chars: int = 0
    error: str = ""

    @staticmethod
    def new(source_path: str, output_path: str, duration_sec: float, status: str,
            chars: int = 0, error: str = "") -> "HistoryItem":
        return HistoryItem(
            id=str(uuid.uuid4()),
            timestamp=datetime.now().isoformat(timespec="seconds"),
            source_path=source_path,
            output_path=output_path,
            duration_sec=duration_sec,
            status=status,
            chars=chars,
            error=error,
        )


def load_history() -> list[dict]:
    p = history_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(items: list[dict]) -> None:
    history_path().write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def add_history_item(item: HistoryItem) -> None:
    items = load_history()
    items.insert(0, asdict(item))
    save_history(items)


def clear_history() -> None:
    save_history([])
