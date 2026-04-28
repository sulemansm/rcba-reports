"""
dashboard.py — Role-specific dashboard views for RCBA Event Reporter.

Roles handled:
  secretariat / admin → full table, filters, approve/reject/late-mark, download
  editor              → full table, read-only + download
  director            → own reports only
"""

import json
import logging
import os
import re
import time

import pandas as pd
import streamlit as st
from datetime import datetime

from report_handler import (
    load_reports,
    update_report,
    update_report_by_id,
    filter_reports,
    get_my_reports,
    compute_stats,
    is_late_report,
    get_status,
)
from auth import can_approve_reject, can_view_all_reports, can_mark_late

log = logging.getLogger(__name__)

# ── helpers ───────────────────────────────────────────────────────────────────

def _status_text(status: str) -> str:
    return {"Approved": "✅ Approved", "Rejected": "❌ Rejected"}.get(status, "🕐 Pending")


def _late_text(report: dict) -> str:
    """Always pass the full report dict — uses is_late_report internally."""
    return "🔴 Late" if is_late_report(report) else "🟢 On Time"


def _patch(rid: str, fallback_idx: int, fields: dict) -> None:
    if rid and update_report_by_id(rid, fields):
        return
    update_report(fallback_idx, fields)


# ── stats bar ─────────────────────────────────────────────────────────────────

def render_stats_bar(stats: dict, show_late: bool = True) -> None:
    cols = st.columns(5 if show_late else 4)
    cols[0].metric("Total",    stats["total"])
    cols[1].metric("Approved", stats["approved"])
    cols[2].metric("Rejected", stats["rejected"])
    cols[3].metric("Pending",  stats["pending"])
    if show_late:
        cols[4].metric("Late", stats["late"])
    st.markdown("<div style='margin-bottom:0.6rem;'></div>", unsafe_allow_html=True)


# ── filter bar ────────────────────────────────────────────────────────────────

def render_filter_bar(reports: list, prefix: str = "") -> dict:
    all_submitters = sorted({
        r.get("submitted_by_name") or r.get("submitted_by_email", "Unknown")
        for r in reports
    })

    with st.expander("🔍  Filters & Search", expanded=False):
        fc1, fc2, fc3 = st.columns([2, 2, 3])
        with fc1:
            status_filter = st.selectbox(
                "Status", ["All", "Pending", "Approved", "Rejected"],
                key=f"{prefix}_filter_status"
            )
        with fc2:
            submitter_filter = st.selectbox(
                "Submitted by", ["All"] + all_submitters,
                key=f"{prefix}_filter_submitter"
            )
        with fc3:
            search_q = st.text_input(
                "Search (title / member / avenue)",
                placeholder="Type to search...",
                key=f"{prefix}_filter_search"
            )

    return {"status": status_filter, "submitted_by": submitter_filter, "search": search_q}


# ── DOCX fetch (Supabase or disk) ─────────────────────────────────────────────

def _get_docx_bytes(r: dict) -> bytes | None:
    # get_docx_file queries docx_files by report_id (the report's integer PK)
    report_db_id = r.get("id")
    if report_db_id:
        try:
            from supabase_handler import get_docx_file
            data = get_docx_file(report_db_id)
            if data:
                return data
        except Exception as exc:
            log.warning("Supabase DOCX fetch failed: %s", exc)

    path = r.get("file_path", "")
    if path and os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()

    return None


# ── summary table ─────────────────────────────────────────────────────────────

def render_report_summary_table(reports: list, show_submitter: bool = True) -> None:
    if not reports:
        return
    rows = []
    for r in reports:
        status  = get_status(r)
        ev_date = (r.get("event_start_date") or r.get("event_date") or "")[:10] or "—"
        sub_at  = (r.get("submission_timestamp") or r.get("submitted_at") or "")[:10] or "—"
        total   = r.get("total_attendance") or r.get("member_attendance_count") or 0
        row = {
            "📄 Title":     r.get("event_title", "—"),
            "🏢 Avenue":    r.get("avenue", "—") or "—",
            "👥 Attend.":   str(total),
            "📅 Event":     ev_date,
            "✅ Status":    _status_text(status),
            "⏰ Timeliness": _late_text(r),
            "📤 Submitted": sub_at,
        }
        if show_submitter:
            row["👤 By"] = r.get("submitted_by_name", "—")
        rows.append(row)

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


# ── single report action card ─────────────────────────────────────────────────

def render_report_row(
    r: dict,
    idx: int,
    real_idx: int,
    role: str,
    reviewer_name: str,
    send_review_email_fn,
    show_submitter: bool = True,
) -> None:
    status = get_status(r)
    rid    = r.get("report_id") or str(r.get("id", ""))

    border = {"Approved": "#4ECDC4", "Rejected": "#FF6B6B"}.get(status, "#FFB347")

    st.markdown(
        f"<div style='border-left:3px solid {border};background:#111827;"
        f"border-radius:10px;padding:0.75rem 1rem 0.5rem;margin-bottom:0.3rem;"
        f"border:1px solid rgba(255,255,255,0.07);'>"
        f"<span style='font-family:Syne,sans-serif;font-size:0.95rem;font-weight:700;"
        f"color:#E8EDF5;'>{r.get('event_title','—')}</span></div>",
        unsafe_allow_html=True,
    )

    ev_date = (r.get("event_start_date") or r.get("event_date") or "")[:10] or "—"
    sub_at  = (r.get("submission_timestamp") or r.get("submitted_at") or "")[:10] or "—"
    sub_line = f"**By:** {r.get('submitted_by_name','—')}  · " if show_submitter else ""
    st.markdown(
        f"🗂 **{r.get('avenue','—')}**  ·  📅 {ev_date}  ·  "
        f"{sub_line}📤 Submitted {sub_at}  ·  {_late_text(r)}  ·  {_status_text(status)}"
    )

    rej_msg = r.get("rejection_message") or r.get("review_comment", "")
    if rej_msg:
        rb = r.get("reviewed_by", "")
        ra = (r.get("reviewed_at", "") or "")[:10]
        prefix = f"**{rb}** · {ra} — " if rb else ""
        st.caption(f"💬 {prefix}{rej_msg}")

    with st.expander(
        f"{'Manage' if can_approve_reject(role) else 'Details'} — {r.get('event_title','Report')}",
        expanded=False
    ):
        docx_bytes = _get_docx_bytes(r)
        if docx_bytes:
            ev_safe = r.get("event_title", "report").replace(" ", "_")
            st.download_button(
                "⬇  Download DOCX",
                data=docx_bytes,
                file_name=f"RCBA_Report_{ev_safe}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                key=f"dl_{rid or real_idx}",
                use_container_width=True,
            )
        else:
            st.caption("No DOCX file attached to this report.")

        if can_approve_reject(role):
            existing = r.get("rejection_message") or r.get("review_comment", "")
            comment_val = st.text_area(
                "Comments / Rejection message",
                value=existing,
                placeholder="Provide feedback or rejection reason (required for rejection)...",
                height=90,
                key=f"comment_{rid or real_idx}",
            )
            col_a, col_r, col_late, _ = st.columns([1, 1, 1, 1])

            with col_a:
                if st.button("✓ Approve", key=f"approve_{rid or real_idx}", type="primary", use_container_width=True):
                    with st.spinner("Saving & emailing…"):
                        try:
                            send_review_email_fn(r, "approve", comment_val, reviewer_name)
                            _patch(rid, real_idx, {
                                "status": "Approved", "approval_status": "approved",
                                "review_comment": comment_val, "rejection_message": "",
                                "reviewed_by": reviewer_name,
                                "reviewed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            })
                            st.success(f"Approved — email sent to {r.get('submitted_by_email','member')}.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not send email: {exc}")

            with col_r:
                if st.button("✗ Reject", key=f"reject_{rid or real_idx}", type="secondary", use_container_width=True):
                    if not comment_val.strip():
                        st.warning("Please add a rejection reason before rejecting.")
                    else:
                        with st.spinner("Saving & emailing…"):
                            try:
                                send_review_email_fn(r, "reject", comment_val, reviewer_name)
                                _patch(rid, real_idx, {
                                    "status": "Rejected", "approval_status": "changes",
                                    "rejection_message": comment_val, "review_comment": comment_val,
                                    "reviewed_by": reviewer_name,
                                    "reviewed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                })
                                st.success(f"Rejected — email sent to {r.get('submitted_by_email','member')}.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Could not send email: {exc}")

            if can_mark_late(role):
                with col_late:
                    cur_late = is_late_report(r)
                    label    = "Mark On-Time" if cur_late else "Mark Late"
                    if st.button(label, key=f"late_{rid or real_idx}", use_container_width=True):
                        _patch(rid, real_idx, {"is_late": not cur_late})
                        st.rerun()


# ── full table renderer ───────────────────────────────────────────────────────

def render_reports_table(
    reports: list,
    role: str,
    reviewer_name: str,
    send_review_email_fn,
    show_submitter: bool = True,
) -> None:
    if not reports:
        st.markdown(
            "<div style='background:var(--surface);border:1px solid var(--border);"
            "border-radius:16px;padding:2rem;text-align:center;'>"
            "<p style='color:var(--muted);margin:0;'>No reports to display</p></div>",
            unsafe_allow_html=True,
        )
        return

    render_report_summary_table(reports, show_submitter=show_submitter)
    st.markdown("<div style='margin:2rem 0 1rem;border-top:1px solid var(--border);padding-top:1.5rem;'></div>", unsafe_allow_html=True)
    st.markdown("<p style='color:var(--muted);font-size:0.9rem;margin-bottom:1rem;'>Actions & Details</p>", unsafe_allow_html=True)

    for idx, r in enumerate(reports):
        render_report_row(
            r, idx, idx, role, reviewer_name,
            send_review_email_fn, show_submitter=show_submitter
        )
        st.markdown("<div style='border-top:1px solid rgba(255,255,255,0.05);margin:1rem 0;'></div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — Secretariat dashboard
# ══════════════════════════════════════════════════════════════════════════════

def page_dashboard_secretariat(send_review_email_fn) -> None:
    st.markdown("""
    <div style='margin-bottom:2rem;'>
        <h1 style='color:#E8EDF5;font-size:2rem;margin:0;font-family:Syne,sans-serif;font-weight:700;'>📋 All Submissions</h1>
        <p style='color:var(--muted);font-size:0.95rem;margin:0.5rem 0 0;'>Review and approve submissions from your team members</p>
    </div>
    """, unsafe_allow_html=True)

    _role = st.session_state.get("role", "secretariat")
    _email = st.session_state.get("user_email", "")
    all_reports = load_reports(email=_email, role=_role)
    render_stats_bar(compute_stats(all_reports), show_late=True)

    if not all_reports:
        st.info("No submissions yet. Reports will appear here once members submit them.")
        return

    filters  = render_filter_bar(all_reports, prefix="sec")
    filtered = filter_reports(all_reports, status=filters["status"],
                              submitted_by=filters["submitted_by"], search=filters["search"])

    st.markdown(f"""
    <div style='background:rgba(0,201,177,0.08);border-left:3px solid #00C9B1;
    border-radius:8px;padding:1rem;margin:1rem 0 1.5rem;'>
        <p style='color:#00C9B1;font-weight:600;margin:0;font-size:0.9rem;'>
        📊 Showing {len(filtered)} of {len(all_reports)} submission(s)</p>
    </div>
    """, unsafe_allow_html=True)

    reviewer_name = st.session_state.get("username", "Secretariat")
    render_reports_table(filtered, role="secretariat", reviewer_name=reviewer_name,
                         send_review_email_fn=send_review_email_fn, show_submitter=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — Editor dashboard
# ══════════════════════════════════════════════════════════════════════════════

def page_dashboard_editor(send_review_email_fn) -> None:
    st.markdown("""
    <div style='margin-bottom:2rem;'>
        <h1 style='color:#E8EDF5;font-size:2rem;margin:0;font-family:Syne,sans-serif;font-weight:700;'>📑 All Reports</h1>
        <p style='color:var(--muted);font-size:0.95rem;margin:0.5rem 0 0;'>View and download all submitted reports</p>
    </div>
    """, unsafe_allow_html=True)

    _role = st.session_state.get("role", "editor")
    _email = st.session_state.get("user_email", "")
    all_reports = load_reports(email=_email, role=_role)
    render_stats_bar(compute_stats(all_reports), show_late=False)

    if not all_reports:
        st.info("No reports yet.")
        return

    filters  = render_filter_bar(all_reports, prefix="ed")
    filtered = filter_reports(all_reports, status=filters["status"],
                              submitted_by=filters["submitted_by"], search=filters["search"])

    st.markdown(f"""
    <div style='background:rgba(0,201,177,0.08);border-left:3px solid #00C9B1;
    border-radius:8px;padding:1rem;margin:1rem 0 1.5rem;'>
        <p style='color:#00C9B1;font-weight:600;margin:0;font-size:0.9rem;'>
        📊 Showing {len(filtered)} of {len(all_reports)} report(s)</p>
    </div>
    """, unsafe_allow_html=True)

    render_reports_table(filtered, role="editor", reviewer_name="",
                         send_review_email_fn=send_review_email_fn, show_submitter=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — Director dashboard (own reports only)
# ══════════════════════════════════════════════════════════════════════════════

def page_dashboard_director() -> None:
    email = st.session_state.get("user_email", "")

    st.markdown("""
    <div style='margin-bottom:2rem;'>
        <h1 style='color:#E8EDF5;font-size:2rem;margin:0;font-family:Syne,sans-serif;font-weight:700;'>📊 My Reports</h1>
        <p style='color:var(--muted);font-size:0.95rem;margin:0.5rem 0 0;'>Track the status of your submitted event reports</p>
    </div>
    """, unsafe_allow_html=True)

    all_reports = load_reports(email=email, role="director")
    my_reports  = get_my_reports(all_reports, email)

    if not my_reports:
        st.info('No reports yet. Submit your first event report via "New Report" above.')
        return

    render_stats_bar(compute_stats(my_reports), show_late=True)

    search_q = st.text_input("🔍 Search reports", placeholder="Search by title or avenue…", key="dir_search")
    filtered = filter_reports(my_reports, search=search_q or None)

    st.markdown(f"""
    <div style='background:rgba(0,201,177,0.08);border-left:3px solid #00C9B1;
    border-radius:8px;padding:1rem;margin:1rem 0 1.5rem;'>
        <p style='color:#00C9B1;font-weight:600;margin:0;font-size:0.9rem;'>
        📄 You have {len(filtered)} report(s)</p>
    </div>
    """, unsafe_allow_html=True)

    render_reports_table(filtered, role="director", reviewer_name="",
                         send_review_email_fn=lambda *a, **kw: None, show_submitter=False)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — Admin: whitelist + role management
# ══════════════════════════════════════════════════════════════════════════════

def _roles_file_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "roles.json")


def load_roles_file() -> dict:
    try:
        with open(_roles_file_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"secretariat_emails": [], "director_emails": [], "editor_emails": [], "admin_emails": [], "roles": {}}
    except Exception as exc:
        log.error("load_roles_file error: %s", exc)
        return {"secretariat_emails": [], "director_emails": [], "editor_emails": [], "admin_emails": [], "roles": {}}


def save_roles_file(data: dict) -> None:
    try:
        with open(_roles_file_path(), "w") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log.error("save_roles_file error: %s", exc)
        st.error(f"Could not save roles: {exc}")


def is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email))


def page_admin() -> None:
    if st.session_state.get("role") != "admin":
        st.error("❌ Access Denied — Admin only")
        st.stop()

    st.markdown("<p class='rcba-page-title'>Team Management</p>", unsafe_allow_html=True)
    st.markdown("<p class='rcba-page-sub'>Manage the authorised member whitelist and assign roles.</p>", unsafe_allow_html=True)

    roles_data = load_roles_file()
    secretariat_emails = set(roles_data.get("secretariat_emails", []))
    director_emails    = set(roles_data.get("director_emails", []))
    editor_emails      = set(roles_data.get("editor_emails", []))

    whitelist = sorted(secretariat_emails | director_emails | editor_emails |
                       set(roles_data.get("admin_emails", [])))

    member_roles = {}
    for email in whitelist:
        if email in set(roles_data.get("admin_emails", [])):
            member_roles[email] = "admin"
        elif email in secretariat_emails:
            member_roles[email] = "secretariat"
        elif email in editor_emails:
            member_roles[email] = "editor"
        else:
            member_roles[email] = "director"

    tab1, tab2 = st.tabs(["➕ Add Member", "📋 Current Members"])

    with tab1:
        st.markdown("""
        <div style='background:rgba(0,201,177,0.08);border:1px solid rgba(0,201,177,0.25);
        border-radius:12px;padding:1.2rem;margin-bottom:1.5rem;'>
            <p style='color:#00C9B1;font-weight:600;margin:0;'>Add New Members</p>
            <p style='color:var(--muted);font-size:0.9rem;margin:0.5rem 0 0;'>
            Separate multiple emails with commas or new lines.</p>
        </div>
        """, unsafe_allow_html=True)

        col1, col2 = st.columns([2, 1])
        with col1:
            new_emails_raw = st.text_area(
                "Email Address(es)", height=100, key="new_member_email",
                placeholder="member@example.com\nmember2@example.com",
                label_visibility="collapsed",
            )
        with col2:
            new_role = st.selectbox("Role", ["director", "editor", "secretariat"], key="new_member_role")

        if st.button("+ Add Member(s)", type="primary", use_container_width=True, key="btn_add_members"):
            email_list = [
                e.strip().lower()
                for line in new_emails_raw.splitlines()
                for e in line.split(",")
                if e.strip()
            ]
            if not email_list:
                st.error("Please enter at least one email address.")
            else:
                added, invalid, existing = [], [], []
                for email in email_list:
                    if not is_valid_email(email):
                        invalid.append(email)
                    elif email in member_roles:
                        existing.append(email)
                    else:
                        added.append(email)
                        if new_role == "secretariat":
                            secretariat_emails.add(email)
                        elif new_role == "editor":
                            editor_emails.add(email)
                        else:
                            director_emails.add(email)

                if added:
                    roles_data["secretariat_emails"] = sorted(secretariat_emails)
                    roles_data["editor_emails"]      = sorted(editor_emails)
                    roles_data["director_emails"]    = sorted(director_emails)
                    save_roles_file(roles_data)
                    st.success(f"✅ Added {len(added)} member(s) as {new_role}: {', '.join(added)}")
                if existing:
                    st.warning(f"Already in system: {', '.join(existing)}")
                if invalid:
                    st.error(f"Invalid emails: {', '.join(invalid)}")
                if added:
                    time.sleep(1)
                    st.rerun()

    with tab2:
        if not whitelist:
            st.info("No members added yet.")
        else:
            df = pd.DataFrame([{"Email": e, "Role": member_roles.get(e, "director")} for e in whitelist])
            st.dataframe(df, use_container_width=True, hide_index=True)

            st.markdown("---")
            st.subheader("Update Member", divider="gray")

            sel = st.selectbox("Select Member", whitelist, key="select_member_to_update")
            if sel:
                cur_role = member_roles.get(sel, "director")
                role_opts = ["director", "editor", "secretariat"]
                new_role_upd = st.selectbox(
                    "New Role", role_opts,
                    index=role_opts.index(cur_role) if cur_role in role_opts else 0,
                    key="update_member_role"
                )
                col_u, col_d = st.columns(2)
                with col_u:
                    if st.button("Update Role", type="primary", use_container_width=True, key="btn_update_role"):
                        secretariat_emails.discard(sel)
                        editor_emails.discard(sel)
                        director_emails.discard(sel)
                        if new_role_upd == "secretariat":
                            secretariat_emails.add(sel)
                        elif new_role_upd == "editor":
                            editor_emails.add(sel)
                        else:
                            director_emails.add(sel)
                        roles_data["secretariat_emails"] = sorted(secretariat_emails)
                        roles_data["editor_emails"]      = sorted(editor_emails)
                        roles_data["director_emails"]    = sorted(director_emails)
                        save_roles_file(roles_data)
                        st.success(f"✓ {sel} updated to {new_role_upd}.")
                        st.rerun()
                with col_d:
                    if st.button("Remove Member", type="secondary", use_container_width=True, key="btn_delete_member"):
                        secretariat_emails.discard(sel)
                        editor_emails.discard(sel)
                        director_emails.discard(sel)
                        admin_set = set(roles_data.get("admin_emails", []))
                        admin_set.discard(sel)
                        roles_data["secretariat_emails"] = sorted(secretariat_emails)
                        roles_data["editor_emails"]      = sorted(editor_emails)
                        roles_data["director_emails"]    = sorted(director_emails)
                        roles_data["admin_emails"]       = sorted(admin_set)
                        save_roles_file(roles_data)
                        st.success(f"✓ {sel} removed.")
                        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE — Manage club members (for attendance / avenue chair dropdowns)
# ══════════════════════════════════════════════════════════════════════════════

def page_manage_members() -> None:
    current_role = st.session_state.get("role")
    if current_role not in ("admin", "secretariat"):
        st.error("❌ Access Denied — Admin/Secretariat only")
        st.stop()

    try:
        from supabase_handler import (
            get_all_members, add_member_to_db, delete_member_from_db,
            member_exists, SUPABASE_ENABLED,
        )
        USE_SUPABASE = SUPABASE_ENABLED
    except Exception:
        USE_SUPABASE = False

    members_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "members.json")

    def load_members_list():
        if USE_SUPABASE:
            try:
                rows = get_all_members()
                if rows is not None:
                    return rows
            except Exception as exc:
                log.warning("Supabase get_all_members failed: %s", exc)
        if os.path.exists(members_file):
            try:
                with open(members_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def save_member(name, email):
        if USE_SUPABASE:
            try:
                if add_member_to_db(name, email, "Member"):
                    return True
            except Exception as exc:
                log.warning("Supabase add_member_to_db failed: %s", exc)
        members = load_members_list()
        members.append({"name": name.strip(), "email": email.strip().lower(),
                         "role": "Member", "added_date": str(datetime.now())})
        try:
            with open(members_file, "w") as f:
                json.dump(members, f, indent=2)
            return True
        except Exception:
            return False

    def delete_member(name):
        if USE_SUPABASE:
            try:
                if delete_member_from_db(name):
                    return True
            except Exception as exc:
                log.warning("Supabase delete_member_from_db failed: %s", exc)
        members = load_members_list()
        new_list = [m for m in members if m.get("name") != name]
        try:
            with open(members_file, "w") as f:
                json.dump(new_list, f, indent=2)
            return True
        except Exception:
            return False

    def check_exists(name="", email=""):
        if USE_SUPABASE:
            try:
                return member_exists(name=name, email=email)
            except Exception:
                pass
        members = load_members_list()
        if name:
            return any(m.get("name", "").lower() == name.lower() for m in members)
        if email:
            return any(m.get("email", "").lower() == email.lower() for m in members)
        return False

    st.markdown("<p class='rcba-page-title'>Manage Club Members</p>", unsafe_allow_html=True)
    st.markdown("<p class='rcba-page-sub'>Add, view, and manage members for attendance and avenue chair roles.</p>", unsafe_allow_html=True)

    members_list = load_members_list()
    tab1, tab2 = st.tabs(["➕ Add Members", "📋 View Members"])

    with tab1:
        c1, c2 = st.columns([2, 1.5])
        with c1:
            member_name  = st.text_input("Member Name *", placeholder="Full name", key="member_name_pg")
        with c2:
            member_email = st.text_input("Email Address", placeholder="email@example.com", key="member_email_pg")

        if st.button("➕ Add Member", type="primary", use_container_width=True, key="btn_add_member_pg"):
            if not member_name.strip():
                st.error("Member name is required.")
            elif member_email.strip() and not is_valid_email(member_email.strip()):
                st.error("Invalid email format.")
            elif check_exists(name=member_name.strip()):
                st.error("This member name already exists.")
            elif member_email.strip() and check_exists(email=member_email.strip()):
                st.error("This email is already registered.")
            else:
                if save_member(member_name.strip(), member_email.strip()):
                    st.success(f"✅ {member_name} added successfully!")
                    st.balloons()
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error("Failed to add member. Please try again.")

    with tab2:
        if not members_list:
            st.info("📭 No club members added yet.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Total Members", len(members_list))
            c2.metric("Latest Added", (members_list[-1].get("added_date") or "—")[:10])
            c3.metric("Status", "Active")

            st.markdown("---")
            rows = [
                {"#": i + 1, "Name": m.get("name", "—"), "Email": m.get("email", "—"),
                 "Added": (m.get("added_date") or "—")[:10]}
                for i, m in enumerate(members_list)
            ]
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button(
                "📥 Download CSV", df.to_csv(index=False),
                file_name=f"members_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv", use_container_width=True,
            )

            st.markdown("---")
            st.subheader("Remove Member", divider="gray")
            if current_role == "secretariat":
                removable = [m for m in members_list if m.get("role", "Member") == "Member"]
            else:
                removable = members_list

            if not removable:
                st.info("No removable members.")
            else:
                names = [m.get("name", "") for m in removable]
                sel_rm = st.selectbox("Select member to remove", names, key="sel_rm")
                c1, c2 = st.columns([0.8, 0.2])
                c1.info(f"⚠️ This will remove **{sel_rm}** from the members list.")
                with c2:
                    if st.button("🗑️ Remove", type="secondary", use_container_width=True, key="btn_rm"):
                        if delete_member(sel_rm):
                            st.success(f"✅ {sel_rm} removed.")
                            time.sleep(1)
                            st.rerun()
                        else:
                            st.error("Failed to remove member.")