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
    image = Image.new("RGB", (800, 1200), (10, 120, 10))   # stored upright-in-the-wrong-direction
    exif = Image.Exif()
    exif[0x0112] = 6  # "rotate 90 degrees clockwise to display"
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    info = images.process_image(buffer.getvalue(), tmp_path / "a.webp")
    assert (info["width"], info["height"]) == (1200, 800)   # shown landscape, so it is accepted


def test_add_hero_without_a_source_link_leaves_that_field_out(tmp_path):
    hero = images.add_hero(make_article(tmp_path), tmp_path / "photo.jpg", alt="A person holding a console", credit="Me",
                           license="Own work", public_dir=tmp_path / "public")
    assert "sourceUrl" not in hero


# --- title and license link (Creative Commons credits) ------------------------------------------------


def test_a_license_link_must_point_at_creative_commons(tmp_path):
    ok = hero_for(tmp_path, licenseUrl="https://creativecommons.org/licenses/by-sa/4.0/")
    assert images.hero_failures(ok, tmp_path) == []
    for bad in ("http://creativecommons.org/licenses/by/4.0/", "https://evil.example/by/4.0/", "creativecommons.org/x"):
        assert any("licenseUrl" in f for f in images.hero_failures(hero_for(tmp_path, licenseUrl=bad), tmp_path)), bad


def test_a_work_title_must_be_short_text(tmp_path):
    assert images.hero_failures(hero_for(tmp_path, title="PlayStation 5 console"), tmp_path) == []
    for bad in ("", "   ", "x" * 201, 5):
        assert any("hero.title" in f for f in images.hero_failures(hero_for(tmp_path, title=bad), tmp_path)), repr(bad)


def test_add_hero_can_record_a_title_and_a_license_link(tmp_path):
    hero = images.add_hero(make_article(tmp_path), tmp_path / "photo.jpg", alt="A console on a table", credit="Jane Doe",
                           license="CC BY-SA 4.0", title="Console photo", license_url="https://creativecommons.org/licenses/by-sa/4.0/",
                           source_url="https://commons.wikimedia.org/wiki/File:Console.jpg", public_dir=tmp_path / "public")
    assert hero["title"] == "Console photo" and hero["licenseUrl"].endswith("/by-sa/4.0/")
    assert list(hero)[:6] == ["src", "alt", "credit", "license", "licenseUrl", "title"]


def test_the_command_accepts_the_title_and_license_link(tmp_path, capsys):
    path = make_article(tmp_path)
    code = images.main(["add", "--article", str(path), "--file", str(tmp_path / "photo.jpg"), "--alt", "A console on a table",
                        "--credit", "Jane Doe", "--license", "CC BY 4.0", "--title", "Console photo",
                        "--license-url", "https://creativecommons.org/licenses/by/4.0/", "--public-dir", str(tmp_path / "public")])
    assert code == 0 and front_of(path.read_text(encoding="utf-8"))["hero"]["title"] == "Console photo"


# --- a name that is the start of a longer model name ----------------------------------------------------------

TOKENS = ["steam", "deck", "2", "teased"]


def test_a_name_followed_by_a_number_or_a_model_word_continues_as_another_model():
    assert images.continues_as_another_model(TOKENS, ["steam", "deck"])
    assert images.continues_as_another_model(["alan", "wake", "ii", "out"], ["alan", "wake"])
    assert images.continues_as_another_model(["ps5", "pro", "price"], ["ps5"], images.HARDWARE_MODEL_WORDS)
    assert images.continues_as_another_model(["geforce", "rtx", "5090", "ti"], ["geforce", "rtx", "5090"], images.HARDWARE_MODEL_WORDS)


def test_a_name_followed_by_anything_else_or_nothing_does_not():
    assert not images.continues_as_another_model(["steam", "deck", "stock", "runs", "out"], ["steam", "deck"])
    assert not images.continues_as_another_model(["valve", "steam", "deck"], ["steam", "deck"])
    assert not images.continues_as_another_model(["alan", "wake", "pro", "tips"], ["alan", "wake"])      # games: numerals only
    assert not images.continues_as_another_model(TOKENS, ["steam", "deck", "2"])
    # A number elsewhere in the headline is not a model number for the name.
    assert not images.continues_as_another_model(["steam", "deck", "stock", "shipped", "2", "million"], ["steam", "deck"])
    assert not images.continues_as_another_model([], ["a"]) and not images.continues_as_another_model(["a"], [])
    assert not images.continues_as_another_model(["a"], ["a", "b"])


def test_one_later_occurrence_followed_by_a_model_word_is_enough_to_refuse():
    assert images.continues_as_another_model(["steam", "deck", "stock", "and", "steam", "deck", "2"], ["steam", "deck"])


# --- a console that cannot show a character -------------------------------------------------------------------------

import sys as _sys  # noqa: E402

HANGUL = "\ud55c\uad6d\uc5b4"   # "Korean": not in the Japanese Windows code page cp932


def cp932_console():
    buffer = io.BytesIO()
    return io.TextIOWrapper(buffer, encoding="cp932", errors="strict", write_through=True, newline=""), buffer


def test_the_test_console_really_cannot_show_the_character():
    console, _buffer = cp932_console()
    with pytest.raises(UnicodeEncodeError):
        print(HANGUL, file=console)


def test_safe_print_shows_a_question_mark_for_what_the_console_cannot_encode():
    console, buffer = cp932_console()
    images.safe_print(f"photo {HANGUL} of the console", file=console)
    assert buffer.getvalue().decode("cp932") == "photo ??? of the console\n"


def test_safe_print_leaves_a_message_the_console_can_show_exactly_as_it_is():
    console, buffer = cp932_console()
    images.safe_print("plain text \u65e5\u672c", file=console)             # Japanese is fine in cp932
    assert buffer.getvalue().decode("cp932") == "plain text \u65e5\u672c\n"
    out = io.StringIO()
    images.safe_print(f"unicode {HANGUL}", file=out)
    assert out.getvalue() == f"unicode {HANGUL}\n"


def test_safe_print_uses_ascii_when_the_stream_does_not_say_what_it_encodes():
    class Picky:
        encoding = None

        def __init__(self):
            self.text = ""

        def write(self, data):
            data.encode("ascii")
            self.text += data

        def flush(self):
            pass

    stream = Picky()
    images.safe_print(f"a {HANGUL} b", file=stream)
    assert stream.text == "a ??? b\n"


def test_safe_print_prints_to_stdout_by_default_and_accepts_any_object(capsys):
    images.safe_print()
    images.safe_print(42)
    assert capsys.readouterr().out == "\n42\n"


def test_a_failing_trace_callback_never_raises_and_is_logged_quietly():
    seen, logged = [], []

    class Log:
        def debug(self, message, **kwargs):
            logged.append((message, kwargs.get("exc_info")))

    def broken(message):
        seen.append(message)
        raise UnicodeEncodeError("cp932", HANGUL, 0, 1, "illegal multibyte sequence")

    say = images.guarded_trace(broken, Log())
    say("a step")
    say("another step")
    assert seen == ["a step", "another step"] and len(logged) == 2 and logged[0][1] is True
    images.guarded_trace(broken)("no logger given")        # also fine without a logger


def test_a_working_trace_callback_receives_every_message_unchanged():
    lines = []
    say = images.guarded_trace(lines.append)
    say("one")
    say("two")
    assert lines == ["one", "two"]


def test_the_add_command_reports_an_error_naming_a_korean_file_on_a_legacy_console(tmp_path, monkeypatch):
    console, buffer = cp932_console()
    monkeypatch.setattr(_sys, "stderr", console)
    article = make_article(tmp_path)
    code = images.main(["add", "--article", str(article), "--file", str(tmp_path / f"{HANGUL}.jpg"), "--alt", "A person holding a console",
                        "--credit", "x", "--license", "Press kit", "--public-dir", str(tmp_path / "public")])
    assert code == 1 and "error:" in buffer.getvalue().decode("cp932") and "?" in buffer.getvalue().decode("cp932")


def test_safe_print_writes_to_a_stream_even_when_that_stream_object_is_falsy():
    class Falsy(io.StringIO):
        def __bool__(self):
            return False

    stream = Falsy()
    images.safe_print("hello", file=stream)
    assert stream.getvalue() == "hello\n"


# --- the shape of a hero ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("width,height,fragment", [
    (1200, 1000, None), (1600, 900, None), (1920, 620, None), (3500, 1000, None), (1000, 1000, None),
    (1536, 2160, None), (800, 1600, None),                                       # an upright console: tall, but fine
    (799, 1600, "too tall and narrow"), (1000, 2500, "too tall and narrow"), (900, 2400, "too tall and narrow"),
    (3501, 1000, "too panoramic"), (4000, 500, "too panoramic"), (0, 500, "no size"), (500, 0, "no size"),
])
def test_only_sensible_proportions_are_accepted_for_a_hero(width, height, fragment):
    problem = images.aspect_problem(width, height)
    assert (problem is None) if fragment is None else (fragment in problem)


def test_the_limits_come_from_the_settings():
    settings = {**images.DEFAULTS, "min_aspect": 1.5, "max_aspect": 2.0}
    assert images.aspect_problem(1400, 1000, settings) and images.aspect_problem(1500, 1000, settings) is None
    assert images.aspect_problem(2000, 1000, settings) is None and images.aspect_problem(2001, 1000, settings)


def test_an_absurdly_tall_or_wide_picture_is_refused_when_processed_and_no_file_is_written(tmp_path):
    with pytest.raises(images.ImageError, match="too tall and narrow"):
        images.process_image(picture(900, 2400), tmp_path / "a.webp")
    with pytest.raises(images.ImageError, match="too panoramic"):
        images.process_image(picture(3000, 500), tmp_path / "b.webp")
    assert not (tmp_path / "a.webp").exists() and not (tmp_path / "b.webp").exists()


def test_the_edge_of_the_allowed_shape_is_accepted_when_processed(tmp_path):
    info = images.process_image(picture(1000, 2000), tmp_path / "a.webp")
    assert (info["width"], info["height"]) == (800, 1600)         # exactly 1 : 2, scaled to the height limit
    with pytest.raises(images.ImageError, match="too tall and narrow"):
        images.process_image(picture(999, 2000), tmp_path / "b.webp")


def test_an_ordinary_portrait_photo_is_accepted_and_scaled_to_the_height_limit(tmp_path):
    info = images.process_image(picture(1536, 2160), tmp_path / "a.webp")
    assert (info["width"], info["height"]) == (1138, 1600) and images.aspect_problem(info["width"], info["height"]) is None
    assert images.process_image(picture(1000, 1500), tmp_path / "b.webp")["height"] == 1500        # already small enough
    both = images.process_image(picture(2400, 3600), tmp_path / "c.webp")
    assert both["height"] == 1600 and both["width"] == 1067


def test_a_wide_picture_is_still_scaled_by_its_width(tmp_path):
    info = images.process_image(picture(3200, 1800), tmp_path / "a.webp")
    assert (info["width"], info["height"]) == (1600, 900)


def test_a_portrait_picture_never_ends_up_narrower_than_the_minimum_width(tmp_path):
    for width, height in ((1000, 2000), (1001, 2002), (1536, 2160), (1100, 2150)):
        assert images.process_image(picture(width, height), tmp_path / "a.webp")["width"] >= images.DEFAULTS["min_width"], (width, height)


def test_the_height_limit_can_be_set(tmp_path):
    info = images.process_image(picture(1000, 1400), tmp_path / "a.webp", settings={**images.DEFAULTS, "max_height": 1000})
    assert info["height"] == 1000 and info["width"] == 714


def test_the_aspect_and_height_limits_can_be_set_in_a_config_file(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("images:\n  min_aspect: 0.7\n  max_aspect: 2\n  max_height: 1200\n", encoding="utf-8")
    config = images.load_image_config(path)
    assert config["min_aspect"] == 0.7 and config["max_aspect"] == 2.0 and isinstance(config["max_aspect"], float) and config["max_height"] == 1200
    path.write_text("images:\n  min_aspect: true\n  max_aspect: -1\n  max_height: 0\n", encoding="utf-8")
    config = images.load_image_config(path)
    assert (config["min_aspect"], config["max_aspect"], config["max_height"]) == (0.5, 3.5, 1600)
    assert (images.DEFAULTS["min_aspect"], images.DEFAULTS["max_aspect"], images.DEFAULTS["max_height"]) == (0.5, 3.5, 1600)


def test_the_publish_check_refuses_a_hero_file_of_an_absurd_shape_and_accepts_a_portrait_one(tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    hero = {"src": "/images/tall.webp", "alt": "A tall picture of a console", "credit": "Me", "license": "Own work"}
    Image.new("RGB", (900, 2400), (1, 2, 3)).save(folder / "tall.webp", "WEBP")       # bypasses the processor on purpose
    assert any("hero image:" in f and "too tall and narrow" in f for f in images.hero_failures({**hero, "width": 900, "height": 2400}, tmp_path))
    Image.new("RGB", (900, 1200), (1, 2, 3)).save(folder / "portrait.webp", "WEBP")
    assert images.hero_failures({**hero, "src": "/images/portrait.webp", "width": 900, "height": 1200}, tmp_path) == []
    Image.new("RGB", (4000, 800), (1, 2, 3)).save(folder / "wide.webp", "WEBP")
    assert any("too panoramic" in f for f in images.hero_failures({**hero, "src": "/images/wide.webp", "width": 4000, "height": 800}, tmp_path))


def test_add_hero_refuses_an_absurdly_tall_photo_and_changes_nothing(tmp_path):
    path = make_article(tmp_path)
    (tmp_path / "tall.jpg").write_bytes(picture(1000, 2600))
    with pytest.raises(images.ImageError, match="too tall and narrow"):
        images.add_hero(path, tmp_path / "tall.jpg", alt="A very tall picture", credit="Me", license="Own work", public_dir=tmp_path / "public")
    assert path.read_text(encoding="utf-8") == ARTICLE and not (tmp_path / "public" / "images").exists()


def test_add_hero_accepts_a_portrait_photo_and_records_the_scaled_size(tmp_path):
    path = make_article(tmp_path)
    (tmp_path / "upright.jpg").write_bytes(picture(1536, 2160))
    hero = images.add_hero(path, tmp_path / "upright.jpg", alt="A console standing upright", credit="Me", license="Own work", public_dir=tmp_path / "public")
    assert (hero["width"], hero["height"]) == (1138, 1600) and images.hero_failures(hero, tmp_path / "public") == []


# --- the pages show a hero in a bounded frame ------------------------------------------------------------------------------

ROOT = images.PROJECT_ROOT


def read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_the_article_page_puts_the_hero_in_a_bounded_centred_frame():
    page, css = read("src/pages/news/[id].astro"), read("src/styles/gx.css")
    assert 'class="gx-heroframe"' in page and 'class="h-auto w-full' not in page
    frame = css[css.index(".gx-heroframe {"):]
    assert "justify-content: center" in frame and "overflow: hidden" in frame
    image = css[css.index(".gx-heroframe img {"):]
    assert "max-height: 560px" in image and "max-width: 100%" in image and "object-fit: contain" in image
    assert "width: auto" in image and "height: auto" in image


def test_cards_and_the_top_story_show_a_non_landscape_picture_whole_instead_of_cropped():
    card, home, css = read("src/components/ArticleCard.astro"), read("src/pages/index.astro"), read("src/styles/gx.css")
    assert "gx-thumb-contain" in card and "hero.width / hero.height < 1.2" in card
    assert "gx-thumb-contain" in home and "top.data.hero.width / top.data.hero.height < 1.2" in home
    assert ".gx-thumb-contain { object-fit: contain; }" in css and "object-fit: cover" in css
