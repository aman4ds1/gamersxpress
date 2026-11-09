"""Tests for the automatic Steam key art stage. Nothing here touches the network."""

import io
import json
import urllib.parse

import pytest
from PIL import Image

import images
import steam

TITLE = "Path of Exile 2 launches on December 11, 2026"
ENTITIES = ["Path of Exile 2", "Grinding Gear Games"]
APP = 2694490
SHOT = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{APP}/ss_abc.1920x1080.jpg?t=1"
HERO = steam.LIBRARY_HERO_URL.format(app_id=APP)


def jpg(width=1920, height=620):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (20, 60, 140)).save(buffer, "JPEG")
    return buffer.getvalue()


def search_url(term):
    return steam.SEARCH_URL.format(term=urllib.parse.quote(term))


def search_json(*items):
    return (200, "application/json", json.dumps({"total": len(items), "items": [
        {"type": "app", "id": i, "name": n} for i, n in items]}).encode())


def details_json(app_id=APP, **data):
    base = {"type": "game", "name": "Path of Exile 2", "developers": ["Grinding Gear Games"],
            "publishers": ["Grinding Gear Games"], "screenshots": [{"id": 0, "path_full": SHOT}],
            "content_descriptors": {"ids": [2, 5]}}
    base.update(data)
    return (200, "application/json", json.dumps({str(app_id): {"success": True, "data": base}}).encode())


class Fake:
    """A stand-in for fetch(): a table of URL -> (status, type, body), unknown URLs are 404."""

    def __init__(self, routes):
        self.routes, self.calls = dict(routes), []

    def __call__(self, url):
        self.calls.append(url)
        answer = self.routes.get(url, (404, "", b""))
        if isinstance(answer, Exception):
            raise answer
        return answer


def happy():
    return Fake({
        search_url("Path of Exile 2"): search_json((APP, "Path of Exile 2"), (238960, "Path of Exile")),
        steam.DETAILS_URL.format(app_id=APP): details_json(),
        HERO: (200, "image/jpeg", jpg()),
        SHOT: (200, "image/jpeg", jpg(1920, 1080)),
    })


def find(fake, tmp_path, **kwargs):
    kwargs.setdefault("title", TITLE)
    kwargs.setdefault("entities", ENTITIES)
    return steam.find_hero(slug="poe-2", public_dir=tmp_path, fetch=fake, **kwargs)


# --- which URLs may be fetched ----------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://store.steampowered.com/api/appdetails?appids=1",
    "https://cdn.cloudflare.steamstatic.com/steam/apps/1/library_hero.jpg",
    "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1/ss.jpg?t=1",
])
def test_steam_links_are_allowed(url):
    assert steam.allowed_url(url)


@pytest.mark.parametrize("url", [
    "http://store.steampowered.com/api/appdetails", "https://evil.example/x.jpg",
    "https://store.steampowered.com.evil.example/x", "https://evilsteamstatic.com/x.jpg",
    "https://user@store.steampowered.com/x", "ftp://store.steampowered.com/x", "", None, "not a url",
])
def test_anything_else_is_refused(url):
    assert not steam.allowed_url(url)


def test_the_real_fetch_refuses_a_link_off_steam_without_touching_the_network():
    with pytest.raises(steam.SteamError, match="not an https Steam link"):
        steam.default_fetch("https://evil.example/x.jpg")


# --- matching ------------------------------------------------------------------------------------


def test_names_are_compared_without_case_apostrophes_trademarks_or_accents():
    assert steam.normalize("Dragon’s Dogma™ 2") == steam.normalize("Dragons Dogma 2") == ["dragons", "dogma", "2"]
    assert steam.normalize("Pokémon") == ["pokemon"] and steam.normalize(None) == []
    assert steam.normalize("Hades™ & Dogma® 2℠") == ["hades", "dogma", "2"]


def test_the_game_whose_name_is_in_the_headline_wins_and_the_longest_name_beats_a_prefix():
    items = [{"id": 1, "name": "Path of Exile"}, {"id": 2, "name": "Path of Exile 2"}, {"id": 3, "name": "Exile Tales"}]
    assert steam.match_game(items, TITLE, ENTITIES)["id"] == 2
    assert steam.match_game(items[:1], TITLE, ENTITIES)["id"] == 1


def test_a_game_whose_name_is_not_in_the_headline_is_not_matched():
    items = [{"id": 1, "name": "Dragon's Crown"}, {"id": 2, "name": "Dogma"}]
    assert steam.match_game(items, "Dragon's Dogma 2 adds a collaboration") is None
    assert steam.match_game([], TITLE) is None and steam.match_game([{"id": 1}], TITLE) is None


def test_a_one_word_name_must_be_a_proper_name_the_headline_stands_on():
    control = [{"id": 1, "name": "Control"}]
    assert steam.match_game(control, "Remedy wants more control over Alan Wake 2 sales", ["Remedy"]) is None
    assert steam.match_game(control, "Control 2 gets a release window", ["Control"])["id"] == 1


def test_the_apostrophe_a_headline_loses_does_not_stop_a_match():
    assert steam.match_game([{"id": 1, "name": "Dragon's Dogma 2"}], "Dragon's Dogma 2 adds Delicious in Dungeon collaboration")["id"] == 1


def test_search_terms_are_the_headlines_names_longest_first_without_repeats_up_to_the_limit():
    terms = steam.search_terms("Nintendo has no plans for another Nintendo Switch 2 price hike", ["Nintendo Switch 2", "Capcom"], 5)
    assert terms[0] == "Nintendo Switch 2" and "Capcom" not in terms and len(terms) == len(set(t.lower() for t in terms))
    assert len(steam.search_terms(TITLE, ENTITIES, 1)) == 1 and steam.search_terms("", [], 3) == []


# --- talking to Steam ------------------------------------------------------------------------------


def test_search_returns_only_well_formed_apps():
    body = json.dumps({"items": [{"id": 1, "name": "A"}, {"id": "x", "name": "B"}, {"id": 3}, "junk", {"id": 4, "name": "D"}]}).encode()
    assert steam.search_games("x", Fake({search_url("x"): (200, "application/json", body)})) == [{"id": 1, "name": "A"}, {"id": 4, "name": "D"}]


@pytest.mark.parametrize("answer", [(500, "", b""), (200, "text/html", b"<html>"), (200, "application/json", b"[1,2]")])
def test_a_bad_answer_is_an_error_or_no_results(answer):
    fake = Fake({search_url("x"): answer})
    try:
        assert steam.search_games("x", fake) == []
    except steam.SteamError:
        pass


def test_only_a_game_that_may_be_shown_has_details():
    ok = Fake({steam.DETAILS_URL.format(app_id=1): details_json(1)})
    assert steam.game_details(1, ok)["name"] == "Path of Exile 2"
    for kind in ("dlc", "demo", "advertising", "music", "mod"):
        assert steam.game_details(1, Fake({steam.DETAILS_URL.format(app_id=1): details_json(1, type=kind)})) is None


@pytest.mark.parametrize("ids,shown", [([1, 2, 5], True), ([3], False), ([4], False), ([2, 3], False), ([], True)])
def test_adult_content_descriptors_hide_the_picture(ids, shown):
    fake = Fake({steam.DETAILS_URL.format(app_id=1): details_json(1, content_descriptors={"ids": ids})})
    assert (steam.game_details(1, fake) is not None) is shown


def test_an_app_steam_reports_as_failed_has_no_details():
    body = json.dumps({"1": {"success": False}}).encode()
    assert steam.game_details(1, Fake({steam.DETAILS_URL.format(app_id=1): (200, "application/json", body)})) is None


def test_picture_candidates_are_the_library_art_then_screenshots_then_the_backdrop_on_steam_hosts_only():
    details = {"screenshots": [{"path_full": SHOT}, {"path_full": "https://evil.example/s.jpg"}, {"path_full": SHOT + "2"}, {}],
               "background_raw": "https://shared.akamai.steamstatic.com/bg.jpg"}
    urls = steam.image_candidates(7, details)
    assert urls[0] == steam.LIBRARY_HERO_URL.format(app_id=7) and urls[-1].endswith("bg.jpg")
    assert SHOT in urls and "https://evil.example/s.jpg" not in urls
    assert steam.image_candidates(7, {}) == [steam.LIBRARY_HERO_URL.format(app_id=7)]


# --- the whole lookup --------------------------------------------------------------------------------


def test_a_story_about_a_game_gets_its_steam_art_credited_and_passing_the_hero_check(tmp_path):
    result = find(happy(), tmp_path)
    hero = result["hero"]
    assert hero["src"] == "/images/poe-2.webp" and hero["license"] == "Steam store"
    assert hero["credit"] == "Grinding Gear Games" and hero["sourceUrl"] == f"https://store.steampowered.com/app/{APP}/"
    assert "Path of Exile 2" in hero["alt"] and (hero["width"], hero["height"]) == (1600, 517)
    assert result["path"] == tmp_path / "images" / "poe-2.webp" and result["path"].is_file()
    assert images.hero_failures(hero, tmp_path) == []


def test_the_wide_library_art_is_preferred_over_a_screenshot(tmp_path):
    fake = happy()
    find(fake, tmp_path)
    assert HERO in fake.calls and SHOT not in fake.calls


def test_a_screenshot_is_used_when_there_is_no_library_art(tmp_path):
    fake = happy()
    fake.routes[HERO] = (404, "", b"")
    result = find(fake, tmp_path)
    assert SHOT in fake.calls and (result["hero"]["width"], result["hero"]["height"]) == (1600, 900)


def test_a_picture_too_narrow_or_not_an_image_is_skipped_for_the_next(tmp_path):
    fake = happy()
    fake.routes[HERO] = (200, "image/jpeg", jpg(600, 200))
    assert find(fake, tmp_path)["hero"]["height"] == 900
    fake.routes[HERO] = (200, "text/html", jpg())   # a readable picture, but the server did not call it one
    assert find(fake, tmp_path)["hero"]["height"] == 900


def test_no_usable_picture_means_no_hero_and_no_file(tmp_path):
    fake = happy()
    fake.routes[HERO] = (404, "", b"")
    fake.routes[SHOT] = (200, "image/jpeg", b"not an image")
    assert find(fake, tmp_path) is None and not (tmp_path / "images").exists()


def test_a_story_with_no_game_in_the_headline_asks_steam_nothing_about_details(tmp_path):
    fake = Fake({search_url("Nintendo Switch 2"): search_json((1, "Nintendo Switch Sports"))})
    assert find(fake, tmp_path, title="Nintendo has no plans for another Nintendo Switch 2 price hike", entities=["Nintendo Switch 2"]) is None
    assert not any("appdetails" in url for url in fake.calls)


def test_dlc_and_adult_apps_give_no_hero(tmp_path):
    for override in ({"type": "dlc"}, {"content_descriptors": {"ids": [3]}}):
        fake = happy()
        fake.routes[steam.DETAILS_URL.format(app_id=APP)] = details_json(**override)
        assert find(fake, tmp_path) is None and HERO not in fake.calls


def test_the_second_name_is_searched_when_the_first_finds_nothing(tmp_path):
    fake = Fake({
        search_url("Quasar Arena: Sentinel"): search_json(),
        search_url("Quasar Arena"): search_json((APP, "Quasar Arena: Sentinel")),
        steam.DETAILS_URL.format(app_id=APP): details_json(name="Quasar Arena: Sentinel"),
        HERO: (200, "image/jpeg", jpg()),
    })
    result = find(fake, tmp_path, title="Quasar Arena: Sentinel is official, with a release date still unknown", entities=["Quasar Arena"])
    assert result is not None and result["name"] == "Quasar Arena: Sentinel"
    assert [u for u in fake.calls if "storesearch" in u] == [search_url("Quasar Arena: Sentinel"), search_url("Quasar Arena")]


def test_only_the_allowed_number_of_searches_is_made(tmp_path):
    fake = Fake({})
    find(fake, tmp_path, title="Alpha Game and Beta Game and Gamma Game and Delta Game", entities=[],
         settings={**steam.DEFAULTS, "max_queries": 2})
    assert len([u for u in fake.calls if "storesearch" in u]) <= 2


def test_the_credit_falls_back_to_the_publisher_and_then_to_valve(tmp_path):
    fake = happy()
    fake.routes[steam.DETAILS_URL.format(app_id=APP)] = details_json(developers=[], publishers=["Some Publisher", "B", "C"])
    assert find(fake, tmp_path)["hero"]["credit"] == "Some Publisher, B"
    fake.routes[steam.DETAILS_URL.format(app_id=APP)] = details_json(developers=[], publishers=[])
    assert find(fake, tmp_path)["hero"]["credit"] == "Valve"


def test_switched_off_it_never_touches_steam(tmp_path):
    fake = happy()
    assert find(fake, tmp_path, settings={**steam.DEFAULTS, "enabled": False}) is None and fake.calls == []


def test_a_network_error_or_a_crash_never_escapes(tmp_path):
    down = Fake({search_url("Path of Exile 2"): steam.SteamError("could not reach Steam")})
    assert find(down, tmp_path) is None

    def boom(url):
        raise RuntimeError("unexpected")

    assert steam.find_hero(title=TITLE, entities=ENTITIES, slug="x", public_dir=tmp_path, fetch=boom) is None


def test_one_failed_picture_download_does_not_stop_the_next_candidate(tmp_path):
    fake = happy()
    fake.routes[HERO] = steam.SteamError("timed out")
    assert find(fake, tmp_path)["hero"]["height"] == 900


# --- config -----------------------------------------------------------------------------------------


def test_the_shipped_config_enables_steam_images_and_a_file_can_override_it(tmp_path):
    assert steam.load_steam_config()["enabled"] is True and steam.load_steam_config()["max_queries"] == 3
    path = tmp_path / "c.yaml"
    path.write_text("images:\n  steam:\n    enabled: false\n    max_queries: 5\n    timeout_seconds: true\n", encoding="utf-8")
    config = steam.load_steam_config(path)
    assert config["enabled"] is False and config["max_queries"] == 5 and config["timeout_seconds"] == 10
    path.write_text("images: [", encoding="utf-8")
    assert steam.load_steam_config(path) == steam.DEFAULTS and steam.load_steam_config(tmp_path / "none.yaml") == steam.DEFAULTS


# --- the step-by-step trace and the check command -------------------------------------------------------


def traced(fake, tmp_path, **kwargs):
    lines = []
    result = steam.find_hero(title=kwargs.pop("title", TITLE), entities=kwargs.pop("entities", ENTITIES), slug="poe-2",
                             public_dir=tmp_path, fetch=fake, trace=lines.append, **kwargs)
    return result, lines


def test_the_trace_reports_every_step_of_a_successful_lookup(tmp_path):
    result, lines = traced(happy(), tmp_path)
    assert result is not None
    text = "\n".join(lines)
    assert "searching for ['Path of Exile 2']" in text
    assert "searched 'Path of Exile 2': 2 result(s) (Path of Exile 2, Path of Exile)" in text
    assert f"matched Path of Exile 2 (app {APP})" in text and "is a game by Grinding Gear Games" in text
    assert f"tried {HERO}: 200 image/jpeg" in text and "(1600x517," in lines[-1] and lines[-1].startswith("steam: Path of Exile 2")


def test_the_trace_says_why_a_lookup_found_nothing(tmp_path):
    none = Fake({search_url("Path of Exile 2"): search_json()})
    assert "no game in the headline" in traced(none, tmp_path)[1][-1]
    dlc = happy()
    dlc.routes[steam.DETAILS_URL.format(app_id=APP)] = details_json(type="dlc")
    assert "is not a game that may be shown" in traced(dlc, tmp_path)[1][-1]
    bad = happy()
    bad.routes[HERO] = (404, "", b"")
    bad.routes[SHOT] = (404, "", b"")
    lines = traced(bad, tmp_path)[1]
    assert f"tried {HERO}: 404 -" in "\n".join(lines) and "no usable picture for Path of Exile 2" in lines[-1]
    narrow = happy()
    narrow.routes[HERO] = (200, "image/jpeg", jpg(600, 200))
    assert "that picture is unusable" in "\n".join(traced(narrow, tmp_path)[1])


def test_the_trace_reports_a_network_problem(tmp_path):
    down = Fake({search_url("Path of Exile 2"): steam.SteamError("could not reach Steam: timed out")})
    assert traced(down, tmp_path)[1][-1] == "steam: could not reach Steam: timed out"


def test_without_a_trace_the_steps_go_to_the_pipeline_log(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="gamersxpress.pipeline.steam"):
        find(happy(), tmp_path)
    assert any("steam: matched Path of Exile 2" in message for message in caplog.messages)


def run_check(tmp_path, capsys, fake, *extra, headline=TITLE):
    code = steam.main(["check", headline, "--out", str(tmp_path / "out"), *extra], fetch=fake)
    return code, capsys.readouterr().out


def test_the_check_command_reports_a_working_lookup_and_saves_the_picture(tmp_path, capsys):
    code, out = run_check(tmp_path, capsys, happy(), "--entity", "Path of Exile 2")
    assert code == 0 and out.rstrip().endswith("RESULT: working")
    assert "hero check: passed" in out and '"license": "Steam store"' in out and f"matched Path of Exile 2 (app {APP})" in out
    assert (tmp_path / "out" / "images" / "steam-check.webp").is_file()


def test_the_check_command_exits_1_when_there_is_no_picture_for_the_headline(tmp_path, capsys):
    code, out = run_check(tmp_path, capsys, Fake({}), headline="Nintendo has no plans for another price hike")
    assert code == 1 and "RESULT: no picture for this headline" in out


def test_the_check_command_exits_2_when_steam_cannot_be_reached(tmp_path, capsys):
    down = Fake({search_url("Path of Exile 2"): steam.SteamError("could not reach Steam: no route")})
    code, out = run_check(tmp_path, capsys, down)
    assert code == 2 and "RESULT: Steam could not be reached" in out


def test_the_check_command_works_even_when_the_pipeline_switch_is_off(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(steam, "load_steam_config", lambda path=None: {**steam.DEFAULTS, "enabled": False})
    assert run_check(tmp_path, capsys, happy())[0] == 0


def test_the_check_command_fails_the_run_when_the_hero_would_not_pass_the_publish_check(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(steam.images_stage, "hero_failures", lambda hero, public_dir=None, settings=None: ["hero.alt is too short"])
    code, out = run_check(tmp_path, capsys, happy())
    assert code == 1 and "hero check FAILED: hero.alt is too short" in out


def test_the_command_is_runnable_as_a_module():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(steam.__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-m", "pipeline.steam", "check", "--help"], cwd=root, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "headline" in result.stdout
