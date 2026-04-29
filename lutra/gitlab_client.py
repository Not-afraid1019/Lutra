"""GitLab REST API v4 client — uses requests.Session directly."""

import logging
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

import requests

log = logging.getLogger("lutra.gitlab")


def connect(base_url: str, pat: str) -> requests.Session:
    """创建带 PRIVATE-TOKEN 的 session。"""
    s = requests.Session()
    s.headers["PRIVATE-TOKEN"] = pat
    s.headers["Content-Type"] = "application/json"
    # Store base_url on session for convenience
    s._gitlab_base_url = base_url  # type: ignore[attr-defined]
    return s


def detect_project(project_dir: str) -> tuple[str, str]:
    """从 git remote URL 解析 (gitlab_base_url, project_path)。

    git@git.n.xiaomi.com:ai-framework/osbot.git
      → ("https://git.n.xiaomi.com", "ai-framework/osbot")
    https://git.n.xiaomi.com/ai-framework/osbot.git
      → ("https://git.n.xiaomi.com", "ai-framework/osbot")
    """
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=project_dir, timeout=5,
        )
        url = proc.stdout.strip()
        if not url:
            return ("", "")
    except Exception:
        return ("", "")

    # SSH: git@host:group/project.git
    m = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
    if m:
        host, path = m.group(1), m.group(2)
        return (f"https://{host}", path)

    # HTTPS: https://host/group/project.git
    m = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?$", url)
    if m:
        host, path = m.group(1), m.group(2)
        return (f"https://{host}", path)

    return ("", "")


def _api_base(base_url: str, project_path: str) -> str:
    """→ "{base_url}/api/v4/projects/{url_encoded_path}" """
    return f"{base_url}/api/v4/projects/{quote(project_path, safe='')}"


def list_open_mrs(
    session: requests.Session,
    base_url: str,
    project_path: str,
    author_username: str | None = None,
) -> list[dict]:
    """GET /projects/:id/merge_requests?state=opened&author_username=xxx"""
    url = f"{_api_base(base_url, project_path)}/merge_requests"
    params: dict = {"state": "opened", "per_page": 100}
    if author_username:
        params["author_username"] = author_username
    resp = session.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def list_discussions(
    session: requests.Session, base_url: str, project_path: str, mr_iid: int,
) -> list[dict]:
    """GET /projects/:id/merge_requests/:iid/discussions"""
    url = f"{_api_base(base_url, project_path)}/merge_requests/{mr_iid}/discussions"
    resp = session.get(url, params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def format_discussions(
    discussions: list[dict], unresolved_only: bool = False,
) -> str:
    """格式化 discussions 为可读文本。"""
    if not discussions:
        return "没有评论。"

    parts = []
    for disc in discussions:
        notes = disc.get("notes", [])
        if not notes:
            continue

        first = notes[0]

        # Skip system notes
        if first.get("system"):
            continue

        # Filter resolved if requested
        resolvable = first.get("resolvable", False)
        resolved = first.get("resolved", False)
        if unresolved_only and resolvable and resolved:
            continue

        disc_id = disc["id"]
        author = first.get("author", {}).get("username", "?")
        body = first.get("body", "")

        # Status
        if resolvable:
            status = "resolved" if resolved else "unresolved"
        else:
            status = "comment"

        header = f"### Discussion {disc_id} [{status}] by @{author}"

        # DiffNote: show file + line
        position = first.get("position")
        if position and position.get("new_path"):
            line = position.get("new_line") or position.get("old_line") or "?"
            header += f"\n**File**: `{position['new_path']}:{line}`"

        lines = [header, "", body]

        # Replies
        for note in notes[1:]:
            reply_author = note.get("author", {}).get("username", "?")
            reply_body = note.get("body", "")
            lines.append(f"\n> **@{reply_author}**: {reply_body}")

        lines.append("")
        parts.append("\n".join(lines))

    if not parts:
        return "没有未解决的评论。" if unresolved_only else "没有评论。"

    return "\n---\n".join(parts)


def reply_discussion(
    session: requests.Session,
    base_url: str,
    project_path: str,
    mr_iid: int,
    discussion_id: str,
    body: str,
) -> dict:
    """POST /projects/:id/merge_requests/:iid/discussions/:id/notes"""
    url = (
        f"{_api_base(base_url, project_path)}"
        f"/merge_requests/{mr_iid}/discussions/{discussion_id}/notes"
    )
    resp = session.post(url, json={"body": body})
    resp.raise_for_status()
    return resp.json()


def resolve_discussion(
    session: requests.Session,
    base_url: str,
    project_path: str,
    mr_iid: int,
    discussion_id: str,
    resolved: bool = True,
) -> dict:
    """PUT /projects/:id/merge_requests/:iid/discussions/:id"""
    url = (
        f"{_api_base(base_url, project_path)}"
        f"/merge_requests/{mr_iid}/discussions/{discussion_id}"
    )
    resp = session.put(url, json={"resolved": resolved})
    resp.raise_for_status()
    return resp.json()


def get_mr(
    session: requests.Session, base_url: str, project_path: str, mr_iid: int,
) -> dict:
    """GET /projects/:id/merge_requests/:iid"""
    url = f"{_api_base(base_url, project_path)}/merge_requests/{mr_iid}"
    resp = session.get(url)
    resp.raise_for_status()
    return resp.json()


def parse_mr_ref(ref: str) -> int:
    """解析 MR 引用: "!123" / "123" / 完整 URL → int(iid)。

    Examples:
        "!123" → 123
        "123" → 123
        "https://git.n.xiaomi.com/group/proj/-/merge_requests/45" → 45
    """
    ref = ref.strip()

    # Full URL: .../merge_requests/123
    m = re.search(r"/merge_requests/(\d+)", ref)
    if m:
        return int(m.group(1))

    # "!123" or "123"
    m = re.match(r"!?(\d+)$", ref)
    if m:
        return int(m.group(1))

    raise ValueError(f"无法解析 MR 引用: {ref!r}")
