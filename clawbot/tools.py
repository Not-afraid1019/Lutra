"""Filesystem and shell tools for the coding agent."""

import logging
import os
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


# ======================================================================
# Executor
# ======================================================================

_MAX_OUTPUT = 15000  # chars


class ToolExecutor:
    """Execute tools with full filesystem access."""

    def __init__(self, work_dir: str = ""):
        self._cwd = Path(work_dir or Path.home()).resolve()
        log.info("Tool executor ready: cwd=%s", self._cwd)

    @property
    def definitions(self) -> list[dict]:
        return TOOL_DEFINITIONS

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
