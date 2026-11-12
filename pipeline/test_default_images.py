"""The default picture for each category: the files, their look, and the order of the picture steps."""

import json
import re
from pathlib import Path

import pytest
import yaml
from PIL import Image

import default_images
import images
import run as run_module

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_IMAGES = ROOT / "public" / "images"


def site_categories() -> dict[str, str]:
    """category -> colour, read from the site's own list (src/data/categories.ts)."""
    text = (ROOT / "src" / "data" / "categories.ts").read_text(encoding="utf-8")
    names = re.findall(r"^\s*'?([a-z-]+)'?: '#[0-9A-Fa-f]{6}'", text, re.M)
    colours = dict(re.findall(r"^\s*'?([a-z-]+)'?: '(#[0-9A-Fa-f]{6})',", text.split("CATEGORY_COLORS")[1], re.M))
    return {name: colours[name] for name in names if name in colours}


def test_every_site_category_has_a_look_and_every_look_is_a_site_category():
    assert set(default_images.LOOK) == set(site_categories()) and len(default_images.LOOK) == 9


def test_the_look_uses_the_sites_category_colours():
    for name, colour in site_categories().items():
        assert default_images.LOOK[name][1].upper() == colour.upper(), name


@pytest.mark.parametrize("category", sorted(default_images.LOOK))
def test_each_picture_file_is_shipped_and_the_right_shape_and_weight(category):
    path = PUBLIC_IMAGES / default_images.default_filename(category)
    assert path.is_file(), f"run: python -m pipeline.default_images  ({path.name} is missing)"
    with Image.open(path) as picture:
        assert picture.size == (default_images.WIDTH, default_images.HEIGHT)
        assert picture.format == "WEBP"
    assert path.stat().st_size <= 400_000  # images.max_bytes in config.yaml
    assert path.stat().st_size <= 60_000, "a default picture should stay small"


def test_the_pictures_are_the_same_every_time_they_are_made():
    first = default_images.render("pc").tobytes()
    assert first == default_images.render("pc").tobytes()


def test_the_default_picture_is_our_own_and_the_site_can_use_it_as_a_hero():
    result = images.default_hero(category="pc", slug="some-story", public_dir=ROOT / "public")
    hero = result["hero"]
    assert hero["src"] == "/images/some-story.webp" and hero["license"] == "Own work" and hero["credit"] == "GamersXpress"
    assert (hero["width"], hero["height"]) == (default_images.WIDTH, default_images.HEIGHT)
    assert len(hero["alt"]) >= 10
    assert result["path"] == ROOT / "public" / "images" / "default-pc.webp"


def test_a_story_without_a_category_gets_the_gaming_news_picture():
    result = images.default_hero(category=None, slug="s", public_dir=ROOT / "public")
    assert result["path"].name == "default-gaming-news.webp"


def test_an_unknown_category_falls_back_to_gaming_news():
    result = images.default_hero(category="made-up", slug="s", public_dir=ROOT / "public")
    assert result["path"].name == "default-gaming-news.webp"


def test_no_picture_file_at_all_means_no_picture(tmp_path):
    assert images.default_hero(category="pc", slug="s", public_dir=tmp_path) is None


def test_a_damaged_picture_file_means_no_picture(tmp_path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "default-pc.webp").write_bytes(b"not a picture")
    assert images.default_hero(category="pc", slug="s", public_dir=tmp_path) is None


# --- the order of the picture steps ----------------------------------------------------------------------------------

def stub(monkeypatch, steam_result, commons_result):
    calls = []

    def steam_find(**kwargs):
        calls.append("steam")
        return steam_result

    def commons_find(**kwargs):
        calls.append("commons")
        return commons_result

    monkeypatch.setattr(run_module.steam_stage, "find_hero", steam_find)
    monkeypatch.setattr(run_module.commons_stage, "find_hero", commons_find)
    return calls


def test_steam_then_commons_then_the_category_picture(monkeypatch, tmp_path):
    (tmp_path / "images").mkdir()
    Image.new("RGB", (1200, 675)).save(tmp_path / "images" / "default-xbox.webp", "WEBP")
    calls = stub(monkeypatch, None, None)
    result = run_module.find_any_hero(title="t", entities=[], slug="s", public_dir=tmp_path, category="xbox")
    assert calls == ["steam", "commons"]
    assert result["hero"]["license"] == "Own work" and result["path"].name == "default-xbox.webp"


def test_a_real_photo_is_never_replaced_by_the_category_picture(monkeypatch, tmp_path):
    (tmp_path / "images").mkdir()
    Image.new("RGB", (1200, 675)).save(tmp_path / "images" / "default-xbox.webp", "WEBP")
    calls = stub(monkeypatch, None, {"hero": {"license": "CC0"}})
    result = run_module.find_any_hero(title="t", entities=[], slug="s", public_dir=tmp_path, category="xbox")
    assert calls == ["steam", "commons"] and result["hero"]["license"] == "CC0"


def test_steam_art_is_still_first(monkeypatch, tmp_path):
    calls = stub(monkeypatch, {"hero": {"license": "Steam store"}}, None)
    result = run_module.find_any_hero(title="t", entities=[], slug="s", public_dir=tmp_path, category="pc")
    assert calls == ["steam"] and result["hero"]["license"] == "Steam store"


def test_the_category_picture_is_not_used_in_a_dry_run():
    assert run_module.default_image_fn(True) is run_module._no_image


@pytest.mark.parametrize("category, label", sorted((name, look[0]) for name, look in default_images.LOOK.items()))
def test_the_alt_text_uses_the_category_label_and_never_doubles_a_word(category, label):
    alt = images.default_hero(category=category, slug="s", public_dir=ROOT / "public")["hero"]["alt"]
    assert alt == f"GamersXpress illustration for {label} coverage"
    words = alt.lower().split()
    assert all(a != b for a, b in zip(words, words[1:])), f"a word is repeated in {alt!r}"


def test_the_alt_text_for_an_unknown_category_is_still_readable():
    alt = images.default_hero(category="made-up", slug="s", public_dir=ROOT / "public")["hero"]["alt"]
    assert alt == "GamersXpress illustration for Made Up coverage"
