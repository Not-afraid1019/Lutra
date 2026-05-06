"""JIRA client — fetch issues, download attachments, format as markdown.

Auth strategy:
    1. Bearer PAT header for API authentication
    2. _aegis_cas cookie for SSO gateway — auto-refreshed from Chrome browser,
       falls back to static value from .env

Uses a shared requests.Session so the cookie jar is maintained across all
requests (JIRA API calls + attachment downloads).  If the server responds
with Set-Cookie the session picks it up automatically.
"""

import json
import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from jira import JIRA

log = logging.getLogger("lutra.jira")

# Magic bytes → extension mapping for common file types
_MAGIC_BYTES = {
    b"\x89PNG": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF8": ".gif",
    b"PK": ".zip",
    b"%PDF": ".pdf",
}


def connect(server: str, pat: str, aegis_cas: str) -> JIRA:
    """Establish JIRA connection with Bearer PAT + aegis_cas cookie.

    Tries to auto-refresh aegis_cas from Chrome cookies first.
    Falls back to the provided aegis_cas value.

    The returned JIRA object owns a requests.Session whose cookie jar
    is shared — call ``get_session(client)`` to reuse it for downloads.
    """
    # Best-effort: read a fresh token from the user's Chrome browser
    token = _get_fresh_aegis_cas(server) or aegis_cas

    headers: dict[str, str] = {"Authorization": f"Bearer {pat}"}
    cookies: dict[str, str] = {}
    if token:
        cookies["_aegis_cas"] = token

    client = JIRA(
        server=server,
        options={"headers": headers, "cookies": cookies},
    )
    return client


def get_session(client: JIRA) -> requests.Session:
    """Return the underlying requests.Session used by a JIRA client.

    The session carries the Bearer header and _aegis_cas cookie and is
    kept up-to-date by any Set-Cookie responses the server sends.
    """
    return client._session


def _get_fresh_aegis_cas(server: str) -> str:
    """Try to get a fresh aegis_cas token from Chrome cookies."""
    try:
        from .aegis import get_aegis_cas
        domain = urlparse(server).hostname or ""
        if domain:
            return get_aegis_cas(domain)
    except Exception as e:
        log.debug("Chrome cookie auto-refresh unavailable: %s", e)
    return ""


def fetch_issue(client: JIRA, issue_key: str) -> dict:
    """Fetch complete JIRA issue data.

    Returns dict with: key, summary, description, comments, attachments,
    status, assignee, reporter, priority, labels, components, created, updated.
    """
    issue = client.issue(issue_key)

    comments = []
    for c in issue.fields.comment.comments:
        comments.append({
            "author": c.author.displayName if c.author else "unknown",
            "body": c.body,
            "created": c.created,
        })

    attachments = []
    for att in getattr(issue.fields, "attachment", None) or []:
        attachments.append({
            "filename": att.filename,
            "url": att.content,
            "size": att.size,
        })

    return {
        "key": issue.key,
        "summary": issue.fields.summary or "",
        "description": issue.fields.description or "",
        "comments": comments,
        "attachments": attachments,
        "status": str(issue.fields.status),
        "assignee": str(issue.fields.assignee) if issue.fields.assignee else "",
        "reporter": str(issue.fields.reporter) if issue.fields.reporter else "",
        "priority": str(issue.fields.priority) if issue.fields.priority else "",
        "labels": list(issue.fields.labels) if issue.fields.labels else [],
        "components": [str(c) for c in issue.fields.components] if issue.fields.components else [],
        "created": issue.fields.created,
        "updated": issue.fields.updated,
    }


def format_issue_markdown(issue_data: dict) -> str:
    """Format issue data as readable Markdown."""
    lines = [
        f"# {issue_data['key']}: {issue_data['summary']}",
        "",
        f"- **Status**: {issue_data['status']}",
        f"- **Priority**: {issue_data['priority']}",
        f"- **Assignee**: {issue_data['assignee']}",
        f"- **Reporter**: {issue_data['reporter']}",
        f"- **Labels**: {', '.join(issue_data['labels']) or 'N/A'}",
        f"- **Components**: {', '.join(issue_data['components']) or 'N/A'}",
        f"- **Created**: {issue_data['created']}",
        f"- **Updated**: {issue_data['updated']}",
        "",
        "## Description",
        "",
        issue_data["description"] or "(empty)",
        "",
    ]

    if issue_data["attachments"]:
        lines.append("## Attachments")
        lines.append("")
        for att in issue_data["attachments"]:
            lines.append(f"- {att['filename']} ({att['size']} bytes)")
        lines.append("")

    if issue_data["comments"]:
        lines.append("## Comments")
        lines.append("")
        for c in issue_data["comments"]:
            lines.append(f"### {c['author']} ({c['created']})")
            lines.append("")
            lines.append(c["body"])
            lines.append("")

    return "\n".join(lines)


def search_issues(client: JIRA, jql: str, max_results: int = 20) -> list[dict]:
    """Search issues with JQL, return summary list."""
    issues = client.search_issues(
        jql,
        maxResults=max_results,
        fields="summary,status,priority,assignee,updated",
    )
    return [
        {
            "key": str(issue.key),
            "summary": str(issue.fields.summary or ""),
            "status": str(issue.fields.status),
            "priority": str(issue.fields.priority) if issue.fields.priority else "",
            "assignee": str(issue.fields.assignee) if issue.fields.assignee else "",
            "updated": str(issue.fields.updated or ""),
        }
        for issue in issues
    ]


# ======================================================================
# Attachment download
# ======================================================================

_URL_PATTERN = re.compile(r'https?://[^\s\])"\'<>]+')


def extract_downloadable_urls(issue_data: dict) -> list[dict]:
    """Extract downloadable URLs from attachments + description/comments.

    Returns list of {"url": str, "filename": str, "source": str}.
    """
    urls = []

    # Official attachments
    for att in issue_data.get("attachments", []):
        urls.append({
            "url": att["url"],
            "filename": att["filename"],
            "source": "attachment",
        })

    # URLs embedded in description and comments
    seen = {u["url"] for u in urls}
    text_sources = []
    if issue_data.get("description"):
        text_sources.append(("description", issue_data["description"]))
    for c in issue_data.get("comments", []):
        text_sources.append(("comment", c.get("body", "")))

    for source_name, text in text_sources:
        for match in _URL_PATTERN.finditer(text):
            url = match.group(0).rstrip(".,;:)")
            if url not in seen and _looks_downloadable(url):
                filename = urlparse(url).path.split("/")[-1] or "download"
                urls.append({
                    "url": url,
                    "filename": filename,
                    "source": source_name,
                })
                seen.add(url)

    return urls


def _looks_downloadable(url: str) -> bool:
    """Heuristic: is this URL likely a file download?"""
    path = urlparse(url).path.lower()
    downloadable_exts = (
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv",
        ".zip", ".tar", ".gz", ".log", ".txt", ".json",
    )
    if any(path.endswith(ext) for ext in downloadable_exts):
        return True
    # JIRA attachment URLs contain /secure/attachment/
    if "/secure/attachment/" in url or "/attachment/" in url:
        return True
    return False


def download_attachments(
    issue_data: dict,
    output_dir: str | Path,
    session: requests.Session,
) -> dict:
    """Download all attachments from an issue.

    Uses the shared *session* (from ``get_session(client)``) which already
    carries the Bearer header and aegis_cas cookie.

    Returns manifest dict: {"files": [...], "errors": [...]}.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    urls = extract_downloadable_urls(issue_data)
    if not urls:
        return {"files": [], "errors": []}

    manifest = {"files": [], "errors": []}

    for entry in urls:
        url = entry["url"]
        filename = entry["filename"]
        try:
            resp = session.get(url, timeout=60, stream=True)
            resp.raise_for_status()

            filepath = output_dir / filename
            # Avoid overwriting: add suffix
            counter = 1
            while filepath.exists():
                stem = filepath.stem
                filepath = output_dir / f"{stem}_{counter}{filepath.suffix}"
                counter += 1

            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Fix extension based on magic bytes / content-type
            content_type = resp.headers.get("Content-Type", "")
            final_path = _fix_filename_ext(filepath, content_type)

            manifest["files"].append({
                "filename": final_path.name,
                "source_url": url,
                "source": entry["source"],
                "size": final_path.stat().st_size,
            })
            log.info("Downloaded: %s (%d bytes)", final_path.name, final_path.stat().st_size)

        except Exception as e:
            log.warning("Failed to download %s: %s", url, e)
            manifest["errors"].append({"url": url, "error": str(e)})

    # Write manifest
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    return manifest


# ======================================================================
# Feedback page log download
# ======================================================================

_FEEDBACK_URL_PATTERN = re.compile(
    r'https?://feedback\.pt\.xiaomi\.com/feedback/logDownloadBox\?feedbackId=(\d+)'
)


def extract_feedback_urls(issue_data: dict) -> list[str]:
    """Extract feedback.pt.xiaomi.com logDownloadBox URLs from issue text.

    Scans description and comments for URLs like:
    https://feedback.pt.xiaomi.com/feedback/logDownloadBox?feedbackId=120375116
    """
    urls = []
    seen = set()
    text_sources = []
    if issue_data.get("description"):
        text_sources.append(issue_data["description"])
    for c in issue_data.get("comments", []):
        text_sources.append(c.get("body", ""))

    for text in text_sources:
        for m in _FEEDBACK_URL_PATTERN.finditer(text):
            url = m.group(0)
            if url not in seen:
                urls.append(url)
                seen.add(url)
    return urls


def download_feedback_logs(
    issue_data: dict,
    output_dir: str | Path,
) -> dict:
    """Download log files from feedback.pt.xiaomi.com pages referenced in issue.

    Authenticates using Chrome cookies (SESSION, _aegis_cas for feedback domain,
    TGC2 for CAS domain).

    Returns manifest dict: {"files": [...], "errors": [...]}.
    """
    from .aegis import get_chrome_cookies

    urls = extract_feedback_urls(issue_data)
    if not urls:
        return {"files": [], "errors": []}

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get Chrome cookies for auth
    feedback_cookies = get_chrome_cookies(
        "feedback.pt.xiaomi.com", ["SESSION", "_aegis_cas"]
    )
    cas_cookies = get_chrome_cookies("cas.mioffice.cn", ["TGC2"])

    if not feedback_cookies:
        log.warning("No Chrome cookies for feedback.pt.xiaomi.com — cannot download")
        return {"files": [], "errors": [{"url": u, "error": "no auth cookies"} for u in urls]}

    # Build session with cookies
    sess = requests.Session()
    for name, val in feedback_cookies.items():
        sess.cookies.set(name, val, domain="feedback.pt.xiaomi.com")
    for name, val in cas_cookies.items():
        sess.cookies.set(name, val, domain="cas.mioffice.cn")

    manifest = {"files": [], "errors": []}

    for page_url in urls:
        feedback_id = _FEEDBACK_URL_PATTERN.search(page_url).group(1)
        try:
            file_urls = _fetch_feedback_file_urls(sess, feedback_id)
        except Exception as e:
            log.warning("Failed to get log URLs for feedbackId=%s: %s", feedback_id, e)
            manifest["errors"].append({"url": page_url, "error": str(e)})
            continue

        for file_url in file_urls:
            try:
                filename = urlparse(file_url).path.split("/")[-1] or f"feedback-{feedback_id}.zip"
                filepath = output_dir / filename
                # Avoid overwriting
                counter = 1
                while filepath.exists():
                    filepath = output_dir / f"{filepath.stem}_{counter}{filepath.suffix}"
                    counter += 1

                resp = sess.get(file_url, timeout=120, stream=True)
                resp.raise_for_status()

                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)

                manifest["files"].append({
                    "filename": filepath.name,
                    "source_url": file_url,
                    "source": f"feedback:{feedback_id}",
                    "size": filepath.stat().st_size,
                })
                log.info("Downloaded feedback log: %s (%d bytes)",
                         filepath.name, filepath.stat().st_size)
            except Exception as e:
                log.warning("Failed to download %s: %s", file_url, e)
                manifest["errors"].append({"url": file_url, "error": str(e)})

    return manifest


def _fetch_feedback_file_urls(sess: requests.Session, feedback_id: str) -> list[str]:
    """Fetch actual download URLs from feedback.pt AJAX endpoint.

    Calls /ajax/getFeedbackLogFile?feedbackId=XXX&productName=phone
    Returns list of download URLs.
    """
    api_url = (
        f"https://feedback.pt.xiaomi.com/ajax/getFeedbackLogFile"
        f"?feedbackId={feedback_id}&productName=phone"
    )
    resp = sess.get(api_url, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    # Response format: {"log": ["url1", ...], "otherKey": ["url2", ...]}
    # or error: {"log": "asyncDesensitize"}
    urls = []
    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
    return urls


def _fix_filename_ext(filepath: Path, content_type: str) -> Path:
    """Fix file extension based on magic bytes and Content-Type."""
    try:
        with open(filepath, "rb") as f:
            head = f.read(8)
    except Exception:
        return filepath

    # Check magic bytes
    for magic, ext in _MAGIC_BYTES.items():
        if head.startswith(magic):
            if filepath.suffix.lower() != ext:
                new_path = filepath.with_suffix(ext)
                filepath.rename(new_path)
                return new_path
            return filepath

    # Fallback: use Content-Type
    if content_type:
        ct = content_type.split(";")[0].strip()
        guessed_ext = mimetypes.guess_extension(ct)
        if guessed_ext and filepath.suffix.lower() != guessed_ext:
            new_path = filepath.with_suffix(guessed_ext)
            filepath.rename(new_path)
            return new_path

    return filepath
