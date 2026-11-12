"""python -m pipeline.backfill: pictures for articles that were published without one."""

from pathlib import Path

import pytest
import yaml
from PIL import Image

import backfill
import gates
import images
import run as run_module

ROOT = Path(__file__).resolve().parent.parent
REAL_PUBLIC = ROOT / "public"


def article(title="Path of Exile 2 launches December 11", category="pc", hero=None, entities=("Path of Exile 2",)):
    front = {
        "title": title,
        "description": "A description that is long enough to be a plausible one for this article here.",
        "pubDate": "2026-11-07",
        "category": category,
        "tags": ["a", "b", "c"],
        "entities": list(entities),
    }
    if hero:
        front["hero"] = hero
    return "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n## What happened\n\nBody text.\n"


def hero_for(slug, *, license="Steam store", credit="Grinding Gear Games"):
    return {"src": f"/images/{slug}.webp", "alt": "Key art for a game", "credit": credit, "license": license, "width": 1200, "height": 675}


@pytest.fixture
def site(tmp_path):
    """A small site: an articles folder and a public folder holding the category pictures."""
    articles, public = tmp_path / "articles", tmp_path / "public"
    articles.mkdir()
    (public / "images").mkdir(parents=True)
    for category in ("pc", "hardware", "gaming-news"):
        Image.new("RGB", (1200, 675), (20, 30, 40)).save(public / "images" / f"default-{category}.webp", "WEBP")
    return articles, public


def write(articles, slug, text):
    path = articles / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    return path


def steam_finder(calls=None):
    """A finder that behaves like a Steam hit: it writes a picture into the folder it is given."""
    def find(*, title, entities, slug, public_dir, category=None):
        if calls is not None:
            calls.append({"title": title, "entities": entities, "slug": slug, "public_dir": Path(public_dir), "category": category})
        destination = Path(public_dir) / "images" / f"{slug}.webp"
        Image.new("RGB", (1200, 675), (200, 10, 10)).save(destination, "WEBP")
        return {"hero": hero_for(slug), "path": destination, "name": "game"}
    return find


def quiet():
    lines = []
    return lines, lines.append


# --- a preview changes nothing ----------------------------------------------------------------------------------------

def test_a_preview_reports_the_plan_and_writes_nothing(site):
    articles, public = site
    path = write(articles, "poe2", article())
    before = path.read_text(encoding="utf-8")
    lines, say = quiet()
    results = backfill.backfill(articles_dir=articles, public_dir=public, finder=steam_finder(), say=say)
    assert [(r["slug"], r["action"], r["kind"]) for r in results] == [("poe2", "add", "Steam art")]
    assert path.read_text(encoding="utf-8") == before
    assert not (public / "images" / "poe2.webp").exists()
    assert "would add Steam art" in lines[0]


def test_the_lookups_use_a_scratch_folder_with_the_category_pictures_never_the_site(site):
    articles, public = site
    write(articles, "poe2", article())
    calls = []
    backfill.backfill(articles_dir=articles, public_dir=public, finder=steam_finder(calls), say=lambda _: None)
    scratch = calls[0]["public_dir"]
    assert scratch != public and not str(scratch).startswith(str(public))
    assert not scratch.exists(), "the scratch folder is removed afterwards"
    assert list((public / "images").glob("poe2*")) == []


def test_the_finder_gets_the_articles_headline_entities_and_category(site):
    articles, public = site
    write(articles, "poe2", article(title="Path of Exile 2 launches", category="pc", entities=("Path of Exile 2", "Grinding Gear Games")))
    calls = []
    backfill.backfill(articles_dir=articles, public_dir=public, finder=steam_finder(calls), say=lambda _: None)
    assert calls[0]["title"] == "Path of Exile 2 launches"
    assert calls[0]["entities"] == ["Path of Exile 2", "Grinding Gear Games"]
    assert calls[0]["category"] == "pc" and calls[0]["slug"] == "poe2"


# --- applying -------------------------------------------------------------------------------------------------------

def test_apply_copies_the_picture_and_sets_the_hero_and_nothing_else_changes(site):
    articles, public = site
    path = write(articles, "poe2", article())
    before_front, before_body = gates.parse_front_matter(path.read_text(encoding="utf-8"))
    backfill.backfill(articles_dir=articles, public_dir=public, apply=True, finder=steam_finder(), say=lambda _: None)
    front, body = gates.parse_front_matter(path.read_text(encoding="utf-8"))
    assert front["hero"] == hero_for("poe2") and body == before_body
    assert {k: v for k, v in front.items() if k != "hero"} == before_front
    picture = public / "images" / "poe2.webp"
    assert picture.is_file() and Image.open(picture).size == (1200, 675)
    assert images.hero_failures(front["hero"], public) == []


def test_apply_twice_changes_nothing_the_second_time(site):
    articles, public = site
    path = write(articles, "poe2", article())
    backfill.backfill(articles_dir=articles, public_dir=public, apply=True, finder=steam_finder(), say=lambda _: None)
    once = path.read_text(encoding="utf-8")
    picture = (public / "images" / "poe2.webp").read_bytes()
    calls = []
    results = backfill.backfill(articles_dir=articles, public_dir=public, apply=True, finder=steam_finder(calls), say=lambda _: None)
    assert [r["action"] for r in results] == ["skip"] and calls == []
    assert path.read_text(encoding="utf-8") == once and (public / "images" / "poe2.webp").read_bytes() == picture


def test_an_article_that_already_has_a_picture_is_never_looked_up_or_touched(site):
    articles, public = site
    path = write(articles, "has-one", article(hero=hero_for("has-one", license="CC BY 4.0", credit="Jane Doe")))
    before = path.read_text(encoding="utf-8")
    calls = []
    results = backfill.backfill(articles_dir=articles, public_dir=public, apply=True, upgrade=True, finder=steam_finder(calls), say=lambda _: None)
    assert [r["action"] for r in results] == ["skip"] and calls == []
    assert path.read_text(encoding="utf-8") == before


def test_a_story_with_no_picture_found_is_left_alone(site):
    articles, public = site
    path = write(articles, "nothing", article())
    before = path.read_text(encoding="utf-8")
    lines, say = quiet()
    results = backfill.backfill(articles_dir=articles, public_dir=public, apply=True, finder=lambda **_: None, say=say)
    assert results[0]["action"] == "none" and path.read_text(encoding="utf-8") == before
    assert "no picture found" in lines[0]


def test_a_picture_that_fails_the_publish_check_is_removed_and_the_article_left_alone(site):
    articles, public = site
    path = write(articles, "bad", article())
    before = path.read_text(encoding="utf-8")

    def finder(*, slug, public_dir, **_):
        destination = Path(public_dir) / "images" / f"{slug}.webp"
        Image.new("RGB", (1200, 675)).save(destination, "WEBP")
        return {"hero": hero_for(slug, license="All rights reserved"), "path": destination}

    lines, say = quiet()
    results = backfill.backfill(articles_dir=articles, public_dir=public, apply=True, finder=finder, say=say)
    assert results[0]["action"] == "rejected" and "not an allowed license" in results[0]["problem"]
    assert path.read_text(encoding="utf-8") == before
    assert not (public / "images" / "bad.webp").exists()
    assert "not used" in lines[0]


def test_a_failed_apply_puts_back_a_picture_that_was_already_there(site):
    articles, public = site
    write(articles, "bad", article())
    existing = public / "images" / "bad.webp"
    Image.new("RGB", (800, 450), (1, 2, 3)).save(existing, "WEBP")
    kept = existing.read_bytes()

    def finder(*, slug, public_dir, **_):
        destination = Path(public_dir) / "images" / f"{slug}.webp"
        Image.new("RGB", (1200, 675)).save(destination, "WEBP")
        return {"hero": hero_for(slug, license="All rights reserved"), "path": destination}

    backfill.backfill(articles_dir=articles, public_dir=public, apply=True, finder=finder, say=lambda _: None)
    assert existing.read_bytes() == kept


# --- choosing which articles ---------------------------------------------------------------------------------------

def test_slug_limits_it_to_the_named_articles_and_an_unknown_name_is_an_error(site):
    articles, public = site
    write(articles, "one", article())
    write(articles, "two", article())
    results = backfill.backfill(articles_dir=articles, public_dir=public, slugs=["two"], finder=steam_finder(), say=lambda _: None)
    assert [r["slug"] for r in results] == ["two"]
    with pytest.raises(images.ImageError, match="no such article: nope"):
        backfill.backfill(articles_dir=articles, public_dir=public, slugs=["nope"], finder=steam_finder(), say=lambda _: None)


def test_upgrade_replaces_a_category_picture_with_a_real_one_but_only_when_asked(site):
    articles, public = site
    own = hero_for("poe2", license="Own work", credit="GamersXpress")
    path = write(articles, "poe2", article(hero=own))
    before = path.read_text(encoding="utf-8")
    plain = backfill.backfill(articles_dir=articles, public_dir=public, apply=True, finder=steam_finder(), say=lambda _: None)
    assert plain[0]["action"] == "skip" and path.read_text(encoding="utf-8") == before
    lines, say = quiet()
    better = backfill.backfill(articles_dir=articles, public_dir=public, apply=True, upgrade=True, finder=steam_finder(), say=say)
    assert better[0]["action"] == "replace" and better[0]["kind"] == "Steam art"
    assert gates.parse_front_matter(path.read_text(encoding="utf-8"))[0]["hero"]["license"] == "Steam store"
    assert "replaced Steam art" in lines[0]


def test_upgrade_does_not_rewrite_a_category_picture_with_the_same_category_picture(site):
    articles, public = site
    own = hero_for("poe2", license="Own work", credit="GamersXpress")
    path = write(articles, "poe2", article(hero=own))
    before = path.read_text(encoding="utf-8")

    def finder(*, slug, public_dir, **_):
        return {"hero": dict(own), "path": Path(public_dir) / "images" / "default-pc.webp"}

    results = backfill.backfill(articles_dir=articles, public_dir=public, apply=True, upgrade=True, finder=finder, say=lambda _: None)
    assert results[0]["action"] == "skip" and path.read_text(encoding="utf-8") == before


# --- the real chain, with the network off --------------------------------------------------------------------------

def test_with_no_steam_or_commons_hit_the_real_chain_gives_the_articles_category_picture(site, monkeypatch):
    articles, public = site
    monkeypatch.setattr(run_module.steam_stage, "find_hero", lambda **_: None)
    monkeypatch.setattr(run_module.commons_stage, "find_hero", lambda **_: None)
    write(articles, "pc-story", article(category="pc"))
    write(articles, "hw-story", article(category="hardware"))
    write(articles, "odd-story", article(category="india"))          # no picture for this category in this small site
    results = {r["slug"]: r for r in backfill.backfill(articles_dir=articles, public_dir=public, apply=True, say=lambda _: None)}
    assert results["pc-story"]["kind"] == "category picture" and results["hw-story"]["kind"] == "category picture"
    assert "PC coverage" in results["pc-story"]["hero"]["alt"] and "Hardware coverage" in results["hw-story"]["hero"]["alt"]
    assert results["odd-story"]["kind"] == "category picture" and "India coverage" in results["odd-story"]["hero"]["alt"]
    for slug in ("pc-story", "hw-story", "odd-story"):
        front, _ = gates.parse_front_matter((articles / f"{slug}.md").read_text(encoding="utf-8"))
        assert images.hero_failures(front["hero"], public) == []


def test_kinds_are_named_by_where_the_picture_came_from():
    assert backfill.kind_of(None) == "none"
    assert backfill.kind_of({"license": "Steam store", "credit": "x"}) == "Steam art"
    assert backfill.kind_of({"license": "Own work", "credit": "GamersXpress"}) == "category picture"
    assert backfill.kind_of({"license": "CC BY-SA 4.0", "credit": "Jane"}) == "Commons photo"
    assert backfill.kind_of({"license": "Own work", "credit": "Jane Doe"}) == "Commons photo"


# --- the command line ----------------------------------------------------------------------------------------------

def run_cli(site, *extra, finder=None, monkeypatch=None):
    articles, public = site
    return backfill.main(["--articles-dir", str(articles), "--public-dir", str(public), *extra])


def test_the_command_previews_by_default_and_says_how_to_apply(site, monkeypatch, capsys):
    articles, public = site
    monkeypatch.setattr(backfill.run_module, "find_any_hero", steam_finder())
    path = write(articles, "poe2", article())
    before = path.read_text(encoding="utf-8")
    assert run_cli(site) == 0
    out = capsys.readouterr().out
    assert "would add Steam art" in out and "Nothing was changed" in out and "--apply" in out
    assert path.read_text(encoding="utf-8") == before


def test_the_command_applies_with_apply_and_tells_the_next_step(site, monkeypatch, capsys):
    articles, public = site
    monkeypatch.setattr(backfill.run_module, "find_any_hero", steam_finder())
    path = write(articles, "poe2", article())
    assert run_cli(site, "--apply") == 0
    out = capsys.readouterr().out
    assert "1 article(s) updated" in out and "npm run check" in out
    assert "hero:" in path.read_text(encoding="utf-8")


def test_the_command_reports_an_unknown_article_and_fails(site, monkeypatch, capsys):
    monkeypatch.setattr(backfill.run_module, "find_any_hero", steam_finder())
    assert run_cli(site, "--slug", "nope") == 1
    assert "no such article: nope" in capsys.readouterr().err


def test_the_command_says_when_there_is_nothing_to_do(site, capsys):
    articles, public = site
    write(articles, "done", article(hero=hero_for("done")))
    assert run_cli(site) == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_titles_in_any_script_print_without_crashing(site, monkeypatch, capsys):
    articles, public = site
    monkeypatch.setattr(backfill.run_module, "find_any_hero", steam_finder())
    write(articles, "japanese", article(title="ファイナルファンタジー の新作"))
    assert run_cli(site) == 0
    assert "would add" in capsys.readouterr().out


def test_the_real_site_defaults_point_at_the_projects_own_folders():
    assert backfill.ARTICLES_DIR == ROOT / "src" / "content" / "articles"
    assert backfill.PUBLIC_DIR == ROOT / "public"
    assert (REAL_PUBLIC / "images" / "default-gaming-news.webp").is_file()
