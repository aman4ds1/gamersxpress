import datetime as dt
import json

import cluster


def item(title, link, *, tier=1, owner="o1", published="2026-10-09T10:00:00+00:00", age_hours=2.0):
    return {
        "title": title,
        "link": link,
        "source_name": owner,
        "tier": tier,
        "owner": owner,
        "region": "us",
        "published": published,
        "age_hours": age_hours,
    }


NOW = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)


def test_normalize_title_and_proper_nouns():
    assert cluster.normalize_title("GTA 6: The Next Big Thing!") == "gta 6 the next big thing"
    nouns = cluster.proper_nouns("Xbox Game Pass adds Halo")
    assert "halo" in nouns
    assert "xbox" not in nouns  # first word is skipped


def test_same_story_matches_near_duplicates():
    a = {"title": "Nvidia announces RTX 5090 at CES 2026"}
    b = {"title": "Nvidia announces RTX 5090 at CES"}
    c = {"title": "Sony delays a PlayStation game"}
    assert cluster.same_story(a, b) is True
    assert cluster.same_story(a, c) is False


def test_cluster_id_from_member_urls_is_stable():
    a = ["https://a.example/1", "https://b.example/2"]
    b = ["https://b.example/2", "https://a.example/1"]
    assert cluster.cluster_id_for_urls(a) == cluster.cluster_id_for_urls(b)


def test_cluster_id_changes_when_member_changes():
    base = ["https://a.example/1", "https://b.example/2"]
    assert cluster.cluster_id_for_urls(base) != cluster.cluster_id_for_urls(base[:-1])
    assert cluster.cluster_id_for_urls(base) != cluster.cluster_id_for_urls(
        ["https://a.example/1", "https://b.example/2", "https://c.example/3"]
    )
    assert cluster.cluster_id_for_urls(base) != cluster.cluster_id_for_urls(
        ["https://a.example/1", "https://b.example/2-changed"]
    )


def test_merge_items_dedupes_by_link_and_drops_stale():
    old = "2026-09-01T00:00:00+00:00"  # more than 7 days before NOW
    pool = {"items": [item("A old", "https://example.com/a", published=old)]}
    merged = cluster.merge_items(
        pool,
        [item("A fresh", "https://example.com/a"), item("B", "https://example.com/b")],
        now=NOW,
    )
    links = [entry["link"] for entry in merged["items"]]
    assert links == ["https://example.com/a", "https://example.com/b"]
    assert merged["items"][0]["title"] == "A fresh"
    assert pool["items"] is merged["items"]


def test_pool_and_seen_round_trip(tmp_path):
    pool_path = tmp_path / "pool.json"
    cluster.save_pool({"items": [item("A", "https://example.com/a")]}, now=NOW, path=pool_path)
    assert cluster.load_pool(pool_path)["items"][0]["title"] == "A"

    seen_path = tmp_path / "seen.json"
    seen = cluster.load_seen(seen_path)
    cluster.mark_seen(seen, "abc123", now=NOW)
    cluster.save_seen(seen, seen_path)
    assert cluster.load_seen(seen_path)["clusters"]["abc123"]["last_seen"] == NOW.isoformat()


def test_cluster_items_groups_and_skips_seen():
    items = [
        item("Nvidia announces RTX 5090", "https://a.com/1", owner="a"),
        item("Nvidia announces RTX 5090 details", "https://b.com/1", owner="b", tier=2),
        item("Nintendo reveals new Switch", "https://c.com/1", owner="c"),
    ]
    clusters = cluster.cluster_items(items, now=NOW)
    assert len(clusters) == 2
    nvidia = next(c for c in clusters if "nvidia" in c["title"].lower())
    assert len(nvidia["items"]) == 2
    assert nvidia["owners"] == ["a", "b"]
    assert nvidia["story_type"] == "announcement"

    seen = {"clusters": {}}
    cluster.mark_seen(
        seen, nvidia["id"], now=NOW, member_urls=[m["link"] for m in nvidia["items"]],
        primary_entities=nvidia["entities"], story_type=nvidia["story_type"],
    )
    remaining = cluster.cluster_items(items, now=NOW, seen=seen)
    assert len(remaining) == 1
    assert remaining[0]["id"] != nvidia["id"]


def test_seen_story_gaining_a_member_is_still_seen():
    first = item("Nvidia announces RTX 5090", "https://a.com/1", owner="a")
    second = item("Nvidia announces RTX 5090 details", "https://b.com/1", owner="b", tier=2)
    later = item("Nvidia announces RTX 5090 Canadian pricing", "https://c.com/1", owner="c")
    covered = cluster.cluster_items([first, second], now=NOW)[0]
    seen = {"clusters": {}}
    cluster.mark_seen(
        seen, covered["id"], now=NOW, member_urls=[m["link"] for m in covered["items"]],
        primary_entities=covered["entities"], story_type=covered["story_type"],
    )

    grew = cluster.cluster_items([first, second, later], now=NOW, seen=seen)
    assert grew == []


def test_same_entities_but_different_story_type_is_not_seen():
    sales = item("Gears of War: E-Day tops sales charts", "https://e.com/1")
    review = item("Gears of War: E-Day review: a tense return", "https://d.com/1")
    sales_cluster = cluster.cluster_items([sales], now=NOW)[0]
    assert sales_cluster["story_type"] == "sales"
    seen = {"clusters": {}}
    cluster.mark_seen(
        seen, sales_cluster["id"], now=NOW, member_urls=[sales["link"]],
        primary_entities=sales_cluster["entities"], story_type=sales_cluster["story_type"],
    )

    clusters = cluster.cluster_items([review], now=NOW, seen=seen)
    assert len(clusters) == 1
    assert clusters[0]["story_type"] == "review"


def test_entity_type_match_allowed_after_cooldown():
    older = NOW - dt.timedelta(days=8)
    covered = cluster.cluster_items([item("Nvidia announces RTX 5090", "https://a.com/1")], now=older)[0]
    seen = {"clusters": {}}
    cluster.mark_seen(
        seen, covered["id"], now=older, member_urls=["https://a.com/1"],
        primary_entities=covered["entities"], story_type=covered["story_type"],
    )

    new = item("Nvidia announces RTX 5090 full details", "https://f.com/2")
    clusters = cluster.cluster_items([new], now=NOW, seen=seen)
    assert len(clusters) == 1
    assert clusters[0]["id"] != covered["id"]


def test_old_format_seen_json_loads_without_error(tmp_path):
    old = {
        "clusters": {
            "abc123": {
                "first_seen": "2026-10-01T00:00:00+00:00",
                "last_seen": "2026-10-01T08:00:00+00:00",
            }
        }
    }
    path = tmp_path / "seen.json"
    path.write_text(json.dumps(old), encoding="utf-8")

    seen = cluster.load_seen(path)
    assert seen["clusters"]["abc123"]["last_seen"] == "2026-10-01T08:00:00+00:00"
    clusters = cluster.cluster_items([item("Nvidia announces RTX 5090", "https://a.com/1")], now=NOW, seen=seen)
    assert len(clusters) == 1


def test_load_pool_tolerates_bad_file(tmp_path):
    path = tmp_path / "pool.json"
    path.write_text("{not json", encoding="utf-8")
    assert cluster.load_pool(path) == {"updated_at": None, "items": []}


def test_pool_file_is_plain_json(tmp_path):
    path = tmp_path / "pool.json"
    cluster.save_pool({"items": []}, now=NOW, path=path)
    assert json.loads(path.read_text(encoding="utf-8"))["updated_at"] == NOW.isoformat()


# --- regression: the e3b478ab26ee cluster must not fuse unrelated games ---
#
# These are the exact headlines that were chained together because title-cased
# words like "Sold", "Copies" and "Million" counted as shared proper nouns.

GEARS_SALES = "Gears of War: E-Day Sold 168,000 Copies on Steam but Xbox Game Pass Players Generated 130% More Revenue"
GEARS_SELLING = "Gears Of War: E-Day–How’s It Selling? Let’s Unpack Things"
GEARS_COURTS = "Gears of War: E-Day reportedly courts over 1.7m Xbox Game Pass players and makes over $37m since launch"
GEARS_REVIEW = "Gears of War: E-Day review"
STAR_WARS = "As Its Developer Recovers, Star Wars Zero Company Reaches One Million Copies Sold"
ACE_COMBAT = "Ace Combat 8 Has Sold One Million Copies Faster Than Any Previous Entry in the Series"
BATTLEFIELD = "Xbox Game Pass Gets Battlefield 6 This Month, Days Before Modern Warfare 4 Launches"


def test_generic_words_are_not_named_entities():
    assert cluster.named_entities("Halo sold a million copies") == set()
    assert "sold" not in cluster.named_entities(GEARS_SALES)
    assert "copies" not in cluster.named_entities(GEARS_SALES)
    assert "million" not in cluster.named_entities(GEARS_SALES)
    assert "review" not in cluster.named_entities(GEARS_REVIEW)
    assert "gears of war e-day" in cluster.named_entities(GEARS_SALES)


def test_story_type_separates_reviews_sales_and_announcements():
    assert cluster.story_type(GEARS_REVIEW) == "review"
    assert cluster.story_type(GEARS_SALES) == "sales"
    assert cluster.story_type(GEARS_SELLING) == "sales"
    assert cluster.story_type(BATTLEFIELD) == "announcement"
    assert cluster.story_type("Forza Horizon 6 delayed to 2027") == "delay"


def test_unrelated_headlines_never_merge():
    unrelated = [GEARS_SALES, STAR_WARS, ACE_COMBAT, BATTLEFIELD, GEARS_REVIEW]
    for index, left in enumerate(unrelated):
        for right in unrelated[index + 1:]:
            left_item = {"title": left, "link": f"https://a.example/{index}"}
            right_item = {"title": right, "link": "https://b.example/0"}
            assert not cluster.same_story(left_item, right_item), (left, right)


def test_same_game_review_and_sales_are_separate_stories():
    sales = {"title": GEARS_SALES, "link": "https://a.example/1"}
    review = {"title": GEARS_REVIEW, "link": "https://a.example/2"}
    assert cluster.shared_entities(sales["title"], review["title"])
    assert cluster.same_story(sales, review) is False


def test_cluster_items_regression_keeps_gears_sales_cluster_only():
    items = [
        item(GEARS_SALES, "https://a.example/sales", owner="techpowerup", tier=2,
             published="2026-10-10T10:00:00+00:00", age_hours=1.0),
        item(GEARS_COURTS, "https://b.example/courts", owner="eurogamer", tier=2,
             published="2026-10-10T09:00:00+00:00", age_hours=2.0),
        item(GEARS_SELLING, "https://c.example/selling", owner="gamespot", tier=2,
             published="2026-10-10T08:00:00+00:00", age_hours=3.0),
        item(STAR_WARS, "https://d.example/starwars", owner="pushsquare", tier=2,
             published="2026-10-10T07:00:00+00:00", age_hours=4.0),
        item(ACE_COMBAT, "https://e.example/ace", owner="pushsquare", tier=2,
             published="2026-10-10T06:00:00+00:00", age_hours=5.0),
        item(BATTLEFIELD, "https://f.example/bf6", owner="techpowerup", tier=2,
             published="2026-10-10T05:00:00+00:00", age_hours=6.0),
        item(GEARS_REVIEW, "https://g.example/review", owner="eurogamer", tier=2,
             published="2026-10-10T04:00:00+00:00", age_hours=7.0),
    ]
    clusters = cluster.cluster_items(items, now=NOW)
    gears_id = cluster.cluster_id_for_urls(
        ["https://a.example/sales", "https://b.example/courts", "https://c.example/selling"]
    )
    earliest = cluster_items_by_id(clusters, gears_id)
    titles = [member["title"] for member in earliest["items"]]
    assert set(titles) == {GEARS_SALES, GEARS_COURTS, GEARS_SELLING}
    count_by_id = {}
    for c in clusters:
        count_by_id[c["id"]] = len(c["items"])
    assert sum(count_by_id.values()) == 7
    assert sum(1 for size in count_by_id.values() if size > 1) == 1


def cluster_items_by_id(clusters, cluster_id):
    return next(c for c in clusters if c["id"] == cluster_id)


def test_cluster_members_must_match_the_seed_not_any_member():
    # "Gears Of War: E-Day–How's It Selling?" joins the sales seed, but the review
    # (whose named entities match, and whose title resembles the member) must not
    # chain in: every member is judged only against the seed item.
    items = [
        item(GEARS_SALES, "https://a.example/sales", published="2026-10-10T10:00:00+00:00", age_hours=1.0),
        item(GEARS_SELLING, "https://b.example/selling", published="2026-10-10T09:00:00+00:00", age_hours=2.0),
        item(GEARS_REVIEW, "https://c.example/review", published="2026-10-10T08:00:00+00:00", age_hours=3.0),
    ]
    clusters = cluster.cluster_items(items, now=NOW)
    sales_id = cluster.cluster_id_for_urls(["https://a.example/sales", "https://b.example/selling"])
    review_id = cluster.cluster_id_for_urls(["https://c.example/review"])
    buckets = {sales_id: set(), review_id: set()}
    for c in clusters:
        for member in c["items"]:
            buckets.setdefault(c["id"], set()).add(member["title"])
    assert buckets[sales_id] == {GEARS_SALES, GEARS_SELLING}
    assert buckets[review_id] == {GEARS_REVIEW}


def test_same_story_headlines_from_different_outlets_still_cluster():
    baldur_a = {"title": "Baldur's Gate 3 Patch 8 is out now", "link": "https://a.example/1", "tier": 2, "owner": "co-a"}
    baldur_b = {"title": "Baldur's Gate 3 Patch 8 released for all platforms", "link": "https://b.example/1", "tier": 2, "owner": "co-b"}
    assert cluster.same_story(baldur_a, baldur_b) is True

    nvidia_a = {"title": "Nvidia announces RTX 5090 at CES 2026", "link": "https://a.example/2", "owner": "co-a"}
    nvidia_b = {"title": "Nvidia announces RTX 5090 at CES", "link": "https://b.example/2", "tier": 2, "owner": "co-b"}
    assert cluster.same_story(nvidia_a, nvidia_b) is True

    clusters = cluster.cluster_items(
        [baldur_a, baldur_b, nvidia_a, nvidia_b, {"title": "Sony delays a PlayStation game", "link": "https://c.example/3", "owner": "co-c"}],
        now=NOW,
    )
    assert len(clusters) == 3
    owners = {c["id"]: c["owners"] for c in clusters}
    assert ["co-a", "co-b"] in owners.values()
