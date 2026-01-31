"""
One-Page Planner + Weekly Progress Logger (Streamlit)
Google Drive persistence + View-only for friends + Editor-only edits for you.

What it does:
- Goals are long-term. Tasks persist week-to-week.
- Each week you manually mark each task: In Progress | Completed | Missed
- You can only "Close Week" when ALL tasks are marked Completed or Missed
- Closing week:
    - appends immutable events to progress_log.json (parsable)
    - resets all task statuses back to In Progress for next week
    - remembers that this ISO week is already closed (prevents double logging)
- Tasks are NOT deleted (you can delete manually if you want)
- Each goal has a Progress Summary that parses the progress log and shows stats + entries.

Persistence:
- planner_data.json and progress_log.json are stored in a Google Drive folder.

Auth:
- Friends can view.
- Only you can edit after entering edit password (stored in Streamlit Secrets).

Streamlit Secrets (TOML):
drive_folder_id = "YOUR_FOLDER_ID"
edit_password = "YOUR_LONG_PASSWORD"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = """-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----"""
client_email = "..."
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
universe_domain = "googleapis.com"

requirements.txt:
streamlit
google-api-python-client
google-auth
"""

import json
import re
import io
import hmac
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone

import streamlit as st

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload


# ----------------------------
# Constants
# ----------------------------
DEFAULT_PARENTS = ["Mental Health", "Physical Health", "Spiritual"]
TASK_STATUSES = ["In Progress", "Completed", "Missed"]
GOAL_STATUSES = ["In Progress", "Done", "Psyche"]

DATA_FILENAME = "planner_data.json"
PROGRESS_FILENAME = "progress_log.json"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


# ----------------------------
# Data model
# ----------------------------
@dataclass
class Goal:
    id: int
    status: str
    summary: str
    deadline: str
    parents: list


@dataclass
class Task:
    id: str                 # auto-generated: <goalId>.<letter>
    summary: str
    parent_goal_id: int
    status: str = "In Progress"   # weekly status


# ----------------------------
# Time helpers
# ----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_iso_week() -> str:
    # ISO week starts Monday. Example: "2026-W05"
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


# ----------------------------
# Auth (view-only by default)
# ----------------------------
def is_editor() -> bool:
    return bool(st.session_state.get("is_editor", False))


def editor_login_ui():
    with st.sidebar:
        st.markdown("### 🔒 Editor")

        if is_editor():
            st.success("Editing enabled")
            if st.button("Log out (view-only)", key="btn_logout"):
                st.session_state["is_editor"] = False
                st.rerun()
        else:
            pw = st.text_input("Edit password", type="password", key="edit_pw")
            if pw and hmac.compare_digest(pw, st.secrets["edit_password"]):
                st.session_state["is_editor"] = True
                st.rerun()

            st.caption("Viewers can browse. Editing requires the password.")


# ----------------------------
# Google Drive helpers
# ----------------------------
@st.cache_resource
def drive_service():
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=SCOPES,
    )
    # cache_discovery=False avoids warnings in some environments
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _folder_id() -> str:
    return st.secrets["drive_folder_id"]


def _find_file_id_in_folder(svc, folder_id: str, filename: str) -> str | None:
    # Shared Drive safe
    q = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
    res = (
        svc.files()
        .list(
            q=q,
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _download_json(svc, file_id: str, default_obj):
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    raw = fh.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return default_obj


def _upload_json(svc, folder_id: str, filename: str, obj, file_id: str | None):
    payload = json.dumps(obj, indent=2).encode("utf-8")
    media = MediaInMemoryUpload(payload, mimetype="application/json", resumable=False)

    if file_id:
        svc.files().update(
            fileId=file_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
    else:
        svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()


def ensure_folder_access_or_stop():
    """
    Hard-fail early with a helpful message if folder_id is wrong or not shared to the service account.
    """
    svc = drive_service()
    folder_id = _folder_id()
    try:
        meta = svc.files().get(
            fileId=folder_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            st.error("drive_folder_id does not point to a folder. Check the ID.")
            st.stop()
    except Exception:
        st.error(
            "Google Drive folder is not accessible to the service account.\n\n"
            "Fix:\n"
            "1) Confirm drive_folder_id is the folder ID (from /drive/folders/<ID>)\n"
            "2) Share the folder with the service account email as Editor\n"
            "3) If it’s a Shared Drive, ensure the service account has access"
        )
        st.stop()


# ----------------------------
# Persistence (Drive-backed)
# ----------------------------
def load_data() -> dict:
    svc = drive_service()
    folder_id = _folder_id()

    fid = _find_file_id_in_folder(svc, folder_id, DATA_FILENAME)
    if fid:
        obj = _download_json(svc, fid, default_obj={})
        # If file exists but is empty/corrupt, fall back to defaults
        if isinstance(obj, dict) and obj:
            return obj

    return {
        "purpose": "To take care of myself mentally, physically, and spiritually, in order to have the capacity to enjoy life.",
        "terms": "Goals = long-term\nTasks = weekly\n\nIf it can't be completed in a week, it isn't a task.\nTasks must fit on one page.",
        "goals": [],
        "tasks": [],
        "updated_at": now_iso(),
        "last_week_closed": None,  # e.g. "2026-W05"
    }


def save_data(data: dict) -> None:
    data["updated_at"] = now_iso()

    svc = drive_service()
    folder_id = _folder_id()
    fid = _find_file_id_in_folder(svc, folder_id, DATA_FILENAME)
    _upload_json(svc, folder_id, DATA_FILENAME, data, fid)


def load_progress_log() -> list[dict]:
    svc = drive_service()
    folder_id = _folder_id()

    fid = _find_file_id_in_folder(svc, folder_id, PROGRESS_FILENAME)
    if fid:
        obj = _download_json(svc, fid, default_obj=[])
        return obj if isinstance(obj, list) else []
    return []


def append_progress_events(events: list[dict]) -> None:
    """
    Drive-backed append (read -> extend -> write).
    For solo use, fine. For multi-user, move to a DB.
    """
    log = load_progress_log()
    log.extend(events)

    svc = drive_service()
    folder_id = _folder_id()
    fid = _find_file_id_in_folder(svc, folder_id, PROGRESS_FILENAME)
    _upload_json(svc, folder_id, PROGRESS_FILENAME, log, fid)


# ----------------------------
# ID generation
# ----------------------------
def next_goal_id(goals: list[dict]) -> int:
    return 1 if not goals else max(g["id"] for g in goals) + 1


def next_task_id_for_goal(tasks: list[dict], goal_id: int) -> str:
    used = set()
    pat = re.compile(rf"^{re.escape(str(goal_id))}\.([a-z])$", re.IGNORECASE)

    for t in tasks:
        if t.get("parent_goal_id") != goal_id:
            continue
        tid = str(t.get("id", "")).strip()
        m = pat.match(tid)
        if m:
            used.add(m.group(1).lower())

    for c in "abcdefghijklmnopqrstuvwxyz":
        if c not in used:
            return f"{goal_id}.{c}"

    raise ValueError(f"Too many tasks under Goal {goal_id} (exceeded 26).")


# ----------------------------
# Progress summary
# ----------------------------
def goal_progress_summary(goal_id: int):
    log = load_progress_log()
    events = [e for e in log if e.get("goal_id") == goal_id]

    comple
