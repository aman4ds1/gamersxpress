"""Tests for real article images: processing, front matter, the publish check and the add command."""

import io
import random

import pytest
import yaml
from PIL import Image

import images

SETTINGS = {**images.DEFAULTS}


def picture(width=1200, height=700, *, mode="RGB", fmt="JPEG", noise=False):
    image = Image.new(mode, (width, height), (40, 90, 160) if mode == "RGB" else 128)
    if noise:
        rng = random.Random(1)
        pixels = image.load()
        for x in range(width):
            for y in range(height):
                pixels[x, y] = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


ARTICLE = "---\ntitle: A title\ncategory: pc\nsources:\n  - name: A\n    url: https://a.example/x\n---\n\n## What happened\n\nBody text.\n"


def front_of(text):
    return yaml.safe_load(text.split("---\n")[1])


def hero_for(tmp_path, **overrides):
    info = images.process_image(picture(), tmp_path / "images" / "story.webp")
    hero = {"src": "/images/story.webp", "alt": "A person holding a console", "credit": "Nintendo",
            "license": "Press kit", "width": info["width"], "height": info["height"]}
    hero.update(overrides)
    return hero


# --- processing ----------------------------------------------------------------------------


def test_a_picture_is_converted_to_webp_at_its_own_size_when_it_is_small_enough(tmp_path):
    info = images.process_image(picture(1200, 700), tmp_path / "a.webp")
    assert (info["width"], info["height"]) == (1200, 700)
    with Image.open(tmp_path / "a.webp") as saved:
        assert saved.format == "WEBP" and saved.size == (1200, 700)
    assert info["bytes"] == (tmp_path / "a.webp").stat().st_size


def test_a_wide_picture_is_scaled_down_keeping_its_proportions(tmp_path):
    info = images.process_image(picture(3200, 1800), tmp_path / "a.webp")
    assert info["width"] == 1600 and info["height"] == 900


def test_a_picture_too_narrow_is_refused(tmp_path):
    with pytest.raises(images.ImageError, match="799px wide"):
        images.process_image(picture(799, 500), tmp_path / "a.webp")
    assert not (tmp_path / "a.webp").exists()


def test_something_that_is_not_a_picture_is_refused(tmp_path):
    with pytest.raises(images.ImageError, match="not a readable image"):
        images.process_image(b"this is not an image", tmp_path / "a.webp")


def test_a_picture_that_cannot_be_made_small_enough_is_refused(tmp_path):
    with pytest.raises(images.ImageError, match="still"):
        images.process_image(picture(900, 600, noise=True), tmp_path / "a.webp", settings={**SETTINGS, "max_bytes": 20_000})


def test_the_quality_drops_until_the_file_fits(tmp_path):
    data = picture(900, 600, noise=True)
    loose = images.process_image(data, tmp_path / "loose.webp", settings={**SETTINGS, "max_bytes": 10_000_000})
    tight = images.process_image(data, tmp_path / "tight.webp", settings={**SETTINGS, "max_bytes": loose["bytes"] - 1})
    assert tight["bytes"] < loose["bytes"] <= 10_000_000


def test_camera_metadata_is_not_kept(tmp_path):
    image = Image.new("RGB", (1200, 700), (10, 10, 10))
    exif = Image.Exif()
    exif[0x010F] = "Secret Camera Co"
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    images.process_image(buffer.getvalue(), tmp_path / "a.webp")
    assert b"Secret Camera Co" not in (tmp_path / "a.webp").read_bytes()


def test_a_picture_with_transparency_is_flattened(tmp_path):
    images.process_image(picture(1200, 700, mode="RGBA", fmt="PNG"), tmp_path / "a.webp")
    with Image.open(tmp_path / "a.webp") as saved:
        assert saved.mode == "RGB"


def test_the_picture_can_be_given_as_a_file_path(tmp_path):
    (tmp_path / "photo.jpg").write_bytes(picture())
    assert images.process_image(tmp_path / "photo.jpg", tmp_path / "out" / "a.webp")["width"] == 1200


# --- front matter ----------------------------------------------------------------------------

HERO = {"src": "/images/story.webp", "alt": "A person: holding a console", "credit": "Nintendo", "license": "Press kit",
        "sourceUrl": "https://example.com/press", "width": 1200, "height": 700}


def test_set_hero_adds_a_block_that_reads_back_exactly_and_leaves_the_rest_alone():
    text = images.set_hero(ARTICLE, HERO)
    assert front_of(text)["hero"] == HERO and front_of(text)["title"] == "A title"
    assert text.endswith("\n\n## What happened\n\nBody text.\n")
    assert front_of(text)["sources"] == [{"name": "A", "url": "https://a.example/x"}]


def test_set_hero_replaces_an_existing_block_instead_of_adding_a_second():
    once = images.set_hero(ARTICLE, HERO)
    twice = images.set_hero(once, {**HERO, "credit": "Someone Else"})
    assert twice.count("hero:") == 1 and front_of(twice)["hero"]["credit"] == "Someone Else"
    assert front_of(twice)["sources"] == front_of(ARTICLE)["sources"]


def test_set_hero_keeps_the_fields_that_follow_a_replaced_block():
    text = ARTICLE.replace("---\n\n##", "hero:\n  src: /images/old.webp\n  alt: old old old old\nimage: /covers/x.webp\n---\n\n##")
    fixed = images.set_hero(text, HERO)
    assert front_of(fixed)["image"] == "/covers/x.webp" and front_of(fixed)["hero"] == HERO


def test_an_article_without_front_matter_is_refused():
    with pytest.raises(images.ImageError, match="front matter"):
        images.set_hero("## Just a body\n", HERO)


# --- the publish check ----------------------------------------------------------------------


def test_an_article_without_a_hero_has_nothing_to_check(tmp_path):
    assert images.hero_failures(None, tmp_path) == []


def test_a_complete_hero_passes(tmp_path):
    assert images.hero_failures(hero_for(tmp_path), tmp_path) == []


@pytest.mark.parametrize("field", ["src", "alt", "credit", "license"])
def test_each_required_hero_field_is_needed(tmp_path, field):
    failures = images.hero_failures(hero_for(tmp_path, **{field: ""}), tmp_path)
    assert f"hero.{field} is missing" in failures


@pytest.mark.parametrize("src", ["https://other.example/x.webp", "/covers/x.webp", "/images/Story.webp", "/images/story.jpg", "images/story.webp"])
def test_the_hero_must_be_one_of_our_own_webp_files(tmp_path, src):
    assert any("must be a local /images/" in f for f in images.hero_failures(hero_for(tmp_path, src=src), tmp_path))


def test_a_short_description_is_refused(tmp_path):
    assert any("too short" in f for f in images.hero_failures(hero_for(tmp_path, alt="a photo"), tmp_path))


@pytest.mark.parametrize("license_name", ["Getty Images", "All rights reserved", "Fair use", "cc by-nc 4.0"])
def test_a_license_that_is_not_on_the_list_is_refused(tmp_path, license_name):
    assert any("not an allowed license" in f for f in images.hero_failures(hero_for(tmp_path, license=license_name), tmp_path))


def test_license_names_match_regardless_of_case(tmp_path):
    assert images.hero_failures(hero_for(tmp_path, license="press KIT"), tmp_path) == []


def test_a_source_link_must_be_https(tmp_path):
    assert any("https" in f for f in images.hero_failures(hero_for(tmp_path, sourceUrl="http://x.example"), tmp_path))
    assert images.hero_failures(hero_for(tmp_path, sourceUrl="https://x.example"), tmp_path) == []


def test_a_missing_file_is_reported(tmp_path):
    assert "hero file /images/ghost.webp does not exist" in images.hero_failures(hero_for(tmp_path, src="/images/ghost.webp"), tmp_path)


def test_a_file_too_large_or_too_narrow_or_unreadable_is_reported(tmp_path):
    hero = hero_for(tmp_path)
    assert any("bytes (max" in f for f in images.hero_failures(hero, tmp_path, {**SETTINGS, "max_bytes": 100}))
    assert any("wide (min" in f for f in images.hero_failures(hero, tmp_path, {**SETTINGS, "min_width": 5000}))
    (tmp_path / "images" / "broken.webp").write_bytes(b"not a picture")
    assert any("not a readable image" in f for f in images.hero_failures(hero_for(tmp_path, src="/images/broken.webp"), tmp_path))


def test_the_declared_size_must_match_the_file(tmp_path):
    assert any("must match the file" in f for f in images.hero_failures(hero_for(tmp_path, width=999), tmp_path))


def test_a_hero_that_is_not_an_object_is_refused(tmp_path):
    assert images.hero_failures("a photo", tmp_path) == ["hero must be an object with src, alt, credit and license"]


# --- config -----------------------------------------------------------------------------------


def test_the_shipped_config_lists_the_allowed_licenses_and_limits():
    config = images.load_image_config()
    assert "Press kit" in config["allowed_licenses"] and "Steam store" in config["allowed_licenses"]
    assert config["min_width"] == 800 and config["max_bytes"] == 400_000


def test_a_config_file_can_override_the_limits_and_a_broken_one_falls_back(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("images:\n  min_width: 1000\n  allowed_licenses: [Own work]\n  quality: true\n", encoding="utf-8")
    config = images.load_image_config(path)
    assert config["min_width"] == 1000 and config["allowed_licenses"] == ["Own work"] and config["quality"] == 82
    path.write_text("images: [", encoding="utf-8")
    assert images.load_image_config(path) == images.DEFAULTS
    assert images.load_image_config(tmp_path / "missing.yaml") == images.DEFAULTS


# --- adding an image by hand -----------------------------------------------------------------


def make_article(tmp_path, name="story"):
    path = tmp_path / "articles" / f"{name}.md"
    path.parent.mkdir(parents=True)
    path.write_text(ARTICLE, encoding="utf-8")
    (tmp_path / "photo.jpg").write_bytes(picture(1800, 1000))
    return path


def test_add_hero_writes_the_image_and_updates_the_article(tmp_path):
    path = make_article(tmp_path)
    hero = images.add_hero(path, tmp_path / "photo.jpg", alt="A person holding a console", credit="Nintendo",
                           license="Press kit", source_url="https://example.com/press", public_dir=tmp_path / "public")
    assert hero["src"] == "/images/story.webp" and (hero["width"], hero["height"]) == (1600, 889)
    assert hero["sourceUrl"] == "https://example.com/press"
    assert (tmp_path / "public" / "images" / "story.webp").is_file()
    assert front_of(path.read_text(encoding="utf-8"))["hero"] == hero
    assert images.hero_failures(hero, tmp_path / "public") == []


def test_add_hero_refuses_a_license_that_is_not_allowed_and_changes_nothing(tmp_path):
    path = make_article(tmp_path)
    with pytest.raises(images.ImageError, match="not an allowed license"):
        images.add_hero(path, tmp_path / "photo.jpg", alt="A person holding a console", credit="Getty",
                        license="Getty Images", public_dir=tmp_path / "public")
    assert path.read_text(encoding="utf-8") == ARTICLE and not (tmp_path / "public").exists()


def test_add_hero_removes_the_picture_again_if_the_result_would_fail_the_check(tmp_path):
    path = make_article(tmp_path)
    with pytest.raises(images.ImageError, match="too short"):
        images.add_hero(path, tmp_path / "photo.jpg", alt="photo", credit="Nintendo", license="Press kit",
                        public_dir=tmp_path / "public")
    assert not (tmp_path / "public" / "images" / "story.webp").exists() and path.read_text(encoding="utf-8") == ARTICLE


def test_add_hero_refuses_an_unusable_article_name(tmp_path):
    path = tmp_path / "Bad Name.md"
    path.write_text(ARTICLE, encoding="utf-8")
    (tmp_path / "photo.jpg").write_bytes(picture())
    with pytest.raises(images.ImageError, match="unusable article name"):
        images.add_hero(path, tmp_path / "photo.jpg", alt="A person holding a console", credit="x", license="Press kit", public_dir=tmp_path)


def test_the_command_adds_an_image_and_reports_it(tmp_path, capsys):
    path = make_article(tmp_path)
    code = images.main(["add", "--article", str(path), "--file", str(tmp_path / "photo.jpg"), "--alt", "A person holding a console",
                        "--credit", "Nintendo", "--license", "Press kit", "--public-dir", str(tmp_path / "public")])
    assert code == 0 and "added /images/story.webp (1600x889)" in capsys.readouterr().out
    assert front_of(path.read_text(encoding="utf-8"))["hero"]["credit"] == "Nintendo"


def test_the_command_reports_a_problem_with_a_message_and_a_failing_exit_code(tmp_path, capsys):
    path = make_article(tmp_path)
    code = images.main(["add", "--article", str(path), "--file", str(tmp_path / "missing.jpg"), "--alt", "A person holding a console",
                        "--credit", "x", "--license", "Press kit", "--public-dir", str(tmp_path / "public")])
    assert code == 1 and "error:" in capsys.readouterr().err
    code = images.main(["add", "--article", str(path), "--file", str(tmp_path / "photo.jpg"), "--alt", "A person holding a console",
                        "--credit", "x", "--license", "Stock photo", "--public-dir", str(tmp_path / "public")])
    assert code == 1 and "not an allowed license" in capsys.readouterr().err


def test_a_photo_the_camera_stored_sideways_is_turned_upright(tmp_path):
    image = Image.new("RGB", (1200, 800), (10, 120, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90 degrees clockwise to display"
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    info = images.process_image(buffer.getvalue(), tmp_path / "a.webp")
    assert (info["width"], info["height"]) == (800, 1200)


def test_add_hero_without_a_source_link_leaves_that_field_out(tmp_path):
    hero = images.add_hero(make_article(tmp_path), tmp_path / "photo.jpg", alt="A person holding a console", credit="Me",
                           license="Own work", public_dir=tmp_path / "public")
    assert "sourceUrl" not in hero
