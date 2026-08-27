import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

VAULT_PATH = Path(r"C:\Users\DUR\Desktop\documents wiki\knowledge-vault")
MODEL       = "gemini-flash-latest"
MIN_CHARS   = 80   # below this, not even worth an LLM triage call


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = _base_dir()
API_CONFIG  = BASE_DIR / "config" / "api_keys.json"


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


_TR_MAP = str.maketrans("şığüöç", "sigouc")


def _slugify(text: str) -> str:
    text = (text or "session").lower().translate(_TR_MAP)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:60] or "session"


# ── Planning (LLM) ────────────────────────────────────────────────────────────

def _plan_ingest(transcript: str, claude_md: str, index_md: str) -> dict:
    model = _get_model()
    prompt = f"""You are the automated wiki maintainer for a personal Obsidian knowledge vault.
Below is this vault's schema/constitution (CLAUDE.md) — follow it EXACTLY for naming,
frontmatter, and page structure. Note its §14: Zyron (this session's source) is allowed
to write WITHOUT asking for approval, unlike the normal manual ingest flow.

=== VAULT SCHEMA (CLAUDE.md) ===
{claude_md[:16000]}

=== CURRENT INDEX (existing pages — link to/extend these, don't duplicate) ===
{index_md[:6000]}

=== VOICE CONVERSATION TRANSCRIPT TO INGEST ===
{transcript[:15000]}

This transcript is a voice conversation between the user and Zyron, their personal AI
assistant. Decide whether it's worth permanently recording. SKIP trivial exchanges —
single one-off commands ("open Chrome", "what's the volume"), small talk, time/weather
checks. ONLY record conversations containing a durable decision, a lesson learned, a
project update, or a fact genuinely worth remembering later.

Return ONLY valid JSON, no markdown:
{{
  "worth_logging": true/false,
  "reason": "one short sentence why (or why not) — used in the log",
  "date": "YYYY-MM-DD",
  "slug": "kebab-case-slug-under-8-words",
  "pages": [
    {{"path": "sources/sessions/YYYY-MM-DD-zyron-slug.md", "content": "full markdown incl. frontmatter"}}
  ],
  "index_entries": [
    {{"category": "Kaynaklar", "line": "- [[sources/sessions/...]] — one line summary *(proje · tarih)*"}}
  ]
}}

Rules:
- All wiki page content in Turkish (technical terms may stay English) — schema §2.
- Every page needs correct frontmatter (title, tags, project, source, date, status) — schema §5.
- Every page needs a non-empty "## Kaynaklar" section citing the session source page.
- kebab-case paths, no Turkish characters in file names (ş→s, ı→i, ğ→g, ü→u, ö→o, ç→c).
- If this relates to an existing project already in the index, output that project's
  projects/<slug>.md with its COMPLETE updated content (not a diff) rather than duplicating.
- If a genuine decision or lesson emerged, add a "decisions/<slug>.md" or "lessons/<slug>.md"
  page too (same rules). Most sessions only need ONE sources/sessions/ page — don't
  manufacture decisions/lessons that aren't really there.
- If worth_logging is false, "pages" and "index_entries" must be empty arrays and nothing
  else in the JSON matters.

JSON:"""

    response = model.generate_content(prompt)
    raw = _strip_fences(response.text)
    return json.loads(raw)


# ── Vault writes ──────────────────────────────────────────────────────────────

def _write_transcript(date: str, slug: str, session_log: list[str]) -> str:
    slug = re.sub(r"^zyron-", "", slug)  # avoid "zyron-zyron-" if the LLM's slug already has it
    rel = f"raw/sessions/{date}-zyron-{slug}.md"
    full = VAULT_PATH / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("\n".join(session_log), encoding="utf-8")
    return rel


def _write_pages(pages: list[dict]) -> list[str]:
    written = []
    vault_resolved = VAULT_PATH.resolve()
    for p in pages:
        rel = (p.get("path") or "").strip()
        content = p.get("content", "")
        if not rel:
            continue
        full = (VAULT_PATH / rel).resolve()
        if not full.is_relative_to(vault_resolved):
            print(f"[WikiAgent] ⚠️ Refused to write outside vault: {rel}")
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        written.append(rel)
        print(f"[WikiAgent] ✅ Wrote: {rel}")
    return written


def _update_index(index_entries: list[dict]) -> None:
    index_path = VAULT_PATH / "index.md"
    if not index_entries or not index_path.exists():
        return
    text = index_path.read_text(encoding="utf-8")
    for entry in index_entries:
        category = (entry.get("category") or "").strip()
        line = (entry.get("line") or "").strip()
        if not category or not line or line in text:
            continue
        header = f"## {category}"
        idx = text.find(header)
        if idx == -1:
            continue
        line_end = text.find("\n", idx)
        insert_at = line_end + 1 if line_end != -1 else len(text)
        after_insert = insert_at + len(line) + 1   # position right after our new "line\n"
        text = text[:insert_at] + line + "\n" + text[insert_at:]

        # Drop the "*henüz boş*" placeholder paragraph for this section now
        # that it has a real entry — it sits right after what we just inserted.
        next_header = text.find("\n## ", after_insert)
        section_end = next_header if next_header != -1 else len(text)
        section = text[after_insert:section_end]
        cleaned = re.sub(r"\n?\*[^\n]*\*\n", "\n", section, count=1)
        text = text[:after_insert] + cleaned + text[section_end:]
    index_path.write_text(text, encoding="utf-8")


def _recompute_counts() -> None:
    index_path = VAULT_PATH / "index.md"
    if not index_path.exists():
        return

    def _count(folder: str) -> int:
        d = VAULT_PATH / folder
        return len(list(d.glob("*.md"))) if d.exists() else 0

    counts = {
        "Projeler":  _count("projects"),
        "Kaynaklar": _count("sources/articles") + _count("sources/sessions"),
        "Varlıklar": _count("entities"),
        "Kavramlar": _count("concepts"),
        "Kararlar":  _count("decisions"),
        "Dersler":   _count("lessons"),
        "Sentezler": _count("syntheses"),
    }
    total = sum(counts.values())

    text = index_path.read_text(encoding="utf-8")
    for cat, n in counts.items():
        text = re.sub(
            rf"(\|\s*{re.escape(cat)}\s*\|\s*)\d+(\s*\|)", rf"\g<1>{n}\g<2>", text
        )
    text = re.sub(
        r"(\|\s*\*\*Toplam\*\*\s*\|\s*\*\*)\d+(\*\*\s*\|)", rf"\g<1>{total}\g<2>", text
    )
    text = re.sub(
        r"Son güncelleme:.*",
        f"Son güncelleme: {datetime.now().strftime('%Y-%m-%d')} (zyron otomatik ingest)",
        text,
    )
    index_path.write_text(text, encoding="utf-8")


def _prepend_log_entry(slug: str, date: str, written: list[str], reason: str) -> None:
    log_path = VAULT_PATH / "log.md"
    if not log_path.exists():
        return
    text = log_path.read_text(encoding="utf-8")

    files_list = "\n".join(f"- `{p}`" for p in written)
    entry = (
        f"## [{date}] ingest | zyron-session-{slug}\n\n"
        f"- Otomatik (Zyron, onaysız — bkz. CLAUDE.md §14) — {reason}\n"
        f"{files_list}\n\n"
    )

    # Real entries start after the LAST "---" divider in the file — the header
    # docstring above it contains example "## [YYYY-MM-DD] ..." lines inside a
    # fenced code block, which a naive "first '## [' match" would insert into.
    divider = text.rfind("\n---\n")
    if divider != -1:
        insert_at = divider + len("\n---\n")
        while insert_at < len(text) and text[insert_at] == "\n":
            insert_at += 1
        text = text[:insert_at] + entry + text[insert_at:]
    else:
        text = text.rstrip() + "\n\n" + entry
    log_path.write_text(text, encoding="utf-8")


def _git_commit_and_push(slug: str, date: str) -> None:
    """Back up the write immediately — this vault's only real disaster recovery
    is its Gitea remote (see CLAUDE.md, added 2026-08-27 after a same-day
    accidental deletion). Never let a failure here break the ingest itself."""
    try:
        run = lambda *args: subprocess.run(
            ["git", *args], cwd=VAULT_PATH, capture_output=True, text=True, timeout=30
        )
        run("add", "-A")
        commit = run("commit", "-m", f"zyron: {date}-zyron-{slug}")
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            print(f"[WikiAgent] git commit failed: {commit.stderr.strip()}")
            return
        push = run("push")
        if push.returncode != 0:
            print(f"[WikiAgent] git push failed: {push.stderr.strip()}")
    except Exception as e:
        print(f"[WikiAgent] git backup skipped: {e}")


# ── Public API ──────────────────────────────────────────────────────────────

def ingest_session(session_log: list[str], lang: str = "Turkish") -> str | None:
    """
    Called automatically after every Zyron conversation ends — no user approval
    (see vault CLAUDE.md §14, an explicit, deliberate exception to the vault's
    normal manual-ingest-with-approval rule). Silently no-ops if the vault is
    missing or the conversation wasn't worth recording.
    """
    if not VAULT_PATH.exists():
        print(f"[WikiAgent] Vault not found at {VAULT_PATH}, skipping auto-ingest.")
        return None

    transcript = "\n".join(session_log)
    if len(transcript) < MIN_CHARS:
        return None

    try:
        claude_md = (VAULT_PATH / "CLAUDE.md").read_text(encoding="utf-8")
        index_md  = (VAULT_PATH / "index.md").read_text(encoding="utf-8")
        plan = _plan_ingest(transcript, claude_md, index_md)
    except json.JSONDecodeError as e:
        print(f"[WikiAgent] Planning returned invalid JSON: {e}")
        return None
    except Exception as e:
        print(f"[WikiAgent] Planning failed: {e}")
        return None

    if not plan.get("worth_logging"):
        print(f"[WikiAgent] Skipped (not worth logging): {plan.get('reason', '')}")
        return None

    date = plan.get("date") or datetime.now().strftime("%Y-%m-%d")
    slug = _slugify(plan.get("slug", "session"))
    reason = plan.get("reason", "")

    try:
        transcript_rel = _write_transcript(date, slug, session_log)
        written = [transcript_rel] + _write_pages(plan.get("pages", []))
        _update_index(plan.get("index_entries", []))
        _recompute_counts()
        _prepend_log_entry(slug, date, written, reason)
    except Exception as e:
        print(f"[WikiAgent] Write failed: {e}")
        return None

    _git_commit_and_push(slug, date)
    print(f"[WikiAgent] Ingested {len(written)} file(s) — {reason}")
    return f"{len(written)} page(s) filed to the vault."


def wiki_recall(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    p = parameters or {}
    query = (p.get("query") or "").strip()

    if not query:
        return "Please tell me what you'd like me to recall, sir."
    if not VAULT_PATH.exists():
        return "The knowledge vault isn't available right now, sir."

    if player:
        player.write_log(f"[WikiRecall] {query}")

    try:
        index_md = (VAULT_PATH / "index.md").read_text(encoding="utf-8")
    except Exception as e:
        return f"Could not read the vault index: {e}"

    model = _get_model()

    try:
        find_prompt = f"""You are querying a personal Obsidian knowledge vault.

Index of all pages:
{index_md[:8000]}

User's question: {query}

Return ONLY a valid JSON array of up to 5 relative page paths (from the index above,
including the .md extension) most likely to answer this question. Empty array if
nothing looks relevant.

JSON array:"""
        raw = _strip_fences(model.generate_content(find_prompt).text)
        paths = json.loads(raw)
    except Exception as e:
        print(f"[WikiRecall] File selection failed: {e}")
        paths = []

    if not paths:
        return "I couldn't find anything about that in the vault, sir."

    vault_resolved = VAULT_PATH.resolve()
    contents = []
    for rel in paths[:5]:
        try:
            full = (VAULT_PATH / rel).resolve()
            if full.is_relative_to(vault_resolved) and full.is_file():
                contents.append(f"--- {rel} ---\n{full.read_text(encoding='utf-8')[:4000]}")
        except Exception:
            continue

    if not contents:
        return "I couldn't find anything about that in the vault, sir."

    synth_prompt = f"""Answer the user's question using ONLY the wiki pages below.
This will be spoken aloud, not read — be concise, 2-4 sentences unless the question
needs more. If the pages don't actually answer the question, say so honestly.

Question: {query}

Wiki pages:
{chr(10).join(contents)[:12000]}

Answer:"""
    try:
        return model.generate_content(synth_prompt).text.strip()
    except Exception as e:
        return f"Found relevant pages but couldn't synthesize an answer: {e}"
