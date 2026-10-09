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


def test_cluster_id_is_stable():
    assert cluster.cluster_id_for("Hello World") == cluster.cluster_id_for("hello, world!")


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

    seen = {"clusters": {nvidia["id"]: {"last_seen": NOW.isoformat()}}}
    remaining = cluster.cluster_items(items, now=NOW, seen=seen)
    assert all(c["id"] != nvidia["id"] for c in remaining)


def test_cooldown_expires():
    older = NOW - dt.timedelta(days=8)
    cid = cluster.cluster_id_for("Nvidia announces RTX 5090")
    seen = {"clusters": {cid: {"last_seen": older.isoformat()}}}
    items = [item("Nvidia announces RTX 5090", "https://a.com/1")]
    clusters = cluster.cluster_items(items, now=NOW, seen=seen)
    assert clusters and clusters[0]["id"] == cid


def test_load_pool_tolerates_bad_file(tmp_path):
    path = tmp_path / "pool.json"
    path.write_text("{not json", encoding="utf-8")
    assert cluster.load_pool(path) == {"updated_at": None, "items": []}


def test_pool_file_is_plain_json(tmp_path):
    path = tmp_path / "pool.json"
    cluster.save_pool({"items": []}, now=NOW, path=path)
    assert json.loads(path.read_text(encoding="utf-8"))["updated_at"] == NOW.isoformat()
