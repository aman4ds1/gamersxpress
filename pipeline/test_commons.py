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

    def __init__(self, categories=None, files=None, pictures=None, subcats=None):
        self.categories, self.files, self.pictures, self.calls = categories or {}, files or {}, pictures or {}, []
        self.subcats = subcats or {}

    def __call__(self, url):
        self.calls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if query.get("list") == ["categorymembers"]:
            names = self.subcats.get(query["cmtitle"][0], [])
            return api({"batchcomplete": True, "query": {"categorymembers": [{"ns": 14, "title": name} for name in names]}})
        if "titles" in query:
            pages = []
            for title in query["titles"][0].split("|"):
                page = self.categories.get(title, cat_page(title.removeprefix("Category:"), missing=True))
                if isinstance(page, Exception):
                    raise page
                pages.append(page)
            return api({"batchcomplete": True, "query": {"pages": pages}})
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
    (["Category:Sony hardware"], True), (["Category:Valve hardware"], True), (["Category:Hardware stores"], False),
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


def category_of(page, fetch=None, trusted=None):
    fake = fetch or Fake(categories={f"Category:{PHRASE}": page})
    return commons.find_category(PHRASE, fake, trusted)


def test_a_category_named_exactly_like_the_product_with_a_hardware_parent_is_hardware():
    found = category_of(cat_page())
    assert found["hardware"] is True and found["reason"] == "a parent category is hardware"
    assert found["title"] == f"Category:{PHRASE}" and found["files"] == 30 and found["subcats"] == 2 and found["parents"] == PARENTS


@pytest.mark.parametrize("page,reason", [
    (cat_page(parents=["Category:Video games"]), "direct parent is about games"),
    (cat_page(parents=[]), "no parent or grandparent"),
    (cat_page(parents=["Category:Something else"]), "no parent or grandparent"),
    (cat_page(parents=["Category:PlayStation 5 games", "Category:PlayStation consoles"]), "direct parent is about games"),
    (cat_page(parents=["Category:Sony Interactive Entertainment", "Category:Video game developers"]), "direct parent is about games"),
])
def test_a_category_that_is_not_hardware_is_found_but_not_used(page, reason):
    found = category_of(page)
    assert found["hardware"] is False and reason in found["reason"]


@pytest.mark.parametrize("page", [cat_page(missing=True), cat_page("PlayStation 5")])   # none, or a different one came back
def test_a_category_that_does_not_exist_or_is_not_the_one_asked_for_is_not_found(page):
    assert category_of(page) is None


def test_an_empty_category_is_not_used():
    found = category_of(cat_page(files=0) | {"categoryinfo": {"files": 0, "subcats": 0}})
    assert found["hardware"] is False and found["reason"] == "the category is empty"


def test_a_category_with_a_single_file_is_still_used():
    # Steam Deck on the live site: one file in the category itself, the photos are in subcategories.
    found = category_of(cat_page(files=1))
    assert found["hardware"] is True and found["files"] == 1


def test_a_category_with_only_subcategories_is_used():
    found = category_of(cat_page() | {"categoryinfo": {"files": 0, "subcats": 4}})
    assert found["hardware"] is True


# ---- the live PlayStation 5 Pro result: parents were PlayStation 5, a date and a maintenance category ----

LIVE_PARENTS = ["Category:PlayStation 5", "Category:2024-11-07", "Category:Uses of Wikidata Infobox"]


def test_a_category_under_the_base_product_is_hardware_through_its_grandparent():
    fake = Fake(categories={
        f"Category:{PHRASE}": cat_page(files=26, parents=LIVE_PARENTS),
        "Category:PlayStation 5": cat_page("PlayStation 5", parents=["Category:PlayStation consoles"]),
        "Category:2024-11-07": cat_page("2024-11-07", parents=["Category:November 2024"]),
        "Category:Uses of Wikidata Infobox": cat_page("Uses of Wikidata Infobox", parents=["Category:Wikidata maintenance"]),
    })
    found = commons.find_category(PHRASE, fake)
    assert found["hardware"] is True and found["reason"] == "a grandparent category is hardware"
    assert "Category:PlayStation consoles" in found["ancestors"]
    asked = [urllib.parse.parse_qs(urllib.parse.urlsplit(c).query)["titles"][0] for c in fake.calls]
    assert asked[0] == f"Category:{PHRASE}" and asked[1] == "|".join(LIVE_PARENTS)    # one extra request, all parents at once


def test_a_grandparent_that_is_not_hardware_leaves_the_category_unused():
    fake = Fake(categories={
        f"Category:{PHRASE}": cat_page(files=26, parents=["Category:Something"]),
        "Category:Something": cat_page("Something", parents=["Category:Things"]),
    })
    found = commons.find_category(PHRASE, fake)
    assert found["hardware"] is False and found["reason"] == "no parent or grandparent category is hardware"


def test_a_games_parent_blocks_the_category_even_when_a_grandparent_is_hardware():
    fake = Fake(categories={
        f"Category:{PHRASE}": cat_page(parents=["Category:Pro games"]),
        "Category:Pro games": cat_page("Pro games", parents=["Category:PlayStation consoles"]),
    })
    found = commons.find_category(PHRASE, fake)
    assert found["hardware"] is False and "games" in found["reason"] and len(fake.calls) == 1


def test_only_the_first_eight_parents_are_looked_up():
    parents = [f"Category:Parent {i}" for i in range(12)]
    fake = Fake(categories={f"Category:{PHRASE}": cat_page(parents=parents)})
    commons.find_category(PHRASE, fake)
    asked = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[1]).query)["titles"][0].split("|")
    assert asked == parents[:8]


def test_a_trusted_product_needs_no_parent_check_and_makes_no_extra_request():
    fake = Fake(categories={f"Category:{PHRASE}": cat_page(parents=["Category:Wikidata things"])})
    found = commons.find_category(PHRASE, fake, trusted=["playstation 5 PRO"])
    assert found["hardware"] is True and found["reason"] == "a product on the trusted list" and len(fake.calls) == 1


def test_a_trusted_product_is_still_refused_when_its_category_is_empty_or_about_something_else():
    empty = Fake(categories={f"Category:{PHRASE}": cat_page() | {"categoryinfo": {"files": 0, "subcats": 0}}})
    assert commons.find_category(PHRASE, empty, trusted=[PHRASE])["hardware"] is False
    other = Fake(categories={f"Category:{PHRASE}": cat_page("PlayStation 5")})
    assert commons.find_category(PHRASE, other, trusted=[PHRASE]) is None


def test_the_trusted_list_is_in_the_shipped_config_and_names_the_obvious_products():
    names = commons.load_commons_config()["trusted_products"]
    for product in ("PlayStation 5", "PlayStation 5 Pro", "Steam Deck", "Xbox Series X", "Nintendo Switch 2"):
        assert product in names
    assert "Steam Deck 2" not in names and commons.DEFAULTS["max_subcategories"] == 3


def test_parents_of_several_categories_come_back_together_and_an_empty_request_makes_none():
    fake = Fake(categories={"Category:A": cat_page("A", parents=["Category:X", "Category:Y"]), "Category:B": cat_page("B", parents=["Category:Z"])})
    assert commons.parent_categories(["Category:A", "Category:B"], fake) == ["Category:X", "Category:Y", "Category:Z"]
    assert commons.parent_categories([], fake) == [] and len(fake.calls) == 1


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
    assert "Category:PlayStation 5 Pro: 30 files, 2 subcategories; parents: Category:PlayStation consoles, Category:Sony hardware" in text
    assert "hardware category (a product on the trusted list)" in text
    assert "skipped 'PlayStation 5 Pro logo': not a plain product photo (logo)" in text and "2 of 3 photos are usable" in text
    assert "(1600x900)" in lines[-1] and "by Jane Doe (CC BY-SA 4.0)" in lines[-1]


def test_the_trace_says_why_nothing_was_found(tmp_path):
    assert "no category named exactly 'PlayStation 5 Pro'" in "\n".join(traced(Fake(), tmp_path)[1])
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


# --- the three live results, replayed ------------------------------------------------------------------------

NO_TRUST = {**commons.DEFAULTS, "trusted_products": []}
DECK = "Steam Deck"


def deck_fake(subcats=None, direct=None):
    """Steam Deck as found live: one file in the category, the photos in subcategories."""
    subcats = subcats or ["Category:Steam Deck OLED", "Category:Steam Deck games", "Category:Steam Deck in use",
                          "Category:Steam Deck 2", "Category:Steam Deck docks", "Category:Steam Deck photographs"]
    in_use = [file_page("Steam Deck in use outdoors.jpg", description="A Steam Deck in use")]
    photographs = [file_page("Steam Deck front view.jpg", description="The Steam Deck handheld", width=3000, height=1800)]
    files = {"Category:Steam Deck": direct if direct is not None else [file_page("Steam Deck logo.png")],
             "Category:Steam Deck in use": in_use, "Category:Steam Deck photographs": photographs}
    pictures = {f["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg()) for group in files.values() for f in group}
    return Fake(categories={"Category:Steam Deck": cat_page(DECK, files=1, parents=["Category:Handheld game consoles"]) | {"categoryinfo": {"files": 1, "subcats": 6}}},
                files=files, pictures=pictures, subcats={"Category:Steam Deck": subcats})


def test_playstation_5_pro_as_found_live_is_hardware_through_its_grandparent_and_gets_a_photo(tmp_path):
    files = good_files()
    fake = Fake(
        categories={
            f"Category:{PHRASE}": cat_page(files=26, parents=LIVE_PARENTS),
            "Category:PlayStation 5": cat_page("PlayStation 5", parents=["Category:PlayStation consoles"]),
        },
        files={f"Category:{PHRASE}": files},
        pictures={f["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg()) for f in files},
    )
    result = find(fake, tmp_path, settings=NO_TRUST)            # without relying on the trusted list
    assert result is not None and result["file"] == "File:PlayStation 5 Pro console.jpg"
    assert images.hero_failures(result["hero"], tmp_path) == []


def test_steam_deck_as_found_live_gets_a_photo_from_its_subcategories(tmp_path):
    fake = deck_fake()
    result = find(fake, tmp_path, title="Steam Deck stock runs out", entities=[DECK])
    assert result is not None and result["file"] in {"File:Steam Deck front view.jpg", "File:Steam Deck in use outdoors.jpg"}
    assert result["hero"]["alt"] == "Photo of the Steam Deck" and result["hero"]["license"] == "CC BY-SA 4.0"
    listed = [urllib.parse.parse_qs(urllib.parse.urlsplit(c).query)["gcmtitle"][0] for c in fake.calls if "gcmtitle=" in c]
    assert listed == ["Category:Steam Deck", "Category:Steam Deck in use", "Category:Steam Deck photographs"]


def test_steam_deck_2_as_found_live_has_no_category_and_so_no_photo(tmp_path):
    fake = deck_fake()
    result, lines = traced(fake, tmp_path, title="Steam Deck 2 teased by Valve", entities=["Steam Deck", "Steam Deck 2"])
    assert result is None and "no category named exactly 'Steam Deck 2'" in "\n".join(lines)
    assert not any("gcmtitle=" in c for c in fake.calls)           # the older model's category is never even asked for


# --- subcategories ------------------------------------------------------------------------------------------------


def test_only_subcategories_that_name_the_same_product_and_nothing_else_are_eligible():
    titles = ["Category:Steam Deck OLED", "Category:Steam Deck games", "Category:Steam Deck 2", "Category:Steam Deck docks",
              "Category:Steam Deck screenshots", "Category:Steam Deck repair", "Category:Steam Deck photographs",
              "Category:Steam Deck in use", "Category:Valve hardware", "Category:Steam Deck Pro prototypes"]
    assert commons.eligible_subcategories(titles, DECK, 5) == ["Category:Steam Deck in use", "Category:Steam Deck photographs"]
    assert commons.eligible_subcategories(titles, "Steam Deck OLED", 5) == ["Category:Steam Deck OLED"]
    assert commons.eligible_subcategories(titles, DECK, 1) == ["Category:Steam Deck in use"] and commons.eligible_subcategories([], DECK, 3) == []


def test_subcategory_titles_are_listed_and_junk_is_ignored():
    fake = Fake(subcats={"Category:X": ["Category:A", "Category:B"]})
    assert commons.list_subcategories("Category:X", fake) == ["Category:A", "Category:B"]
    junk = lambda url: api({"query": {"categorymembers": ["x", {"ns": 14}, {"title": "Category:Ok"}]}})
    assert commons.list_subcategories("Category:X", junk) == ["Category:Ok"]
    assert commons.list_subcategories("Category:X", lambda url: api({"query": {}})) == []


def test_subcategories_are_not_asked_for_when_the_category_itself_has_a_usable_photo(tmp_path):
    direct = [file_page("Steam Deck front.jpg", description="Steam Deck")]
    fake = deck_fake(direct=direct)
    fake.pictures[direct[0]["imageinfo"][0]["thumburl"]] = (200, "image/jpeg", jpg())
    assert find(fake, tmp_path, title="Steam Deck stock runs out", entities=[DECK])["file"] == "File:Steam Deck front.jpg"
    assert not any("list=categorymembers" in c for c in fake.calls)


def test_only_the_allowed_number_of_subcategories_is_searched(tmp_path):
    names = [f"Category:Steam Deck photos {i}" for i in range(6)]
    fake = deck_fake(subcats=names)
    find(fake, tmp_path, title="Steam Deck stock runs out", entities=[DECK], settings={**commons.DEFAULTS, "max_subcategories": 2})
    listed = [urllib.parse.parse_qs(urllib.parse.urlsplit(c).query)["gcmtitle"][0] for c in fake.calls if "gcmtitle=" in c]
    assert listed == ["Category:Steam Deck", names[0], names[1]]


def test_a_photo_from_a_subcategory_must_pass_every_photo_check(tmp_path):
    bad = [file_page("Steam Deck in use.jpg", license="CC BY-NC 4.0", description="Steam Deck"),
           file_page("Steam Deck OLED in use.jpg", description="Steam Deck OLED")]
    fake = deck_fake()
    fake.files["Category:Steam Deck in use"] = bad
    fake.files["Category:Steam Deck photographs"] = []
    assert find(fake, tmp_path, title="Steam Deck stock runs out", entities=[DECK]) is None


# --- a photo whose own text names a different model -----------------------------------------------------------------


@pytest.mark.parametrize("title,description", [
    ("Steam Deck OLED front", ""), ("Handheld", "The Steam Deck OLED model"), ("Steam Deck 2 render concept", "Steam Deck"),
    ("Steam Deck Pro", ""),
])
def test_a_photo_that_names_a_different_model_is_refused(title, description):
    reason = commons.unusable_reason(photo(title=title, description=description), DECK, commons.DEFAULTS)
    assert reason is not None and ("different model" in reason or "not a plain" in reason)


def test_a_photo_of_the_base_model_is_not_refused_for_naming_other_words():
    assert commons.unusable_reason(photo(title="Steam Deck front", description="Steam Deck 512GB, shown with a case"), DECK, commons.DEFAULTS) is None
    assert commons.unusable_reason(photo(title="Steam Deck stock", description="Steam Deck"), DECK, commons.DEFAULTS) is None


def test_a_pro_photo_inside_the_base_models_category_is_refused():
    assert "different model" in commons.unusable_reason(photo(title="PlayStation 5 Pro console"), "PlayStation 5", commons.DEFAULTS)
    assert commons.unusable_reason(photo(title="PlayStation 5 console"), "PlayStation 5", commons.DEFAULTS) is None


# --- the trace explains the new steps -----------------------------------------------------------------------------


def test_the_trace_shows_parents_grandparents_and_why_a_category_was_not_used(tmp_path):
    fake = Fake(categories={
        f"Category:{PHRASE}": cat_page(files=26, parents=LIVE_PARENTS),
        "Category:PlayStation 5": cat_page("PlayStation 5", parents=["Category:Entertainment"]),
    })
    result, lines = traced(fake, tmp_path, settings=NO_TRUST)
    text = "\n".join(lines)
    assert result is None
    assert "parents: Category:PlayStation 5, Category:2024-11-07, Category:Uses of Wikidata Infobox; grandparents:" in text
    assert "not used, no parent or grandparent category is hardware" in text


def test_the_trace_says_when_it_falls_back_to_subcategories(tmp_path):
    lines = traced(deck_fake(), tmp_path, title="Steam Deck stock runs out", entities=[DECK])[1]
    assert any("no usable photo in the category itself; trying subcategories ['Category:Steam Deck in use', 'Category:Steam Deck photographs']" in l for l in lines)


# --- config ------------------------------------------------------------------------------------------------------------


def test_the_trusted_list_and_the_subcategory_limit_can_be_set_in_a_config_file(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("images:\n  commons:\n    trusted_products: [Alpha One]\n    max_subcategories: 5\n", encoding="utf-8")
    config = commons.load_commons_config(path)
    assert config["trusted_products"] == ["Alpha One"] and config["max_subcategories"] == 5
    path.write_text("images:\n  commons:\n    trusted_products: nope\n    max_subcategories: 0\n", encoding="utf-8")
    config = commons.load_commons_config(path)
    assert config["trusted_products"] == commons.DEFAULTS["trusted_products"] and config["max_subcategories"] == 3


# --- a Windows console that cannot show a character in a file name ------------------------------------------------------

import sys as _sys  # noqa: E402

HANGUL = "\ud55c\uad6d\uc5b4"


def cp932_console():
    buffer = io.BytesIO()
    return io.TextIOWrapper(buffer, encoding="cp932", errors="strict", write_through=True), buffer


def korean_file_fake():
    files = [file_page(f"PlayStation 5 Pro {HANGUL}.jpg", description="The PlayStation 5 Pro console")]
    return Fake({f"Category:{PHRASE}": cat_page()}, {f"Category:{PHRASE}": files},
                {files[0]["imageinfo"][0]["thumburl"]: (200, "image/jpeg", jpg())})


def test_the_check_command_finishes_on_a_legacy_console_when_a_file_name_has_unencodable_characters(tmp_path, monkeypatch):
    console, buffer = cp932_console()
    monkeypatch.setattr(_sys, "stdout", console)
    code = commons.main(["check", TITLE, "--entity", PHRASE, "--out", str(tmp_path / "out")], fetch=korean_file_fake())
    output = buffer.getvalue().decode("cp932")
    assert code == 0 and output.rstrip().endswith("RESULT: working")
    assert "PlayStation 5 Pro ???" in output and (tmp_path / "out" / "images" / "commons-check.webp").is_file()


def test_a_failing_trace_does_not_turn_a_found_photo_into_no_photo(tmp_path):
    def broken(message):
        raise UnicodeEncodeError("cp932", HANGUL, 0, 1, "illegal multibyte sequence")

    result = commons.find_hero(title=TITLE, entities=[PHRASE], slug="ps5-pro", public_dir=tmp_path, category="hardware",
                               fetch=korean_file_fake(), trace=broken)
    assert result is not None and result["hero"]["title"] == f"PlayStation 5 Pro {HANGUL}"
    assert images.hero_failures(result["hero"], tmp_path) == []


def test_a_failing_trace_does_not_break_the_network_error_path_either(tmp_path):
    def broken(message):
        raise RuntimeError("console gone")

    down = Fake(categories={f"Category:{PHRASE}": commons.CommonsError("could not reach Wikimedia Commons: no route")})
    assert commons.find_hero(title=TITLE, entities=[PHRASE], slug="x", public_dir=tmp_path, category="hardware", fetch=down, trace=broken) is None


def test_the_pipeline_log_path_handles_the_same_file_name(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="gamersxpress.pipeline.commons"):
        result = commons.find_hero(title=TITLE, entities=[PHRASE], slug="ps5-pro", public_dir=tmp_path, category="hardware", fetch=korean_file_fake())
    assert result is not None and any(HANGUL in message for message in caplog.messages)


def test_the_saved_path_line_survives_a_folder_name_the_console_cannot_show(tmp_path, monkeypatch):
    console, buffer = cp932_console()
    monkeypatch.setattr(_sys, "stdout", console)
    out = tmp_path / f"out {HANGUL}"
    code = commons.main(["check", TITLE, "--entity", PHRASE, "--out", str(out)], fetch=korean_file_fake())
    output = buffer.getvalue().decode("cp932")
    assert code == 0 and "saved: " in output and "out ???" in output and (out / "images" / "commons-check.webp").is_file()
