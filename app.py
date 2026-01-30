"""
One-Page Planner + Weekly Progress Logger (Streamlit)

What it does:
- Goals are long-term. Tasks persist week-to-week.
- Each week you manually mark each task: In Progress | Completed | Missed
- You can only "Close Week" when ALL tasks are marked Completed or Missed (no "unmarked")
- Closing week:
    - appends immutable events to progress_log.json (parsable)
    - resets all task statuses back to In Progress for next week
    - remembers that this ISO week is already closed (prevents double logging)
- Tasks are NOT deleted (you can delete manually if you want)
- Each goal has a Progress Summary that parses the progress log and shows stats + entries.

Files created in the SAME directory as this file:
- planner_data.json   (current state)
- progress_log.json   (append-only log)

Run:
  python3 -m pip install streamlit
  python3 -m streamlit run app.py
"""

import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path

import streamlit as st

# ----------------------------
# Paths
# ----------------------------
APP_DIR = Path(__file__).resolve().parent
DATA_FILE = APP_DIR / "planner_data.json"
PROGRESS_FILE = APP_DIR / "progress_log.json"

# ----------------------------
# Constants
# ----------------------------
DEFAULT_PARENTS = ["Mental Health", "Physical Health", "Spiritual"]
TASK_STATUSES = ["In Progress", "Completed", "Missed"]
GOAL_STATUSES = ["In Progress", "Done", "Psyche"]


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
# Persistence helpers
# ----------------------------
def load_data() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
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
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def load_progress_log() -> list[dict]:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return []


def append_progress_events(events: list[dict]) -> None:
    """
    Append-only log. We do a full rewrite for simplicity.
    If you want true append streaming later, we can switch to NDJSON.
    """
    log = load_progress_log()
    log.extend(events)
    PROGRESS_FILE.write_text(json.dumps(log, indent=2), encoding="utf-8")


# ----------------------------
# ID generation
# ----------------------------
def next_goal_id(goals: list[dict]) -> int:
    return 1 if not goals else max(g["id"] for g in goals) + 1


def next_task_id_for_goal(tasks: list[dict], goal_id: int) -> str:
    # Allocate <goalId>.<letter>, skipping letters already used under that goal
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
    """
    Stats computed from immutable progress log.
    Returns: completed_count, missed_count, total_logged, completion_rate_percent, events_for_goal
    """
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
data = load_data()
week = current_iso_week()

st.title("One-Page Planner")
st.caption(f"Week: **{week}**")
st.caption(f"State: {DATA_FILE.name} • Log: {PROGRESS_FILE.name}")

# Purpose + Terms (auto-save)
colA, colB = st.columns(2)
with colA:
    new_purpose = st.text_area("Purpose", data.get("purpose", ""), height=100)
with colB:
    new_terms = st.text_area("Terms / Rules", data.get("terms", ""), height=100)

if new_purpose != data.get("purpose") or new_terms != data.get("terms"):
    data["purpose"] = new_purpose
    data["terms"] = new_terms
    save_data(data)

st.divider()

# ----------------------------
# Add Goal
# ----------------------------
with st.expander("➕ Add Goal", expanded=False):
    g_summary = st.text_input("Goal summary", key="g_summary")
    g_deadline = st.date_input("Deadline", value=date.today(), key="g_deadline")
    g_status = st.selectbox("Goal status", GOAL_STATUSES, index=0, key="g_status")
    g_parents = st.multiselect("Parents", DEFAULT_PARENTS, default=[], key="g_parents")

    if st.button("Create Goal", key="btn_create_goal"):
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

# ----------------------------
# Add Task (must choose goal; auto task id)
# ----------------------------
with st.expander("➕ Add Task", expanded=False):
    if not data["goals"]:
        st.info("Create a goal first.")
    else:
        t_summary = st.text_input("Task summary", key="t_summary")

        goal_options = {f'Goal {g["id"]}: {g["summary"]}': g["id"] for g in data["goals"]}
        selected_goal_label = st.selectbox("Parent goal (required)", list(goal_options.keys()), key="t_parent")
        selected_goal_id = goal_options[selected_goal_label]

        preview_id = next_task_id_for_goal(data["tasks"], selected_goal_id)
        st.caption(f"Task ID will be: **{preview_id}**")

        if st.button("Create Task", key="btn_create_task"):
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

# ----------------------------
# Weekly Tasks (persisting list)
# ----------------------------
st.subheader("Weekly Tasks (persisting list)")

if not data["tasks"]:
    st.info("No tasks yet.")
else:
    cols = st.columns(3)
    for i, t in enumerate(data["tasks"]):
        with cols[i % 3]:
            st.markdown(f"### {t['id']}")
            st.write(t["summary"])
            st.caption(f"Goal: {t['parent_goal_id']}")

            # Status dropdown (manual)
            current_status = t.get("status", "In Progress")
            if current_status not in TASK_STATUSES:
                current_status = "In Progress"

            new_status = st.selectbox(
                "Weekly status",
                TASK_STATUSES,
                index=TASK_STATUSES.index(current_status),
                key=f"task_status_{t['id']}"
            )

            if new_status != current_status:
                t["status"] = new_status
                save_data(data)

            # Manual delete (user prerogative)
            if st.button("Delete task", key=f"del_task_{t['id']}"):
                data["tasks"] = [tt for tt in data["tasks"] if tt["id"] != t["id"]]
                save_data(data)
                st.rerun()

st.divider()

# ----------------------------
# Close Week (log + reset statuses; no deletion)
# ----------------------------
st.subheader("Close Week")

if data.get("last_week_closed") == week:
    st.info("This week has already been closed/logged.")
else:
    in_progress = [t for t in data["tasks"] if t.get("status") == "In Progress"]
    if in_progress:
        st.error(
            f"{len(in_progress)} task(s) are still **In Progress**.\n\n"
            "Mark every task as **Completed** or **Missed** before closing the week."
        )
        close_disabled = True
    else:
        close_disabled = False

    if st.button("✅ Confirm week is over (log results + reset)", key="btn_close_week", disabled=close_disabled):
        events = []
        for t in data["tasks"]:
            status = t.get("status", "In Progress")
            if status == "Completed":
                ev = "completed"
            elif status == "Missed":
                ev = "missed"
            else:
                # Should never happen due to gating, but keep safe:
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

        # Reset weekly statuses for next week (keep tasks)
        for t in data["tasks"]:
            t["status"] = "In Progress"

        data["last_week_closed"] = week
        save_data(data)

        st.success("Week logged. Tasks kept. Statuses reset.")
        st.rerun()

st.divider()

# ----------------------------
# Goal Progress Summary (parses progress_log.json)
# ----------------------------
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

# ----------------------------
# Downloads (optional)
# ----------------------------
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
