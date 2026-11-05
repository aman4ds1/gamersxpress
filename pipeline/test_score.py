import re
import time

import pytest

import cluster as cluster_mod
import score


def cluster(title, *, tiers=(1,), owners=("a",), age_hours=2.0, entities=(), items=None, source_names=()):
    if items is None:
        items = [{"source_name": name} for name in source_names]
    return {
        "id": "c1",
        "title": title,
        "items": [dict(item) for item in (items or [])],
        "entities": list(entities),
        "tiers": list(tiers),
        "owners": list(owners),
        "age_hours": age_hours,
    }


def two_source_cluster(title, *, tiers=(1,), owners=("a",), age_hours=2.0, entities=(),
                       source_names=(), second_owner=None):
    """A cluster with two distinct source URLs (the ``content_limits.min_sources``
    minimum) that also has two distinct source owners, so it clears both the
    source floor and the ``score.min_distinct_owners`` owner floor. The cluster's
    ``owners`` field is left exactly as passed (scoring and confirmability stay as
    the test intended); only the per-item owner of the second URL is made distinct.
    Pass ``second_owner=...`` to control it, or build with :func:`cluster` when the
    test is about the owner gate itself.
    """
    names = list(source_names)
    first_name = names[0] if names else "Outlet A"
    second_name = names[1] if len(names) > 1 else first_name
    first_owner = owners[0] if owners else "a"
    if second_owner is None:
        second_owner = owners[1] if len(owners) > 1 else f"{first_owner}-b"
    items = [
        {"source_name": first_name, "link": "https://a.example/1", "owner": first_owner},
        {"source_name": second_name, "link": "https://b.example/1", "owner": second_owner},
    ]
    return cluster(
        title, tiers=tiers, owners=owners, age_hours=age_hours, entities=entities, items=items,
    )


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
        two_source_cluster("Nvidia announces RTX 5090", tiers=(1,), owners=("a", "b")),
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


EXCLUDE_PATTERNS = ["podcast", "walkthrough", "deal", "savings", "how to"]


def test_playstation_podcast_headline_is_excluded():
    c = two_source_cluster("Official PlayStation Podcast Episode 550: Now This is Podcasting",
                           tiers=(1,), owners=("sony",))
    ranked = score.rank([c], exclude_patterns=EXCLUDE_PATTERNS)
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["excluded_by"] == "podcast"
    assert ranked[0]["eligible"] is False
    assert score.ineligible_reason(c, EXCLUDE_PATTERNS) == "excluded pattern: 'podcast'"


def test_three_walkthroughs_from_one_owner_are_not_confirmable():
    items = [{"link": f"https://pcgamer.example/{i}", "owner": "future"} for i in range(3)]
    c = cluster(
        "How to complete Tracks of the Beast in Arc Raiders",
        items=items, tiers=(2,), owners=("future",),
    )
    ranked = score.rank([c], exclude_patterns=EXCLUDE_PATTERNS)
    assert ranked[0]["confirmable"] is False
    assert ranked[0]["eligible"] is False
    assert score.ineligible_reason(c, EXCLUDE_PATTERNS) == "not confirmable"


def test_single_tier2_source_is_not_confirmable():
    c = cluster("Squadron 42 has a Steam page", tiers=(2,), owners=("a",))
    assert cluster_mod.confirmable(c) is False
    assert score.ineligible_reason(c, EXCLUDE_PATTERNS) == "not confirmable"


def test_single_tier1_source_is_not_eligible():
    # A lone tier-1 announcement is confirmable but no longer coverable: content
    # limits need at least two distinct source URLs before a story is gathered.
    c = cluster("Nvidia announces RTX 5090", tiers=(1,), owners=("a",),
                items=[{"link": "https://a.example/1"}])
    assert cluster_mod.confirmable(c) is True
    assert score.source_count(c) == 1
    assert score.ineligible_reason(c, EXCLUDE_PATTERNS) == "too few sources"
    assert score.rank([c], exclude_patterns=EXCLUDE_PATTERNS)[0]["eligible"] is False


def test_one_source_cluster_is_never_eligible():
    c = cluster("Nvidia announces RTX 5090", tiers=(1,), owners=("a", "b"),
                items=[{"link": "https://a.example/1"}])
    assert score.source_count(c) == 1
    assert score.ineligible_reason(c) == "too few sources"
    assert score.rank([c])[0]["eligible"] is False


def test_two_tier2_sources_from_different_owners_are_eligible():
    c = two_source_cluster("Xbox and PlayStation US sales sink", tiers=(2,), owners=("a", "b"))
    assert cluster_mod.confirmable(c) is True
    assert score.ineligible_reason(c, EXCLUDE_PATTERNS) is None
    assert score.rank([c], exclude_patterns=EXCLUDE_PATTERNS)[0]["eligible"] is True


def test_tier1_cluster_with_excluded_pattern_is_excluded():
    c = two_source_cluster("GIGABYTE deals and savings roundup", tiers=(1,), owners=("a", "b"))
    ranked = score.rank([c], exclude_patterns=EXCLUDE_PATTERNS)
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["excluded_by"] == "deal"
    assert ranked[0]["eligible"] is False
    assert score.ineligible_reason(c, EXCLUDE_PATTERNS) == "excluded pattern: 'deal'"


# --- whole-word format/promotion exclusions (shipped config + defaults) ------


def test_licensing_deal_is_not_excluded():
    c = two_source_cluster("Microsoft's Xbox licensing deal with Sony", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["eligible"] is True


def test_plural_licensing_deals_is_not_excluded():
    # The bare "\bdeals\b" pattern used to catch this; only exact promo phrases
    # like "best deals" are excluded now, so a licensing story is not filtered.
    c = two_source_cluster("Sony and Microsoft sign new licensing deals",
                tiers=(1,), owners=("a",), source_names=("Eurogamer",))
    ranked = score.rank([c])
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["excluded_by"] is None
    assert ranked[0]["eligible"] is True


def test_best_deals_is_excluded():
    c = cluster("Best deals on gaming PCs", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert "best deals" in ranked[0]["excluded_by"]
    assert ranked[0]["eligible"] is False
    assert ranked[0]["excluded_by"] is not None


def test_podcast_episode_is_excluded():
    c = cluster("Official PlayStation Podcast Episode 550", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is False
    assert ranked[0]["excluded_by"] is not None


def test_percent_off_is_excluded():
    c = cluster("Up to 40% off Xbox controllers", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is False
    assert ranked[0]["excluded_by"] is not None


def test_number_percent_off_is_excluded():
    c = cluster("Save 40% off select games", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert "% off" in ranked[0]["excluded_by"]
    assert ranked[0]["eligible"] is False


def test_up_to_percent_off_is_excluded():
    c = cluster("Up to 28% Off!", tiers=(1,), owners=("a",), source_names=("Eurogamer",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert ranked[0]["eligible"] is False


def test_weekly_active_users_is_not_excluded():
    c = two_source_cluster("Steam weekly active users hit a record", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["excluded_by"] is None
    assert ranked[0]["eligible"] is True


def test_acquisition_deal_is_not_excluded():
    c = two_source_cluster("Sega buys game studio in $200M deal", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["excluded_by"] is None
    assert ranked[0]["eligible"] is True


# --- whole-word exclusion adjustments shipped in the last pass ---------------


def test_free_weekend_is_no_longer_excluded():
    c = two_source_cluster("Arc Raiders free weekend on Steam", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is None
    assert ranked[0]["eligible"] is True


def test_free_this_weekend_is_excluded():
    c = cluster("Game X is free this weekend on Steam", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert ranked[0]["eligible"] is False


def test_free_play_days_is_excluded():
    c = cluster("Free Play Days — ARC Raiders arrives on Game Pass", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert ranked[0]["eligible"] is False
    assert "play days" in ranked[0]["excluded_by"]


def test_earnings_report_is_excluded():
    c = cluster("AMD to Report Fiscal Third Quarter 2026 Financial Results", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is False
    assert "to report" in (ranked[0]["excluded_by"] or "")


def test_conference_call_is_excluded():
    c = cluster("Earnings conference call set for next week", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is False
    assert "conference call" in (ranked[0]["excluded_by"] or "")


# --- topical relevance: gaming topic vs mixed/unknown + keyword gate ----------


def test_xbox_wire_dmz_headline_is_on_topic():
    c = two_source_cluster(
        "DMZ — 5 Features to Get Excited for in Call of Duty: Modern Warfare 4's New Mode",
        source_names=("Xbox Wire",),
    )
    ranked = score.rank([c])
    assert ranked[0]["relevant"] is True
    assert ranked[0]["eligible"] is True
    assert score.ineligible_reason(c) is None


def test_nvidia_blog_telecom_ai_is_off_topic():
    c = two_source_cluster(
        "Why Telecom Operators Are Building Their AI Strategy on Open Models",
        source_names=("NVIDIA Blog",),
    )
    ranked = score.rank([c])
    assert ranked[0]["relevant"] is False
    assert ranked[0]["eligible"] is False
    assert score.ineligible_reason(c) == "off-topic"


def test_amd_newsroom_award_is_off_topic():
    c = cluster("AMD CEO Lisa Su Receives National Medal of Science", source_names=("AMD Newsroom",))
    ranked = score.rank([c])
    assert ranked[0]["relevant"] is False
    assert ranked[0]["eligible"] is False


def test_gaming_source_without_keyword_is_on_topic():
    c = two_source_cluster("Everything you need to know", source_names=("Eurogamer",))
    ranked = score.rank([c])
    assert ranked[0]["relevant"] is True
    assert ranked[0]["eligible"] is True
    assert score.ineligible_reason(c) is None


def test_same_headline_from_mixed_source_is_off_topic():
    c = two_source_cluster("Everything you need to know", source_names=("Tom's Hardware",))
    ranked = score.rank([c])
    assert ranked[0]["relevant"] is False
    assert ranked[0]["eligible"] is False
    assert score.ineligible_reason(c) == "off-topic"


def test_telecom_ai_headline_is_off_topic_without_gaming_source():
    c = cluster("Why Telecom Operators Are Turning to AI Agents")
    ranked = score.rank([c])
    assert ranked[0]["relevant"] is False
    assert ranked[0]["eligible"] is False


def test_graphics_card_phrase_is_a_relevance_keyword():
    c = two_source_cluster("Nvidia confirms next graphics card launch window")
    assert score.relevant_cluster(c) is True
    assert score.rank([c])[0]["eligible"] is True


def test_pull_up_to_7_eleven_in_forza_is_not_eligible():
    c = two_source_cluster("Pull Up to 7-Eleven in Forza Horizon 6")
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is False
    assert score.ineligible_reason(c) == "off-topic"


# --- news-value bonus + min_score 65 ----------------------------------------


def test_news_signal_adds_the_full_bonus():
    quiet = cluster("Warhammer Survivors quietly updates its roadmap", source_names=("Eurogamer",))
    launch = cluster("Warhammer Survivors launches November 10, new characters revealed",
                     source_names=("Eurogamer",))
    assert score.score_cluster(launch) == score.score_cluster(quiet) + 10


def test_news_signal_pushes_single_stale_source_over_threshold():
    quiet = two_source_cluster("Warhammer Survivors quietly updates its roadmap",
                    source_names=("Eurogamer",), age_hours=50)
    launch = two_source_cluster("Warhammer Survivors launches November 10, new characters revealed",
                     source_names=("Eurogamer",), age_hours=50)
    assert score.score_cluster(quiet) == 60
    assert score.score_cluster(launch) == 70
    assert score.pick([quiet]) is None
    assert score.pick([launch]) is not None


def test_warhammer_launch_is_eligible_with_news_bonus():
    c = two_source_cluster("Warhammer Survivors launches November 10, new characters revealed",
                source_names=("Eurogamer",))
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is True
    assert ranked[0]["score"] == 100
    assert score.pick([c]) is not None


# --- date signal (same +10 bonus) and expanded verbs -------------------------


def test_date_signal_matches_month_then_day():
    assert score.has_news_signal("Rockbeasts arrives November 18", ()) is True
    assert score.has_news_signal("Rockbeasts Nov 18", ()) is True
    assert score.has_news_signal("Rockbeasts sept 5", ()) is True


def test_date_signal_matches_day_then_month():
    assert score.has_news_signal("Rockbeasts 18 November", ()) is True
    assert score.has_news_signal("Rockbeasts 18 Nov", ()) is True


def test_date_signal_ignores_bare_month_and_year():
    assert score.has_news_signal("Critical Role joins Rockbeasts, launching January 2027", ()) is False
    assert score.has_news_signal("A quiet month ahead", ()) is False
    assert score.has_news_signal("March into battle", ()) is False


def test_several_signals_add_the_bonus_once():
    both = cluster("Rockbeasts launches November 18", tiers=(1,), owners=("a",), age_hours=50)
    assert score.score_cluster(both) == 70.0


def test_monster_hunter_heading_to_xbox_november_is_eligible():
    c = two_source_cluster(
        "Monster Hunter Stories 3: Twisted Reflection Heading to XBOX on PC November 18",
        tiers=(1,), owners=("a",), age_hours=50,
    )
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is True
    assert ranked[0]["score"] == 70
    assert score.pick([c]) is not None


def test_critical_role_joins_launching_january_is_eligible():
    c = two_source_cluster(
        "Critical Role joins Rockbeasts, launching January 2027",
        tiers=(1,), owners=("a",), age_hours=50, source_names=("Eurogamer",),
    )
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is True
    assert ranked[0]["score"] == 70
    assert score.pick([c]) is not None


def test_coming_to_xbox_game_pass_is_still_excluded():
    c = two_source_cluster("Coming to XBOX Game Pass: Battlefield 6, Valor Mortis, Forever Ago, and More",
                tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert ranked[0]["eligible"] is False
    assert score.ineligible_reason(c).startswith("excluded pattern")


# --- new whole-phrase exclusions --------------------------------------------


def test_rally_up_on_geforce_now_is_excluded():
    c = two_source_cluster("Rally Up: 'Gears of War: E-Day' Launches on GeForce NOW",
                tiers=(1,), owners=("nvidia",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert ranked[0]["eligible"] is False
    assert "geforce now" in ranked[0]["excluded_by"]
    assert score.ineligible_reason(c).startswith("excluded pattern")


def test_players_choice_is_excluded():
    c = two_source_cluster("Players' Choice: Vote for September's best new game",
                tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert ranked[0]["eligible"] is False
    assert "players' choice" in ranked[0]["excluded_by"]
    assert score.ineligible_reason(c).startswith("excluded pattern")


# --- corroborated threshold (min_score_corroborated) -------------------------


def test_two_owner_cluster_at_corroborated_threshold_is_picked():
    c = two_source_cluster("A quiet story reported by two outlets",
                tiers=(2,), owners=("a", "b"), age_hours=48, source_names=("Eurogamer",))
    assert score.score_cluster(c) == 55
    assert score.min_score_for(c) == 55
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is True
    assert score.pick([c]) is not None


def test_single_owner_cluster_below_min_score_is_not_picked():
    c = two_source_cluster("A quiet story from one outlet",
                tiers=(1,), owners=("a",), age_hours=50, source_names=("Eurogamer",))
    assert score.score_cluster(c) == 60
    assert score.min_score_for(c) == 65
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is True
    assert score.pick([c]) is None


# --- item-promo exclusions: the exact headline that got published by mistake --


def test_shattered_relay_preorder_blueprint_headline_is_excluded():
    c = two_source_cluster(
        "Get the Shattered Relay Weapon Blueprint in Call of Duty: Mobile as a "
        "Call of Duty: Modern Warfare 4 Pre-order Bonus",
        source_names=("Xbox Wire",),
    )
    assert score.source_count(c) == 2
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is False
    assert ranked[0]["excluded_by"] is not None
    assert score.ineligible_reason(c).startswith("excluded pattern")


def test_marvel_tokon_dlc_roster_is_still_eligible():
    c = two_source_cluster(
        "Marvel Tōkon: Fighting Souls reveals Year 1 DLC roster",
        source_names=("Eurogamer",),
    )
    assert score.ineligible_reason(c) is None
    assert score.rank([c])[0]["eligible"] is True


# --- countable source floor ---------------------------------------------------


def test_fetch_false_and_tier3_items_do_not_count_toward_source_floor():
    c = cluster(
        "Halo returns to Xbox Game Pass",
        items=[
            {"link": "https://a.example/1", "tier": 1},
            {"link": "https://b.example/1", "tier": 3},
            {"link": "https://c.example/1", "tier": 2, "fetchable": False},
        ],
    )
    assert score.source_urls(c) == ["https://a.example/1"]
    assert score.source_count(c) == 1
    assert score.ineligible_reason(c, min_sources=2) == "too few sources"


def test_items_without_fetchable_field_still_count():
    c = cluster(
        "Nvidia announces RTX 5090",
        items=[
            {"link": "https://a.example/1", "owner": "a"},
            {"link": "https://b.example/1", "owner": "b"},
        ],
    )
    assert score.source_count(c) == 2
    assert score.owner_count(c) == 2
    assert score.ineligible_reason(c, min_sources=2) is None


# --- item-level exclusion before clustering ----------------------------------


def test_filter_excluded_items_drops_matching_and_keeps_the_rest():
    items = [
        {"title": "Nvidia announces RTX 5090", "link": "https://a.example/1"},
        {"title": "Best deals on gaming PCs this week", "link": "https://b.example/1"},
    ]
    kept = score.filter_excluded_items(items, ["best deals"])
    assert [item["link"] for item in kept] == ["https://a.example/1"]


def test_filter_excluded_items_cannot_raise_a_cluster_source_count():
    items = [
        {"title": "Nvidia announces RTX 5090", "link": "https://a.example/1", "tier": 1, "owner": "a"},
        {"title": "Best deals on gaming PCs this week", "link": "https://b.example/1", "tier": 1, "owner": "b"},
    ]
    kept = score.filter_excluded_items(items, ["best deals"])
    c = cluster("Nvidia announces RTX 5090", items=kept, tiers=(1,), owners=("a",))
    assert score.source_count(c) == 1


def test_filter_excluded_items_reports_each_pattern_once():
    seen = []
    items = [
        {"title": "Save 20% now", "link": "https://a.example/1"},
        {"title": "Save 30% today", "link": "https://b.example/1"},
    ]
    score.filter_excluded_items(items, ["save \\d+%"], on_excluded=lambda pattern, count: seen.append((pattern, count)))
    assert seen == [("save \\d+%", 2)]


def test_default_max_candidates_per_run_is_five():
    assert score.DEFAULT_MAX_CANDIDATES_PER_RUN == 5


# --- pick_candidates: one ranking pass per run --------------------------------


def _confirmable_cluster(index, *, owners=("a", "b")):
    c = two_source_cluster(
        f"Nvidia announces RTX {7000 + index}", tiers=(1,), owners=owners
    )
    c["id"] = f"c{index}"
    return c


def test_rank_scores_three_hundred_clusters_quickly():
    clusters = [_confirmable_cluster(i) for i in range(300)]
    started = time.perf_counter()
    ranked = score.rank(clusters, exclude_patterns=EXCLUDE_PATTERNS)
    elapsed = time.perf_counter() - started
    assert len(ranked) == 300
    # With config.yaml parsed once per rank (not once per cluster) this is a
    # fraction of a second; the old per-cluster config read took ~20s.
    assert elapsed < 5.0


def test_pick_candidates_ranks_once_per_run(monkeypatch):
    calls = {"n": 0}
    real_rank = score.rank

    def counting_rank(*args, **kwargs):
        calls["n"] += 1
        return real_rank(*args, **kwargs)

    monkeypatch.setattr(score, "rank", counting_rank)
    clusters = [_confirmable_cluster(i) for i in range(4)]
    picked = score.pick_candidates(clusters, max_candidates=3, min_score=50)
    assert len(picked) == 3
    assert calls["n"] == 1


def test_pick_candidates_matches_repeated_pick():
    clusters = [
        _confirmable_cluster(0),
        _confirmable_cluster(1),
        _confirmable_cluster(2),
        _confirmable_cluster(3),
    ]
    expected: list[str] = []
    remaining = list(clusters)
    while len(expected) < 3:
        chosen = score.pick(remaining, min_score=50)
        if chosen is None:
            break
        expected.append(chosen["id"])
        remaining = [c for c in remaining if c.get("id") != chosen["id"]]
    got = [
        c["id"] for c in score.pick_candidates(clusters, max_candidates=3, min_score=50)
    ]
    assert got == expected


def test_pick_candidates_returns_empty_when_nothing_clears_threshold():
    clusters = [cluster("x", tiers=(3,), owners=("a",), age_hours=100)]
    assert score.pick_candidates(clusters, min_score=50) == []


def test_pick_candidates_caps_candidates_sharing_a_lead_entity():
    # Three GTA 6 clusters in one run fill only two slots; the per-entity cap is
    # keyed on the lead entity, so three similar headlines cannot crowd out the
    # rest of the run.
    clusters = [
        two_source_cluster("GTA 6 PC release reportedly targets early 2027", tiers=(1,), owners=("a", "b")),
        two_source_cluster("GTA 6 coming to PC in Q1 2027", tiers=(1,), owners=("a", "b")),
        two_source_cluster("Take-Two says GTA 6 is on track", tiers=(1,), owners=("a", "b")),
        two_source_cluster("Nvidia announces RTX 6090", tiers=(1,), owners=("a", "b")),
    ]
    picked = score.pick_candidates(
        clusters, max_candidates=5, max_candidates_per_entity=2, min_score=0,
        min_score_corroborated=0,
    )
    titles = [c["title"] for c in picked]
    assert sum("GTA 6" in title for title in titles) == 2
    assert "Nvidia announces RTX 6090" in titles


def test_pick_candidates_treats_different_lead_entities_as_separate():
    # A story keyed on a person and one keyed on a specific division are not the
    # same lead entity, even though both are about Xbox, so neither counts
    # against the other. "xbox" is an umbrella entity and never a lead key.
    clusters = [
        two_source_cluster(
            "Xbox announces what sounds like a generative AI-focused game-making "
            "department, Xbox Labs, while avoiding mentioning AI",
            tiers=(1,), owners=("a", "b"),
        ),
        two_source_cluster(
            "Xbox CEO Asha Sharma reportedly says we will not survive without new IPs",
            tiers=(1,), owners=("a", "b"),
        ),
    ]
    picked = score.pick_candidates(
        clusters, max_candidates=5, max_candidates_per_entity=1, min_score=0,
        min_score_corroborated=0,
    )
    assert len(picked) == 2



# --- affiliate / listicle format exclusions ----------------------------------


def test_default_exclusions_cover_affiliate_and_listicle_formats():
    patterns = score.DEFAULT_EXCLUDE_TITLE_PATTERNS
    excluded = [
        "one of these 12 cosy winter gaming upgrades",
        "You'll be needing one of these 12 cosy winter gaming upgrades",
        "The best gift guide for gamers",
        "Our buying guide to the top controllers",
        "best PC upgrades for 2026",
        "The best gaming accessories under $50",
        "best Black Friday deals",
    ]
    for title in excluded:
        assert cluster_mod.excluded_title_pattern(title, patterns) is not None, title


def test_default_exclusions_do_not_match_real_news_headlines():
    patterns = score.DEFAULT_EXCLUDE_TITLE_PATTERNS
    kept = [
        "Microsoft's licensing deal with Sony",
        "Best graphics cards for 1440p",
        "Phoenix Cyclops joins MARVEL T\u014dkon: Fighting Souls on November 2",
    ]
    for title in kept:
        assert cluster_mod.excluded_title_pattern(title, patterns) is None, title


def test_affiliate_listicle_headline_is_filtered_out():
    title = (
        "Freezing your bots off in Arc Raiders Frozen Trail? You'll be needing "
        "one of these 12 cosy winter gaming upgrades"
    )
    kept = score.filter_excluded_items(
        [{"title": title, "link": "https://a.example/1"}],
        score.DEFAULT_EXCLUDE_TITLE_PATTERNS,
    )
    assert kept == []


# --- distinct-owner floor (score.min_distinct_owners) -------------------------


def test_two_urls_from_one_owner_are_not_eligible():
    c = cluster(
        "Nvidia announces RTX 5090",
        tiers=(1,),
        owners=("nvidia",),
        items=[
            {"link": "https://a.example/1", "owner": "nvidia"},
            {"link": "https://b.example/1", "owner": "nvidia"},
        ],
    )
    assert score.source_count(c) == 2
    assert score.owner_count(c) == 1
    assert score.ineligible_reason(c, min_sources=2) == "too few independent owners"
    assert score.rank([c], min_sources=2)[0]["eligible"] is False


def test_tier1_plus_second_owner_is_eligible():
    c = cluster(
        "Xbox and PlayStation US sales sink",
        tiers=(1, 2),
        owners=("microsoft", "sony"),
        items=[
            {"link": "https://a.example/1", "owner": "microsoft"},
            {"link": "https://b.example/1", "owner": "sony"},
        ],
    )
    assert score.owner_count(c) == 2
    assert score.ineligible_reason(c, min_sources=2) is None
    assert score.rank([c], min_sources=2)[0]["eligible"] is True


def test_owner_count_ignores_unfetchable_and_tier3_items():
    c = cluster(
        "Nvidia announces RTX 5090",
        items=[
            {"link": "https://a.example/1", "owner": "a"},
            {"link": "https://b.example/1", "owner": "b", "fetchable": False},
            {"link": "https://c.example/1", "owner": "c", "tier": 3},
        ],
    )
    assert score.source_count(c) == 1
    assert score.owner_count(c) == 1
    assert score.ineligible_reason(c, min_sources=1) == "too few independent owners"


def test_load_min_distinct_owners_reads_config_and_defaults():
    assert score.load_min_distinct_owners({}) == score.DEFAULT_MIN_DISTINCT_OWNERS
    assert score.load_min_distinct_owners({"score": {"min_distinct_owners": 1}}) == 1
    assert score.load_min_distinct_owners({"score": {"min_distinct_owners": "x"}}) == (
        score.DEFAULT_MIN_DISTINCT_OWNERS
    )


# --- blocked topics (score.blocked_topics) --------------------------------------------

EXPECTED_TOPIC_GROUPS = {
    "lawsuits_and_regulation", "backlash_and_controversy", "labor_and_layoffs", "politics",
    "misconduct_allegations", "guides_and_service_posts",
    "roundups_and_newsletters", "non_gaming_enterprise",
}
#: Politics and layoffs are covered when the story is about the gaming scene or gaming hardware.
CONDITIONAL_GROUPS = {"labor_and_layoffs", "politics"}

#: (group, headline): headlines the shipped patterns must match. For the conditional
#: groups this is the pattern check only; whether the story is gaming-related is
#: decided separately (see GAMING_SCENE_COVERED below).
BLOCKED_HEADLINES = [
    ("lawsuits_and_regulation", "Elder Scrolls Skyblivion mod faces lawsuit from former volunteer"),
    ("lawsuits_and_regulation", "Nintendo sues Palworld developer over patent claims"),
    ("lawsuits_and_regulation", "UK regulators clear Xbox cloud gaming deal"),
    ("lawsuits_and_regulation", "FTC challenges the console maker's studio acquisition"),
    ("backlash_and_controversy", "Dead by Daylight removes free DLC following backlash"),
    ("backlash_and_controversy", "Behaviour Interactive Addresses Dead by Daylight Controversy"),
    ("backlash_and_controversy", "Dead by Daylight Fans Are Uninstalling the Game in Protest"),
    ("backlash_and_controversy", "Fans call for a boycott of the new Call of Duty"),
    ("backlash_and_controversy", "Steam game hit by review bombing after price hike"),
    ("labor_and_layoffs", "Doom Director Says We're Doing Great After Half Of id Software Was Laid Off"),
    ("labor_and_layoffs", "Microsoft announces new Xbox layoffs"),
    ("labor_and_layoffs", "Raven Software QA workers vote to unionize"),
    ("labor_and_layoffs", "SAG-AFTRA video game actors reach a new agreement"),
    ("labor_and_layoffs", "Ubisoft staff stage walkout over return-to-office"),
    ("labor_and_layoffs", "Intel lays off 500 staff in its CPU division"),
    ("politics", "Trump administration weighs new video game violence rules"),
    ("politics", "Senators question Roblox over child safety"),
    ("politics", "AI data centers defeated at US midterm elections"),
    ("politics", "Nvidia chip export controls tighten again"),
    ("politics", "Putin demands Russian data centers supply capacity to the state"),
    ("misconduct_allegations", "Former Activision exec accused of harassment"),
    ("misconduct_allegations", "Former Xbox COO denies being fired for policy violations"),
    ("misconduct_allegations", "New allegations surface against esports league owner"),
    ("guides_and_service_posts", "Wordle Hints and Answer for November 4, 2026: Game #1964"),
    ("guides_and_service_posts", "NYT Connections Hints Today (#1242): Answers for Wednesday"),
    ("guides_and_service_posts", "How to Play GTA 6 Early on PS5 With the New Zealand Trick"),
    ("guides_and_service_posts", "Genshin Impact: Imaginarium Theater 7.2 Guide (November 2026)"),
    ("guides_and_service_posts", "Fortnite redeem codes for November 2026"),
    ("guides_and_service_posts", "Where to buy the Switch 2 OLED right now"),
    ("guides_and_service_posts", "Best gaming laptops of 2026"),
    ("roundups_and_newsletters", "This week in PC games: go helldiving in Gravhounds"),
    ("roundups_and_newsletters", "Weekly roundup: Xbox, PlayStation and Nintendo news"),
    ("roundups_and_newsletters", "The GamesRadar newsletter returns"),
    ("non_gaming_enterprise", "Intel Diverts 18A Capacity From Panther Lake to Meet Xeon Demand"),
    ("non_gaming_enterprise", "AMD EPYC Venice CPUs reach tape-out"),
    ("non_gaming_enterprise", "Creator says Amazon closed his account after he bought seven Nvidia DGX Sparks"),
    ("non_gaming_enterprise", "Quantum computing startup partners with Nvidia"),
    ("non_gaming_enterprise", "Samsung unveils an enterprise SSD for AI servers"),
]

#: Gaming news that shares words with a blocked topic and must stay eligible.
ALLOWED_HEADLINES = [
    "Counter-Strike 2 update adds new Dust 2 changes",           # "strike" is only labor as "on strike"
    "Doug Bowser steps down as Nintendo of America president",   # industry titles, not politics
    "Assault Android Cactus 2 launches on Switch",               # "assault" is a game-title word
    "Xbox raises console prices in the US as tariffs bite",      # price rises are hardware news
    "Valve announces Steam Deck 2 release date",
    "NVIDIA launches GeForce RTX 5090 Super with 32GB of GDDR7",
    "Zelda: Ocarina of Time Remake Reviews Make It 2026's Highest Rated Game",  # news about reviews
    "The Ocarina of Time remake has claimed 2026's review crown",
    "Steam Making Big Changes to Its Game Review Process",
    # Game and gaming hardware reviews are covered.
    "Enshrouded 1.0 review",
    "Castlevania: Belmont's Curse: The Kotaku Review",
    "The Legend of Zelda: Ocarina of Time Is Still Perfect on Switch 2 | Review",
    "Hollow Knight: Silksong review in progress",
    "Biwin M560 2TB SSD Review: The DRAM-less drive to beat",
    "Hardware Review: SCUF Omega PS5 Controller",
    "Hands-on with the DockCase Mount Ling 2 Pro",
    "Windows 11 Preview adds Cloud Rebuild to reinstall OS and drivers",
    "AI data center demand pushes GPU and RAM prices up for PC gamers",
    "Microsoft appoints a new CEO of Microsoft Gaming",
    "Phantom Blade Zero sells 4 million copies in 72 hours",
    "Call of Duty Warzone Season 1 officially launches this Friday",
    "Summer Game Fest 2026 date announced",
    "PlayStation State of Play returns next week with new announcements",
    "Hollow Knight: Silksong wins Game of the Year",
    "Elden Ring Nightreign adds a new Nightfarer",
    "Nintendo Switch 2 sales top 20 million",
]

#: Layoffs and politics stories about the gaming scene or gaming hardware: covered.
GAMING_SCENE_COVERED = [
    "Microsoft announces new Xbox layoffs",
    "Doom Director Says We're Doing Great After Half Of id Software Was Laid Off",
    "Raven Software QA workers vote to unionize",
    "SAG-AFTRA video game actors reach a new agreement",
    "Trump administration weighs new video game violence rules",
    "New export controls could hit GeForce RTX 5090 supply",
    "Senators question Roblox over child safety in games",
]

#: The same topics with no link to gaming: skipped.
NOT_GAMING_SKIPPED = [
    "Intel lays off 500 staff in its CPU division",
    "Nvidia chip export controls tighten again for China",
    "AI data centers defeated at US midterm elections",
    "MAGA clings to Darth Vader as it braces for election results",
    "AI chip smugglers exploit trade control loophole to bring banned HBM into China",
]


def test_the_shipped_config_blocks_the_expected_gaming_topics():
    topics = score.load_blocked_topics()
    assert {group for group, *_rest in topics} == EXPECTED_TOPIC_GROUPS
    for _group, pattern, _unless in topics:
        re.compile(pattern)  # a pattern that does not compile silently becomes a substring match


def test_only_politics_and_layoffs_are_lifted_for_the_gaming_scene():
    conditional = {group for group, _pattern, unless_gaming in score.load_blocked_topics() if unless_gaming}
    assert conditional == CONDITIONAL_GROUPS


@pytest.mark.parametrize("group, headline", BLOCKED_HEADLINES)
def test_a_blocked_headline_names_its_topic(group, headline):
    hit = cluster_mod.blocked_topic(headline, score.load_blocked_topics())
    assert hit is not None, headline
    assert hit[0] == group


@pytest.mark.parametrize("headline", ALLOWED_HEADLINES)
def test_gaming_news_that_shares_a_word_is_not_blocked(headline):
    assert cluster_mod.blocked_topic(headline, score.load_blocked_topics()) is None


@pytest.mark.parametrize("group, headline", [
    (group, headline) for group, headline in BLOCKED_HEADLINES if group not in CONDITIONAL_GROUPS
])
def test_the_other_groups_block_even_a_gaming_story(group, headline):
    hit = cluster_mod.blocked_topic(headline, score.load_blocked_topics(), gaming_scene=True)
    assert hit is not None and hit[0] == group


@pytest.mark.parametrize("headline", GAMING_SCENE_COVERED)
def test_politics_and_layoffs_about_gaming_are_covered(headline):
    topics = score.load_blocked_topics()
    assert cluster_mod.blocked_topic(headline, topics) is not None   # the pattern matches ...
    assert cluster_mod.blocked_topic(headline, topics, gaming_scene=True) is None  # ... and is lifted


def test_every_topic_group_is_exercised_by_a_blocked_headline():
    assert {group for group, _headline in BLOCKED_HEADLINES} == EXPECTED_TOPIC_GROUPS


def test_load_blocked_topics_reads_both_group_forms_in_order():
    config = {"score": {"blocked_topics": {
        # Blank entries are dropped: an empty regex would match every headline.
        "alpha": ["one", "", "  ", None, "two"],
        "beta": {"unless_gaming": True, "patterns": ["three"]},
        "gamma": {"patterns": ["four"]},
        "skipped": "not a list", "empty": [], "bad_mapping": {"unless_gaming": True, "patterns": "x"},
    }}}
    assert score.load_blocked_topics(config) == [
        ("alpha", "one", False), ("alpha", "two", False),
        ("beta", "three", True), ("gamma", "four", False),
    ]


@pytest.mark.parametrize("config", [
    {}, {"score": {}}, {"score": None}, {"score": {"blocked_topics": None}},
    {"score": {"blocked_topics": ["not", "a", "mapping"]}},
])
def test_a_missing_or_malformed_topics_block_blocks_nothing(config):
    assert score.load_blocked_topics(config) == []


def test_gaming_scene_keywords_come_from_the_config_and_leave_out_generic_hardware():
    keywords = score.load_gaming_scene_keywords()
    assert {"gaming", "xbox", "playstation", "nintendo", "geforce", "rtx", "radeon", "esports"} <= set(keywords)
    assert not {"gpu", "cpu", "processor", "vram", "pc"} & set(keywords)
    assert score.load_gaming_scene_keywords({}) == []
    assert score.load_gaming_scene_keywords({"score": {"gaming_scene_keywords": ["", " ", "xbox", None]}}) == ["xbox"]
    assert score.load_gaming_scene_keywords({"score": {"gaming_scene_keywords": "xbox"}}) == []


LAWSUIT = "Elder Scrolls Skyblivion mod faces lawsuit from former volunteer"
LAYOFFS = "Doom Director Says We're Doing Great After Half Of id Software Was Laid Off"
CPU_LAYOFFS = "Intel lays off 500 staff in its CPU division"


def gaming_cluster(title, **kwargs):
    return two_source_cluster(title, source_names=("PC Gamer",), **kwargs)


def hardware_cluster(title, **kwargs):
    # Tom's Hardware is a "mixed" source, so only a keyword can make the story relevant.
    return two_source_cluster(title, source_names=("Tom's Hardware",), **kwargs)


def test_a_gaming_cluster_with_a_blocked_topic_is_ineligible_and_says_why():
    c = gaming_cluster(LAWSUIT)
    reason = score.ineligible_reason(c)
    assert reason.startswith("blocked topic: lawsuits_and_regulation (")
    ranked = score.rank([c])[0]
    assert ranked["eligible"] is False
    assert ranked["excluded_by"].startswith("topic lawsuits_and_regulation: ")


def test_layoffs_in_the_gaming_scene_are_covered():
    c = gaming_cluster(LAYOFFS)
    assert score.ineligible_reason(c) is None
    assert score.rank([c])[0]["eligible"] is True


def test_layoffs_at_a_gaming_studio_from_a_mixed_source_need_a_gaming_keyword():
    covered = hardware_cluster("Xbox studio lays off 100 staff in new round of layoffs")
    assert score.ineligible_reason(covered) is None
    # "cpu" makes the story relevant, but it is not gaming: it is skipped.
    skipped = hardware_cluster(CPU_LAYOFFS)
    assert score.ineligible_reason(skipped).startswith("blocked topic: labor_and_layoffs (")
    assert score.rank([skipped])[0]["eligible"] is False


def test_politics_about_gaming_hardware_is_covered_and_other_politics_is_not():
    covered = hardware_cluster("New export controls could hit GeForce RTX 5090 supply")
    assert score.ineligible_reason(covered) is None
    skipped = hardware_cluster("Nvidia GPU export controls tighten again for China")
    assert score.ineligible_reason(skipped).startswith("blocked topic: politics (")


def test_a_gaming_story_in_an_always_blocked_group_is_still_skipped():
    c = hardware_cluster("Xbox studio faces lawsuit over layoffs")
    assert score.ineligible_reason(c).startswith("blocked topic: lawsuits_and_regulation (")


def test_a_gaming_keyword_in_a_members_headline_or_entities_counts():
    c = hardware_cluster(CPU_LAYOFFS)
    assert score.ineligible_reason(c).startswith("blocked topic")
    c["items"][1]["title"] = "Xbox maker Microsoft is cutting staff"
    assert score.ineligible_reason(c) is None
    c = hardware_cluster(CPU_LAYOFFS, entities=("Nintendo",))
    assert score.ineligible_reason(c) is None


def test_gaming_scene_cluster_needs_a_gaming_source_or_a_gaming_keyword():
    topics = {"PC Gamer": "gaming", "Tom's Hardware": "mixed"}
    assert score.gaming_scene_cluster(gaming_cluster(CPU_LAYOFFS), source_topics=topics) is True
    assert score.gaming_scene_cluster(hardware_cluster(CPU_LAYOFFS), source_topics=topics) is False
    assert score.gaming_scene_cluster(
        hardware_cluster("Intel lays off staff in its Xbox chip team"), source_topics=topics,
    ) is True
    # An empty keyword list is "no signal", unlike the relevance gate where it means "on-topic".
    assert score.gaming_scene_cluster(
        hardware_cluster("Intel lays off staff in its Xbox chip team"), keywords=[], source_topics=topics,
    ) is False
    assert score.relevant_cluster(hardware_cluster(CPU_LAYOFFS), keywords=[], source_topics=topics) is True


@pytest.mark.parametrize("title", [
    "AI GPU orders surge", "Server CPU supply tightens", "Processor maker cuts staff", "AI chips head to China",
])
def test_generic_hardware_words_do_not_make_a_story_gaming(title):
    assert score.gaming_scene_cluster(hardware_cluster(title), source_topics={"Tom's Hardware": "mixed"}) is False


def test_an_empty_topic_list_covers_everything_again():
    c = gaming_cluster(LAWSUIT)
    assert score.ineligible_reason(c, blocked_topics=[]) is None
    assert score.rank([c], blocked_topics=[])[0]["eligible"] is True


def test_the_gaming_keywords_can_be_overridden():
    c = hardware_cluster("Xbox studio lays off 100 staff in new round of layoffs")
    assert score.ineligible_reason(c, gaming_scene_keywords=[]).startswith("blocked topic: labor_and_layoffs")
    assert score.rank([c], gaming_scene_keywords=[])[0]["eligible"] is False
    assert score.rank([c], gaming_scene_keywords=["xbox"])[0]["eligible"] is True


def test_a_topic_in_any_members_headline_blocks_the_cluster():
    # Kotaku says "faces a lawsuit"; the seed headline (PC Gamer's) does not.
    c = gaming_cluster("Skyblivion modders say the project is moving on after a dispute")
    assert score.ineligible_reason(c) is None
    c["items"][1]["title"] = LAWSUIT
    assert score.ineligible_reason(c).startswith("blocked topic: lawsuits_and_regulation")
    assert score.rank([c])[0]["eligible"] is False


def test_pick_candidates_skips_blocked_topics_and_keeps_the_rest():
    blocked = gaming_cluster(LAWSUIT)
    fine = gaming_cluster("Valve announces Steam Deck 2 release date")
    picked = score.pick_candidates([blocked, fine], min_score=0, min_score_corroborated=0)
    assert [c["title"] for c in picked] == ["Valve announces Steam Deck 2 release date"]
    everything = score.pick_candidates(
        [blocked, fine], min_score=0, min_score_corroborated=0, blocked_topics=[],
    )
    assert sorted(c["title"] for c in everything) == sorted([LAWSUIT, "Valve announces Steam Deck 2 release date"])
    covered = score.pick_candidates(
        [hardware_cluster(CPU_LAYOFFS)], min_score=0, min_score_corroborated=0, gaming_scene_keywords=["cpu"],
    )
    assert [c["title"] for c in covered] == [CPU_LAYOFFS]


def test_off_topic_is_reported_before_a_blocked_topic():
    c = two_source_cluster(
        "Telecom operators lay off staff as AI shifts the network", source_names=("NVIDIA Blog",)
    )
    assert score.ineligible_reason(c) == "off-topic"


def test_a_format_exclusion_is_reported_before_a_blocked_topic():
    c = gaming_cluster("The podcast on why the studio faces a lawsuit")
    assert score.ineligible_reason(c, ["podcast"]) == "excluded pattern: 'podcast'"



# --- reviews and gaming hardware are covered --------------------------------------------


def test_there_is_no_reviews_group_any_more():
    groups = {group for group, *_rest in score.load_blocked_topics()}
    assert "reviews_and_impressions" not in groups
    assert groups == EXPECTED_TOPIC_GROUPS


@pytest.mark.parametrize("title", [
    "Biwin M560 2TB SSD Review: The DRAM-less drive to beat",
    "ASRock B860 Challenger Wifi Motherboard Review: Entry-level ATX at a great price",
    "Mchose launches updated Ace68 V2 HE keyboard as a $69 Wooting alternative",
    "FSP announces new flagship 1200W Gold non-modular power supply",
    "DDR5 prices climb again as DRAM supply tightens",
    "Corsair slashes $180 off its MP700 SE Gen 5 NVMe drive",
    "Razer unveils a $350 PS5 controller",
    "Budget 27-inch 240Hz monitor lands with a 1ms response time",
    "Lightweight wireless headset adds a 60-hour battery",
    "Corsair launches a 1000W PSU with a 12V-2x6 connector",
    "Seagate unveils a PCIe 5.0 NVMe drive with 14 GB/s reads",
    "DDR4 stock dwindles as makers shift to newer memory",
    "DRAM contract prices rise again this quarter",
    "RAM prices double in a month for retail kits",
    "Memory prices keep climbing for retail shoppers",
])
def test_gaming_hardware_stories_from_a_mixed_outlet_are_on_topic_and_covered(title):
    c = hardware_cluster(title)
    assert score.relevant_cluster(c) is True
    assert score.ineligible_reason(c) is None
    assert score.rank([c])[0]["eligible"] is True


@pytest.mark.parametrize("title", [
    "Bambu Lab H3C 3D printer review: A nine-tool monster",
    "Windows 11 Preview adds Cloud Rebuild to reinstall OS and drivers",
    "Cloudflare engineers optimize DNS build services for twice the speed",
    "AI agent used to perform brain surgery on a mouse",
])
def test_non_gaming_stories_from_a_mixed_outlet_stay_off_topic(title):
    c = hardware_cluster(title)
    assert score.relevant_cluster(c) is False
    assert score.ineligible_reason(c) == "off-topic"


def test_an_enterprise_version_of_a_hardware_story_is_still_skipped():
    # "ssd" makes the story relevant; "enterprise" is what keeps it out.
    c = hardware_cluster("Samsung unveils an enterprise SSD for AI servers")
    assert score.relevant_cluster(c) is True
    assert score.ineligible_reason(c).startswith("blocked topic: non_gaming_enterprise (")


def test_the_hardware_keywords_are_unambiguous_words():
    keywords = set(score.load_relevance_keywords())
    assert {"ssd", "nvme", "motherboard", "keyboard", "headset", "controller", "monitor", "psu",
            "power supply", "ddr5", "ddr4", "dram", "ram prices", "memory prices"} <= keywords
    # Bare "mouse", "ram" and "case" also name animals, verbs and court cases.
    assert not {"mouse", "ram", "case", "cooler"} & keywords
