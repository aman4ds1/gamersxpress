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


EXCLUDE_PATTERNS = ["podcast", "walkthrough", "deal", "savings", "how to"]


def test_playstation_podcast_headline_is_excluded():
    c = cluster("Official PlayStation Podcast Episode 550: Now This is Podcasting", tiers=(1,), owners=("sony",))
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


def test_single_tier1_source_is_eligible():
    c = cluster("Nvidia announces RTX 5090", tiers=(1,), owners=("a",))
    assert cluster_mod.confirmable(c) is True
    assert score.ineligible_reason(c, EXCLUDE_PATTERNS) is None
    assert score.rank([c], exclude_patterns=EXCLUDE_PATTERNS)[0]["eligible"] is True


def test_two_tier2_sources_from_different_owners_are_eligible():
    c = cluster("Xbox and PlayStation US sales sink", tiers=(2,), owners=("a", "b"))
    assert cluster_mod.confirmable(c) is True
    assert score.ineligible_reason(c, EXCLUDE_PATTERNS) is None
    assert score.rank([c], exclude_patterns=EXCLUDE_PATTERNS)[0]["eligible"] is True


def test_tier1_cluster_with_excluded_pattern_is_excluded():
    c = cluster("GIGABYTE deals and savings roundup", tiers=(1,), owners=("a", "b"))
    ranked = score.rank([c], exclude_patterns=EXCLUDE_PATTERNS)
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["excluded_by"] == "deal"
    assert ranked[0]["eligible"] is False
    assert score.ineligible_reason(c, EXCLUDE_PATTERNS) == "excluded pattern: 'deal'"


# --- whole-word format/promotion exclusions (shipped config + defaults) ------


def test_licensing_deal_is_not_excluded():
    c = cluster("Microsoft's Xbox licensing deal with Sony", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["eligible"] is True


def test_black_friday_deals_is_excluded():
    c = cluster("Best Black Friday deals on GPUs", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
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


def test_weekly_active_users_is_not_excluded():
    c = cluster("Steam weekly active users hit a record", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["excluded_by"] is None
    assert ranked[0]["eligible"] is True


def test_acquisition_deal_is_not_excluded():
    c = cluster("Sega buys game studio in $200M deal", tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["confirmable"] is True
    assert ranked[0]["excluded_by"] is None
    assert ranked[0]["eligible"] is True


# --- whole-word exclusion adjustments shipped in the last pass ---------------


def test_free_weekend_is_no_longer_excluded():
    c = cluster("Arc Raiders free weekend on Steam", tiers=(1,), owners=("a",))
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
    c = cluster(
        "DMZ — 5 Features to Get Excited for in Call of Duty: Modern Warfare 4's New Mode",
        source_names=("Xbox Wire",),
    )
    ranked = score.rank([c])
    assert ranked[0]["relevant"] is True
    assert ranked[0]["eligible"] is True
    assert score.ineligible_reason(c) is None


def test_nvidia_blog_telecom_ai_is_off_topic():
    c = cluster(
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
    c = cluster("Everything you need to know", source_names=("Eurogamer",))
    ranked = score.rank([c])
    assert ranked[0]["relevant"] is True
    assert ranked[0]["eligible"] is True
    assert score.ineligible_reason(c) is None


def test_same_headline_from_mixed_source_is_off_topic():
    c = cluster("Everything you need to know", source_names=("Tom's Hardware",))
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
    c = cluster("Nvidia confirms next graphics card launch window")
    assert score.relevant_cluster(c) is True
    assert score.rank([c])[0]["eligible"] is True


def test_pull_up_to_7_eleven_in_forza_is_not_eligible():
    c = cluster("Pull Up to 7-Eleven in Forza Horizon 6")
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
    quiet = cluster("Warhammer Survivors quietly updates its roadmap",
                    source_names=("Eurogamer",), age_hours=50)
    launch = cluster("Warhammer Survivors launches November 10, new characters revealed",
                     source_names=("Eurogamer",), age_hours=50)
    assert score.score_cluster(quiet) == 60
    assert score.score_cluster(launch) == 70
    assert score.pick([quiet]) is None
    assert score.pick([launch]) is not None


def test_warhammer_launch_is_eligible_with_news_bonus():
    c = cluster("Warhammer Survivors launches November 10, new characters revealed",
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
    c = cluster(
        "Monster Hunter Stories 3: Twisted Reflection Heading to XBOX on PC November 18",
        tiers=(1,), owners=("a",), age_hours=50,
    )
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is True
    assert ranked[0]["score"] == 70
    assert score.pick([c]) is not None


def test_critical_role_joins_launching_january_is_eligible():
    c = cluster(
        "Critical Role joins Rockbeasts, launching January 2027",
        tiers=(1,), owners=("a",), age_hours=50, source_names=("Eurogamer",),
    )
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is True
    assert ranked[0]["score"] == 70
    assert score.pick([c]) is not None


def test_coming_to_xbox_game_pass_is_still_excluded():
    c = cluster("Coming to XBOX Game Pass: Battlefield 6, Valor Mortis, Forever Ago, and More",
                tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert ranked[0]["eligible"] is False
    assert score.ineligible_reason(c).startswith("excluded pattern")


# --- new whole-phrase exclusions --------------------------------------------


def test_rally_up_on_geforce_now_is_excluded():
    c = cluster("Rally Up: 'Gears of War: E-Day' Launches on GeForce NOW",
                tiers=(1,), owners=("nvidia",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert ranked[0]["eligible"] is False
    assert "geforce now" in ranked[0]["excluded_by"]
    assert score.ineligible_reason(c).startswith("excluded pattern")


def test_players_choice_is_excluded():
    c = cluster("Players' Choice: Vote for September's best new game",
                tiers=(1,), owners=("a",))
    ranked = score.rank([c])
    assert ranked[0]["excluded_by"] is not None
    assert ranked[0]["eligible"] is False
    assert "players' choice" in ranked[0]["excluded_by"]
    assert score.ineligible_reason(c).startswith("excluded pattern")


# --- corroborated threshold (min_score_corroborated) -------------------------


def test_two_owner_cluster_at_corroborated_threshold_is_picked():
    c = cluster("A quiet story reported by two outlets",
                tiers=(2,), owners=("a", "b"), age_hours=48, source_names=("Eurogamer",))
    assert score.score_cluster(c) == 55
    assert score.min_score_for(c) == 55
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is True
    assert score.pick([c]) is not None


def test_single_source_cluster_below_min_score_is_not_picked():
    c = cluster("A quiet story from one outlet",
                tiers=(1,), owners=("a",), age_hours=50, source_names=("Eurogamer",))
    assert score.score_cluster(c) == 60
    assert score.min_score_for(c) == 65
    ranked = score.rank([c])
    assert ranked[0]["eligible"] is True
    assert score.pick([c]) is None
