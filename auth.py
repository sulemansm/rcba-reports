"""
auth.py — Role resolution for RCBA Event Reporter.

Roles (precedence: admin > secretariat > editor > director):
  admin        → super admin (same as secretariat + can manage admins)
  secretariat  → full admin view, approve/reject
  editor       → view all + submit
  director     → submit own + view own (default for any whitelisted member)

Role data sources (tried in order):
  1. Supabase roles_config table
  2. Local roles.json
"""

import json
import logging
import os
from typing import Literal

log = logging.getLogger(__name__)

RoleType = Literal["admin", "secretariat", "editor", "director"]

ROLES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roles.json")


def _load_roles_json() -> dict:
    if not os.path.exists(ROLES_FILE):
        return {"admin_emails": [], "secretariat_emails": [], "editor_emails": [], "director_emails": [], "roles": {}}
    try:
        with open(ROLES_FILE) as f:
            return json.load(f)
    except Exception as exc:
        log.error("roles.json load error: %s", exc)
        return {"admin_emails": [], "secretariat_emails": [], "editor_emails": [], "director_emails": [], "roles": {}}


def get_role(email: str) -> RoleType:
    """Return the role for the given email. Default: 'director'."""
    email = email.strip().lower()

    # ── 1. Supabase roles_config ───────────────────────────────────────────
    try:
        from supabase_handler import get_role_from_db, SUPABASE_ENABLED
        if SUPABASE_ENABLED:
            db_role = get_role_from_db(email)
            if db_role and db_role in ("admin", "secretariat", "editor", "director"):
                return db_role  # type: ignore[return-value]
    except Exception as exc:
        log.warning("get_role_from_db failed: %s", exc)

    # ── 2. roles.json ─────────────────────────────────────────────────────
    data = _load_roles_json()
    admin       = {e.strip().lower() for e in data.get("admin_emails", [])}
    secretariat = {e.strip().lower() for e in data.get("secretariat_emails", [])}
    editors     = {e.strip().lower() for e in data.get("editor_emails", [])}
    directors   = {e.strip().lower() for e in data.get("director_emails", [])}
    roles_map   = {k.strip().lower(): v for k, v in data.get("roles", {}).items()}

    if email in admin:
        return "admin"
    if email in secretariat:
        return "secretariat"
    if email in editors:
        return "editor"
    if email in directors:
        return "director"

    # Legacy flat map
    mapped = roles_map.get(email, "")
    if mapped in ("admin", "secretariat", "editor", "director"):
        return mapped  # type: ignore[return-value]

    return "director"


# ── permission helpers ────────────────────────────────────────────────────────

def is_admin(role: str) -> bool:
    return role == "admin"

def is_secretariat(role: str) -> bool:
    return role == "secretariat"

def is_admin_or_secretariat(role: str) -> bool:
    return role in ("admin", "secretariat")

def can_approve_reject(role: str) -> bool:
    return is_admin_or_secretariat(role)

def can_view_all_reports(role: str) -> bool:
    return role in ("admin", "secretariat", "editor")

def can_submit_report(role: str) -> bool:
    return role in ("admin", "secretariat", "director", "editor")

def can_mark_late(role: str) -> bool:
    return is_admin_or_secretariat(role)


# ── display helpers ───────────────────────────────────────────────────────────

def role_display_label(role: str) -> str:
    return {"admin": "Admin", "secretariat": "Secretariat", "editor": "Editor", "director": "Director"}.get(role, "Member")

def role_badge_class(role: str) -> str:
    return {"admin": "admin", "secretariat": "secretariat", "editor": "editor", "director": "director"}.get(role, "director")
