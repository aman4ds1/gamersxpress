import json
import os
import subprocess

import pytest

import ci
import notify

IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in IDENTITY.items():
        monkeypatch.setenv(key, value)


def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


ENTITIES = {"entities": [{"canonical": "PlayStation 5", "aliases": ["PS5"]}]}


@pytest.fixture
def repo(tmp_path):
    """A working clone of a bare remote, with one commit on main."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "checkout", "-b", "main")
    write_json(work / "data" / "pool.json", {"items": []})
    write_json(work / "data" / "breaker.json", {"consecutive_failures": 0, "open": False})
    write_json(work / "data" / "entities.json", ENTITIES)
    (work / "src" / "content" / "articles").mkdir(parents=True)
    (work / "src" / "content" / "articles" / ".gitkeep").write_text("")
    git(work, "add", "-A")
    git(work, "commit", "-m", "init")
    git(work, "push", "-u", "origin", "main")
    return work


def fresh_clone(repo, tmp_path, name="runner"):
    clone = tmp_path / name
    git(tmp_path, "clone", str(repo.parent / "remote.git"), str(clone))
    return clone


def branch_commits(repo, branch):
    return git(repo, "--git-dir", str(repo.parent / "remote.git"), "rev-list", branch).split()


# --- state --------------------------------------------------------------------


def test_restore_with_no_state_branch_is_a_first_run(repo):
    before = (repo / "data" / "pool.json").read_text()
    assert ci.restore_state(repo, repo / "data") == []
    assert (repo / "data" / "pool.json").read_text() == before


def test_restore_fails_loudly_when_the_remote_cannot_be_read(repo):
    # Treating a network error as "no state yet" would let the workflow overwrite
    # good state with an empty one.
    with pytest.raises(ci.CiError):
        ci.restore_state(repo, repo / "data", remote="no-such-remote")


def test_state_round_trips_through_the_state_branch(repo, tmp_path):
    write_json(repo / "data" / "pool.json", {"items": [1, 2, 3]})
    write_json(repo / "data" / "seen.json", {"seen": ["a"]})
    ci.save_state(repo, repo / "data")

    runner = fresh_clone(repo, tmp_path)
    restored = ci.restore_state(runner, runner / "data")
    assert "pool.json" in restored and "seen.json" in restored
    assert json.loads((runner / "data" / "pool.json").read_text()) == {"items": [1, 2, 3]}
    assert json.loads((runner / "data" / "seen.json").read_text()) == {"seen": ["a"]}


def test_the_state_branch_is_always_one_parentless_commit(repo):
    ci.save_state(repo, repo / "data")
    first = branch_commits(repo, ci.STATE_BRANCH)
    write_json(repo / "data" / "pool.json", {"items": [9]})
    ci.save_state(repo, repo / "data")
    second = branch_commits(repo, ci.STATE_BRANCH)
    assert len(first) == 1 and len(second) == 1
    assert first != second


def test_the_state_branch_holds_only_state_files(repo):
    (repo / "data" / "orgs.json").write_text("{}")
    write_json(repo / "data" / "failed.json", {})
    ci.save_state(repo, repo / "data")
    listed = git(
        repo, "--git-dir", str(repo.parent / "remote.git"), "ls-tree", "--name-only", ci.STATE_BRANCH
    ).split()
    assert set(listed) <= set(ci.STATE_FILES)
    assert "orgs.json" not in listed


def test_saving_never_touches_the_working_tree_or_the_checked_out_branch(repo):
    (repo / "data" / "scratch.txt").write_text("untracked")
    write_json(repo / "data" / "pool.json", {"items": [5]})
    head = git(repo, "rev-parse", "HEAD")
    status = git(repo, "status", "--porcelain")
    ci.save_state(repo, repo / "data")
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(repo, "status", "--porcelain") == status


def test_saving_refuses_a_state_file_that_is_not_json(repo):
    (repo / "data" / "pool.json").write_text("{ broken")
    with pytest.raises(ci.CiError):
        ci.save_state(repo, repo / "data")
    assert not ci.remote_has_branch(repo, ci.STATE_BRANCH)


def test_restore_refuses_state_that_is_not_json(repo, tmp_path):
    # Plant a corrupt state branch directly.
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=b"{ broken", capture_output=True
    ).stdout.decode().strip()
    tree = subprocess.run(
        ["git", "mktree"], cwd=repo, input=f"100644 blob {blob}\tpool.json\n".encode(), capture_output=True
    ).stdout.decode().strip()
    commit = git(repo, "commit-tree", tree, "-m", "bad")
    git(repo, "push", "origin", f"{commit}:refs/heads/{ci.STATE_BRANCH}")
    with pytest.raises(ci.CiError):
        ci.restore_state(repo, repo / "data")


def test_merge_entities_keeps_main_and_adds_only_new_state_entities():
    base = {"entities": [{"canonical": "PlayStation 5", "aliases": ["PS5"]}]}
    state = {
        "entities": [
            {"canonical": "PlayStation 5", "aliases": ["PS5", "Play Station 5"]},
            {"canonical": "ps5", "aliases": []},
            {"canonical": "Duskbloods", "aliases": []},
        ]
    }
    merged = ci.merge_entities(base, state)
    assert [entry["canonical"] for entry in merged["entities"]] == ["PlayStation 5", "Duskbloods"]
    assert merged["entities"][0]["aliases"] == ["PS5"]
    assert base == {"entities": [{"canonical": "PlayStation 5", "aliases": ["PS5"]}]}


def test_restore_merges_entities_instead_of_overwriting_main(repo, tmp_path):
    write_json(
        repo / "data" / "entities.json",
        {"entities": [*ENTITIES["entities"], {"canonical": "Duskbloods", "aliases": []}]},
    )
    ci.save_state(repo, repo / "data")

    runner = fresh_clone(repo, tmp_path)
    write_json(
        runner / "data" / "entities.json",
        {"entities": [*ENTITIES["entities"], {"canonical": "Added On Main", "aliases": []}]},
    )
    ci.restore_state(runner, runner / "data")
    canonicals = [e["canonical"] for e in json.loads((runner / "data" / "entities.json").read_text())["entities"]]
    assert canonicals == ["PlayStation 5", "Added On Main", "Duskbloods"]


def test_reset_breaker_closes_it_and_keeps_the_last_failure_date(tmp_path):
    write_json(
        tmp_path / "breaker.json",
        {"consecutive_failures": 3, "open": True, "last_failure_date": "2026-10-30", "recent_failures": ["x"]},
    )
    ci.reset_breaker(tmp_path)
    assert json.loads((tmp_path / "breaker.json").read_text()) == {
        "consecutive_failures": 0,
        "open": False,
        "last_failure_date": "2026-10-30",
        "recent_failures": [],
    }


# --- draft branch -------------------------------------------------------------

REPORT = {"status": "completed", "slug": "sony-shows-prism-collection", "category": "hardware"}


def add_article(repo, slug="sony-shows-prism-collection", cover=True):
    (repo / "src" / "content" / "articles" / f"{slug}.md").write_text(
        '---\ntitle: "Sony shows the Prism Collection"\n---\n\nBody.\n', encoding="utf-8"
    )
    if cover:
        (repo / "public" / "covers").mkdir(parents=True, exist_ok=True)
        (repo / "public" / "covers" / f"{slug}.webp").write_bytes(b"RIFFfake")


def test_draft_branch_holds_exactly_the_article_and_its_cover(repo):
    add_article(repo)
    info = ci.prepare_draft_branch(repo, REPORT, run_id="777")
    assert info["branch"] == "draft/sony-shows-prism-collection-777"
    assert info["title"] == "Sony shows the Prism Collection"
    remote = repo.parent / "remote.git"
    diff = git(repo, "--git-dir", str(remote), "diff", "--name-only", "main", info["branch"]).split()
    assert sorted(diff) == [
        "public/covers/sony-shows-prism-collection.webp",
        "src/content/articles/sony-shows-prism-collection.md",
    ]
    # One commit on top of main.
    assert len(branch_commits(repo, info["branch"])) == len(branch_commits(repo, "main")) + 1


def test_draft_branch_leaves_the_working_tree_and_index_alone(repo):
    add_article(repo)
    head = git(repo, "rev-parse", "HEAD")
    status = git(repo, "status", "--porcelain")
    ci.prepare_draft_branch(repo, REPORT, run_id="1")
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == status
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_draft_branch_never_carries_changed_state_files(repo):
    # The run rewrites tracked state files; they must not ride along in the pull request.
    add_article(repo)
    write_json(repo / "data" / "pool.json", {"items": ["changed by the run"]})
    info = ci.prepare_draft_branch(repo, REPORT, run_id="2")
    remote = repo.parent / "remote.git"
    diff = git(repo, "--git-dir", str(remote), "diff", "--name-only", "main", info["branch"]).split()
    assert "data/pool.json" not in diff


def test_draft_branch_without_a_cover_still_works(repo):
    add_article(repo, cover=False)
    info = ci.prepare_draft_branch(repo, REPORT, run_id="3")
    assert info["files"] == ["src/content/articles/sony-shows-prism-collection.md"]


@pytest.mark.parametrize("status", ["failed", "nothing", "paused", "skipped"])
def test_only_a_completed_run_gets_a_draft_branch(repo, status):
    add_article(repo)
    assert ci.prepare_draft_branch(repo, {**REPORT, "status": status}, run_id="4") is None
    assert not ci.remote_has_branch(repo, "draft/sony-shows-prism-collection-4")


def test_a_completed_run_whose_article_is_missing_gets_no_branch(repo):
    assert ci.prepare_draft_branch(repo, REPORT, run_id="5") is None


@pytest.mark.parametrize("slug", ["../evil", "Upper-Case", "has space", "a/b", "", "-lead", "x" * 200])
def test_an_unsafe_slug_is_refused(repo, slug):
    with pytest.raises(ci.CiError):
        ci.prepare_draft_branch(repo, {**REPORT, "slug": slug}, run_id="6")


@pytest.mark.parametrize("run_id", ["", "a b", "x/y", "1;rm", "9" * 40])
def test_an_unsafe_run_id_is_refused(repo, run_id):
    add_article(repo)
    with pytest.raises(ci.CiError):
        ci.prepare_draft_branch(repo, REPORT, run_id=run_id)


def test_the_pull_request_body_has_the_review_checklist():
    body = ci.pr_body({"title": "T"}, report={"verification": "single-origin"}, run_url="https://github.com/o/r/actions/runs/1")
    assert "Every claim matches the cited sources" in body
    assert "Merging publishes the article" in body
    assert "Relayed report" in body
    assert "https://github.com/o/r/actions/runs/1" in body


def test_the_pull_request_body_drops_a_run_url_that_is_not_github():
    assert "evil" not in ci.pr_body({"title": "T"}, run_url="https://evil.example/x")


def test_titles_lose_mentions_and_markup_characters():
    cleaned = ci._plain("Hi @someone <b>`x`</b> [y]\nsecond line")
    assert not set(cleaned) & set("@<>`[]\n")
    assert "someone" in cleaned and "second line" in cleaned


# --- command line -------------------------------------------------------------


def test_set_output_keeps_every_value_on_one_line(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    ci._set_output("title", "line one\nbranch=evil")
    assert out.read_text().splitlines() == ["title=line one branch=evil"]


def test_cli_draft_branch_writes_outputs_and_a_body_file(repo, tmp_path, monkeypatch):
    add_article(repo)
    write_json(repo / "data" / "reports" / "last-run.json", REPORT)
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    body = tmp_path / "body.md"
    code = ci.main(["draft-branch", "--repo", str(repo), "--run-id", "9", "--pr-body-file", str(body)])
    assert code == 0
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert outputs["has_draft"] == "true"
    assert outputs["branch"] == "draft/sony-shows-prism-collection-9"
    assert body.is_file()


def test_cli_draft_branch_with_nothing_to_draft_says_so(repo, tmp_path, monkeypatch):
    write_json(repo / "data" / "reports" / "last-run.json", {"status": "nothing"})
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert ci.main(["draft-branch", "--repo", str(repo), "--run-id", "9"]) == 0
    assert out.read_text().strip() == "has_draft=false"


def test_cli_notify_sends_the_message_and_never_fails_the_job(repo, monkeypatch):
    add_article(repo)
    sent = []
    monkeypatch.setattr(notify, "send_telegram", lambda text, **kw: sent.append(text) or False)
    write_json(
        repo / "data" / "reports" / "last-run.json",
        {**REPORT, "article_path": str(repo / "src" / "content" / "articles" / "sony-shows-prism-collection.md")},
    )
    code = ci.main(["notify", "--repo", str(repo), "--pr-url", "https://github.com/o/r/pull/3"])
    assert code == 0  # even though sending failed
    assert "Sony shows the Prism Collection" in sent[0]
    assert "https://github.com/o/r/pull/3" in sent[0]


def test_cli_notify_stays_quiet_for_a_quiet_run(repo, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_telegram", lambda text, **kw: sent.append(text) or True)
    write_json(repo / "data" / "reports" / "last-run.json", {"status": "nothing"})
    assert ci.main(["notify", "--repo", str(repo)]) == 0
    assert sent == []


def test_cli_notify_without_a_report_does_not_crash(repo):
    assert ci.main(["notify", "--repo", str(repo)]) == 0


def test_cli_telegram_test_fails_when_telegram_is_not_configured(repo, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert ci.main(["telegram-test", "--repo", str(repo)]) == 1


def test_cli_restore_reports_a_failure_as_exit_1(repo):
    git(repo, "remote", "rename", "origin", "elsewhere")
    assert ci.main(["restore", "--repo", str(repo)]) == 1


@pytest.mark.parametrize(
    "content, expected",
    [('{"open": true}', "true"), ('{"open": false}', "false"), ("{ broken", "false"), (None, "false")],
)
def test_cli_breaker_status_reports_whether_the_breaker_is_open(repo, tmp_path, monkeypatch, content, expected):
    path = repo / "data" / "breaker.json"
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert ci.main(["breaker-status", "--repo", str(repo)]) == 0
    assert out.read_text().strip() == f"breaker_open={expected}"
