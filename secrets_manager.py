"""
secrets_manager.py
Resolves secrets from (in priority order):
  1. Streamlit st.secrets  (Render / Streamlit Cloud with secrets.toml)
  2. OS environment variables (Render env vars or local .env)
  3. Provided default

No private Streamlit APIs are used.
"""

import os
import json
import logging

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


def get_secret(key: str, default: str = "") -> str:
    """Return a secret string value."""
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    value = os.environ.get(key)
    if value is not None:
        return value
    return default


def get_secret_dict(key: str, default: dict | None = None) -> dict:
    """Return a secret that is stored as a JSON dict."""
    if default is None:
        default = {}
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key in st.secrets:
            val = st.secrets[key]
            if isinstance(val, dict):
                return dict(val)
            if isinstance(val, str):
                return json.loads(val)
    except Exception:
        pass
    raw = os.environ.get(key, "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return default


def get_oauth_redirect_uri() -> str:
    """
    Return the OAuth redirect URI.

    Priority:
      1. OAUTH_REDIRECT_URI secret/env (explicit override — always wins)
      2. RENDER_EXTERNAL_URL env set automatically by Render
      3. localhost fallback for local dev
    """
    override = get_secret("OAUTH_REDIRECT_URI", "")
    if override and override.startswith("http"):
        # Normalise — must end with exactly one slash
        return override.rstrip("/") + "/"

    # Render sets this automatically for every deployed service
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url and render_url.startswith("http"):
        return render_url.rstrip("/") + "/"

    return "http://localhost:8501/"


def load_google_credentials() -> dict:
    """
    Load Google OAuth credentials dict (the 'web' or 'installed' sub-object).

    Sources tried in order:
      1. GOOGLE_CREDENTIALS_JSON secret (JSON string or dict)
      2. GOOGLE_CREDENTIALS_FILE path (local file)
    """
    # ── source 1: JSON secret ──────────────────────────────────────────────
    creds_json = get_secret("GOOGLE_CREDENTIALS_JSON", "")
    if creds_json:
        try:
            if isinstance(creds_json, str):
                data = json.loads(creds_json)
            else:
                data = dict(creds_json)
            result = data.get("web") or data.get("installed") or {}
            if result.get("client_id"):
                return result
        except Exception as exc:
            log.warning("GOOGLE_CREDENTIALS_JSON parse error: %s", exc)

    # ── source 2: file ─────────────────────────────────────────────────────
    creds_file = get_secret("GOOGLE_CREDENTIALS_FILE", "google_credentials.json")
    if os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                data = json.load(f)
            result = data.get("web") or data.get("installed") or {}
            if result.get("client_id"):
                return result
        except Exception as exc:
            log.warning("GOOGLE_CREDENTIALS_FILE read error: %s", exc)

    return {}


def has_google_credentials() -> bool:
    creds = load_google_credentials()
    return bool(creds.get("client_id") and creds.get("client_secret"))


def get_whitelisted_emails() -> set:
    """Return the set of authorised email addresses (lower-cased)."""
    raw = get_secret("WHITELISTED_EMAILS", "")
    if isinstance(raw, (list, tuple)):
        return {str(e).strip().lower() for e in raw if str(e).strip()}
    return {e.strip().lower() for e in raw.split(",") if e.strip()}
