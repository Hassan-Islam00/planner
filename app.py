import json
import re
import io
import hmac
import copy
import time
import random
import ssl
import socket
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone

import streamlit as st

import httplib2
import google_auth_httplib2
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
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Session-state keys
SS_WORKING = "working_data"          # local working copy (browser session)
SS_LOADED_AT = "working_loaded_at"   # updated_at at time of load (for warnings)
SS_DIRTY = "working_dirty"
SS_SAVING = "working_saving"
SS_DRIVE_READY = "drive_ready"
SS_PROGRESS = "progress_cache"       # cached progress log (loaded only when user asks)

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
    id: str
    summary: str
    parent_goal_id: int
    status: str = "In Progress"


# ----------------------------
# Time helpers
# ----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_iso_week() -> str:
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
# Local-state helpers
# ----------------------------
def saving() -> bool:
    return bool(st.session_state.get(SS_SAVING, False))


def mark_dirty():
    st.session_state[SS_DIRTY] = True


def clear_dirty():
    st.session_state[SS_DIRTY] = False


def is_dirty() -> bool:
    return bool(st.session_state.get(SS_DIRTY, False))


# ----------------------------
# Google Drive helpers (with timeout + retries)
# ----------------------------
@st.cache_resource
def drive_service():
    """
    Create a Drive service with explicit httplib2 timeout.
    Streamlit Cloud can have flaky TLS; this reduces long hangs and makes retries cleaner.
    """
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=SCOPES,
    )
    http = httplib2.Http(timeout=60)
    authed_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
    return build("drive", "v3", http=authed_http, cache_discovery=False)


def _folder_id() -> str:
    return st.secrets["drive_folder_id"]


def _retryable_exc(e: Exception) -> bool:
    # Treat transient network/TLS/timeouts as retryable
    if isinstance(e, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError)):
        return True
    msg = str(e).lower()
    # common transient strings
    transient_markers = [
        "decryption failed",
        "bad record mac",
        "ssl",
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "broken pipe",
        "server disconnected",
        "503",
        "502",
        "500",
        "429",
    ]
    return any(m in msg for m in transient_markers)


def _execute_with_retries(req, what: str = "google api", max_tries: int = 5):
    """
    Wrap .execute() with exponential backoff.
    """
    base = 0.6
    last = None
    for i in range(max_tries):
        try:
            return req.execute()
        except Exception as e:
            last = e
            if not _retryable_exc(e) or i == max_tries - 1:
                raise
            # exponential backoff + jitter
            sleep_s = base * (2 ** i) + random.uniform(0, 0.35)
            time.sleep(sleep_s)
    raise last  # should never reach


def _find_file_id_in_folder(svc, folder_id: str, filename: str) -> str | None:
    q = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
    req = svc.files().list(
        q=q,
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    )
    res = _execute_with_retries(req, what=f"list {filename}")
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _download_json(svc, file_id: str, default_obj):
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)

    done = False
    # next_chunk() has its own retry logic, but we still wrap transient failures
    tries = 0
    while not done:
        try:
            _, done = downloader.next_chunk()
        except Exception as e:
            tries += 1
            if not _retryable_exc(e) or tries >= 5:
                raise
            time.sleep(0.6 * (2 ** (tries - 1)) + random.uniform(0, 0.35))

    fh.seek(0)
    try:
        return json.loads(fh.read().decode("utf-8"))
    except Exception:
        return default_obj


def _upload_json(svc, folder_id: str, filename: str, obj, file_id: str | None):
    payload = json.dumps(obj, indent=2).encode("utf-8")
    media = MediaInMemoryUpload(payload, mimetype="application/json", resumable=False)

    if file_id:
        req = svc.files().update(fileId=file_id, media_body=media, supportsAllDrives=True)
        _execute_with_retries(req, what=f"update {filename}")
    else:
        req = svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        _execute_with_retries(req, what=f"create {filename}")


def ensure_drive_ready_once_or_stop():
    """
    IMPORTANT: This *reads* Drive. We run it once per browser session.
    It does NOT write anything.
    """
    if st.session_state.get(SS_DRIVE_READY, False):
        return

    svc = drive_service()
    folder_id = _folder_id()

    try:
        req = svc.files().get(
            fileId=folder_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        )
        meta = _execute_with_retries(req, what="folder metadata")
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            st.error("drive_folder_id does not point to a folder. Check the ID.")
            st.stop()
        st.session_state[SS_DRIVE_READY] = True
    except Exception as e:
        st.error(
            "Google Drive folder is not accessible right now.\n\n"
            "Fix:\n"
            "1) Confirm drive_folder_id is the folder ID (from /drive/folders/<ID>)\n"
            "2) Share the folder with the service account email as Editor\n\n"
            "If this is Streamlit Cloud being flaky, hit Reload and try again."
        )
        st.exception(e)
        st.stop()


# ----------------------------
# Persistence (planner_data.json)
# ----------------------------
def default_data() -> dict:
    return {
        "purpose": "To take care of myself mentally, physically, and spiritually, in order to have the capacity to enjoy life.",
        "terms": "Goals = long-term\nTasks = weekly\n\nIf it can't be completed in a week, it isn't a task.\nTasks must fit on one page.",
        "goals": [],
        "tasks": [],
        "updated_at": now_iso(),
        "last_week_closed": None,
    }


def load_data_from_drive() -> dict:
    svc = drive_service()
    folder_id = _folder_id()

    fid = _find_file_id_in_folder(svc, folder_id, DATA_FILENAME)
    if fid:
        obj = _download_json(svc, fid, default_obj={})
        if isinstance(obj, dict) and obj:
            return obj

    return default_data()


def save_data_to_drive(data: dict) -> None:
    data["updated_at"] = now_iso()
    svc = drive_service()
    folder_id = _folder_id()
    fid = _find_file_id_in_folder(svc, folder_id, DATA_FILENAME)
    _upload_json(svc, folder_id, DATA_FILENAME, data, fid)


# ----------------------------
# Progress log (loaded only when user asks)
# ----------------------------
def load_progress_log_from_drive() -> list[dict]:
    svc = drive_service()
    folder_id = _folder_id()
    fid = _find_file_id_in_folder(svc, folder_id, PROGRESS_FILENAME)
    if fid:
        obj = _download_json(svc, fid, default_obj=[])
        return obj if isinstance(obj, list) else []
    return []


def append_progress_events(events: list[dict]) -> None:
    """
    Writes progress_log.json immediately.
    If you want *zero* Drive writes except Save, tell me and I’ll buffer these too.
    """
    log = load_progress_log_from_drive()
    log.extend(events)

    svc = drive_service()
    folder_id = _folder_id()
    fid = _find_file_id_in_folder(svc, folder_id, PROGRESS_FILENAME)
    _upload_json(svc, folder_id, PROGRESS_FILENAME, log, fid)

    # keep local cache in sync if it's loaded
    if SS_PROGRESS in st.session_state:
        st.session_state[SS_PROGRESS] = log


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
# Progress summary (uses cached log only)
# ----------------------------
def goal_progress_summary_cached(goal_id: int):
    log = st.session_state.get(SS_PROGRESS, [])
    events = [e for e in log if e.get("goal_id") == goal_id]
    completed = sum(1 for e in events if e.get("event") == "completed")
    missed = sum(1 for e in events if e.get("event") == "missed")
    total = completed + missed
    rate = 0.0 if total == 0 else round(100.0 * completed / total, 1)
    return completed, missed, total, rate, events


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="One-Page Planner", layout="wide")

needed = ("drive_folder_id", "gcp_service_account", "edit_password")
missing = [k for k in needed if k not in st.secrets]
if missing:
    st.error(f"Missing Streamlit Secrets: {', '.join(missing)}")
    st.stop()

editor_login_ui()
can_edit = is_editor()

# Drive check ONCE per session (read-only)
ensure_drive_ready_once_or_stop()

week = current_iso_week()

# Load planner_data.json ONCE per session
if SS_WORKING not in st.session_state:
    try:
        drive_data = load_data_from_drive()  # one read at startup
    except Exception as e:
        st.error(
            "Could not load planner_data.json from Drive right now.\n\n"
            "You can still use the app locally and Save later.\n"
        )
        st.exception(e)
        drive_data = default_data()

    st.session_state[SS_WORKING] = copy.deepcopy(drive_data)
    st.session_state[SS_LOADED_AT] = drive_data.get("updated_at")
    clear_dirty()

data = st.session_state[SS_WORKING]

# ----------------------------
# Top bar: Save / Reload / Revert / Load progress
# ----------------------------
barA, barB, barC, barD, barE = st.columns([1.1, 1.1, 1.1, 1.5, 3.2])

with barA:
    if st.button("💾 Save", disabled=(not can_edit or saving() or not is_dirty()), key="btn_save"):
        st.session_state[SS_SAVING] = True
        try:
            with st.spinner("Saving planner_data.json to Drive…"):
                # basic "someone else updated Drive" warning (doesn't block)
                try:
                    latest = load_data_from_drive()
                    if (
                        st.session_state.get(SS_LOADED_AT)
                        and latest.get("updated_at")
                        and latest.get("updated_at") != st.session_state.get(SS_LOADED_AT)
                    ):
                        st.warning(
                            "Drive version changed since you loaded. "
                            "Saving will overwrite the Drive version."
                        )
                except Exception:
                    # If Drive is flaky, still attempt save; retries will handle transient issues
                    pass

                save_data_to_drive(data)  # SINGLE WRITE CALL for planner_data.json
                st.session_state[SS_LOADED_AT] = data.get("updated_at")
                clear_dirty()

            st.success("Saved.")
        finally:
            st.session_state[SS_SAVING] = False
        st.rerun()

with barB:
    if st.button("🔄 Reload", disabled=saving(), key="btn_reload"):
        st.session_state[SS_SAVING] = True
        try:
            with st.spinner("Reloading planner_data.json from Drive…"):
                fresh = load_data_from_drive()
                st.session_state[SS_WORKING] = copy.deepcopy(fresh)
                st.session_state[SS_LOADED_AT] = fresh.get("updated_at")
                clear_dirty()
            st.info("Reloaded.")
        finally:
            st.session_state[SS_SAVING] = False
        st.rerun()

with barC:
    if st.button("↩️ Revert", disabled=(saving() or not is_dirty()), key="btn_revert"):
        # revert local edits to the last loaded state (Drive snapshot)
        st.session_state[SS_SAVING] = True
        try:
            with st.spinner("Reverting to Drive version…"):
                fresh = load_data_from_drive()
                st.session_state[SS_WORKING] = copy.deepcopy(fresh)
                st.session_state[SS_LOADED_AT] = fresh.get("updated_at")
                clear_dirty()
            st.info("Reverted.")
        finally:
            st.session_state[SS_SAVING] = False
        st.rerun()

with barD:
    if st.button("📥 Load progress log", disabled=saving(), key="btn_load_progress"):
        st.session_state[SS_SAVING] = True
        try:
            with st.spinner("Loading progress_log.json from Drive…"):
                st.session_state[SS_PROGRESS] = load_progress_log_from_drive()
            st.success("Progress log loaded.")
        finally:
            st.session_state[SS_SAVING] = False
        st.rerun()

with barE:
    if not can_edit:
        st.info("👀 View-only mode.")
    else:
        if is_dirty():
            st.warning("Unsaved changes (local only). Click **Save** to write to Drive.")
        else:
            st.caption("All changes saved (or no changes).")

st.title("One-Page Planner")
st.caption(f"Week: **{week}**")

# ----------------------------
# Purpose + Terms (local only)
# ----------------------------
colA, colB = st.columns(2)
with colA:
    new_purpose = st.text_area("Purpose", data.get("purpose", ""), height=100, disabled=(not can_edit or saving()))
with colB:
    new_terms = st.text_area("Terms / Rules", data.get("terms", ""), height=100, disabled=(not can_edit or saving()))

if can_edit and not saving():
    if new_purpose != data.get("purpose"):
        data["purpose"] = new_purpose
        mark_dirty()
    if new_terms != data.get("terms"):
        data["terms"] = new_terms
        mark_dirty()

st.divider()

# ----------------------------
# Add Goal (local only)
# ----------------------------
with st.expander("➕ Add Goal", expanded=False):
    g_summary = st.text_input("Goal summary", key="g_summary", disabled=(not can_edit or saving()))
    g_deadline = st.date_input("Deadline", value=date.today(), key="g_deadline", disabled=(not can_edit or saving()))
    g_status = st.selectbox("Goal status", GOAL_STATUSES, index=0, key="g_status", disabled=(not can_edit or saving()))
    g_parents = st.multiselect("Parents", DEFAULT_PARENTS, default=[], key="g_parents", disabled=(not can_edit or saving()))

    if st.button("Create Goal", key="btn_create_goal", disabled=(not can_edit or saving())):
        if not g_summary.strip():
            st.error("Goal summary can't be empty.")
        else:
            gid = next_goal_id(data["goals"])
            data["goals"].append(asdict(Goal(
                id=gid,
                status=g_status,
                summary=g_summary.strip(),
                deadline=g_deadline.isoformat(),
                parents=g_parents
            )))
            mark_dirty()
            st.success(f"Created Goal ({gid}).")

# ----------------------------
# Add Task (local only)
# ----------------------------
with st.expander("➕ Add Task", expanded=False):
    if not data["goals"]:
        st.info("Create a goal first.")
    else:
        t_summary = st.text_input("Task summary", key="t_summary", disabled=(not can_edit or saving()))
        goal_options = {f'Goal {g["id"]}: {g["summary"]}': g["id"] for g in data["goals"]}
        selected_goal_label = st.selectbox(
            "Parent goal (required)",
            list(goal_options.keys()),
            key="t_parent",
            disabled=(not can_edit or saving())
        )
        selected_goal_id = goal_options[selected_goal_label]

        preview_id = next_task_id_for_goal(data["tasks"], selected_goal_id)
        st.caption(f"Task ID will be: **{preview_id}**")

        if st.button("Create Task", key="btn_create_task", disabled=(not can_edit or saving())):
            if not t_summary.strip():
                st.error("Task summary is required.")
            else:
                tid = next_task_id_for_goal(data["tasks"], selected_goal_id)
                data["tasks"].append(asdict(Task(
                    id=tid,
                    summary=t_summary.strip(),
                    parent_goal_id=selected_goal_id,
                    status="In Progress"
                )))
                mark_dirty()
                st.success(f"Created Task ({tid}).")

st.divider()

# ----------------------------
# Weekly Tasks (local until Save)
# ----------------------------
st.subheader("Weekly Tasks (local until Save)")

if not data["tasks"]:
    st.info("No tasks yet.")
else:
    cols = st.columns(3)
    for i, t in enumerate(data["tasks"]):
        with cols[i % 3]:
            st.markdown(f"### {t['id']}")
            st.write(t["summary"])
            st.caption(f"Goal: {t['parent_goal_id']}")

            current_status = t.get("status", "In Progress")
            if current_status not in TASK_STATUSES:
                current_status = "In Progress"

            new_status = st.selectbox(
                "Weekly status",
                TASK_STATUSES,
                index=TASK_STATUSES.index(current_status),
                key=f"task_status_{t['id']}",
                disabled=(not can_edit or saving())
            )

            if can_edit and not saving() and new_status != current_status:
                t["status"] = new_status
                mark_dirty()

            if st.button("Delete task", key=f"del_task_{t['id']}", disabled=(not can_edit or saving())):
                data["tasks"] = [tt for tt in data["tasks"] if tt["id"] != t["id"]]
                mark_dirty()
                st.rerun()

st.divider()

# ----------------------------
# Close Week
# ----------------------------
st.subheader("Close Week")

if data.get("last_week_closed") == week:
    st.info("This week has already been closed/logged.")
else:
    in_progress = [t for t in data["tasks"] if t.get("status") == "In Progress"]
    close_disabled = bool(in_progress)

    if in_progress:
        st.error(
            f"{len(in_progress)} task(s) are still **In Progress**.\n\n"
            "Mark every task as **Completed** or **Missed** before closing the week."
        )

    if st.button(
        "✅ Confirm week is over (log results + reset)",
        key="btn_close_week",
        disabled=(close_disabled or not can_edit or saving())
    ):
        events = []
        for t in data["tasks"]:
            status = t.get("status", "In Progress")
            if status == "Completed":
                ev = "completed"
            elif status == "Missed":
                ev = "missed"
            else:
                continue

            events.append({
                "timestamp": now_iso(),
                "week": week,
                "goal_id": t["parent_goal_id"],
                "task_id": t["id"],
                "event": ev,
                "summary": t["summary"],
            })

        # This DOES write to Drive (progress_log.json).
        # If you want this also buffered until Save, say so and I’ll change it.
        try:
            append_progress_events(events)
        except Exception as e:
            st.error("Failed to write progress_log.json to Drive right now.")
            st.exception(e)
            st.stop()

        for t in data["tasks"]:
            t["status"] = "In Progress"

        data["last_week_closed"] = week
        mark_dirty()

        st.success("Week logged. Planner reset locally. Click 💾 Save to persist planner_data.json.")
        st.rerun()

st.divider()

# ----------------------------
# Goal Progress Summary (NO automatic Drive calls)
# ----------------------------
st.subheader("Goal Progress Summary")

if SS_PROGRESS not in st.session_state:
    st.info("Progress log not loaded. Click **📥 Load progress log** at the top to view history.")
else:
    if not data["goals"]:
        st.info("No goals yet.")
    else:
        for g in data["goals"]:
            with st.expander(f"📊 Goal {g['id']}: {g['summary']}"):
                completed, missed, total, rate, events = goal_progress_summary_cached(g["id"])
                st.metric("Completion rate", f"{rate}%", f"{completed}/{total} (completed/total)")
                st.write(f"Missed: **{missed}**")

                if not events:
                    st.caption("No log entries yet for this goal.")
                else:
                    st.write("Recent log entries (newest last):")
                    for e in events[-50:]:
                        emoji = "✅" if e.get("event") == "completed" else "❌"
                        st.write(f"{emoji} **{e.get('week','')}** — {e.get('task_id','')} — {e.get('summary','')}")

st.divider()

# ----------------------------
# Downloads (NO automatic Drive calls)
# ----------------------------
st.download_button(
    "Download planner_data.json (local working copy)",
    data=json.dumps(data, indent=2),
    file_name="planner_data.json",
    mime="application/json",
    key="dl_state",
)

if SS_PROGRESS in st.session_state:
    st.download_button(
        "Download progress_log.json (cached)",
        data=json.dumps(st.session_state[SS_PROGRESS], indent=2),
        file_name="progress_log.json",
        mime="application/json",
        key="dl_log_cached",
    )
else:
    st.caption("Load progress log to enable progress_log.json download.")
