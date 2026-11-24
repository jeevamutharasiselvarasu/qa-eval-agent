"""Settings stored in the app database (Settings page), with .env values as the fallback."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.common import audit
from ..core.secrets import decrypt, encrypt
from ..db import session_scope
from ..models import AppSetting

JIRA_KEY = "jira"


def _env_jira() -> dict:
    s = get_settings()
    return {"mode": s.jira_mode if s.jira_mode in ("mock", "cloud") else "mock", "base_url": s.jira_base_url,
            "email": s.jira_email, "api_token": s.jira_api_token, "ac_custom_fields": s.jira_ac_custom_fields,
            "timeout_s": s.jira_timeout_s, "source": "env", "updated_by": None, "updated_at": None}


def get_jira_config(db: Session | None = None) -> dict:
    """Effective Jira configuration (includes the decrypted token; never return this dict to clients)."""
    def read(d: Session) -> dict:
        row = d.get(AppSetting, JIRA_KEY)
        if not row:
            return _env_jira()
        v = row.value or {}
        return {"mode": v.get("mode", "mock"), "base_url": v.get("base_url", ""), "email": v.get("email", ""),
                "api_token": decrypt(row.secret_enc) if row.secret_enc else "", "ac_custom_fields": v.get("ac_custom_fields", []),
                "timeout_s": v.get("timeout_s") or get_settings().jira_timeout_s, "source": "settings",
                "updated_by": row.updated_by, "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "token_unreadable": bool(row.secret_enc) and decrypt(row.secret_enc) is None}
    if db is not None:
        return read(db)
    with session_scope() as d:
        return read(d)


def public_jira(cfg: dict) -> dict:
    out = {k: v for k, v in cfg.items() if k != "api_token"}
    out["token_set"] = bool(cfg.get("api_token"))
    return out


def normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise ValueError("The Jira site URL must be an https:// address, e.g. https://your-site.atlassian.net")
    if p.path not in ("", "/") or p.query or p.fragment or p.username:
        raise ValueError("Enter only the site address, e.g. https://your-site.atlassian.net (no path or query)")
    _guard_host(p.hostname)
    return f"https://{p.hostname}" + (f":{p.port}" if p.port else "")


def _guard_host(host: str) -> None:
    """Refuse loopback, link-local (cloud metadata) and unspecified addresses to avoid server-side request forgery."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # unresolvable now; the connection test will report it
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise ValueError(f"The Jira site address resolves to a disallowed network address ({ip})")


def merged_jira(db: Session, data: dict) -> dict:
    """Current settings with the submitted changes applied (used by save and by 'Test connection')."""
    cur = get_jira_config(db)
    mode = data.get("mode", cur["mode"])
    if mode not in ("mock", "cloud"):
        raise ValueError("mode must be 'mock' or 'cloud'")
    new = dict(cur, mode=mode)
    if "base_url" in data and data["base_url"] is not None:
        new["base_url"] = normalize_base_url(data["base_url"])
    if "email" in data and data["email"] is not None:
        new["email"] = data["email"].strip()
    if data.get("api_token"):
        new["api_token"] = data["api_token"].strip()
    if data.get("clear_token"):
        new["api_token"] = ""
    if "ac_custom_fields" in data and data["ac_custom_fields"] is not None:
        fields = data["ac_custom_fields"]
        if isinstance(fields, str):
            fields = [f.strip() for f in fields.split(",")]
        new["ac_custom_fields"] = [f for f in fields if f]
    if mode == "cloud" and not (new["base_url"] and new["email"] and new["api_token"]):
        raise ValueError("Jira Cloud needs the site URL, your Atlassian account email and an API token")
    return new


def save_jira(db: Session, data: dict, actor: str) -> dict:
    new = merged_jira(db, data)
    row = db.get(AppSetting, JIRA_KEY) or AppSetting(key=JIRA_KEY)
    row.value = {"mode": new["mode"], "base_url": new["base_url"], "email": new["email"],
                 "ac_custom_fields": new["ac_custom_fields"], "timeout_s": new.get("timeout_s")}
    row.secret_enc = encrypt(new["api_token"]) if new["api_token"] else None
    row.updated_by = actor
    db.add(row)
    db.flush()
    audit(db, actor, "JIRA_SETTINGS_SAVED", None, "SETTINGS", JIRA_KEY, mode=new["mode"], base_url=new["base_url"],
          email=new["email"], token_changed=bool(data.get("api_token") or data.get("clear_token")))
    return public_jira(get_jira_config(db))
