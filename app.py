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
SCOPES = ["https://www.googleapis.com/auth/drive"]

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
# Google Drive helpers
# ----------------------------
@st.cache_resource
def drive_service():
    creds = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _folder_id() -> str:
    return st.secrets["drive_folder_id"]


def _find_file_id_in_folder(svc, folder_id: str, filename: str) -> str | None:
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
    try:
        return json.loads(fh.read().decode("utf-8"))
    except Exception:
        return default_obj


def _upload_json(svc, folder_id: str, filename: str, obj, file_id: str | None):
    payload = json.dumps(obj, indent=2).encode("utf-8")
    media = MediaInMemoryUpload(payload, mimetype="application/json", resumable=False)

    if file_id:
        svc.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
    else:
        svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()


def ensure_folder_access_or_stop():
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
    except Exception as e:
        st.error(
            "Google Drive folder is not accessible to the service account.\n\n"
            "Fix:\n"
            "1) Confirm drive_folder_id is the folder ID (from /drive/folders/<ID>)\n"
            "2) Share the folder with the service account email as Editor\n"
        )
        st.exception(e)
        st.stop()


# ----------------------------
# Persistence
# ----------------------------
def load_data() -> dict:
    svc = drive_service()
    folder_id = _folder_id()

    fid = _find_file_id_in_folder(svc, folder_id, DATA_FILENAME)
    if fid:
        obj = _download_json(svc, fid, default_obj={})
        if isinstance(obj, dict) and obj:
            return obj

    return {
        "purpose": "To take care of myself mentally, physically, and spiritually, in order to have the capacity to enjoy life.",
        "terms": "Goals = long-term\nTasks = weekly\n\nIf it can't be completed in a week, it isn't a task.\nTasks must fit on one page.",
        "goals": [],
        "tasks": [],
        "updated_at": now_iso(),
        "last_week_closed": None,
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
    completed = sum(1 for e in events if e.get("event") == "completed")
    missed = sum(1 for e in events if e.get("event") == "missed")
    total = completed + missed
    rate = 0.0 if total == 0 else round(100.0 * completed / total, 1)
    return completed, missed, total, rate, events


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="One-Page Planner", layout="wide")

# Secrets guard
needed = ("drive_folder_id", "gcp_service_account", "edit_password")
missing = [k for k in needed if k not in st.secrets]
if missing:
    st.error(f"Missing Streamlit Secrets: {', '.join(missing)}")
    st.stop()

editor_login_ui()
can_edit = is_editor()

ensure_folder_access_or_stop()

data = load_data()
week = current_iso_week()

# ----------------------------
# Auto-reset tasks on new week
# ----------------------------
if data.get("last_week_closed") != week:
    changed = False
    for t in data.get("tasks", []):
        if t.get("status") != "In Progress":
            t["status"] = "In Progress"
            changed = True

    # Only save if we actually changed anything (avoids extra Drive writes)
    if changed:
        save_data(data)


st.title("One-Page Planner")
st.caption(f"Week: **{week}**")
if not can_edit:
    st.info("👀 View-only mode. Editing is disabled.")

# Purpose + Terms
colA, colB = st.columns(2)
with colA:
    new_purpose = st.text_area("Purpose", data.get("purpose", ""), height=100, disabled=not can_edit)
with colB:
    new_terms = st.text_area("Terms / Rules", data.get("terms", ""), height=100, disabled=not can_edit)

if can_edit and (new_purpose != data.get("purpose") or new_terms != data.get("terms")):
    data["purpose"] = new_purpose
    data["terms"] = new_terms
    save_data(data)

st.divider()

# Add Goal
with st.expander("➕ Add Goal", expanded=False):
    g_summary = st.text_input("Goal summary", key="g_summary", disabled=not can_edit)
    g_deadline = st.date_input("Deadline", value=date.today(), key="g_deadline", disabled=not can_edit)
    g_status = st.selectbox("Goal status", GOAL_STATUSES, index=0, key="g_status", disabled=not can_edit)
    g_parents = st.multiselect("Parents", DEFAULT_PARENTS, default=[], key="g_parents", disabled=not can_edit)

    if st.button("Create Goal", key="btn_create_goal", disabled=not can_edit):
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
            save_data(data)
            st.success(f"Created Goal ({gid}).")

# Add Task
with st.expander("➕ Add Task", expanded=False):
    if not data["goals"]:
        st.info("Create a goal first.")
    else:
        t_summary = st.text_input("Task summary", key="t_summary", disabled=not can_edit)
        goal_options = {f'Goal {g["id"]}: {g["summary"]}': g["id"] for g in data["goals"]}
        selected_goal_label = st.selectbox(
            "Parent goal (required)",
            list(goal_options.keys()),
            key="t_parent",
            disabled=not can_edit
        )
        selected_goal_id = goal_options[selected_goal_label]

        preview_id = next_task_id_for_goal(data["tasks"], selected_goal_id)
        st.caption(f"Task ID will be: **{preview_id}**")

        if st.button("Create Task", key="btn_create_task", disabled=not can_edit):
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
                save_data(data)
                st.success(f"Created Task ({tid}).")

st.divider()

# Weekly Tasks
st.subheader("Weekly Tasks (persisting list)")

def saving() -> bool:
    return bool(st.session_state.get("_saving", False))

def safe_save_data(data: dict) -> None:
    st.session_state["_saving"] = True
    try:
        with st.spinner("Saving…"):
            save_data(data)
    finally:
        st.session_state["_saving"] = False


if not data["tasks"]:
    st.info("No tasks yet.")
else:
    changed_any = False  # <-- coalesce flag

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

            # update in-memory only; save once after the loop
            if can_edit and new_status != current_status:
                t["status"] = new_status
                changed_any = True

            if st.button(
                "Delete task",
                key=f"del_task_{t['id']}",
                disabled=(not can_edit or saving())
            ):
                data["tasks"] = [tt for tt in data["tasks"] if tt["id"] != t["id"]]
                safe_save_data(data)
                st.rerun()

    # single save for all status changes triggered this rerun
    if can_edit and changed_any and not saving():
        safe_save_data(data)
        st.rerun()
v

# Close Week
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
        disabled=(close_disabled or not can_edit)
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

        append_progress_events(events)

        for t in data["tasks"]:
            t["status"] = "In Progress"

        data["last_week_closed"] = week
        save_data(data)

        st.success("Week logged. Tasks kept. Statuses reset.")
        st.rerun()

st.divider()

# Goal Progress Summary
st.subheader("Goal Progress Summary")

if not data["goals"]:
    st.info("No goals yet.")
else:
    for g in data["goals"]:
        with st.expander(f"📊 Goal {g['id']}: {g['summary']}"):
            completed, missed, total, rate, events = goal_progress_summary(g["id"])
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

# Downloads
st.download_button(
    "Download planner_data.json",
    data=json.dumps(data, indent=2),
    file_name="planner_data.json",
    mime="application/json",
    key="dl_state",
)

progress_log = load_progress_log()
st.download_button(
    "Download progress_log.json",
    data=json.dumps(progress_log, indent=2),
    file_name="progress_log.json",
    mime="application/json",
    key="dl_log",
)
