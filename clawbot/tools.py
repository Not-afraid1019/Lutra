"""Filesystem, shell, and JIRA tools for the coding agent."""

import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger("clawbot.tools")

# ======================================================================
# Tool definitions (Anthropic tool_use schema)
# ======================================================================

TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": (
            "Read file contents with line numbers. "
            "For large files, use offset and limit to read a portion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (absolute or relative to working directory)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Starting line number (1-based, default 1)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to read (default 500, 0=all)",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create a new file or overwrite an existing file with the given content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (absolute or relative to working directory)",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Edit a file by replacing old_text with new_text. "
            "old_text must match exactly (including whitespace). "
            "Prefer this over write_file for targeted changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path (absolute or relative to working directory)",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find (must be unique in the file)",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories. Directories are shown with a trailing /.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path (absolute or relative to working directory)",
                },
            },
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search for a regex pattern in files. "
            "Returns matching lines with file paths and line numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: working directory)",
                },
                "include": {
                    "type": "string",
                    "description": "File glob pattern, e.g. '*.py', '*.ts'",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in the working directory. "
            "Use for git, tests, builds, system operations, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
            },
            "required": ["command"],
        },
    },
]

# JIRA tools — only registered when JIRA is configured
JIRA_TOOL_DEFINITIONS = [
    {
        "name": "jira_get_issue",
        "description": (
            "Fetch a JIRA issue by key (e.g. 'PROJ-123'). "
            "Returns full details: summary, description, status, comments, attachments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "JIRA issue key, e.g. 'PROJ-123'",
                },
            },
            "required": ["issue_key"],
        },
    },
    {
        "name": "jira_list_issues",
        "description": (
            "List unresolved JIRA issues assigned to the current user. "
            "Returns a summary table with key, summary, status, priority."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Max issues to return (default 20)",
                },
            },
        },
    },
    {
        "name": "jira_search",
        "description": (
            "Search JIRA issues using JQL (JIRA Query Language). "
            "Example: 'project = PROJ AND status = Open ORDER BY updated DESC'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jql": {
                    "type": "string",
                    "description": "JQL query string",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max issues to return (default 20)",
                },
            },
            "required": ["jql"],
        },
    },
    {
        "name": "jira_analyze",
        "description": (
            "Analyze a JIRA issue in depth: fetch the issue, download attachments, "
            "filter sensitive data, then invoke Claude CLI to produce a root-cause "
            "analysis with impact scope and fix proposals. "
            "Use when user says '分析' a JIRA issue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "JIRA issue key, e.g. 'OSBOT-32'",
                },
            },
            "required": ["issue_key"],
        },
    },
    {
        "name": "jira_fix",
        "description": (
            "Fix a JIRA issue: run analysis (if not done), then invoke Claude CLI "
            "to implement the fix, create a git branch, commit, push, and return "
            "a merge request link. Use when user says '修复' a JIRA issue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {
                    "type": "string",
                    "description": "JIRA issue key, e.g. 'OSBOT-32'",
                },
            },
            "required": ["issue_key"],
        },
    },
]


# ======================================================================
# Executor
# ======================================================================

_MAX_OUTPUT = 15000  # chars


class ToolExecutor:
    """Execute tools with full filesystem access + optional JIRA."""

    def __init__(
        self,
        work_dir: str = "",
        jira_config: dict | None = None,
        mimo_config: dict | None = None,
        data_dir: str = "",
        project_dir: str = "",
    ):
        self._cwd = Path(work_dir or Path.home()).resolve()
        self._jira_client = None
        self._jira_config = jira_config or {}
        self._mimo_config = mimo_config or {}
        self._data_dir = Path(data_dir) if data_dir else Path("data")
        self._project_dir = Path(project_dir).resolve() if project_dir else self._cwd

        # Initialize JIRA if configured
        if jira_config and jira_config.get("server") and jira_config.get("pat"):
            try:
                from . import jira_client
                self._jira_client = jira_client.connect(
                    jira_config["server"],
                    jira_config["pat"],
                    jira_config.get("aegis_cas", ""),
                )
                log.info("JIRA connected: %s", jira_config["server"])
            except Exception as e:
                log.warning("JIRA connection failed: %s", e)

        log.info("Tool executor ready: cwd=%s jira=%s",
                 self._cwd, "yes" if self._jira_client else "no")

    @property
    def definitions(self) -> list[dict]:
        defs = list(TOOL_DEFINITIONS)
        if self._jira_client:
            defs.extend(JIRA_TOOL_DEFINITIONS)
        return defs

    def execute(self, name: str, inputs: dict) -> str:
        """Execute a tool by name. Always returns a string."""
        handler = getattr(self, f"_tool_{name}", None)
        if not handler:
            return f"Error: unknown tool '{name}'"
        try:
            return handler(**inputs)
        except Exception as e:
            log.error("Tool %s failed: %s", name, e)
            return f"Error: {e}"

    def _resolve(self, path: str) -> Path:
        """Resolve path: absolute paths used as-is, relative from cwd."""
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        return (self._cwd / path).resolve()

    # ── tools ──

    def _tool_read_file(self, path: str, offset: int = 1, limit: int = 500) -> str:
        p = self._resolve(path)
        if not p.is_file():
            return f"Error: '{path}' not found or not a file"

        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)

        start = max(offset, 1) - 1  # 0-based
        end = start + limit if limit > 0 else total
        selected = lines[start:end]

        numbered = [
            f"{start + i + 1:5d} | {line}" for i, line in enumerate(selected)
        ]
        result = "\n".join(numbered)

        if len(result) > _MAX_OUTPUT:
            result = result[:_MAX_OUTPUT] + "\n… (truncated)"

        header = f"({total} lines total)"
        if start > 0 or end < total:
            header = f"(showing lines {start+1}-{min(start+len(selected), total)} of {total})"
        return f"{header}\n{result}"

    def _tool_write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines = content.count("\n") + 1
        return f"Written {lines} lines to {path}"

    def _tool_edit_file(self, path: str, old_text: str, new_text: str) -> str:
        p = self._resolve(path)
        if not p.is_file():
            return f"Error: '{path}' not found"

        content = p.read_text(encoding="utf-8")
        count = content.count(old_text)

        if count == 0:
            return "Error: old_text not found in file"
        if count > 1:
            return f"Error: old_text matches {count} locations — make it more specific"

        new_content = content.replace(old_text, new_text, 1)
        p.write_text(new_content, encoding="utf-8")
        return f"Edited {path} (1 replacement)"

    def _tool_list_directory(self, path: str = ".") -> str:
        p = self._resolve(path)
        if not p.is_dir():
            return f"Error: '{path}' not found or not a directory"

        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        result = []
        for e in entries:
            if e.name.startswith(".") and e.name not in (".env.example",):
                continue
            name = f"{e.name}/" if e.is_dir() else e.name
            result.append(name)

        if not result:
            return "(empty directory)"
        if len(result) > 300:
            result = result[:300]
            result.append(f"… ({len(list(p.iterdir())) - 300} more)")
        return "\n".join(result)

    def _tool_search_code(
        self, pattern: str, path: str = ".", include: str | None = None
    ) -> str:
        target = self._resolve(path)
        cmd = ["grep", "-rn", "--color=never"]
        if include:
            cmd.extend(["--include", include])
        # Exclude common noise
        for excl in (".git", "node_modules", "__pycache__", ".venv", "venv"):
            cmd.extend(["--exclude-dir", excl])
        cmd.extend([pattern, str(target)])

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15
            )
        except subprocess.TimeoutExpired:
            return "Error: search timed out (15s)"

        output = proc.stdout
        if not output:
            return "No matches found"

        # Make paths relative
        root_prefix = str(self._cwd) + "/"
        output = output.replace(root_prefix, "")

        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + "\n… (truncated)"
        return output

    def _tool_run_command(self, command: str, timeout: int = 30) -> str:
        timeout = min(timeout, 120)
        log.info("[CMD] %s (timeout=%ds)", command, timeout)

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out ({timeout}s)"

        parts = []
        if proc.stdout:
            parts.append(proc.stdout)
        if proc.stderr:
            parts.append(f"[stderr]\n{proc.stderr}")
        if proc.returncode != 0:
            parts.append(f"[exit code: {proc.returncode}]")

        output = "\n".join(parts) if parts else "(no output)"

        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + "\n… (truncated)"
        return output

    # ── JIRA tools ──

    def _tool_jira_get_issue(self, issue_key: str) -> str:
        if not self._jira_client:
            return "Error: JIRA not configured"
        from . import jira_client
        issue = jira_client.fetch_issue(self._jira_client, issue_key)
        result = jira_client.format_issue_markdown(issue)
        if len(result) > _MAX_OUTPUT:
            result = result[:_MAX_OUTPUT] + "\n… (truncated)"
        return result

    def _tool_jira_list_issues(self, max_results: int = 20) -> str:
        if not self._jira_client:
            return "Error: JIRA not configured"
        from . import jira_client
        jql = (
            "assignee = currentUser() AND resolution = Unresolved "
            "ORDER BY updated DESC"
        )
        issues = jira_client.search_issues(
            self._jira_client, jql, max_results=max_results
        )
        if not issues:
            return "No unresolved issues found."

        lines = [f"Found {len(issues)} issues:\n"]
        for iss in issues:
            lines.append(
                f"- **{iss['key']}** [{iss['status']}] {iss['summary']}"
                f"  (P: {iss['priority']}, Updated: {iss['updated']})"
            )
        return "\n".join(lines)

    def _tool_jira_search(self, jql: str, max_results: int = 20) -> str:
        if not self._jira_client:
            return "Error: JIRA not configured"
        from . import jira_client
        issues = jira_client.search_issues(
            self._jira_client, jql, max_results=max_results
        )
        if not issues:
            return "No issues matched the query."

        lines = [f"Found {len(issues)} issues:\n"]
        for iss in issues:
            lines.append(
                f"- **{iss['key']}** [{iss['status']}] {iss['summary']}"
                f"  (Assignee: {iss['assignee']}, Updated: {iss['updated']})"
            )
        return "\n".join(lines)

    # ── JIRA analyze / fix ──

    def _issue_dir(self, issue_key: str) -> Path:
        """Return data directory for a JIRA issue, e.g. data/jira/osbot-32/."""
        return self._data_dir / "jira" / issue_key.lower()

    def _tool_jira_analyze(self, issue_key: str) -> str:
        if not self._jira_client:
            return "Error: JIRA not configured"

        from . import jira_client
        from .sensitive_filter import filter_text

        issue_key = issue_key.upper().strip()
        issue_dir = self._issue_dir(issue_key)
        issue_dir.mkdir(parents=True, exist_ok=True)
        att_dir = issue_dir / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)

        # 1. Fetch issue
        log.info("[ANALYZE] Fetching %s", issue_key)
        issue_data = jira_client.fetch_issue(self._jira_client, issue_key)

        # 2. Download attachments
        log.info("[ANALYZE] Downloading attachments for %s", issue_key)
        manifest = jira_client.download_attachments(
            issue_data,
            att_dir,
            self._jira_config.get("server", ""),
            self._jira_config.get("pat", ""),
            self._jira_config.get("aegis_cas", ""),
        )

        # 3. Format issue as text and filter sensitive data
        raw_text = jira_client.format_issue_markdown(issue_data)

        # Append attachment manifest info
        if manifest.get("files"):
            raw_text += "\n\n## Downloaded Attachments\n"
            for f in manifest["files"]:
                raw_text += f"\n- {f['filename']} ({f['size']} bytes)"

        log.info("[ANALYZE] Filtering sensitive data")
        filtered_text = filter_text(
            raw_text,
            self._mimo_config.get("api_key", ""),
            self._mimo_config.get("base_url", ""),
            self._mimo_config.get("model", ""),
            self._mimo_config.get("provider_id", ""),
        )

        # 4. Write filtered issue
        filtered_path = issue_dir / "filtered_issue.txt"
        filtered_path.write_text(filtered_text, encoding="utf-8")
        log.info("[ANALYZE] Written filtered issue: %s", filtered_path)

        # 5. Build analysis prompt
        att_listing = ""
        if manifest.get("files"):
            att_listing = "\n附件已下载到 attachments/ 目录，可用 read_file 查看。\n"

        prompt = f"""\
请分析以下 JIRA issue 并给出详细的分析报告。

{filtered_text}
{att_listing}
请按以下格式输出分析报告：

## 问题概述
简要描述问题

## 根因分析
分析问题的根本原因，包括：
- 直接原因
- 深层原因
- 相关的代码路径和模块

## 影响范围
- 受影响的功能/模块
- 影响的用户群体
- 严重程度评估

## 修复方案
给出 1-3 个修复方案，每个方案包括：
- 方案描述
- 需要修改的文件和代码
- 优缺点
- 推荐指数

## 建议
推荐的修复方案及理由"""

        # 6. Run Claude CLI for analysis
        log.info("[ANALYZE] Running Claude CLI analysis for %s", issue_key)
        analysis = _run_claude_cli(prompt, self._project_dir)

        # 7. Write analysis log
        analysis_path = issue_dir / "analysis.log"
        analysis_path.write_text(analysis, encoding="utf-8")
        log.info("[ANALYZE] Written analysis: %s", analysis_path)

        if len(analysis) > _MAX_OUTPUT:
            analysis = analysis[:_MAX_OUTPUT] + "\n… (truncated)"
        return analysis

    def _tool_jira_fix(self, issue_key: str) -> str:
        if not self._jira_client:
            return "Error: JIRA not configured"

        issue_key = issue_key.upper().strip()
        issue_dir = self._issue_dir(issue_key)
        analysis_path = issue_dir / "analysis.log"

        # 1. Ensure analysis exists
        if not analysis_path.exists():
            log.info("[FIX] No analysis found, running analyze first for %s", issue_key)
            analyze_result = self._tool_jira_analyze(issue_key)
            if analyze_result.startswith("Error:"):
                return analyze_result

        analysis = analysis_path.read_text(encoding="utf-8")

        # 2. Build fix prompt
        branch_name = f"fix/{issue_key.lower()}"
        prompt = f"""\
请根据以下分析报告，对代码进行最小改动修复。

## 分析报告
{analysis}

## 修复要求
1. 只做必要的最小改动，不要重构或优化无关代码
2. 确保修复不会引入新的问题
3. 如果需要添加测试，请一并添加
4. 修复完成后，简要说明你做了哪些修改"""

        # 3. Run Claude CLI to implement the fix
        log.info("[FIX] Running Claude CLI fix for %s", issue_key)
        fix_output = _run_claude_cli(prompt, self._project_dir)

        # 4. Git operations: branch, commit, push
        log.info("[FIX] Git operations for %s", issue_key)
        git_result = _git_branch_commit_push(
            self._project_dir, branch_name, issue_key
        )

        # 5. Extract MR link
        mr_link = ""
        if git_result.get("push_output"):
            mr_link = _extract_mr_link(git_result["push_output"])
        if not mr_link:
            mr_link = _construct_mr_link(self._project_dir, branch_name)

        # 6. Write fix log
        fix_log_lines = [
            f"# Fix Log: {issue_key}",
            f"\n## Branch: {branch_name}",
            f"\n## MR Link: {mr_link or 'N/A'}",
            f"\n## Claude Output\n{fix_output}",
            f"\n## Git Result\n{git_result.get('summary', '')}",
        ]
        fix_log = "\n".join(fix_log_lines)
        issue_dir.mkdir(parents=True, exist_ok=True)
        (issue_dir / "fix.log").write_text(fix_log, encoding="utf-8")
        log.info("[FIX] Written fix log: %s", issue_dir / "fix.log")

        # 7. Return result
        parts = [f"## {issue_key} 修复完成\n"]
        if mr_link:
            parts.append(f"**MR 链接**: {mr_link}\n")
        parts.append(f"**分支**: {branch_name}\n")
        if git_result.get("summary"):
            parts.append(f"**Git 操作**:\n{git_result['summary']}\n")
        parts.append(f"**修复详情**:\n{fix_output}")

        result = "\n".join(parts)
        if len(result) > _MAX_OUTPUT:
            result = result[:_MAX_OUTPUT] + "\n… (truncated)"
        return result


# ======================================================================
# Module-level helper functions
# ======================================================================

def _run_claude_cli(prompt: str, cwd: Path, timeout: int = 300) -> str:
    """Run `claude -p --dangerously-skip-permissions` with the given prompt."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions"],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        output = proc.stdout or ""
        if proc.stderr:
            output += f"\n[stderr]\n{proc.stderr}"
        if proc.returncode != 0:
            output += f"\n[exit code: {proc.returncode}]"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: Claude CLI timed out ({timeout}s)"
    except FileNotFoundError:
        return "Error: 'claude' CLI not found. Please install Claude Code CLI."
    except Exception as e:
        return f"Error running Claude CLI: {e}"


def _git_branch_commit_push(
    project_dir: Path, branch_name: str, issue_key: str
) -> dict:
    """Create branch, add, commit, push. Returns dict with summary and push_output."""
    result = {"summary": "", "push_output": ""}
    steps = []

    def run_git(cmd: str) -> tuple[str, int]:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(project_dir),
            capture_output=True, text=True, timeout=30,
        )
        return (proc.stdout + proc.stderr).strip(), proc.returncode

    # Checkout branch
    out, rc = run_git(f"git checkout -b {branch_name}")
    if rc != 0:
        # Branch may already exist
        out2, rc2 = run_git(f"git checkout {branch_name}")
        if rc2 != 0:
            steps.append(f"checkout failed: {out} / {out2}")
            result["summary"] = "\n".join(steps)
            return result
        steps.append(f"Switched to existing branch: {branch_name}")
    else:
        steps.append(f"Created branch: {branch_name}")

    # Stage all changes
    out, rc = run_git("git add -A")
    steps.append(f"git add -A: {'ok' if rc == 0 else out}")

    # Check if there are changes to commit
    out, rc = run_git("git diff --cached --stat")
    if not out.strip():
        steps.append("No changes to commit")
        result["summary"] = "\n".join(steps)
        return result

    # Commit
    commit_msg = f"fix({issue_key.lower()}): auto-fix based on JIRA analysis"
    out, rc = run_git(f'git commit -m "{commit_msg}"')
    steps.append(f"commit: {'ok' if rc == 0 else out}")

    # Push
    out, rc = run_git(f"git push -u origin {branch_name}")
    result["push_output"] = out
    steps.append(f"push: {'ok' if rc == 0 else out}")

    result["summary"] = "\n".join(steps)
    return result


def _extract_mr_link(push_output: str) -> str:
    """Extract merge request / pull request URL from git push output."""
    # GitLab: remote: https://gitlab.com/.../merge_requests/new?...
    # GitHub: remote: https://github.com/.../pull/new/...
    patterns = [
        r'(https?://[^\s]+/merge_requests/new[^\s]*)',
        r'(https?://[^\s]+/pull/new[^\s]*)',
        r'(https?://[^\s]+/merge_requests/\d+)',
        r'(https?://[^\s]+/pull/\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, push_output)
        if m:
            return m.group(1)
    return ""


def _construct_mr_link(project_dir: Path, branch_name: str) -> str:
    """Fallback: construct MR link from remote URL."""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=str(project_dir), timeout=5,
        )
        remote_url = proc.stdout.strip()
        if not remote_url:
            return ""

        # Normalize SSH/HTTP URL to base web URL
        if remote_url.startswith("git@"):
            # git@gitlab.com:group/project.git → https://gitlab.com/group/project
            remote_url = remote_url.replace(":", "/", 1).replace("git@", "https://")
        remote_url = remote_url.removesuffix(".git")

        if "gitlab" in remote_url:
            return f"{remote_url}/-/merge_requests/new?merge_request%5Bsource_branch%5D={branch_name}"
        elif "github" in remote_url:
            return f"{remote_url}/pull/new/{branch_name}"
        else:
            # Generic: try GitLab-style
            return f"{remote_url}/-/merge_requests/new?merge_request%5Bsource_branch%5D={branch_name}"
    except Exception:
        return ""
