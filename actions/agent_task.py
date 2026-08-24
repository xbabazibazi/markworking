import json
import platform
import re
import subprocess
import sys
import threading
from pathlib import Path

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
MODEL_PLANNER   = "gemini-flash-latest"
DEFAULT_TIMEOUT = 30
_OS             = platform.system()


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _get_model():
    from google import genai
    client = genai.Client(api_key=_get_api_key())

    class _W:
        def generate_content(self, contents):
            return client.models.generate_content(model=MODEL_PLANNER, contents=contents)

    return _W()


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


_DESTRUCTIVE_PATTERNS = [
    r"\bdel(ete)?\b", r"\berase\b", r"\brm\b", r"\brmdir\b", r"\brd\s+/s\b",
    r"remove-item[^\n]*-recurse", r"remove-item[^\n]*-force", r"clear-content",
    r"\bformat\b", r"\bdiskpart\b", r"\buninstall\b", r"\bshutdown\b",
    r"restart-computer", r"stop-computer", r"taskkill\s+/f", r"stop-process[^\n]*-force",
    r"\breg\s+delete\b", r"remove-itemproperty", r"\bnet\s+user\b",
    r"drop\s+table", r"truncate\s+table", r"delete\s+from",
    r"git\s+push\s+--force", r"git\s+reset\s+--hard", r"git\s+clean\s+-f",
    r"chmod\s+777", r"\bicacls\b", r"vssadmin", r"bcdedit", r"\bkill\s+-9\b",
    r"disable-", r"set-executionpolicy",
]
_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE_PATTERNS), re.IGNORECASE)


def _is_destructive(text: str) -> bool:
    return bool(_DESTRUCTIVE_RE.search(text))


_PENDING: dict | None = None


def _plan(description: str) -> dict:
    model = _get_model()

    shell_name = "PowerShell" if _OS == "Windows" else "bash"

    prompt = f"""You are a senior systems administrator. Break the user's request into a
short sequence of {shell_name} commands that accomplish it on this machine.

Operating system: {_OS}
Shell: {shell_name}
User request: {description}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "summary": "one short sentence describing the overall plan",
  "steps": [
    {{"description": "what this step does", "command": "the exact {shell_name} command"}}
  ]
}}

Rules:
1. Keep it minimal — only steps truly needed to accomplish the request.
2. Each command must be a single, complete, directly-runnable {shell_name} command.
3. Prefer native {shell_name} cmdlets/tools already available on a normal install.
4. Never invent file paths that weren't mentioned — ask for clarification via a step
   description if something is ambiguous, but still produce your best-guess command.
5. Do not wrap commands in quotes beyond what the shell itself requires.

JSON:"""

    response = model.generate_content(prompt)
    raw = _strip_fences(response.text)
    return json.loads(raw)


def _run_step(command: str, timeout: int) -> tuple[bool, str]:
    try:
        if _OS == "Windows":
            args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            args = ["bash", "-c", command]

        result = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, **_WIN_HIDE,
        )

        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        combined = "\n".join(p for p in (out, err) if p) or "(no output)"

        return result.returncode == 0, combined

    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except Exception as e:
        return False, f"Execution error: {e}"


def _execute_plan_bg(steps: list[dict], timeout: int, description: str, speak) -> str:
    def _worker():
        result = _execute_plan(steps, timeout)
        if speak:
            speak(f"[TASK_COMPLETE] agent_task — {description[:60]}\n{result}")

    threading.Thread(target=_worker, daemon=True).start()
    return (
        f"Starting this in the background, sir: {description[:80]}. "
        f"I will let you know when it's done."
    )


def _execute_plan(steps: list[dict], timeout: int) -> str:
    lines = []
    for i, step in enumerate(steps, 1):
        desc = step.get("description", f"step {i}")
        cmd  = step.get("command", "")
        if not cmd:
            continue

        print(f"[AgentTask] ▶ [{i}/{len(steps)}] {desc}: {cmd}")
        ok, output = _run_step(cmd, timeout)
        lines.append(f"[{i}] {desc} — {'OK' if ok else 'FAILED'}\n{output[:800]}")

        if not ok:
            lines.append(f"Stopped after step {i} failed.")
            break

    return "\n\n".join(lines) if lines else "No steps executed."


def agent_task(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    global _PENDING

    p           = parameters or {}
    description = p.get("description", "").strip()
    confirmed   = bool(p.get("confirmed", False))
    timeout     = int(p.get("timeout", DEFAULT_TIMEOUT))

    if not description:
        return "Please describe the task you want done, sir."

    if player:
        player.write_log(f"[AgentTask] {description} (confirmed={confirmed})")

    if confirmed and _PENDING and _PENDING.get("description") == description:
        steps = _PENDING["steps"]
        _PENDING = None
        return _execute_plan_bg(steps, timeout, description, speak)

    try:
        plan = _plan(description)
    except json.JSONDecodeError as e:
        return f"Planning failed to produce valid steps: {e}"
    except Exception as e:
        return f"Planning error: {e}"

    steps   = plan.get("steps", [])
    summary = plan.get("summary", description)

    if not steps:
        return "I could not break this task into any concrete steps, sir."

    plan_text = "\n".join(
        f"{i}. {s.get('description', '')} ({s.get('command', '')})"
        for i, s in enumerate(steps, 1)
    )

    if confirmed:
        return _execute_plan_bg(steps, timeout, description, speak)

    risky = [s for s in steps if _is_destructive(s.get("command", "")) or _is_destructive(s.get("description", ""))]

    if risky:
        _PENDING = {"description": description, "steps": steps}
        return (
            f"[CONFIRMATION_NEEDED] Plan: {summary}\n{plan_text}\n\n"
            f"This includes a step that may be irreversible (destructive command detected). "
            f"Ask the user to explicitly confirm out loud before proceeding. "
            f"If they confirm, call agent_task again with the SAME description and confirmed=true. "
            f"If they decline, do nothing further."
        )

    return _execute_plan_bg(steps, timeout, description, speak)
