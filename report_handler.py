"""
report_handler.py
Unified report persistence layer.
Primary: Supabase.  Fallback: local reports_store.json.
"""
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ── Supabase import (graceful fallback) ────────────────────────────────────────
try:
    from supabase_handler import (
        save_report_to_db,
        load_reports_from_db,
        update_report_status_db,
        update_report_fields_db,
        SUPABASE_ENABLED,
    )
    USE_SUPABASE = SUPABASE_ENABLED
except Exception as exc:
    USE_SUPABASE = False
    log.warning("[report_handler] Supabase init failed — JSON fallback: %s", exc)

# ── JSON fallback path ─────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS_FILE = os.path.join(_HERE, "reports_store.json")
os.makedirs(_HERE, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# JSON helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _load_json() -> List[Dict]:
    if not os.path.exists(REPORTS_FILE):
        return []
    try:
        with open(REPORTS_FILE) as f:
            return json.load(f)
    except Exception as exc:
        log.error("[report_handler] JSON load error: %s", exc)
        return []


def _save_json(reports: List[Dict]) -> None:
    try:
        with open(REPORTS_FILE, "w") as f:
            json.dump(reports, f, indent=2, default=str)
    except Exception as exc:
        log.error("[report_handler] JSON save error: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def save_report(record: dict, docx_binary: Optional[bytes] = None) -> Dict:
    """Persist a new report record (Supabase first, JSON fallback)."""
    record.setdefault("report_id", str(uuid.uuid4())[:8].upper())
    record.setdefault("status", "submitted")
    record.setdefault("submission_timestamp", str(datetime.now()))

    if USE_SUPABASE:
        try:
            result = save_report_to_db(record, docx_binary=docx_binary)
            if result.get("success"):
                log.info("[report_handler] Supabase save OK — id %s", result.get("report_id"))
                return result
            log.warning("[report_handler] Supabase save returned failure: %s", result)
        except Exception as exc:
            log.error("[report_handler] Supabase save exception: %s — falling back to JSON", exc)

    # JSON fallback
    reports = _load_json()
    reports.append(record)
    _save_json(reports)
    log.info("[report_handler] JSON save OK")
    return {"success": True, "report_id": record.get("report_id")}


def load_reports(email: Optional[str] = None, role: Optional[str] = None) -> List[Dict]:
    """Load reports (Supabase first, JSON fallback)."""
    if USE_SUPABASE:
        try:
            rows = load_reports_from_db(email=email, role=role)
            log.info("[report_handler] Supabase loaded %d reports", len(rows))
            return rows
        except Exception as exc:
            log.error("[report_handler] Supabase load error: %s — falling back to JSON", exc)

    rows = _load_json()
    if role == "director" and email:
        rows = [r for r in rows if r.get("submitted_by_email", "").lower() == email.lower()]
    elif role not in ("secretariat", "admin", "editor", None):
        rows = []
    return sorted(rows, key=lambda r: r.get("submission_timestamp", ""), reverse=True)


def update_report(idx: int, fields: dict) -> None:
    """Update report by JSON list index (legacy fallback path only)."""
    reports = _load_json()
    if 0 <= idx < len(reports):
        reports[idx].update(fields)
        _save_json(reports)


def update_report_by_id(report_id: str, fields: dict) -> bool:
    """
    Update report by report_id (string UUID used in JSON) or integer Supabase id.
    Returns True on success.
    """
    # Try Supabase numeric ID first
    if USE_SUPABASE:
        try:
            numeric_id = int(report_id)
            return update_report_fields_db(numeric_id, fields)
        except (ValueError, TypeError):
            pass

    # JSON fallback
    reports = _load_json()
    for r in reports:
        if r.get("report_id") == report_id:
            r.update(fields)
            _save_json(reports)
            return True
    return False


def update_report_status(report_id_or_int, status: str, approved_by: str = "", comments: str = "") -> bool:
    if USE_SUPABASE:
        try:
            numeric_id = int(report_id_or_int)
            return update_report_status_db(
                numeric_id, status,
                approved_by=approved_by, comments=comments
            )
        except (ValueError, TypeError):
            log.warning("[report_handler] update_report_status: '%s' is not a numeric Supabase ID — falling back to JSON", report_id_or_int)
        except Exception as exc:
            log.error("[report_handler] Supabase status update error: %s", exc)

    return update_report_by_id(str(report_id_or_int), {
        "status":            status,
        "approved_by":       approved_by,
        "approval_comments": comments,
        "approved_at":       str(datetime.now()) if approved_by else "",
    })


def get_status(report: dict) -> str:
    """Normalise varied status strings to Pending / Approved / Rejected."""
    raw = report.get("status") or report.get("approval_status", "submitted")
    mapping = {
        "submitted": "Pending",
        "pending":   "Pending",
        "approved":  "Approved",
        "rejected":  "Rejected",
        "changes":   "Rejected",
    }
    return mapping.get(str(raw).lower().strip(), "Pending")


def is_late(event_date_str: str, submitted_at_str: str) -> bool:
    """Return True when submission is more than 7 days after the event date."""
    try:
        ev  = datetime.strptime(str(event_date_str)[:10],  "%Y-%m-%d").date()
        sub = datetime.strptime(str(submitted_at_str)[:10], "%Y-%m-%d").date()
        return (sub - ev).days > 7
    except Exception:
        return False


def is_late_report(report: dict) -> bool:
    """Convenience wrapper that accepts a full report dict."""
    ev  = report.get("event_date") or report.get("event_start_date", "")
    sub = report.get("submitted_at") or report.get("submission_timestamp", "")
    if "is_late" in report and isinstance(report["is_late"], bool):
        return report["is_late"]
    return is_late(ev, sub)


def filter_reports(
    reports: List[dict],
    status: Optional[str] = None,
    submitted_by: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
) -> List[dict]:
    out = reports

    if status and status != "All":
        out = [r for r in out if get_status(r) == status]

    if submitted_by and submitted_by != "All":
        out = [r for r in out if
               r.get("submitted_by_email", "").lower() == submitted_by.lower() or
               r.get("submitted_by_name", "").lower() == submitted_by.lower()]

    if date_from:
        out = [r for r in out if
               (r.get("event_date") or r.get("event_start_date", ""))[:10] >= date_from]

    if date_to:
        out = [r for r in out if
               (r.get("event_date") or r.get("event_start_date", ""))[:10] <= date_to]

    if search:
        q = search.lower()
        out = [r for r in out if
               q in r.get("event_title", "").lower() or
               q in r.get("submitted_by_name", "").lower() or
               q in r.get("avenue", "").lower()]

    return out


def get_my_reports(reports: List[dict], email: str) -> List[dict]:
    return [r for r in reports if r.get("submitted_by_email", "").lower() == email.lower()]


def compute_stats(reports: List[dict]) -> dict:
    total    = len(reports)
    approved = sum(1 for r in reports if get_status(r) == "Approved")
    rejected = sum(1 for r in reports if get_status(r) == "Rejected")
    pending  = total - approved - rejected
    late     = sum(1 for r in reports if is_late_report(r))
    return {"total": total, "approved": approved, "rejected": rejected, "pending": pending, "late": late}