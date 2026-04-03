"""
supabase_handler.py
Supabase backend for RCBA Event Reporter.
Works with Render env vars and local .env.

Field mapping (canonical, used throughout the app):
  reports table columns that differ from the old stubs are noted inline.
"""
import base64
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ── credentials ────────────────────────────────────────────────────────────────
try:
    from secrets_manager import get_secret
    SUPABASE_URL = get_secret("SUPABASE_URL", "").strip()
    SUPABASE_KEY = get_secret("SUPABASE_KEY", "").strip()
except Exception:
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

# ── client ─────────────────────────────────────────────────────────────────────
supabase = None
SUPABASE_ENABLED = False

if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client, Client
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        SUPABASE_ENABLED = True
        log.info("[supabase_handler] Supabase initialised OK")
    except Exception as exc:
        log.error("[supabase_handler] Init failed: %s", exc)
else:
    log.warning("[supabase_handler] Missing SUPABASE_URL or SUPABASE_KEY — JSON fallback active")


# ═══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ═══════════════════════════════════════════════════════════════════════════════

def save_report_to_db(report_data: Dict[str, Any], docx_binary: Optional[bytes] = None) -> Dict:
    """
    Insert a report row.

    Accepts the canonical field names written by app.py / report_handler.py:
      member_attendance        → list[str]
      member_attendance_count  → int
      guest_attendance_count   → int
      district_attendance_count → int
      ambassadorial_attendance_count → int
      total_attendance         → int
      avenue_chairs            → list[str]
    """
    if not SUPABASE_ENABLED or supabase is None:
        return {"error": "Supabase not available", "success": False}

    try:
        payload = {
            "event_title":             str(report_data.get("event_title", "")).strip(),
            "event_date":              str(report_data.get("event_start_date", "")).strip() or None,
            "submitted_by_email":      str(report_data.get("submitted_by_email", "")).strip().lower(),
            "submitted_by_name":       str(report_data.get("submitted_by_name", "")).strip(),
            "avenue":                  str(report_data.get("avenue", "")).strip(),
            "drive_link":              str(report_data.get("drive_link", "")).strip(),
            "status":                  "submitted",
            "is_late":                 bool(report_data.get("is_late", False)),
            "submitted_at":            datetime.now().isoformat(),
            # attendance
            "total_attendance":        int(report_data.get("total_attendance") or 0),
            "member_names":            report_data.get("member_attendance") if isinstance(report_data.get("member_attendance"), list) else [],
            "member_attendance_count": int(report_data.get("member_attendance_count") or 0),
            "guest_count":             int(report_data.get("guest_attendance_count") or 0),
            "guest_names":             str(report_data.get("guest_names", "")).strip(),
            "district_count":          int(report_data.get("district_attendance_count") or 0),
            "district_names":          str(report_data.get("district_names", "")).strip(),
            "ambassadorial_count":     int(report_data.get("ambassadorial_attendance_count") or 0),
            "ambassadorial_names":     str(report_data.get("ambassadorial_club_names", "")).strip(),
            "avenue_chairs":           report_data.get("avenue_chairs") if isinstance(report_data.get("avenue_chairs"), list) else [],
        }

        response = supabase.table("reports").insert(payload).execute()
        if not response.data:
            return {"error": "No data returned from insert", "success": False}

        report_id = response.data[0].get("id")
        log.info("[supabase_handler] Report %s saved", report_id)

        # Store DOCX as base64 in docx_files table
        if docx_binary:
            try:
                b64 = base64.b64encode(docx_binary).decode("utf-8")
                docx_resp = supabase.table("docx_files").insert({
                    "report_id":    report_id,
                    "filename":     f"report_{report_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx",
                    "file_content": b64,
                    "file_size":    len(docx_binary),
                }).execute()
                if docx_resp.data:
                    docx_id = docx_resp.data[0].get("id")
                    supabase.table("reports").update({"docx_file_id": docx_id}).eq("id", report_id).execute()
                    log.info("[supabase_handler] DOCX %s saved for report %s", docx_id, report_id)
            except Exception as exc:
                log.warning("[supabase_handler] DOCX save failed: %s", exc)

        return {"success": True, "report_id": report_id}

    except Exception as exc:
        log.error("[supabase_handler] save_report_to_db error: %s", exc)
        return {"error": str(exc), "success": False}


def load_reports_from_db(email: Optional[str] = None, role: Optional[str] = None) -> List[Dict]:
    """Load reports, filtered by role. Returns [] on any error."""
    if not SUPABASE_ENABLED or supabase is None:
        return []
    try:
        if role == "director" and email:
            resp = (
                supabase.table("reports")
                .select("*")
                .eq("submitted_by_email", email.lower())
                .order("submitted_at", desc=True)
                .execute()
            )
        elif role in ("secretariat", "admin", "editor"):
            resp = (
                supabase.table("reports")
                .select("*")
                .order("submitted_at", desc=True)
                .execute()
            )
        else:
            return []

        rows = resp.data or []

        # Normalise field names so the rest of the app always sees the same keys
        for r in rows:
            r.setdefault("event_start_date", r.get("event_date", ""))
            r.setdefault("submission_timestamp", r.get("submitted_at", ""))
            r.setdefault("member_attendance", r.get("member_names", []))
            r.setdefault("member_attendance_count", r.get("member_attendance_count") or len(r.get("member_names", [])))
            r.setdefault("guest_attendance_count", r.get("guest_count", 0))
            r.setdefault("district_attendance_count", r.get("district_count", 0))
            r.setdefault("ambassadorial_attendance_count", r.get("ambassadorial_count", 0))
            r.setdefault("ambassadorial_club_names", r.get("ambassadorial_names", ""))
            r.setdefault("report_id", str(r.get("id", "")))

        return rows
    except Exception as exc:
        log.error("[supabase_handler] load_reports_from_db error: %s", exc)
        return []


def update_report_status_db(report_id: Any, status: str, approved_by: str = "", comments: str = "") -> bool:
    if not SUPABASE_ENABLED or supabase is None:
        return False
    try:
        update_data: Dict[str, Any] = {
            "status":       status,
            "last_updated": datetime.now().isoformat(),
        }
        if approved_by:
            update_data["approved_by_email"] = approved_by
            update_data["approved_at"]        = datetime.now().isoformat()
        if comments:
            update_data["approval_comments"] = comments

        resp = supabase.table("reports").update(update_data).eq("id", int(report_id)).execute()
        return bool(resp.data)
    except Exception as exc:
        log.error("[supabase_handler] update_report_status_db error: %s", exc)
        return False


def update_report_fields_db(report_id: Any, fields: Dict) -> bool:
    """Generic field update by Supabase integer ID."""
    if not SUPABASE_ENABLED or supabase is None:
        return False
    try:
        resp = supabase.table("reports").update(fields).eq("id", int(report_id)).execute()
        return bool(resp.data)
    except Exception as exc:
        log.error("[supabase_handler] update_report_fields_db error: %s", exc)
        return False


def get_docx_file(report_id: Any) -> Optional[bytes]:
    if not SUPABASE_ENABLED or supabase is None:
        return None
    try:
        resp = (
            supabase.table("docx_files")
            .select("file_content")
            .eq("report_id", int(report_id))
            .execute()
        )
        if resp.data and resp.data[0].get("file_content"):
            return base64.b64decode(resp.data[0]["file_content"])
        return None
    except Exception as exc:
        log.error("[supabase_handler] get_docx_file error: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# MEMBERS
# ═══════════════════════════════════════════════════════════════════════════════

def add_member_to_db(name: str, email: str, role: str = "Member") -> bool:
    if not SUPABASE_ENABLED or supabase is None:
        return False
    try:
        resp = supabase.table("members").insert({
            "name":       name.strip(),
            "email":      email.strip().lower(),
            "role":       role.strip(),
            "added_date": datetime.now().isoformat(),
        }).execute()
        return bool(resp.data)
    except Exception as exc:
        log.error("[supabase_handler] add_member_to_db error: %s", exc)
        return False


def get_all_members() -> List[Dict]:
    if not SUPABASE_ENABLED or supabase is None:
        return []
    try:
        resp = supabase.table("members").select("*").order("name").execute()
        return resp.data or []
    except Exception as exc:
        log.error("[supabase_handler] get_all_members error: %s", exc)
        return []


def delete_member_from_db(member_name: str) -> bool:
    if not SUPABASE_ENABLED or supabase is None:
        return False
    try:
        supabase.table("members").delete().eq("name", member_name).execute()
        return True
    except Exception as exc:
        log.error("[supabase_handler] delete_member_from_db error: %s", exc)
        return False


def member_exists(name: str = "", email: str = "") -> bool:
    if not SUPABASE_ENABLED or supabase is None:
        return False
    try:
        if name:
            r = supabase.table("members").select("id").eq("name", name).execute()
            if r.data:
                return True
        if email:
            r = supabase.table("members").select("id").eq("email", email.lower()).execute()
            if r.data:
                return True
        return False
    except Exception as exc:
        log.error("[supabase_handler] member_exists error: %s", exc)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# ROLES (stored in Supabase roles_config table)
# ═══════════════════════════════════════════════════════════════════════════════

def get_role_from_db(email: str) -> Optional[str]:
    if not SUPABASE_ENABLED or supabase is None:
        return None
    try:
        resp = (
            supabase.table("roles_config")
            .select("role")
            .eq("email", email.lower())
            .execute()
        )
        return resp.data[0]["role"] if resp.data else None
    except Exception as exc:
        log.error("[supabase_handler] get_role_from_db error: %s", exc)
        return None


def assign_role_in_db(email: str, role: str) -> bool:
    if not SUPABASE_ENABLED or supabase is None:
        return False
    try:
        payload = {"email": email.lower(), "role": role, "assigned_at": datetime.now().isoformat()}
        existing = supabase.table("roles_config").select("id").eq("email", email.lower()).execute()
        if existing.data:
            resp = supabase.table("roles_config").update(payload).eq("email", email.lower()).execute()
        else:
            resp = supabase.table("roles_config").insert(payload).execute()
        return bool(resp.data)
    except Exception as exc:
        log.error("[supabase_handler] assign_role_in_db error: %s", exc)
        return False
