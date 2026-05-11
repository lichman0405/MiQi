"""Shell execution tool."""

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from miqi.agent.tools.base import Tool


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        env_passthrough: list[str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.env_passthrough: frozenset[str] = frozenset(env_passthrough or [])
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
            r"\bsudo\b",                     # privilege escalation
            r"\beval\b",                     # code/string evaluation
            r"\bsource\b",                   # source external scripts
            r"`[^`\n]{1,500}`",              # backtick command substitution
            r"\$\([^)\n]{1,500}\)",          # $() command substitution
            r"\|\s*(ba|da|z|fi|c)?sh\b",    # pipe to any shell variant
            r"\b(?:curl|wget)\b[^;\n]{0,200}\|\s*python[23]?\b",  # download-and-execute via Python
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        # Set by AgentService._wire_approval when desktop backend is active.
        # Signature: (command, pattern_description, session_key, execution_id, tool_call_id) -> decision str
        self.approval_fn: Callable[..., Awaitable[str]] | None = None

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        # Approval check for dangerous commands (desktop IPC path)
        approval_result = await self._check_approval(command, **kwargs)
        if approval_result is not None:
            return approval_result

        process = None
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._build_safe_env(),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
            except asyncio.TimeoutError:
                await self._terminate_process(process)
                return f"Error: Command timed out after {self.timeout} seconds"
            except asyncio.CancelledError:
                await self._terminate_process(process)
                raise

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Truncate very long output
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

            return result

        except asyncio.CancelledError:
            raise
        except Exception as e:
            return f"Error executing command: {str(e)}"

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        """Terminate/kill a subprocess and wait for it to exit."""
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    def _build_safe_env(self) -> dict[str, str]:
        """Return a sanitised copy of os.environ with credential variables removed.

        MCP servers inject secrets (API keys, tokens, passwords) into the
        process environment.  Without this filter, any shell subprocess spawned
        by the agent would inherit those secrets, leaking them to executed
        commands (e.g. ``exec("env")``).

        Variables listed in ``self.env_passthrough`` are explicitly exempted
        from the filter.  This lets operators selectively allow scripts run via
        the exec tool to access specific credentials (e.g. ``OPENAI_API_KEY``)
        without opening the door to every secret in the environment.

        Note: this filter does NOT apply to MCP server processes — those are
        started by the MCP SDK (StdioServerParameters) and always inherit the
        parent environment unchanged.
        """
        _sensitive = re.compile(
            r"(api[_-]?key|secret|token|password|passwd)", re.IGNORECASE
        )
        _sensitive_prefixes = (
            "OPENAI_", "ANTHROPIC_", "FEISHU_", "DINGTALK_",
            "TELEGRAM_", "SLACK_", "DISCORD_", "QQ_", "GROQ_",
            "AZURE_", "AWS_", "GOOGLE_", "GITHUB_", "BRAVE_", "OLLAMA_",
        )
        return {
            k: v for k, v in os.environ.items()
            if k in self.env_passthrough
            or (not _sensitive.search(k) and not k.startswith(_sensitive_prefixes))
        }

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            # Only match absolute paths — avoid false positives on relative
            # paths like ".venv/bin/python" where "/bin/python" would be
            # incorrectly extracted by the old pattern.
            posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", cmd)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    async def _check_approval(self, command: str, **kwargs: Any) -> str | None:
        """Check if a dangerous command requires approval.

        Returns None if the command is safe or approved, or a blocking
        message string if denied.  When ``approval_fn`` is set (desktop
        mode), the flow is:

          1. detect_dangerous_command() → check if dangerous
          2. If dangerous and approval_fn set → await approval_fn()
          3. If decision is "deny" → return blocking message
          4. If decision is "once"/"session"/"always" → return None (proceed)

        When approval_fn is NOT set (CLI/gateway), dangerous commands
        pass through and the existing CLI prompt mechanism handles it
        (``command_approval.check_dangerous_command``).
        """
        from miqi.agent.command_approval import (
            detect_dangerous_command,
            is_approved,
        )

        is_dangerous, pattern_key, description = detect_dangerous_command(command)
        if not is_dangerous:
            return None

        # Check session/permanent approvals
        session_key = kwargs.get("_session_key", "")
        if pattern_key and is_approved(session_key, pattern_key):
            return None

        # Desktop IPC path: use async approval flow
        if self.approval_fn is not None:
            execution_id = kwargs.get("_execution_id", "")
            tool_call_id = kwargs.get("_tool_call_id", "")
            try:
                decision = await self.approval_fn(
                    command, description or "", session_key,
                    execution_id, tool_call_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return f"Error: Approval check failed: {exc}"

            if decision == "deny":
                return (
                    f"BLOCKED: User denied this potentially dangerous command "
                    f"(matched '{description}' pattern). Do NOT retry this command."
                )
            # "once", "session", "always" — proceed with execution
            return None

        # CLI/gateway path: the existing check_dangerous_command handles
        # prompting.  For non-interactive contexts (gateway, cron), it
        # auto-approves.  This matches the existing behaviour.
        return None
