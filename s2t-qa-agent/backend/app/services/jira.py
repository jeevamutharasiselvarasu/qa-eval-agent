"""JiraConnector: retrieves an issue and normalizes it into a provider-neutral document.

Two adapters:
  * MockJiraConnector  - reads clearly labelled fixtures from backend/fixtures/jira/<KEY>.json
  * JiraCloudConnector - Jira Cloud REST API v3 (basic auth: email + API token from env)
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..config import get_settings

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "jira"
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,19}-\d{1,9}$")
TEXT_ATTACHMENT_TYPES = {".csv", ".txt", ".md", ".json", ".sql", ".tsv"}


class JiraError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code  # INVALID_KEY | NOT_FOUND | UNAUTHORIZED | FORBIDDEN | RATE_LIMITED | TIMEOUT | NOT_CONFIGURED | UPSTREAM


@dataclass
class JiraDocument:
    key: str
    id: str
    source: str  # MOCK | LIVE
    summary: str
    description: str
    issue_type: str = ""
    status: str = ""
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    project: dict = field(default_factory=dict)
    custom_fields: dict[str, str] = field(default_factory=dict)
    comments: list[dict] = field(default_factory=list)  # {id, author, created, body}
    attachments: list[dict] = field(default_factory=list)  # {id, filename, mime, size, content|None, skipped_reason}
    links: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def validate_key(key: str) -> str:
    key = (key or "").strip().upper()
    if not ISSUE_KEY_RE.match(key):
        raise JiraError("INVALID_KEY", f"'{key}' is not a valid Jira issue key (expected e.g. DATA-1234)")
    return key


def adf_to_text(node: Any) -> str:
    """Convert Atlassian Document Format to plain text, keeping paragraph/list structure."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type")
    content = node.get("content", []) or []
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    inner = "".join(adf_to_text(c) for c in content)
    if t in ("paragraph", "heading"):
        return inner + "\n"
    if t == "listItem":
        return "- " + inner.strip() + "\n"
    if t in ("bulletList", "orderedList", "doc", "blockquote", "panel", "table", "tableRow"):
        return inner + ("\n" if t != "doc" else "")
    if t in ("tableCell", "tableHeader"):
        return inner.strip() + " | "
    if t == "codeBlock":
        return "```\n" + inner + "\n```\n"
    return inner


class MockJiraConnector:
    """Fixture-backed connector. Every document it returns is labelled source=MOCK."""

    def __init__(self, fixture_dir: Path = FIXTURE_DIR):
        self.fixture_dir = fixture_dir

    def fetch(self, key: str) -> JiraDocument:
        key = validate_key(key)
        path = self.fixture_dir / f"{key}.json"
        if not path.exists():
            raise JiraError("NOT_FOUND", f"Issue {key} not found (mock fixtures available: "
                                         f"{', '.join(sorted(p.stem for p in self.fixture_dir.glob('*.json')))})")
        raw = json.loads(path.read_text())
        if raw.get("_simulate_error"):
            raise JiraError(raw["_simulate_error"], f"Simulated Jira error {raw['_simulate_error']}")
        atts = []
        for a in raw.get("attachments", []):
            atts.append({"id": a["id"], "filename": a["filename"], "mime": a.get("mime", "text/plain"),
                         "size": len(a.get("content", "")), "content": a.get("content"), "skipped_reason": None})
        return JiraDocument(
            key=key, id=str(raw.get("id", key)), source="MOCK", summary=raw.get("summary", ""),
            description=raw.get("description", ""), issue_type=raw.get("issue_type", ""),
            status=raw.get("status", ""), labels=raw.get("labels", []), components=raw.get("components", []),
            project=raw.get("project", {}), custom_fields=raw.get("custom_fields", {}),
            comments=raw.get("comments", []), attachments=atts, links=raw.get("links", []),
        )


class JiraCloudConnector:
    def __init__(self, client: httpx.Client | None = None):
        s = get_settings()
        if not (s.jira_base_url and s.jira_email and s.jira_api_token):
            raise JiraError("NOT_CONFIGURED", "JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN must be set")
        if not s.jira_base_url.startswith("https://"):
            raise JiraError("NOT_CONFIGURED", "JIRA_BASE_URL must use https")
        token = base64.b64encode(f"{s.jira_email}:{s.jira_api_token}".encode()).decode()
        self.base = s.jira_base_url.rstrip("/")
        self.ac_fields = s.jira_ac_custom_fields
        self.max_att = s.jira_max_attachment_bytes
        self.client = client or httpx.Client(
            timeout=s.jira_timeout_s,
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            follow_redirects=False,
        )

    def _get(self, path: str, **params) -> httpx.Response:
        try:
            r = self.client.get(self.base + path, params=params or None)
        except httpx.TimeoutException as e:
            raise JiraError("TIMEOUT", f"Jira request timed out: {e}") from e
        except httpx.HTTPError as e:
            raise JiraError("UPSTREAM", f"Jira request failed: {e}") from e
        if r.status_code == 401:
            raise JiraError("UNAUTHORIZED", "Jira rejected the credentials")
        if r.status_code == 403:
            raise JiraError("FORBIDDEN", "No permission to view this issue")
        if r.status_code == 404:
            raise JiraError("NOT_FOUND", "Issue not found or not visible to this account")
        if r.status_code == 429:
            raise JiraError("RATE_LIMITED", f"Jira rate limit hit; retry after {r.headers.get('Retry-After', '?')}s")
        if r.status_code >= 400:
            raise JiraError("UPSTREAM", f"Jira returned HTTP {r.status_code}")
        return r

    def fetch(self, key: str) -> JiraDocument:
        key = validate_key(key)
        data = self._get(f"/rest/api/3/issue/{key}", fields="*all", expand="names").json()
        f = data.get("fields", {})
        names = data.get("names", {})
        custom = {}
        for fid in self.ac_fields:
            val = f.get(fid)
            if val:
                custom[names.get(fid, fid)] = adf_to_text(val) if isinstance(val, dict) else str(val)
        # auto-detect fields called "Acceptance Criteria"
        for fid, nm in names.items():
            if "acceptance" in nm.lower() and f.get(fid) and nm not in custom:
                v = f[fid]
                custom[nm] = adf_to_text(v) if isinstance(v, dict) else str(v)
        comments = []
        cdata = self._get(f"/rest/api/3/issue/{key}/comment", maxResults=100).json()
        for c in cdata.get("comments", []):
            comments.append({"id": c["id"], "author": (c.get("author") or {}).get("displayName", ""),
                             "created": c.get("created", ""), "body": adf_to_text(c.get("body"))})
        atts = []
        for a in f.get("attachment", []) or []:
            item = {"id": a["id"], "filename": a["filename"], "mime": a.get("mimeType", ""),
                    "size": a.get("size", 0), "content": None, "skipped_reason": None}
            ext = Path(a["filename"]).suffix.lower()
            if ext not in TEXT_ATTACHMENT_TYPES:
                item["skipped_reason"] = f"Unsupported attachment type {ext} (text-based types only in Phase 1)"
            elif a.get("size", 0) > self.max_att:
                item["skipped_reason"] = "Attachment exceeds size limit"
            else:
                r = self._get(f"/rest/api/3/attachment/content/{a['id']}")
                item["content"] = r.content.decode("utf-8", errors="replace")
            atts.append(item)
        links = []
        for l in f.get("issuelinks", []) or []:
            other = l.get("outwardIssue") or l.get("inwardIssue") or {}
            links.append({"type": (l.get("type") or {}).get("name", ""), "key": other.get("key", ""),
                          "summary": (other.get("fields") or {}).get("summary", "")})
        return JiraDocument(
            key=data.get("key", key), id=str(data.get("id", "")), source="LIVE",
            summary=f.get("summary", ""), description=adf_to_text(f.get("description")),
            issue_type=(f.get("issuetype") or {}).get("name", ""), status=(f.get("status") or {}).get("name", ""),
            labels=f.get("labels", []) or [], components=[c.get("name", "") for c in f.get("components", []) or []],
            project={"key": (f.get("project") or {}).get("key", ""), "name": (f.get("project") or {}).get("name", "")},
            custom_fields=custom, comments=comments, attachments=atts, links=links,
        )


def get_connector():
    s = get_settings()
    if s.jira_mode == "cloud":
        return JiraCloudConnector()
    return MockJiraConnector()
