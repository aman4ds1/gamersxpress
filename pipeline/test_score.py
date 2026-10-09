import score


def cluster(title, *, tiers=(1,), owners=("a",), age_hours=2.0, entities=()):
    return {
        "id": "c1",
        "title": title,
        "items": [],
        "entities": list(entities),
        "tiers": list(tiers),
        "owners": list(owners),
        "age_hours": age_hours,
    }


def test_guess_category_by_keyword_and_entity():
    assert score.guess_category({"title": "PS5 Pro restock", "entities": []}) == "playstation"
    assert score.guess_category({"title": "Something", "entities": ["Nvidia"]}) == "hardware"
    assert score.guess_category({"title": "A quiet week", "entities": []}) == "gaming-news"


def test_score_rewards_tier_recency_and_owners():
    strong = score.score_cluster(cluster("x", tiers=(1,), owners=("a", "b", "c"), age_hours=2))
    weak = score.score_cluster(cluster("x", tiers=(3,), owners=("a",), age_hours=100))
    assert strong > weak
    assert strong >= 40 + 30 + 3 * 10


def test_score_uses_performance_boost():
    base = score.score_cluster(cluster("PS5 Pro restock"))
    boosted = score.score_cluster(cluster("PS5 Pro restock"), performance={"playstation": 15})
    assert boosted == base + 15


def test_pick_returns_none_below_min_score():
    clusters = [cluster("x", tiers=(3,), owners=("a",), age_hours=100)]
    assert score.pick(clusters, min_score=50) is None


def test_pick_returns_top_cluster_with_category_and_score():
    clusters = [
        cluster("Nvidia announces RTX 5090", tiers=(1,), owners=("a", "b")),
        cluster("Sony quarterly results", tiers=(3,), owners=("c",), age_hours=90),
    ]
    chosen = score.pick(clusters, min_score=50)
    assert chosen is not None
    assert chosen["category"] == "hardware"
    assert chosen["score"] >= 50


def test_load_performance_missing_file(tmp_path):
    assert score.load_performance(tmp_path / "nope.json") == {}


def test_load_performance_filters_non_numbers(tmp_path):
    path = tmp_path / "perf.json"
    path.write_text('{"playstation": 12, "pc": "nope"}', encoding="utf-8")
    assert score.load_performance(path) == {"playstation": 12.0}
