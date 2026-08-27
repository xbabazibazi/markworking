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

MODEL             = "gemini-flash-latest"
MAX_TREE_ENTRIES  = 500
MAX_READ_FILES    = 10
MAX_FILE_CHARS    = 6000
GIT_TIMEOUT       = 20
MAX_VERIFY_ROUNDS = 2
TEST_TIMEOUT      = 90

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
    from core.llm_retry import get_model
    return get_model(MODEL)


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


def _review_plan(task: str, plan: dict) -> dict:
    """Second opinion: a senior developer reviewing a colleague's diff before it
    ships. Catches scope creep and obvious mistakes before anything is written."""
    model = _get_model()
    changes = plan.get("changes", [])
    diff_text = "\n\n".join(
        f"--- {c.get('path')} ---\n{c.get('content', '')}" for c in changes
    )

    prompt = f"""You are a senior developer doing code review on a colleague's proposed
change before it ships. Be critical but fair — this is a real review, not a rubber stamp.

Task that was supposed to be accomplished: {task}

Proposed summary: {plan.get('summary', '')}

Proposed file changes (complete new content per file):
{diff_text[:30000]}

Review checklist:
- Does this actually accomplish the stated task — nothing more, nothing less?
- Any unrelated or unnecessary changes that should be reverted?
- Any obvious bugs, missing error handling, or logic mistakes?
- Does it match the existing code's style and conventions?

Return ONLY valid JSON — no markdown:
{{
  "approved": true/false,
  "feedback": "if not approved, specific actionable feedback on what to fix; empty string if approved"
}}

JSON:"""

    try:
        response = model.generate_content(prompt)
        return json.loads(_strip_fences(response.text))
    except Exception as e:
        print(f"[ProjectAgent] ⚠️ Review failed (proceeding without it): {e}")
        return {"approved": True, "feedback": ""}


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


def _verify_files(root: Path, rel_paths: list[str]) -> dict[str, str]:
    """Syntax-check written files; returns rel_path -> error text for failures.
    Checkers that aren't installed (e.g. node) are skipped silently — better
    no verification than a false failure."""
    checkers = {
        ".py":  [sys.executable, "-m", "py_compile"],
        ".js":  ["node", "--check"],
        ".mjs": ["node", "--check"],
        ".cjs": ["node", "--check"],
    }
    errors: dict[str, str] = {}
    for rel in rel_paths:
        full = root / rel
        ext = full.suffix.lower()

        if ext == ".json":
            try:
                json.loads(full.read_text(encoding="utf-8"))
            except Exception as e:
                errors[rel] = f"JSON parse error: {e}"
            continue

        checker = checkers.get(ext)
        if not checker:
            continue
        try:
            result = subprocess.run(
                checker + [str(full)],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=30, **_WIN_HIDE,
            )
            if result.returncode != 0:
                errors[rel] = (result.stderr or result.stdout).strip()[:1500]
        except FileNotFoundError:
            continue   # checker binary not on this machine
        except Exception:
            continue
    return errors


def _detect_test_command(root: Path) -> list[str] | None:
    """Best-effort detection of how to run this project's existing test suite.
    Returns None if nothing recognisable is found — not every project has tests,
    and that's not something project_agent should invent."""
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if data.get("scripts", {}).get("test"):
                if (root / "pnpm-lock.yaml").exists():
                    return ["pnpm", "test"]
                if (root / "yarn.lock").exists():
                    return ["yarn", "test"]
                return ["npm", "test"]
        except Exception:
            pass

    has_pytest_cfg = any((root / f).exists() for f in ("pytest.ini", "setup.cfg", "tox.ini"))
    pyproject = root / "pyproject.toml"
    has_pyproject_pytest = False
    if pyproject.exists():
        try:
            has_pyproject_pytest = "pytest" in pyproject.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass
    has_tests_dir = (root / "tests").is_dir() or (root / "test").is_dir()
    if has_pytest_cfg or has_pyproject_pytest or has_tests_dir:
        return [sys.executable, "-m", "pytest", "-q"]

    if (root / "go.mod").exists():
        return ["go", "test", "./..."]

    return None


def _run_tests(root: Path) -> dict | None:
    """Runs the detected test command. Returns None if no test runner was found —
    distinct from a run that happened and failed."""
    cmd = _detect_test_command(root)
    if not cmd:
        return None
    try:
        result = subprocess.run(
            cmd, cwd=str(root),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=TEST_TIMEOUT, **_WIN_HIDE,
        )
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        return {"passed": result.returncode == 0, "output": output[:8000]}
    except subprocess.TimeoutExpired:
        return {"passed": False, "output": f"Test run exceeded {TEST_TIMEOUT}s timeout — treating as failure."}
    except FileNotFoundError as e:
        print(f"[ProjectAgent] ⚠️ Test runner not available: {e}")
        return None
    except Exception as e:
        return {"passed": False, "output": str(e)}


def _fix_test_failures(root: Path, task: str, test_output: str, written: list[str]) -> list[str]:
    """Asks the model to repair the files it wrote given real test failure output."""
    model = _get_model()
    file_contents = _read_files(root, written)
    context = "\n\n".join(f"--- {p} ---\n{c}" for p, c in file_contents.items())

    prompt = f"""You are a senior developer. The change below passes syntax checks but FAILS
the project's existing test suite. Fix the code so the tests pass, without breaking the
original task.

Task: {task}

Test failure output:
{test_output[:6000]}

Current content of the files you changed:
{context[:30000]}

Return ONLY valid JSON — no markdown:
{{"changes": [{{"path": "relative/path", "content": "COMPLETE new file content"}}]}}

Only include files that actually need to change to fix the failing tests.

JSON:"""

    try:
        response = model.generate_content(prompt)
        plan = json.loads(_strip_fences(response.text))
        return _apply_changes(root, plan.get("changes", []))
    except Exception as e:
        print(f"[ProjectAgent] ⚠️ Test-failure fix planning failed: {e}")
        return []


def _fix_broken_files(root: Path, task: str, errors: dict[str, str]) -> list[str]:
    """Ask the model to repair each file that failed verification; returns rewritten paths."""
    model = _get_model()
    fixed = []
    for rel, error in errors.items():
        full = root / rel
        try:
            broken = full.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        prompt = f"""You are a senior developer. The file below was just written for this task
but fails its syntax check. Fix it. Return ONLY the complete corrected file content —
no explanation, no markdown, no backticks.

Task the file is part of: {task}

Syntax error:
{error}

Broken file ({rel}):
{broken[:20000]}

Corrected file content:"""
        try:
            corrected = _strip_fences(model.generate_content(prompt).text)
            if corrected:
                full.write_text(corrected, encoding="utf-8")
                fixed.append(rel)
                print(f"[ProjectAgent] 🔧 Repaired: {rel}")
        except Exception as e:
            print(f"[ProjectAgent] ⚠️ Could not repair {rel}: {e}")
    return fixed


def _run_git(root: Path, args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git"] + args, cwd=str(root),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT, **_WIN_HIDE,
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

        # Self-review: a second senior-developer pass on the diff before it's
        # ever written to disk. One bounded revision round if rejected — real
        # code review doesn't loop forever either.
        log("Reviewing own plan before writing...")
        review = _review_plan(task, plan)
        review_note = ""
        if not review.get("approved", True) and review.get("feedback"):
            log(f"Self-review requested changes: {review['feedback'][:150]}")
            revised_task = (
                f"{task}\n\nA senior review of your first attempt found this issue — "
                f"address it: {review['feedback']}"
            )
            plan = _plan_changes(revised_task, tree_text, file_contents)
            changes = plan.get("changes", [])
            summary = plan.get("summary", task)
            review_note = "(Revised once after self-review caught an issue.) "
            if not changes:
                if speak:
                    speak(f"[TASK_COMPLETE] project_agent — {task[:60]}\nSelf-review flagged the plan and the revision produced no changes, sir: {review['feedback'][:200]}")
                return

        written = _apply_changes(root, changes)

        # Verify-and-fix loop: a senior developer checks their work before
        # shipping. First syntax, then (if that passes) the project's own
        # test suite — both get up to MAX_VERIFY_ROUNDS repair attempts from
        # a shared budget.
        verify_note = ""
        rounds = 0
        errors = _verify_files(root, written)
        while errors and rounds < MAX_VERIFY_ROUNDS:
            rounds += 1
            log(f"Verification failed for {len(errors)} file(s) — repairing (round {rounds})...")
            _fix_broken_files(root, task, errors)
            errors = _verify_files(root, written)

        test_result = None
        if not errors:
            test_result = _run_tests(root)
            while (
                test_result and not test_result["passed"] and rounds < MAX_VERIFY_ROUNDS
            ):
                rounds += 1
                log(f"Test suite failed — repairing (round {rounds})...")
                _fix_test_failures(root, task, test_result["output"], written)
                errors = _verify_files(root, written)
                if errors:
                    break
                test_result = _run_tests(root)

        if errors:
            verify_note = (
                f"WARNING — {len(errors)} file(s) still fail syntax checks after "
                f"{rounds} repair round(s): {', '.join(errors)}. "
            )
        elif test_result and not test_result["passed"]:
            verify_note = (
                f"WARNING — project's test suite still fails after {rounds} repair "
                f"round(s): {test_result['output'][:300]} "
            )
        elif test_result and test_result["passed"]:
            verify_note = f"(Test suite passes{' after ' + str(rounds) + ' repair round(s)' if rounds else ''}.) "
        elif rounds:
            verify_note = f"(Fixed {rounds} round(s) of syntax errors before finishing.) "

        broken = bool(errors) or bool(test_result and not test_result["passed"])

        git_result = "Skipped (not requested)."
        if do_git and written:
            if broken:
                git_result = "Skipped — will not commit code that fails verification or tests."
            else:
                log("Committing and pushing...")
                git_result = _commit_and_push(root, written, summary)

        result = (
            f"{summary}\n"
            f"{review_note}{verify_note}"
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
