"""CI helpers for the scheduled workflow (docs/CI-SETUP.md).

A GitHub runner starts empty, but the pipeline needs yesterday's state: the item
pool, the seen and failed lists, the circuit breaker, the published log and the
feed cache. That state lives on its own branch, ``pipeline-state``, never on
``main`` (``main`` only ever receives reviewed articles). Each run restores it
before the pipeline and saves it afterwards as ONE orphan commit that replaces
the branch, so the branch never grows.

``data/entities.json`` is special: ``main`` holds the curated file and a run can
append new entities to it. Restore keeps ``main``'s entries and adds the entities
only the saved state knows.

Every git step uses plumbing (``hash-object``, ``mktree``, ``commit-tree``, a
temporary index), so the working tree and the checked-out branch are never
touched. In draft mode each finished article goes to its own branch,
``draft/<slug>-<run id>``, holding exactly the article and its cover; the
workflow opens a pull request from it, and merging that pull request publishes.

    python -m pipeline.ci restore
    python -m pipeline.ci save
    python -m pipeline.ci reset-breaker
    python -m pipeline.ci breaker-status
    python -m pipeline.ci draft-branch --report data/reports/last-run.json --run-id 123
    python -m pipeline.ci notify --report data/reports/last-run.json
    python -m pipeline.ci telegram-test
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import notify

PROJECT_ROOT = _PIPELINE_DIR.parent
logger = logging.getLogger("gamersxpress.pipeline.ci")

STATE_BRANCH = "pipeline-state"
#: Run-time state, relative to ``data/``. Curated read-only data (``orgs.json``,
#: ``origins.json``) stays on ``main`` and is not listed.
STATE_FILES = (
    "pool.json",
    "seen.json",
    "failed.json",
    "breaker.json",
    "published-log.json",
    "feed-cache.json",
    "entities.json",
)
ENTITIES_FILE = "entities.json"

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
MAX_SLUG_CHARS = 120

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class CiError(RuntimeError):
    """A CI step could not be completed safely."""


def _identity_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME=BOT_NAME,
        GIT_AUTHOR_EMAIL=BOT_EMAIL,
        GIT_COMMITTER_NAME=BOT_NAME,
        GIT_COMMITTER_EMAIL=BOT_EMAIL,
    )
    if extra:
        env.update(extra)
    return env


def _git(
    repo: Path,
    *args: str,
    input: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=repo, input=input, env=env, capture_output=True
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()[:300]
        raise CiError(f"git {args[0]} failed (exit {result.returncode}): {detail}")
    return result


def _text(result: subprocess.CompletedProcess) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


def remote_has_branch(repo: Path, branch: str, *, remote: str = "origin") -> bool:
    """True when ``remote`` has ``branch``. A network or auth error raises."""
    # The full ref name: a bare "name" would also match "other/name".
    result = _git(repo, "ls-remote", "--exit-code", "--heads", remote, f"refs/heads/{branch}", check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 2:  # git's "no matching refs"
        return False
    detail = result.stderr.decode("utf-8", "replace").strip()[:300]
    raise CiError(f"could not look up branch {branch!r} on {remote}: {detail}")


# --- state ------------------------------------------------------------------


def merge_entities(base: dict, state: dict) -> dict:
    """``base`` (the curated file from ``main``) plus entities only ``state`` has."""
    merged = json.loads(json.dumps(base))
    entries = merged.setdefault("entities", [])
    known: set[str] = set()
    for entry in entries:
        known.add(str(entry.get("canonical", "")).casefold())
        known.update(str(alias).casefold() for alias in entry.get("aliases", []))
    for entry in state.get("entities", []) if isinstance(state, dict) else []:
        canonical = str(entry.get("canonical", "")).strip()
        if canonical and canonical.casefold() not in known:
            entries.append(entry)
            known.add(canonical.casefold())
            known.update(str(alias).casefold() for alias in entry.get("aliases", []))
    return merged


def restore_state(repo: Path, data_dir: Path, *, remote: str = "origin") -> list[str]:
    """Copy the saved state into ``data_dir``. Returns the files restored.

    No state branch means a first run: nothing is restored and that is fine.
    Any other failure raises, so the workflow never saves over state it could
    not read.
    """
    if not remote_has_branch(repo, STATE_BRANCH, remote=remote):
        logger.info("no %s branch yet: starting from the state on main", STATE_BRANCH)
        return []
    ref = f"refs/remotes/{remote}/{STATE_BRANCH}"
    _git(repo, "fetch", "--depth=1", "--no-tags", remote, f"+refs/heads/{STATE_BRANCH}:{ref}")
    data_dir.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    for name in STATE_FILES:
        if _git(repo, "cat-file", "-e", f"{ref}:{name}", check=False).returncode != 0:
            continue
        raw = _git(repo, "show", f"{ref}:{name}").stdout
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise CiError(f"saved state file {name} is not valid JSON") from exc
        target = data_dir / name
        if name == ENTITIES_FILE and target.is_file():
            merged = merge_entities(json.loads(target.read_text(encoding="utf-8")), document)
            target.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        else:
            target.write_bytes(raw)
        restored.append(name)
    logger.info("restored state files: %s", ", ".join(restored) or "(none)")
    return restored


def save_state(
    repo: Path,
    data_dir: Path,
    *,
    remote: str = "origin",
    message: str = "pipeline state",
) -> str:
    """Replace the state branch with one commit holding the current state files."""
    lines: list[str] = []
    for name in STATE_FILES:
        path = data_dir / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            json.loads(raw)
        except ValueError as exc:
            raise CiError(f"refusing to save {name}: it is not valid JSON") from exc
        sha = _text(_git(repo, "hash-object", "-w", "--stdin", input=raw))
        lines.append(f"100644 blob {sha}\t{name}")
    if not lines:
        raise CiError("no state files to save")
    tree = _text(_git(repo, "mktree", input=("\n".join(lines) + "\n").encode("utf-8")))
    commit = _text(_git(repo, "commit-tree", tree, "-m", message, env=_identity_env()))
    _git(repo, "push", "--force", remote, f"{commit}:refs/heads/{STATE_BRANCH}")
    logger.info("saved %d state file(s) as %s", len(lines), commit[:10])
    return commit


def reset_breaker(data_dir: Path) -> dict:
    """Close the circuit breaker (the same fields ``run.reset_breaker`` clears)."""
    path = data_dir / "breaker.json"
    breaker: dict[str, Any] = {}
    if path.is_file():
        breaker = json.loads(path.read_text(encoding="utf-8"))
    breaker.update(consecutive_failures=0, open=False, recent_failures=[])
    breaker.setdefault("last_failure_date", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(breaker, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return breaker


# --- draft branch -----------------------------------------------------------


def _plain(text: object, limit: int = 120) -> str:
    """Single-line text that is safe in a commit subject or pull request title.

    Titles come from a model, so mentions, markup and line breaks are removed.
    """
    cleaned = re.sub(r"[@<>`\[\]]", "", " ".join(str(text or "").split()))
    return cleaned[:limit].strip()


def prepare_draft_branch(
    repo: Path,
    report: dict,
    *,
    run_id: str,
    remote: str = "origin",
) -> dict | None:
    """Push a branch holding exactly the new article and its cover, or None.

    Returns None unless the run completed. The branch is one commit on top of
    HEAD, built in a temporary index, so the working tree is left alone.
    """
    if report.get("status") != "completed":
        return None
    slug = str(report.get("slug") or "")
    if not SLUG_RE.match(slug) or len(slug) > MAX_SLUG_CHARS:
        raise CiError(f"unsafe article slug: {slug[:60]!r}")
    if not RUN_ID_RE.match(str(run_id)):
        raise CiError("unsafe run id")
    article = f"src/content/articles/{slug}.md"
    cover = f"public/covers/{slug}.webp"
    if not (repo / article).is_file():
        logger.warning("run completed but %s is missing: no draft branch", article)
        return None
    files = [article] + ([cover] if (repo / cover).is_file() else [])
    title = notify.article_title(repo / article) or slug
    branch = f"draft/{slug}-{run_id}"

    with tempfile.TemporaryDirectory() as scratch:
        env = _identity_env({"GIT_INDEX_FILE": str(Path(scratch) / "index")})
        _git(repo, "read-tree", "HEAD", env=env)
        _git(repo, "add", "--", *files, env=env)
        tree = _text(_git(repo, "write-tree", env=env))
        changed = set(_text(_git(repo, "diff-tree", "-r", "--name-only", "HEAD", tree)).splitlines())
        if changed != set(files):
            raise CiError(f"draft commit would change unexpected files: {sorted(changed ^ set(files))}")
        commit = _text(
            _git(
                repo, "commit-tree", tree, "-p", "HEAD",
                "-m", f"Draft: {_plain(title, 70)}", env=env,
            )
        )
    _git(repo, "push", remote, f"{commit}:refs/heads/{branch}")
    logger.info("pushed draft branch %s (%d file(s))", branch, len(files))
    return {"branch": branch, "slug": slug, "title": _plain(title, 70), "files": files, "commit": commit}


def pr_body(info: dict, *, report: dict | None = None, run_url: str | None = None) -> str:
    """Markdown body for the draft pull request."""
    lines = [
        "Draft article from the scheduled pipeline.",
        "",
        "**Check before merging**",
        "- [ ] Every claim matches the cited sources (open them)",
        "- [ ] Rumors are labeled; nothing unconfirmed is stated as fact",
        "- [ ] Opinions are credited to whoever voiced them",
        "- [ ] The article adds context, not only a restatement",
        "",
    ]
    if (report or {}).get("verification") == "single-origin":
        lines += ["**Relayed report:** one named source, several outlets. Read it harder.", ""]
    lines.append("Merging publishes the article: the site deploys automatically.")
    if run_url and run_url.startswith(notify.GITHUB_URL_PREFIX):
        lines += ["", f"Workflow run: {run_url}"]
    return "\n".join(lines) + "\n"


# --- command line -----------------------------------------------------------


def _load_report(path: str | Path) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _set_output(key: str, value: str) -> None:
    """Write a GitHub step output (single-line values only)."""
    value = " ".join(str(value).split())
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")
    else:
        print(f"{key}={value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.ci", description="CI helpers for the scheduled workflow.")
    parser.add_argument("command", choices=["restore", "save", "reset-breaker", "breaker-status", "draft-branch", "notify", "telegram-test"])
    parser.add_argument("--repo", default=str(PROJECT_ROOT), help="repository root")
    parser.add_argument("--data-dir", default=None, help="data directory (default: <repo>/data)")
    parser.add_argument("--report", default=None, help="last-run.json (default: <data>/reports/last-run.json)")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    parser.add_argument("--run-url", default=None)
    parser.add_argument("--pr-url", default=None)
    parser.add_argument("--pr-body-file", default=None, help="where draft-branch writes the pull request body")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    repo = Path(args.repo)
    data_dir = Path(args.data_dir) if args.data_dir else repo / "data"
    report_path = Path(args.report) if args.report else data_dir / "reports" / "last-run.json"

    try:
        if args.command == "restore":
            restore_state(repo, data_dir)
        elif args.command == "save":
            save_state(repo, data_dir)
        elif args.command == "reset-breaker":
            reset_breaker(data_dir)
            logger.info("circuit breaker reset")
        elif args.command == "breaker-status":
            try:
                is_open = bool(json.loads((data_dir / "breaker.json").read_text(encoding="utf-8")).get("open"))
            except (OSError, ValueError):
                is_open = False
            _set_output("breaker_open", "true" if is_open else "false")
        elif args.command == "draft-branch":
            report = _load_report(report_path)
            info = prepare_draft_branch(repo, report, run_id=args.run_id)
            _set_output("has_draft", "true" if info else "false")
            if info:
                body_file = Path(args.pr_body_file or tempfile.mkstemp(suffix=".md")[1])
                body_file.write_text(pr_body(info, report=report, run_url=args.run_url), encoding="utf-8")
                _set_output("branch", info["branch"])
                _set_output("title", info["title"])
                _set_output("body_file", str(body_file))
        elif args.command == "notify":
            report = _load_report(report_path)
            article = report.get("article_path")
            title = notify.article_title(article) if article else None
            message = notify.build_message(report, title=title, run_url=args.run_url, pr_url=args.pr_url)
            if message:
                notify.send_telegram(message)
            else:
                logger.info("no alert for this run (status %s)", report.get("status"))
        elif args.command == "telegram-test":
            sent = notify.send_telegram("✅ GamersXpress: Telegram alerts are working.")
            return 0 if sent else 1
    except CiError as exc:
        logger.error("ci error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
