import json
import re
import subprocess
import sys
import threading
from pathlib import Path

MODEL             = "gemini-flash-latest"
MAX_TREE_ENTRIES  = 500
MAX_READ_FILES    = 10
MAX_FILE_CHARS    = 6000
GIT_TIMEOUT       = 20

_IGNORE_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    "target", ".next", "bin", "obj", "Debug", "Release", ".idea", ".vscode",
    "vendor", ".pytest_cache", ".mypy_cache", "coverage", "out",
}


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR      = _base_dir()
API_CONFIG    = BASE_DIR / "config" / "api_keys.json"
KNOWN_PROJECTS_PATH = BASE_DIR / "config" / "known_projects.json"


def _get_api_key() -> str:
    return json.loads(API_CONFIG.read_text(encoding="utf-8"))["gemini_api_key"]


def _get_model():
    from google import genai
    client = genai.Client(api_key=_get_api_key())

    class _W:
        def generate_content(self, contents):
            return client.models.generate_content(model=MODEL, contents=contents)

    return _W()


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def _load_known_projects() -> dict:
    try:
        return json.loads(KNOWN_PROJECTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_project_path(name_or_path: str) -> Path | None:
    raw = (name_or_path or "").strip()
    if not raw:
        return None

    p = Path(raw)
    if p.is_absolute() and p.is_dir():
        return p

    known = _load_known_projects()
    key = raw.lower().strip()
    if key in known:
        cand = Path(known[key])
        return cand if cand.is_dir() else None

    for alias, path_str in known.items():
        if alias in key or key in alias:
            cand = Path(path_str)
            return cand if cand.is_dir() else None

    return None


def _build_file_tree(root: Path) -> str:
    lines = []
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= MAX_TREE_ENTRIES:
            lines.append(f"... ({count}+ entries, truncated)")
            break
        if any(part in _IGNORE_DIRS for part in path.parts):
            continue
        if path.is_file():
            rel = path.relative_to(root)
            lines.append(str(rel).replace("\\", "/"))
            count += 1
    return "\n".join(lines)


def _select_relevant_files(task: str, tree_text: str) -> list[str]:
    model = _get_model()
    prompt = f"""You are a senior developer exploring an existing codebase before making a change.

Task: {task}

Project file tree (relative paths):
{tree_text[:12000]}

Return ONLY a valid JSON array of up to {MAX_READ_FILES} relative file paths from the tree above
that you need to READ to understand how to accomplish the task (existing code style, related
logic, config, entry points). Pick the most relevant ones only.

JSON array:"""

    response = model.generate_content(prompt)
    raw = _strip_fences(response.text)
    try:
        files = json.loads(raw)
        return [f for f in files if isinstance(f, str)][:MAX_READ_FILES]
    except Exception:
        return []


def _read_files(root: Path, rel_paths: list[str]) -> dict[str, str]:
    contents = {}
    for rel in rel_paths:
        try:
            full = (root / rel).resolve()
            if not full.is_relative_to(root.resolve()) or not full.is_file():
                continue
            text = full.read_text(encoding="utf-8", errors="ignore")
            if len(text) > MAX_FILE_CHARS:
                text = text[:MAX_FILE_CHARS] + f"\n\n[Truncated — {len(text)} total chars]"
            contents[rel] = text
        except Exception as e:
            contents[rel] = f"[Could not read: {e}]"
    return contents


def _plan_changes(task: str, tree_text: str, file_contents: dict[str, str]) -> dict:
    model = _get_model()

    context = "\n\n".join(
        f"--- {path} ---\n{content}" for path, content in file_contents.items()
    )

    prompt = f"""You are a senior developer making a change to an existing project.

Task: {task}

Project file tree (relative paths):
{tree_text[:12000]}

Contents of the files you asked to read:
{context[:40000]}

Make the minimal, correct change to accomplish the task. Return ONLY valid JSON — no markdown:
{{
  "summary": "one short sentence describing what changed",
  "changes": [
    {{"path": "relative/path/to/file.ext", "content": "COMPLETE new file content, not a diff"}}
  ]
}}

Rules:
1. "path" must be a relative path (forward slashes), inside the project — never outside it.
2. "content" is the COMPLETE new content of the file — you are replacing the whole file, not patching it.
3. Match the project's existing code style, naming conventions, and language exactly.
4. Only include files that actually need to change or be created — do not rewrite untouched files.
5. Keep the change minimal and focused on the task — no unrelated refactors.

JSON:"""

    response = model.generate_content(prompt)
    raw = _strip_fences(response.text)
    return json.loads(raw)


def _apply_changes(root: Path, changes: list[dict]) -> list[str]:
    written = []
    for change in changes:
        rel = change.get("path", "").strip()
        content = change.get("content", "")
        if not rel:
            continue
        full = (root / rel).resolve()
        if not full.is_relative_to(root.resolve()):
            print(f"[ProjectAgent] ⚠️ Refused to write outside project: {rel}")
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        written.append(rel)
        print(f"[ProjectAgent] ✅ Wrote: {rel}")
    return written


def _run_git(root: Path, args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git"] + args, cwd=str(root),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        return result.returncode == 0, "\n".join(p for p in (out, err) if p)
    except Exception as e:
        return False, str(e)


def _commit_and_push(root: Path, written: list[str], summary: str) -> str:
    if not (root / ".git").exists():
        return "Not a git repository — skipped commit."

    ok, _ = _run_git(root, ["add"] + written)
    if not ok:
        return "Could not stage changed files for commit."

    ok, out = _run_git(root, ["commit", "-m", f"Jarvis: {summary}"])
    if not ok:
        if "nothing to commit" in out.lower():
            return "Nothing to commit (files unchanged)."
        return f"Commit failed: {out[:200]}"

    ok, out = _run_git(root, ["push"])
    if not ok:
        return f"Committed locally, but push failed: {out[:200]}"

    return "Committed and pushed."


def _run_task(root: Path, task: str, do_git: bool, speak, player) -> None:
    def log(msg: str):
        print(f"[ProjectAgent] {msg}")
        if player:
            player.write_log(f"[ProjectAgent] {msg}")

    try:
        log(f"Scanning {root}...")
        tree_text = _build_file_tree(root)

        log("Finding relevant files...")
        relevant = _select_relevant_files(task, tree_text)
        file_contents = _read_files(root, relevant) if relevant else {}

        log(f"Planning change ({len(file_contents)} files read)...")
        plan = _plan_changes(task, tree_text, file_contents)
        changes = plan.get("changes", [])
        summary = plan.get("summary", task)

        if not changes:
            if speak:
                speak(f"[TASK_COMPLETE] project_agent — {task[:60]}\nI could not determine any concrete file changes for this task, sir.")
            return

        written = _apply_changes(root, changes)

        git_result = "Skipped (not requested)."
        if do_git and written:
            log("Committing and pushing...")
            git_result = _commit_and_push(root, written, summary)

        result = (
            f"{summary}\n"
            f"Files changed: {', '.join(written) if written else '(none)'}\n"
            f"Git: {git_result}"
        )
        if speak:
            speak(f"[TASK_COMPLETE] project_agent — {task[:60]}\n{result}")

    except json.JSONDecodeError as e:
        if speak:
            speak(f"[TASK_COMPLETE] project_agent — {task[:60]}\nPlanning failed to produce valid steps: {e}")
    except Exception as e:
        if speak:
            speak(f"[TASK_COMPLETE] project_agent — {task[:60]}\nFailed: {e}")


def project_agent(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p       = parameters or {}
    project = p.get("project", "").strip()
    task    = p.get("task", "").strip()
    do_git  = bool(p.get("commit", True))

    if not task:
        return "Please describe what change you want made, sir."
    if not project:
        return "Please tell me which project — a known project name or a full folder path, sir."

    root = resolve_project_path(project)
    if root is None:
        return f"I don't know a project called '{project}', sir. Give me its full folder path."

    if player:
        player.write_log(f"[ProjectAgent] {root.name}: {task}")

    threading.Thread(target=_run_task, args=(root, task, do_git, speak, player), daemon=True).start()
    return (
        f"Starting work on {root.name} in the background, sir: {task[:80]}. "
        f"I will let you know when it's done."
    )
