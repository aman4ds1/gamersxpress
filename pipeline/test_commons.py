"""Tests for the automatic Wikimedia Commons photo stage. Nothing here touches the network."""

import io
import json
import urllib.parse

import pytest
from PIL import Image

import commons
import images

TITLE = "PlayStation 5 Pro price rises in Europe"
PHRASE = "PlayStation 5 Pro"
UPLOAD = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/{name}/1600px-{name}"
PARENTS = ["Category:PlayStation consoles", "Category:Sony hardware"]
ARTIST = '<a href="//commons.wikimedia.org/wiki/User:Jane" title="User:Jane">Jane Doe</a>'


def jpg(width=1600, height=900):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 80, 160)).save(buffer, "JPEG")
    return buffer.getvalue()


def cat_page(name=PHRASE, *, files=30, parents=PARENTS, missing=False):
    page = {"ns": 14, "title": f"Category:{name}"}
    if missing:
        return {**page, "missing": True}
    return {**page, "pageid": 7, "categoryinfo": {"size": files, "pages": 0, "files": files, "subcats": 2},
            "categories": [{"ns": 14, "title": parent} for parent in parents]}


def file_page(name="PlayStation 5 Pro console.jpg", *, license="CC BY-SA 4.0", artist=ARTIST, width=4000, height=2250,
              mime="image/jpeg", description="The PlayStation 5 Pro console", restrictions="", thumb="auto"):
    meta = {"LicenseShortName": {"value": license}, "Artist": {"value": artist},
            "ImageDescription": {"value": description}, "Restrictions": {"value": restrictions}}
    url = UPLOAD.format(name=name.replace(" ", "_"))
    info = {"url": url.replace("/thumb", "").rsplit("/", 1)[0], "width": width, "height": height, "mime": mime, "extmetadata": meta}
    if thumb == "auto":
        info["thumburl"] = url
    elif thumb:
        info["thumburl"] = thumb
    return {"pageid": abs(hash(name)) % 10_000, "ns": 6, "title": f"File:{name}", "imageinfo": [info]}


def api(data, status=200):
    return (status, "application/json", json.dumps(data).encode())


class Fake:
    """A stand-in for fetch(): category lookups, file listings and picture downloads."""

    def __init__(self, categories=None, files=None, pictures=None):
        self.categories, self.files, self.pictures, self.calls = categories or {}, files or {}, pictures or {}, []

    def __call__(self, url):
        self.calls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "titles" in query:
            page = self.categories.get(query["titles"][0], cat_page(query["titles"][0].removeprefix("Category:"), missing=True))
            if isinstance(page, Exception):
                raise page
            return api({"batchcomplete": True, "query": {"pages": [page]}})
        if "gcmtitle" in query:
            return api({"batchcomplete": True, "query": {"pages": self.files.get(query["gcmtitle"][0], [])}})
        answer = self.pictures.get(url, (404, "", b""))
        if isinstance(answer, Exception):
            raise answer
        return answer


def good_files():
    best = file_page("PlayStation 5 Pro console.jpg")
    return [best, file_page("Some other photo.jpg", description="A PlayStation 5 Pro in a living room", width=3000, height=3000)]


def happy():
    files = good_files()
    return Fake(
        categories={f"Category:{PHRASE}": cat_page()},
        files={f"Category:{PHRASE}": files},
        pictures={f["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg()) for f in files},
    )


def find(fake, tmp_path, **kwargs):
    kwargs.setdefault("title", TITLE)
    kwargs.setdefault("entities", [PHRASE])
    kwargs.setdefault("category", "hardware")
    return commons.find_hero(slug="ps5-pro", public_dir=tmp_path, fetch=fake, **kwargs)


# --- which URLs may be fetched ----------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://commons.wikimedia.org/w/api.php?action=query", "https://upload.wikimedia.org/wikipedia/commons/a/ab/X.jpg"])
def test_wikimedia_links_are_allowed(url):
    assert commons.allowed_url(url)


@pytest.mark.parametrize("url", [
    "http://commons.wikimedia.org/w/api.php", "https://en.wikipedia.org/w/api.php", "https://evil.example/x.jpg",
    "https://commons.wikimedia.org.evil.example/x", "https://user@commons.wikimedia.org/x", "https://upload.wikimedia.org.evil.example/x",
    "ftp://upload.wikimedia.org/x", "", None, "not a url",
])
def test_anything_else_is_refused(url):
    assert not commons.allowed_url(url)


def test_the_real_fetch_refuses_a_link_off_wikimedia_without_touching_the_network():
    with pytest.raises(commons.CommonsError, match="not an https Wikimedia link"):
        commons.default_fetch("https://evil.example/x.jpg")


# --- text helpers ---------------------------------------------------------------------------------


def test_html_snippets_become_plain_text():
    assert commons.clean_text(ARTIST) == "Jane Doe"
    assert commons.clean_text("A &amp; B <b>bold</b>\n\n text") == "A & B bold text"
    assert commons.clean_text(None) == "" and commons.clean_text({"value": 1}) != ""


def test_file_titles_are_readable():
    assert commons.file_title("File:PlayStation_5_(front).jpg") == "PlayStation 5 (front)"
    assert commons.file_title("File:Caf\u00e9  photo.JPEG") == "Caf\u00e9 photo"
    assert commons.file_title("") == ""


def test_names_are_compared_without_case_apostrophes_or_trademarks():
    assert commons.normalize("PlayStation\u00ae 5 Pro") == ["playstation", "5", "pro"] and commons.normalize(None) == []


# --- licenses -------------------------------------------------------------------------------------


@pytest.mark.parametrize("name,label,url", [
    ("CC BY-SA 4.0", "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
    ("CC BY-SA 3.0", "CC BY-SA 3.0", "https://creativecommons.org/licenses/by-sa/3.0/"),
    ("CC BY 4.0", "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    ("CC BY 2.0", "CC BY 2.0", "https://creativecommons.org/licenses/by/2.0/"),
    ("  cc by-sa  4.0 ", "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
    ("CC0", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    ("CC0 1.0", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    ("Public domain", "Public domain", None),
])
def test_allowed_licenses_get_their_label_and_text_link(name, label, url):
    info = commons.license_info(name)
    assert info["label"] == label and info["url"] == url and info["attribution"] is (not label in ("CC0", "Public domain"))


@pytest.mark.parametrize("name", [
    "CC BY-NC 4.0", "CC BY-ND 4.0", "CC BY-NC-SA 4.0", "GFDL", "Attribution", "CC BY-SA 3.0 de", "CC BY 2.5", "CC BY 1.0",
    "Fair use", "All rights reserved", "Copyrighted free use", "", None, "CC BY", "CC BY-SA 4.0 and GFDL",
])
def test_every_other_license_is_refused(name):
    assert commons.license_info(name) is None


def test_share_alike_can_be_switched_off():
    config = {**commons.DEFAULTS, "share_alike": False}
    assert commons.license_info("CC BY-SA 4.0", config) is None
    assert commons.license_info("CC BY 4.0", config)["label"] == "CC BY 4.0" and commons.license_info("CC0", config)


def test_a_license_version_must_be_on_the_configured_list(monkeypatch):
    monkeypatch.setattr(commons.images_stage, "load_image_config", lambda path=None: {**images.DEFAULTS, "allowed_licenses": ["CC BY 4.0", "CC0"]})
    assert commons.license_info("CC BY 4.0") and commons.license_info("CC0")
    assert commons.license_info("CC BY-SA 4.0") is None and commons.license_info("Public domain") is None


# --- matching ---------------------------------------------------------------------------------------


def test_only_multi_word_product_names_are_looked_up_longest_first_without_repeats():
    assert commons.product_phrases("Nintendo has no plans for another Nintendo Switch 2 price hike", ["Nintendo Switch 2"], 3) == ["Nintendo Switch 2"]
    assert commons.product_phrases(TITLE, [], 3) == [PHRASE]
    assert commons.product_phrases("Valve ships a Steam Deck update", ["Steam Deck", "Valve"], 3) == ["Steam Deck"]
    assert commons.product_phrases("Sony", ["Sony"], 3) == [] and commons.product_phrases("", [], 3) == []
    many = "Steam Deck, Nintendo Switch 2, PlayStation 5 Pro and Xbox Series X compared"
    names = ["Steam Deck", "Nintendo Switch 2", "PlayStation 5 Pro", "Xbox Series X"]
    assert len(commons.product_phrases(many, names, 4)) == 4
    limited = commons.product_phrases(many, names, 2)
    assert len(limited) == 2 and limited == commons.product_phrases(many, names, 4)[:2]


@pytest.mark.parametrize("parents,expected", [
    (["Category:PlayStation consoles"], True), (["Category:Handheld game consoles"], True), (["Category:Nvidia graphics cards"], True),
    (["Category:Video game controllers"], True), (["Category:Computer hardware by manufacturer"], True),
    (["Category:Xbox controllers"], True), (["Category:Video game consoles by generation"], True), (["Category:Intel microprocessors"], True),
    (["Category:Video games"], False), (["Category:Companies of Japan"], False), (["Category:People"], False), ([], False),
    (["Category:Console games"], False), (["Category:Video games by console"], False), (["Category:PlayStation 5 games"], False),
    (["Category:Sony Interactive Entertainment"], False), (["Category:Video game developers"], False),
])
def test_only_a_category_under_a_hardware_parent_counts_as_hardware(parents, expected):
    assert commons.is_hardware_category(parents) is expected


def test_a_photo_must_name_every_word_of_the_product_itself():
    named = {"title": "PlayStation 5 Pro console", "description": ""}
    assert commons.says_product(named, PHRASE)
    assert commons.says_product({"title": "Console", "description": "The Pro model of the PlayStation 5"}, PHRASE)
    assert not commons.says_product({"title": "PS5 Pro console", "description": ""}, PHRASE)
    assert not commons.says_product({"title": "PlayStation 5 console", "description": ""}, PHRASE)
    assert not commons.says_product({"title": "x", "description": "y"}, "")


def photo(**overrides):
    info = {"title": "PlayStation 5 Pro console", "description": "", "mime": "image/jpeg", "width": 4000, "height": 2250,
            "thumb_url": "https://upload.wikimedia.org/x.jpg", "license": "CC BY-SA 4.0", "artist": "Jane Doe", "restrictions": ""}
    info.update(overrides)
    return info


def test_a_good_photo_has_no_reason_to_be_refused():
    assert commons.unusable_reason(photo(), PHRASE, commons.DEFAULTS) is None


@pytest.mark.parametrize("overrides,fragment", [
    ({"mime": "image/svg+xml"}, "not a photo format"), ({"mime": None}, "not a photo format"),
    ({"width": 1199}, "wide (min 1200)"), ({"width": None}, "wide (min 1200)"), ({"thumb_url": ""}, "no picture link"),
    ({"license": "CC BY-NC 4.0"}, "is not allowed"), ({"license": ""}, "is not allowed"), ({"artist": ""}, "no named author"),
    ({"restrictions": "Personality rights"}, "personality-rights"), ({"title": "PlayStation 5 Pro logo"}, "logo"),
    ({"title": "PlayStation 5 Pro box"}, "box"), ({"title": "PlayStation 5 Pro cosplay"}, "cosplay"),
    ({"title": "Console photo"}, "do not name"),
])
def test_each_reason_to_refuse_a_photo_is_applied(overrides, fragment):
    assert fragment in commons.unusable_reason(photo(**overrides), PHRASE, commons.DEFAULTS)


def test_public_domain_and_cc0_need_no_named_author():
    for name in ("Public domain", "CC0"):
        assert commons.unusable_reason(photo(license=name, artist=""), PHRASE, commons.DEFAULTS) is None


def test_photos_are_ranked_named_in_title_then_landscape_then_wider():
    named = photo(title="PlayStation 5 Pro front", width=2000, height=1200)
    described = photo(title="Console photo", description="PlayStation 5 Pro", width=6000, height=3000)
    square = photo(title="PlayStation 5 Pro square", width=4000, height=4000)
    wide = photo(title="PlayStation 5 Pro wide", width=3000, height=1700)
    order = sorted([square, described, named, wide], key=lambda item: commons.rank(item, PHRASE))
    assert order == [wide, named, square, described]


# --- talking to Commons -----------------------------------------------------------------------------


def category_of(page, fetch=None):
    fake = fetch or Fake(categories={f"Category:{PHRASE}": page})
    return commons.find_category(PHRASE, fake)


def test_the_category_named_exactly_like_the_product_under_a_hardware_parent_is_found():
    found = category_of(cat_page())
    assert found["title"] == f"Category:{PHRASE}" and found["files"] == 30 and found["parents"] == PARENTS


@pytest.mark.parametrize("page", [
    cat_page(missing=True), cat_page(files=2), cat_page(parents=["Category:Video games"]), cat_page(parents=[]),
    cat_page("PlayStation 5"),   # a different category came back
])
def test_any_other_category_is_not_used(page):
    assert category_of(page) is None


@pytest.mark.parametrize("data", [{"query": {"pages": []}}, {"query": {}}, {"batchcomplete": True}, {"query": {"pages": ["junk"]}}])
def test_an_answer_with_no_category_page_has_no_category(data):
    assert commons.find_category(PHRASE, lambda url: api(data)) is None


@pytest.mark.parametrize("answer,message", [
    ((500, "", b""), "answered 500"), ((404, "", b"{}"), "answered 404"), ((200, "text/html", b"<html>"), "did not return JSON"),
    ((200, "application/json", b"[1]"), "reported an error"), ((200, "application/json", b'{"error": {"code": "badvalue"}}'), "reported an error"),
])
def test_a_bad_api_answer_is_an_error_that_says_what_went_wrong(answer, message):
    with pytest.raises(commons.CommonsError, match=message):
        commons.find_category(PHRASE, lambda url: answer)


def test_a_category_that_does_not_exist_has_no_files_and_is_not_used():
    assert category_of(cat_page(missing=True)) is None


def test_files_are_listed_with_their_license_data_and_plain_text_fields():
    files = commons.list_files(f"Category:{PHRASE}", happy(), 40)
    assert [f["title"] for f in files] == ["PlayStation 5 Pro console", "Some other photo"]
    first = files[0]
    assert first["license"] == "CC BY-SA 4.0" and first["artist"] == "Jane Doe" and first["description"] == "The PlayStation 5 Pro console"
    assert first["width"] == 4000 and first["mime"] == "image/jpeg" and first["thumb_url"].startswith("https://upload.wikimedia.org/")


def test_files_without_picture_data_are_skipped_and_the_full_url_backs_up_a_missing_thumb():
    bad = {"ns": 6, "title": "File:No info.jpg"}
    nothumb = file_page("No thumb.jpg", thumb=None)
    fake = Fake(files={f"Category:{PHRASE}": [bad, {"title": "File:Empty.jpg", "imageinfo": []}, nothumb]})
    listed = commons.list_files(f"Category:{PHRASE}", fake, 40)
    assert [f["title"] for f in listed] == ["No thumb"] and listed[0]["thumb_url"] == nothumb["imageinfo"][0]["url"]


# --- the whole lookup ---------------------------------------------------------------------------------


def test_a_hardware_story_gets_a_credited_photo_that_passes_the_hero_check(tmp_path):
    result = find(happy(), tmp_path)
    hero = result["hero"]
    assert hero["src"] == "/images/ps5-pro.webp" and hero["alt"] == f"Photo of the {PHRASE}"
    assert hero["credit"] == "Jane Doe" and hero["license"] == "CC BY-SA 4.0"
    assert hero["licenseUrl"] == "https://creativecommons.org/licenses/by-sa/4.0/" and hero["title"] == "PlayStation 5 Pro console"
    assert hero["sourceUrl"] == "https://commons.wikimedia.org/wiki/File:PlayStation_5_Pro_console.jpg"
    assert (hero["width"], hero["height"]) == (1600, 900) and result["path"].is_file() and result["file"] == "File:PlayStation 5 Pro console.jpg"
    assert images.hero_failures(hero, tmp_path) == []


def test_the_best_photo_is_used_and_unsuitable_ones_are_skipped(tmp_path):
    files = [
        file_page("PlayStation 5 Pro logo.jpg"), file_page("PlayStation 5 Pro NC.jpg", license="CC BY-NC 4.0"),
        file_page("PlayStation 5 Pro anon.jpg", artist=""), file_page("PlayStation 5 Pro small.jpg", width=900),
        file_page("PlayStation 5 Pro good.jpg"),
    ]
    fake = Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files},
                {f["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg()) for f in files})
    result = find(fake, tmp_path)
    assert result["file"] == "File:PlayStation 5 Pro good.jpg"
    assert len([c for c in fake.calls if c.startswith("https://upload.wikimedia.org")]) == 1


def test_the_next_photo_is_tried_when_a_download_fails_or_is_unusable(tmp_path):
    fake = happy()
    first, second = (f["imageinfo"][0]["thumburl"] for f in good_files())
    fake.pictures[first] = (404, "", b"")
    assert find(fake, tmp_path)["file"] == "File:Some other photo.jpg"
    fake.pictures[first] = (200, "image/jpeg", jpg(500, 300))       # too narrow once downloaded
    assert find(fake, tmp_path)["file"] == "File:Some other photo.jpg"
    fake.pictures[first] = (200, "text/html", jpg())                 # readable, but not served as an image
    assert find(fake, tmp_path)["file"] == "File:Some other photo.jpg"
    fake.pictures[first] = commons.CommonsError("could not reach Wikimedia Commons: timed out")
    assert find(fake, tmp_path)["file"] == "File:Some other photo.jpg"


def test_a_product_of_another_generation_never_finds_the_wrong_photos(tmp_path):
    fake = Fake(categories={"Category:Nintendo Switch": cat_page("Nintendo Switch", parents=["Category:Handheld game consoles"])},
                files={"Category:Nintendo Switch": [file_page("Nintendo Switch console.jpg", description="Nintendo Switch")]})
    assert find(fake, tmp_path, title="Nintendo Switch 2 price rises", entities=["Nintendo Switch 2"]) is None
    assert not any("gcmtitle" in c for c in fake.calls)


def test_the_article_category_must_be_one_of_the_configured_ones(tmp_path):
    fake = happy()
    assert find(fake, tmp_path, category="playstation") is None and fake.calls == []
    assert find(happy(), tmp_path, category="tech") is not None
    assert find(happy(), tmp_path, category=None) is not None


def test_switched_off_it_never_touches_wikimedia(tmp_path):
    fake = happy()
    assert find(fake, tmp_path, settings={**commons.DEFAULTS, "enabled": False}) is None and fake.calls == []


def test_the_second_product_name_is_tried_when_the_first_has_no_category(tmp_path):
    fake = Fake({"Category:Steam Deck": cat_page("Steam Deck", parents=["Category:Handheld game consoles"])},
                {"Category:Steam Deck": [file_page("Steam Deck front.jpg", description="Steam Deck")]})
    fake.pictures = {f["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg()) for f in fake.files["Category:Steam Deck"]}
    result = find(fake, tmp_path, title="Valve Steam Deck restock sells out", entities=["Steam Deck"])
    assert result is not None and result["name"] == "Steam Deck"
    asked = [urllib.parse.parse_qs(urllib.parse.urlsplit(c).query)["titles"][0] for c in fake.calls if "titles=" in c]
    assert asked == ["Category:Valve Steam Deck", "Category:Steam Deck"]   # longest first


def test_no_suitable_photo_means_no_hero_and_no_file(tmp_path):
    files = [file_page("PlayStation 5 Pro logo.jpg"), file_page("PlayStation 5 Pro NC.jpg", license="CC BY-NC 4.0")]
    fake = Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files})
    assert find(fake, tmp_path) is None and not (tmp_path / "images").exists()


def test_a_picture_link_outside_wikimedia_is_never_downloaded(tmp_path):
    files = [file_page("PlayStation 5 Pro console.jpg", thumb="https://evil.example/x.jpg")]
    fake = Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files}, {"https://evil.example/x.jpg": (200, "image/jpeg", jpg())})
    assert find(fake, tmp_path) is None and "https://evil.example/x.jpg" not in fake.calls


def test_a_public_domain_photo_without_an_author_is_credited_to_commons_with_no_license_link(tmp_path):
    files = [file_page("PlayStation 5 Pro console.jpg", license="Public domain", artist="")]
    fake = Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files},
                {files[0]["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg())})
    hero = find(fake, tmp_path)["hero"]
    assert hero["credit"] == "Wikimedia Commons" and hero["license"] == "Public domain" and "licenseUrl" not in hero
    assert images.hero_failures(hero, tmp_path) == []


def test_a_cc0_photo_links_the_cc0_text(tmp_path):
    files = [file_page("PlayStation 5 Pro console.jpg", license="CC0", artist="")]
    fake = Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files}, {files[0]["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg())})
    assert find(fake, tmp_path)["hero"]["licenseUrl"] == "https://creativecommons.org/publicdomain/zero/1.0/"


def test_the_source_link_is_the_encoded_file_page(tmp_path):
    name = "PlayStation 5 Pro (front) \u00e9d's.jpg"
    files = [file_page(name, description="PlayStation 5 Pro")]
    fake = Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files}, {files[0]["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg())})
    url = find(fake, tmp_path)["hero"]["sourceUrl"]
    assert url.startswith("https://commons.wikimedia.org/wiki/File:PlayStation_5_Pro_(front)_") and " " not in url and url.endswith(".jpg")
    assert urllib.parse.unquote(url).endswith("\u00e9d's.jpg")


def test_a_long_author_name_is_shortened_and_html_in_it_is_removed(tmp_path):
    files = [file_page("PlayStation 5 Pro console.jpg", artist="<b>" + "A" * 300 + "</b>")]
    fake = Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files}, {files[0]["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg())})
    credit = find(fake, tmp_path)["hero"]["credit"]
    assert credit == "A" * 100 and "<" not in credit


def test_only_the_allowed_number_of_photos_is_downloaded(tmp_path):
    files = [file_page(f"PlayStation 5 Pro view {i}.jpg") for i in range(8)]
    fake = Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files})
    assert find(fake, tmp_path, settings={**commons.DEFAULTS, "max_candidates": 3}) is None
    assert len([c for c in fake.calls if c.startswith("https://upload.wikimedia.org")]) == 3


def test_a_network_error_or_a_crash_never_escapes(tmp_path):
    down = Fake(categories={f"Category:{PHRASE}": commons.CommonsError("could not reach Wikimedia Commons: no route")})
    assert find(down, tmp_path) is None

    def boom(url):
        raise RuntimeError("unexpected")

    assert commons.find_hero(title=TITLE, entities=[PHRASE], slug="x", public_dir=tmp_path, category="hardware", fetch=boom) is None


# --- the trace and the check command ---------------------------------------------------------------------


def traced(fake, tmp_path, **kwargs):
    lines = []
    result = find(fake, tmp_path, trace=lines.append, **kwargs)
    return result, lines


def test_the_trace_reports_every_step_including_why_photos_were_skipped(tmp_path):
    files = [file_page("PlayStation 5 Pro logo.jpg")] + good_files()
    fake = Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files},
                {f["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg()) for f in files})
    result, lines = traced(fake, tmp_path)
    text = "\n".join(lines)
    assert result is not None and "looking for a product category among ['PlayStation 5 Pro']" in text
    assert "Category:PlayStation 5 Pro (30 files, under Category:PlayStation consoles" in text
    assert "skipped 'PlayStation 5 Pro logo': not a plain product photo (logo)" in text and "2 of 3 photos are usable" in text
    assert "(1600x900)" in lines[-1] and "by Jane Doe (CC BY-SA 4.0)" in lines[-1]


def test_the_trace_says_why_nothing_was_found(tmp_path):
    assert "no hardware category named exactly 'PlayStation 5 Pro'" in "\n".join(traced(Fake(), tmp_path)[1])
    assert "is not one of" in traced(happy(), tmp_path, category="esports")[1][0]
    down = Fake(categories={f"Category:{PHRASE}": commons.CommonsError("could not reach Wikimedia Commons: no route")})
    assert traced(down, tmp_path)[1][-1] == "commons: could not reach Wikimedia Commons: no route"


def test_without_a_trace_the_steps_go_to_the_pipeline_log(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="gamersxpress.pipeline.commons"):
        find(happy(), tmp_path)
    assert any("commons: looking for a product category" in message for message in caplog.messages)


def run_check(tmp_path, capsys, fake, *extra, headline=TITLE):
    code = commons.main(["check", headline, "--out", str(tmp_path / "out"), *extra], fetch=fake)
    return code, capsys.readouterr().out


def test_the_check_command_reports_a_working_lookup_and_saves_the_photo(tmp_path, capsys):
    code, out = run_check(tmp_path, capsys, happy(), "--entity", PHRASE)
    assert code == 0 and out.rstrip().endswith("RESULT: working") and "hero check: passed" in out
    assert '"license": "CC BY-SA 4.0"' in out and (tmp_path / "out" / "images" / "commons-check.webp").is_file()


def test_the_check_command_exits_1_when_there_is_no_photo_and_2_when_wikimedia_is_unreachable(tmp_path, capsys):
    code, out = run_check(tmp_path, capsys, Fake())
    assert code == 1 and "RESULT: no photo for this headline" in out
    down = Fake(categories={f"Category:{PHRASE}": commons.CommonsError("could not reach Wikimedia Commons: no route")})
    code, out = run_check(tmp_path, capsys, down)
    assert code == 2 and "RESULT: Wikimedia Commons could not be reached" in out


def test_the_check_command_uses_the_given_category_and_ignores_the_off_switch(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(commons, "load_commons_config", lambda path=None: {**commons.DEFAULTS, "enabled": False})
    assert run_check(tmp_path, capsys, happy())[0] == 0
    code, out = run_check(tmp_path, capsys, happy(), "--category", "esports")
    assert code == 1 and "is not one of" in out


def test_the_check_command_fails_when_the_hero_would_not_pass_the_publish_check(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(commons.images_stage, "hero_failures", lambda hero, public_dir=None, settings=None: ["hero.alt is too short"])
    code, out = run_check(tmp_path, capsys, happy())
    assert code == 1 and "hero check FAILED: hero.alt is too short" in out


def test_the_command_is_runnable_as_a_module():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(commons.__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-m", "pipeline.commons", "check", "--help"], cwd=root, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "headline" in result.stdout


# --- config ----------------------------------------------------------------------------------------------


def test_the_shipped_config_enables_commons_photos_for_hardware_and_tech_and_a_file_can_override_it(tmp_path):
    config = commons.load_commons_config()
    assert config["enabled"] is True and config["categories"] == ["hardware", "tech"] and config["share_alike"] is True
    path = tmp_path / "c.yaml"
    path.write_text("images:\n  commons:\n    enabled: false\n    share_alike: false\n    categories: [pc]\n    min_width: 2000\n    max_files: true\n", encoding="utf-8")
    config = commons.load_commons_config(path)
    assert config["enabled"] is False and config["share_alike"] is False and config["categories"] == ["pc"]
    assert config["min_width"] == 2000 and config["max_files"] == 40
    path.write_text("images: [", encoding="utf-8")
    assert commons.load_commons_config(path) == commons.DEFAULTS and commons.load_commons_config(tmp_path / "none.yaml") == commons.DEFAULTS


# --- an older model is never used for a newer model's story ------------------------------------------------


def old_model_fake():
    return Fake({"Category:Steam Deck": cat_page("Steam Deck", parents=["Category:Handheld game consoles"])},
                {"Category:Steam Deck": [file_page("Steam Deck front.jpg", description="Steam Deck")]})


@pytest.mark.parametrize("title,entities", [
    ("Steam Deck 2 teased by Valve", ["Steam Deck", "Steam Deck 2"]),
    ("Valve teases Steam Deck 2 while Steam Deck stock runs out", ["Steam Deck"]),
    ("Steam Deck Pro rumoured for next year", ["Steam Deck"]),
    ("Steam Deck OLED gets a price cut", ["Steam Deck"]),
])
def test_the_category_of_an_older_model_is_never_looked_up_for_a_newer_models_story(tmp_path, title, entities):
    fake = old_model_fake()
    assert find(fake, tmp_path, title=title, entities=entities) is None
    assert "Category:Steam Deck" not in [urllib.parse.parse_qs(urllib.parse.urlsplit(c).query).get("titles", [""])[0] for c in fake.calls]


def test_a_pro_model_story_looks_up_the_pro_category_and_not_the_base_models():
    assert commons.product_phrases("PlayStation 5 Pro price rises in Europe", ["PlayStation 5", PHRASE], 3) == [PHRASE]
    assert commons.product_phrases("Nintendo Switch price rises", ["Nintendo Switch"], 3) == ["Nintendo Switch"]
    assert commons.product_phrases("Xbox Series X price rises", ["Xbox Series X"], 3) == ["Xbox Series X"]
