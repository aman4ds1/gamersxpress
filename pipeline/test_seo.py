import re
from pathlib import Path

import pytest

import seo
import verify
from providers import Generation

CATEGORIES_TS = Path(__file__).resolve().parent.parent / "src" / "data" / "categories.ts"

ARTICLE = """---
title: Draft
description: Draft description.
---

## What happened

NVIDIA announced the GeForce RTX 5090 at $1,999.

## Why gamers should care

The flagship card costs $1,999 for high-end gaming PCs.
"""


def make_facts(claim="The GeForce RTX 5090 launches at $1,999.", value="$1,999", kind="confirmed"):
    return {
        "id": "story-1",
        "claims": [
            {
                "claim": claim,
                "value": value,
                "source_url": "https://nvidia.example/5090",
                "confidence": 0.95,
                "kind": kind,
            }
        ],
        "sources": [
            {"source_name": "NVIDIA Newsroom", "link": "https://nvidia.example/5090", "tier": 1, "owner": "nvidia", "region": "global"}
        ],
    }


def seo_data(**overrides):
    data = {
        "title": "NVIDIA launches GeForce RTX 5090 at $1,999",
        "description": "NVIDIA's flagship graphics card arrives with more memory and a higher price for high-end gaming PCs and creators.",
        "slug": "geforce-rtx-5090-launch",
        "category": "hardware",
        "tags": ["nvidia", "gpu", "rtx-5090"],
        "entities": ["NVIDIA", "RTX 5090"],
        "imageAlt": "GamersXpress cover image for the GeForce RTX 5090 launch with the Hardware label.",
    }
    data.update(overrides)
    return data


def make_generate(payloads):
    """Return a stub generate that yields the given payload(s) in order."""
    if not isinstance(payloads, list):
        payloads = [payloads]
    calls = []

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        calls.append(prompt)
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        return Generation(role=role, provider="mock", model="m", family="f", value=payload, fallbacks=[])

    generate.calls = calls
    return generate


# --- configuration -----------------------------------------------------------


def test_allowed_categories_match_categories_ts():
    text = CATEGORIES_TS.read_text(encoding="utf-8")
    block = re.search(r"CATEGORIES\s*=\s*\[(.*?)\]", text, re.DOTALL).group(1)
    assert tuple(re.findall(r"'([^']+)'", block)) == seo.ALLOWED_CATEGORIES


def test_content_limits_match_gates_and_config():
    import gates

    limits = gates.load_content_limits()
    assert seo.MAX_TITLE == limits["max_title"]
    assert seo.MIN_DESC == limits["min_description"]
    assert seo.MAX_DESC == limits["max_description"]
    assert seo.content_limits() == limits


def test_validate_uses_injected_content_limits():
    limits = {"max_title": 10, "min_description": 1, "max_description": 5}
    errors = seo.validate(
        seo_data(title="A" * 11, description="ok"),
        article=ARTICLE,
        facts=make_facts(),
        entities=seo.load_entities(),
        limits=limits,
    )
    assert any("title is 11 characters (max 10)" in e for e in errors)


# --- validation --------------------------------------------------------------


def test_validate_accepts_good_data():
    errors = seo.validate(seo_data(), article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert errors == []


def test_validate_rejects_long_title():
    errors = seo.validate(seo_data(title="A" * 66), article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("title" in e for e in errors)


def test_validate_rejects_short_and_long_description():
    short = seo.validate(seo_data(description="Too short."), article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("description" in e for e in short)
    long = seo.validate(seo_data(description="word " * 40), article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("description" in e for e in long)


def test_validate_rejects_bad_slug_and_category_and_tags():
    assert any("slug" in e for e in seo.validate(seo_data(slug="Not A Slug"), article=ARTICLE, facts=make_facts(), entities=seo.load_entities()))
    assert any("category" in e for e in seo.validate(seo_data(category="games"), article=ARTICLE, facts=make_facts(), entities=seo.load_entities()))
    assert any("tags" in e for e in seo.validate(seo_data(tags=["a", "b"]), article=ARTICLE, facts=make_facts(), entities=seo.load_entities()))


def test_validate_rejects_invented_price_in_title():
    data = seo_data(title="NVIDIA launches a $499 gaming GPU")
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("not in the facts sheet" in e for e in errors)


def test_validate_rejects_value_missing_from_body():
    body = "## What happened\n\nThe card was announced.\n"
    data = seo_data(title="NVIDIA launches GeForce RTX 5090 at $1,999")
    errors = seo.validate(data, article=body, facts=make_facts(), entities=seo.load_entities())
    assert any("article body does not" in e for e in errors)


def test_validate_rejects_rumor_stated_as_fact():
    errors = seo.validate(seo_data(), article=ARTICLE, facts=make_facts(kind="rumor"), entities=seo.load_entities())
    assert any("rumor" in e for e in errors)


def test_validate_rejects_padded_filler_in_snippet():
    data = seo_data(description="The mode unlocks additional gear and opportunities for everyone who preorders it now.")
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("unlocks additional gear and opportunities" in e for e in errors)


def test_validate_rejects_invented_label_mid_tier_in_snippet():
    data = seo_data(description="A mid-tier card with more memory and a higher price for high-end gaming PCs and creators.")
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("mid-tier" in e for e in errors)


def test_validate_rejects_confidence_words_in_snippet():
    data = seo_data(description="A low confidence launch claim about more memory for high-end gaming PCs and creators.")
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("low confidence" in e for e in errors)


def test_validate_rejects_internal_term_facts_sheet_in_snippet():
    data = seo_data(description="The facts sheet confirms more memory and a higher price for high-end gaming PCs and creators.")
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("facts sheet" in e for e in errors)


def dmz_facts():
    return {
        "id": "dmz-1",
        "claims": [
            {
                "claim": "DMZ jet pack deployments cost 100,000 in-game DMZ Cash each.",
                "value": "100,000 in-game DMZ Cash per deployment",
                "source_url": "https://eurogamer.example/dmz",
                "confidence": 0.98,
                "kind": "confirmed",
            }
        ],
        "sources": [
            {"source_name": "Eurogamer", "link": "https://eurogamer.example/dmz", "tier": 2, "owner": "ign-entertainment", "region": "uk"}
        ],
    }


def dmz_article():
    return "## What happened\n\nThe DMZ jet pack costs 100,000 DMZ Cash per deployment."


def test_validate_rejects_dollar_amount_for_in_game_currency_without_name():
    bad = seo_data(
        title="DMZ jet pack costs $100,000 to deploy",
        description="The DMZ jet pack costs $100,000 per deployment, the most expensive paid spawn option available.",
    )
    errors = seo.validate(bad, article=dmz_article(), facts=dmz_facts(), entities=seo.load_entities())
    assert any("DMZ Cash" in e and "without naming it" in e for e in errors)


def test_validate_accepts_dollar_amount_for_in_game_currency_when_named():
    good = seo_data(
        title="DMZ jet pack costs 100,000 DMZ Cash per deployment",
        description="The DMZ jet pack costs $100,000 in DMZ Cash per deployment, the most expensive paid spawn option available.",
    )
    errors = seo.validate(good, article=dmz_article(), facts=dmz_facts(), entities=seo.load_entities())
    assert errors == []


# --- attribution, causal connectives and overlaps ----------------------------


def overlap_facts():
    return {
        "id": "overlap-1",
        "claims": [
            {
                "claim": "Most DMZ players also play Warzone.",
                "value": "85% overlap with Warzone players",
                "source_url": "https://eurogamer.example/overlap",
                "confidence": 0.8,
                "origin": "alinea analytics",
                "kind": "estimate",
            }
        ],
        "sources": [
            {"source_name": "Eurogamer", "link": "https://eurogamer.example/overlap", "tier": 2, "owner": "ign-entertainment", "region": "uk"}
        ],
    }


def overlap_article():
    return "## What happened\n\nAbout 85% of DMZ players also play Warzone, according to estimates from Alinea Analytics."


def test_validate_rejects_causal_phrase_in_description():
    data = seo_data(
        description="The launch is driven by demand for more memory and faster ray tracing in high-end gaming PCs and creator builds.",
    )
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("driven by" in e for e in errors)


def test_validate_rejects_causal_phrase_in_title():
    data = seo_data(title="NVIDIA raises RTX 5090 price because demand is high")
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("causal" in e and "because" in e for e in errors)


def test_validate_rejects_overlap_percentage_written_as_origin():
    data = seo_data(
        title="Study finds 85% overlap between DMZ and Warzone players",
        description="A study estimates an 85% overlap, and says most DMZ players came from Warzone according to the firm behind the data.",
    )
    errors = seo.validate(data, article=overlap_article(), facts=overlap_facts(), entities=seo.load_entities())
    assert any("came from" in e for e in errors)


def test_validate_allows_overlap_percentage_without_origin_phrase():
    data = seo_data(
        title="Study estimates 85% overlap between DMZ and Warzone players",
        description="A new estimate says 85% of DMZ players also play Warzone, a figure that could shape the mode's next content update.",
    )
    errors = seo.validate(data, article=overlap_article(), facts=overlap_facts(), entities=seo.load_entities())
    assert not any("origin phrase" in e for e in errors)


def test_causal_phrases_cover_the_writer_ban():
    for phrase in ("in contrast", "because", "driven by", "thanks to", "as a result"):
        assert phrase in seo.CAUSAL_PHRASES


# --- relative time, completeness and contrast --------------------------------


def test_validate_rejects_relative_time_in_snippet():
    data = seo_data(
        description="NVIDIA's flagship graphics card is available now with more memory and a higher price for gaming PCs and creators."
    )
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("relative-time" in e for e in errors)


def test_validate_rejects_completeness_word_without_a_fact():
    data = seo_data(
        description="NVIDIA's complete flagship graphics card arrives with more memory and a higher price for gaming PCs."
    )
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("complete" in e for e in errors)


def test_validate_allows_completeness_word_when_a_fact_states_it():
    facts = make_facts(claim="NVIDIA's complete flagship line launches at $1,999.", value="$1,999")
    data = seo_data(
        description="NVIDIA's complete flagship graphics card arrives with more memory and a higher price for gaming PCs."
    )
    errors = seo.validate(data, article=ARTICLE, facts=facts, entities=seo.load_entities())
    assert not any("complete" in e for e in errors)


def test_validate_rejects_contrast_phrase_in_snippet():
    data = seo_data(
        description="NVIDIA's flagship card arrives as a premium option rather than a budget GPU for high-end gaming PCs and creators."
    )
    errors = seo.validate(data, article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert any("rather than" in e for e in errors)


def test_snippet_banned_phrases_include_the_no_other_details_filler():
    assert "no other details are confirmed" in seo.SNIPPET_BANNED_PHRASES


def test_seo_instruction_covers_new_content_rules():
    for required in (
        "calendar dates",
        "relative-time",
        "'full' or 'complete'",
        "date range or start date",
        "main game or product from the headline",
    ):
        assert required in seo.SYSTEM_INSTRUCTION


# --- entities ----------------------------------------------------------------


def test_canonicalize_entities_maps_aliases_and_keeps_supported_new():
    entities = [{"canonical": "PlayStation 5", "aliases": ["PS5"]}]
    facts = {
        "claims": [
            {"claim": "Arc Raiders launches on PS5.", "value": "PS5", "source_url": "https://x", "confidence": 0.9, "kind": "confirmed"}
        ]
    }
    names, new_entities = seo.canonicalize_entities(["PS5", "Arc Raiders", "Totally Fake Game"], facts, entities)
    assert names == ["PlayStation 5", "Arc Raiders"]
    assert new_entities == [{"canonical": "Arc Raiders", "aliases": []}]


def test_canonicalize_entities_is_insensitive_to_accents():
    # A diacritic entity ("Tōkon") must match the same name written plainly in
    # the facts sheet instead of being dropped as unknown.
    facts = {
        "claims": [
            {"claim": "Marvel Tokon: Fighting Souls reveals its roster.", "value": "Tokon",
             "source_url": "https://x", "confidence": 0.9, "kind": "confirmed"}
        ]
    }
    names, _new = seo.canonicalize_entities(["Marvel Tōkon: Fighting Souls"], facts, [])
    assert names == ["Marvel Tōkon: Fighting Souls"]


def test_canonicalize_text_rewrites_aliases():
    entities = [{"canonical": "PlayStation 5", "aliases": ["PS5"]}]
    assert seo.canonicalize_text("PS5 and PS5 Pro", entities) == "PlayStation 5 and PlayStation 5 Pro"


# --- slugs -------------------------------------------------------------------


def test_unique_slug_appends_suffix():
    existing = {"geforce-rtx-5090-launch"}
    assert seo.unique_slug("GeForce RTX 5090 Launch", existing) == "geforce-rtx-5090-launch-2"


def test_unique_slug_respects_length_limit():
    slug = seo.unique_slug("a" * 100, set())
    assert len(slug) <= seo.MAX_SLUG
    assert seo.SLUG_RE.fullmatch(slug)
    taken = {"b" * seo.MAX_SLUG}
    slug2 = seo.unique_slug("b" * 100, taken)
    assert slug2 not in taken
    assert len(slug2) <= seo.MAX_SLUG


def test_existing_slugs_scans_articles_and_drafts(tmp_path):
    articles = tmp_path / "articles"
    drafts = tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    (articles / "one.md").write_text("---\n---\n", encoding="utf-8")
    (drafts / "Two.md").write_text("---\n---\n", encoding="utf-8")
    assert seo.existing_slugs(articles, drafts) == {"one", "two"}
    assert seo.existing_slugs(tmp_path / "missing", tmp_path / "also-missing") == set()


# --- normalization (code owns formatting) ------------------------------------


LONG_SLUG_TITLE = "Estimated Gears of War E-Day Sales Raise Query About Last-Minute"
LONG_SLUG_64 = "estimated-gears-of-war-e-day-sales-raise-query-about-last-minute"
LONG_SLUG_CUT = "estimated-gears-of-war-e-day-sales-raise-query-about-last"


def test_truncate_slug_cuts_an_over_long_slug_at_word_boundary():
    # Was a 64-character slug when MAX_SLUG was 60; that one now fits whole (next test).
    too_long = LONG_SLUG_64 + "-and-a-few-more-words"
    assert len(too_long) > seo.MAX_SLUG
    cut = seo.truncate_slug(too_long)
    assert len(cut) <= seo.MAX_SLUG
    assert seo.SLUG_RE.fullmatch(cut)
    assert too_long.startswith(cut + "-")


def test_a_64_character_slug_is_kept_whole():
    assert len(LONG_SLUG_64) == 64
    assert seo.truncate_slug(LONG_SLUG_64) == LONG_SLUG_64
    assert seo.unique_slug(LONG_SLUG_TITLE, set()) == LONG_SLUG_64


def test_a_title_at_the_title_limit_never_loses_a_word_from_its_slug():
    limit = seo.content_limits()["max_title"]
    title = ("Phantom Blade Zero Sells Over Four Million Copies In Three Days " * 2)[:limit].strip()
    assert len(title) >= limit - 1
    slug = seo.unique_slug(title, set())
    assert slug == seo.slugify(title)
    assert len(slug) <= seo.MAX_SLUG


def test_the_slug_limit_covers_the_title_limit():
    # A slug is the title with hyphens, so a title that fits must give a slug that fits.
    assert seo.MAX_SLUG >= seo.content_limits()["max_title"]


def test_validate_accepts_a_slug_at_the_limit_and_rejects_one_over():
    def slug_errors(length):
        slug = ("word-" * 30)[:length].strip("-")
        errors = seo.validate(seo_data(slug=slug), article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
        return [e for e in errors if "slug" in e]

    assert slug_errors(seo.MAX_SLUG) == []
    assert any("characters" in e for e in slug_errors(seo.MAX_SLUG + 3))


def test_slugify_transliterates_accents():
    assert seo.slugify("Pokémon Café Déjà Vu") == "pokemon-cafe-deja-vu"


def test_normalize_tags_hyphenates_dedupes_and_drops_empty():
    assert seo.normalize_tags(["game pass", "GPU", "gpu", "  ", "", "Game Pass"]) == [
        "game-pass",
        "gpu",
    ]


def test_normalize_tags_caps_the_count():
    tags = [f"tag {i}" for i in range(9)]
    assert seo.normalize_tags(tags) == [f"tag-{i}" for i in range(seo.MAX_TAGS)]


# --- prompt ------------------------------------------------------------------


def test_build_prompt_includes_categories_facts_and_errors():
    prompt = seo.build_prompt(ARTICLE, make_facts(), entities=seo.load_entities(), errors=["title too long"])
    assert "Allowed categories" in prompt
    assert "hardware" in prompt
    assert "The GeForce RTX 5090 launches" in prompt
    assert "title too long" in prompt


def test_snippet_banned_phrases_cover_new_writer_rules():
    for required in (
        "unlocks additional gear and opportunities",
        "mid-tier",
        "low confidence",
        "high confidence",
        "facts sheet",
    ):
        assert required in seo.SNIPPET_BANNED_PHRASES


def test_seo_instruction_covers_snippet_rules():
    for required in [
        "Every sentence must be supported by a claim",
        "unlocks additional gear and opportunities",
        "cost as something players receive",
        "mid-tier",
        "low confidence",
        "facts sheet",
        "100,000 in-game DMZ Cash",
        "slug is generated in code from the final title",
    ]:
        assert required in seo.SYSTEM_INSTRUCTION


# --- generate_seo ------------------------------------------------------------


def test_generate_seo_canonicalizes_and_uniquifies(tmp_path, monkeypatch):
    entities_path = tmp_path / "entities.json"
    seo.save_entities([{"canonical": "NVIDIA", "aliases": []}, {"canonical": "GeForce RTX 5090", "aliases": ["RTX 5090"]}], entities_path)
    articles = tmp_path / "articles"
    drafts = tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    (articles / "nvidia-announces-its-flagship-gpu.md").write_text("---\n---\n", encoding="utf-8")

    generate = make_generate(seo_data(title="NVIDIA announces its flagship GPU"))
    result = seo.generate_seo(
        ARTICLE,
        make_facts(),
        generate=generate,
        entities_path=entities_path,
        articles_dir=articles,
        drafts_dir=drafts,
    )
    assert result["slug"] == "nvidia-announces-its-flagship-gpu-2"
    assert result["entities"] == ["NVIDIA", "GeForce RTX 5090"]
    assert result["generator"] == {"provider": "mock", "model": "m", "family": "f"}


def test_generate_seo_retries_once_with_errors(tmp_path):
    articles = tmp_path / "articles"
    drafts = tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    bad = seo_data(title="NVIDIA launches a $499 gaming GPU")
    generate = make_generate([bad, seo_data()])
    result = seo.generate_seo(
        ARTICLE,
        make_facts(),
        generate=generate,
        entities_path=tmp_path / "entities.json",
        articles_dir=articles,
        drafts_dir=drafts,
    )
    assert result["slug"] == "nvidia-launches-geforce-rtx-5090-at-1-999"
    assert len(generate.calls) == 2
    assert "previous answer was invalid" in generate.calls[1]


def test_generate_seo_derives_slug_from_final_title_ignoring_model_slug(tmp_path):
    articles = tmp_path / "articles"
    drafts = tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    data = seo_data(slug="call-of-duty-modern-warfare-4-five-features-to-get-excited-for")
    generate = make_generate(data)
    result = seo.generate_seo(
        ARTICLE,
        make_facts(),
        generate=generate,
        entities_path=tmp_path / "entities.json",
        articles_dir=articles,
        drafts_dir=drafts,
    )
    assert result["slug"] == "nvidia-launches-geforce-rtx-5090-at-1-999"
    assert result["slug"] != data["slug"]


def test_generate_seo_raises_after_two_failures(tmp_path):
    articles = tmp_path / "articles"
    drafts = tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    generate = make_generate(seo_data(title="NVIDIA launches a $499 gaming GPU"))
    with pytest.raises(seo.SeoError):
        seo.generate_seo(
            ARTICLE,
            make_facts(),
            generate=generate,
            entities_path=tmp_path / "entities.json",
            articles_dir=articles,
            drafts_dir=drafts,
        )
    assert len(generate.calls) == 2


def test_generate_seo_fixes_formatting_without_a_retry(tmp_path):
    articles = tmp_path / "articles"
    drafts = tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    data = seo_data(
        slug="estimated-gears-of-war-e-day-sales-raise-query-about-last-minute-with-extra-words-added",
        title=LONG_SLUG_TITLE,
        tags=["game pass", "Game Pass", "", "xbox", "pc gaming", "pc-gaming"],
    )
    generate = make_generate(data)
    result = seo.generate_seo(
        ARTICLE,
        make_facts(),
        generate=generate,
        entities_path=tmp_path / "entities.json",
        articles_dir=articles,
        drafts_dir=drafts,
    )
    assert result["slug"] == LONG_SLUG_64  # the whole title; it was ...-about-last at MAX_SLUG 60
    assert len(result["slug"]) <= seo.MAX_SLUG
    assert result["slug"] != data["slug"]
    assert result["tags"] == ["game-pass", "xbox", "pc-gaming"]
    assert len(generate.calls) == 1


def test_generate_seo_avoids_slug_collision(tmp_path):
    articles = tmp_path / "articles"
    drafts = tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    (articles / f"{LONG_SLUG_64}.md").write_text("---\n---\n", encoding="utf-8")
    data = seo_data(title=LONG_SLUG_TITLE)
    generate = make_generate(data)
    result = seo.generate_seo(
        ARTICLE,
        make_facts(),
        generate=generate,
        entities_path=tmp_path / "entities.json",
        articles_dir=articles,
        drafts_dir=drafts,
    )
    # No room for "-2" after 64 characters, so the stem is cut at a word boundary.
    assert result["slug"] == f"{LONG_SLUG_CUT}-2"
    assert result["slug"] != LONG_SLUG_64
    assert len(result["slug"]) <= seo.MAX_SLUG
    assert len(generate.calls) == 1


# --- entity names in text and required entities -------------------------------


def test_canonicalize_text_does_not_double_a_name_that_contains_an_alias():
    # "Nintendo Switch 2" already holds the alias "Switch 2"; a second pass over
    # the finished name used to produce "Nintendo Nintendo Switch 2".
    entities = [{"canonical": "Nintendo Switch 2", "aliases": ["Switch 2"]}]
    text = "Nintendo Switch 2 owners and Switch 2 fans"
    assert seo.canonicalize_text(text, entities) == "Nintendo Switch 2 owners and Nintendo Switch 2 fans"


DUSK_ARTICLE = """---
title: Draft
description: Draft description.
---

## What happened

FromSoftware's Nintendo Switch 2 exclusive, The Duskbloods, has been delayed into 2027.
"""


def dusk_facts(top="Duskbloods"):
    facts = make_facts(
        claim="FromSoft's Switch 2 exclusive, The Duskbloods, has been delayed into 2027",
        value="2027",
    )
    facts["coherence"] = {"canonical_entities": [top, "FromSoft Switch 2"]}
    return facts


def dusk_seo(**overrides):
    return seo_data(
        title="FromSoftware delays The Duskbloods into 2027",
        description=(
            "FromSoftware has delayed its Nintendo Switch 2 exclusive game The Duskbloods into 2027 "
            "to allow time for final adjustments to game rules and balancing."
        ),
        slug="fromsoftware-delays-the-duskbloods",
        category="gaming-news",
        tags=["fromsoftware", "switch-2", "delay"],
        entities=["FromSoftware", "Nintendo Switch 2"],
        imageAlt="GamersXpress cover image for FromSoftware delaying The Duskbloods with the Gaming News label.",
        **overrides,
    )


def run_dusk_seo(tmp_path, facts):
    entities_path = tmp_path / "entities.json"
    seo.save_entities(
        [{"canonical": "FromSoftware", "aliases": []}, {"canonical": "Nintendo Switch 2", "aliases": ["Switch 2"]}],
        entities_path,
    )
    (tmp_path / "articles").mkdir()
    (tmp_path / "drafts").mkdir()
    return seo.generate_seo(
        DUSK_ARTICLE,
        facts,
        generate=make_generate(dusk_seo()),
        entities_path=entities_path,
        articles_dir=tmp_path / "articles",
        drafts_dir=tmp_path / "drafts",
    ), entities_path


def test_generate_seo_adds_the_headline_game_the_model_left_out(tmp_path):
    result, entities_path = run_dusk_seo(tmp_path, dusk_facts())
    assert "Duskbloods" in result["entities"]
    assert "Nintendo Nintendo" not in result["description"]
    assert "Nintendo Switch 2 exclusive game" in result["description"]
    assert "Duskbloods" in [e["canonical"] for e in seo.load_entities(entities_path)]


def test_generate_seo_does_not_add_a_top_entity_the_article_never_names(tmp_path):
    # The facts sheet supports "Elden Ring" (so the entity filter would keep it),
    # but neither the title nor the article names it: only the mention check
    # stops it from being added.
    facts = dusk_facts(top="Elden Ring")
    facts["claims"].append(
        {
            "claim": "The Elden Ring developer announced the delay",
            "value": "Elden Ring",
            "source_url": "https://nvidia.example/5090",
            "confidence": 0.95,
            "kind": "reported",
        }
    )
    result, _ = run_dusk_seo(tmp_path, facts)
    assert "Elden Ring" not in result["entities"]


# --- entities the article never names ------------------------------------------------


@pytest.mark.parametrize("names, kept", [
    (["FromSoftware", "Valve", "Activision"], ["FromSoftware"]),
    (["Nintendo Switch 2", "Sony"], ["Nintendo Switch 2"]),
    (["Valve"], []),
    ([], []),
])
def test_drop_unmentioned_entities_keeps_only_named_ones(names, kept):
    body = "FromSoftware delayed its Nintendo Switch 2 exclusive into 2027."
    assert seo.drop_unmentioned_entities(names, title="Delay news", article=body) == kept


def test_drop_unmentioned_entities_counts_the_title():
    assert seo.drop_unmentioned_entities(
        ["Valve"], title="Valve ships an update", article="The patch is out."
    ) == ["Valve"]


def run_dusk_seo_with(tmp_path, facts, entities):
    entities_path = tmp_path / "entities.json"
    seo.save_entities(
        [{"canonical": "FromSoftware", "aliases": []},
         {"canonical": "Nintendo Switch 2", "aliases": ["Switch 2"]},
         {"canonical": "Valve", "aliases": []}],
        entities_path,
    )
    (tmp_path / "articles").mkdir()
    (tmp_path / "drafts").mkdir()
    data = dusk_seo()
    data["entities"] = entities
    result = seo.generate_seo(
        DUSK_ARTICLE, facts, generate=make_generate(data),
        entities_path=entities_path, articles_dir=tmp_path / "articles",
        drafts_dir=tmp_path / "drafts",
    )
    return result, entities_path


def test_generate_seo_drops_a_known_entity_the_article_never_names(tmp_path):
    # "Valve" is a known entity, so canonicalizing keeps it without looking at the
    # article; the publish gate would then fail the draft for naming it.
    result, _ = run_dusk_seo_with(tmp_path, dusk_facts(), ["FromSoftware", "Valve"])
    assert "Valve" not in result["entities"]
    assert "FromSoftware" in result["entities"]


def test_generate_seo_does_not_save_a_dropped_new_entity(tmp_path):
    # "Elden Ring" is in the facts (so it is a valid new entity) but not in the article.
    facts = dusk_facts()
    facts["claims"].append(
        {"claim": "The Elden Ring developer announced the delay", "value": "Elden Ring",
         "source_url": "https://nvidia.example/5090", "confidence": 0.95, "kind": "reported"}
    )
    result, entities_path = run_dusk_seo_with(tmp_path, facts, ["FromSoftware", "Elden Ring"])
    assert "Elden Ring" not in result["entities"]
    assert "Elden Ring" not in [e["canonical"] for e in seo.load_entities(entities_path)]


# --- the site's own voice: no outlet names in the title or description -------------------


def test_seo_instruction_asks_for_the_sites_own_voice_and_hedges():
    for required in (
        "State facts directly in the site's own voice",
        "never name a news outlet or website in the title or description",
        "'Eurogamer reports'",
        "Keep an estimate's or rumor's hedge",
        "Never state an estimate or a rumor as fact",
        "in contrast",
        "driven by",
        "came from",
    ):
        assert required in seo.SYSTEM_INSTRUCTION
    assert "according to estimates from X" not in seo.SYSTEM_INSTRUCTION
    assert "attribute to the outlet" not in seo.SYSTEM_INSTRUCTION


def test_validate_rejects_an_outlet_name_in_the_title_or_description():
    facts = make_facts()
    title_errors = seo.validate(
        seo_data(title="NVIDIA Newsroom says RTX 5090 costs $1,999"),
        article=ARTICLE, facts=facts, entities=seo.load_entities(),
    )
    assert any(e.startswith("title names a source outlet ('NVIDIA Newsroom')") for e in title_errors)
    description_errors = seo.validate(
        seo_data(description="The flagship graphics card arrives with more memory, as reported by NVIDIA Newsroom today."),
        article=ARTICLE, facts=facts, entities=seo.load_entities(),
    )
    assert any(e.startswith("description names a source outlet ('NVIDIA Newsroom')") for e in description_errors)


def test_validate_accepts_a_title_and_description_without_an_outlet_name():
    errors = seo.validate(seo_data(), article=ARTICLE, facts=make_facts(), entities=seo.load_entities())
    assert not any("source outlet" in e for e in errors)


def test_generate_seo_retries_when_the_model_names_an_outlet(tmp_path):
    bad = dusk_seo()
    bad["title"] = "NVIDIA Newsroom says the console is delayed to 2027"
    good = dusk_seo()
    calls = []
    sequence = make_generate([bad, good])

    def generate(role, prompt, **kwargs):
        calls.append(prompt)
        return sequence(role, prompt, **kwargs)

    facts = dusk_facts()
    facts["sources"] = [{**facts["sources"][0], "source_name": "NVIDIA Newsroom"}, *facts["sources"][1:]]
    entities_path = tmp_path / "entities.json"
    seo.save_entities([], entities_path)
    (tmp_path / "articles").mkdir()
    (tmp_path / "drafts").mkdir()
    result = seo.generate_seo(
        DUSK_ARTICLE, facts, generate=generate, entities_path=entities_path,
        articles_dir=tmp_path / "articles", drafts_dir=tmp_path / "drafts",
    )
    assert "NVIDIA Newsroom" not in result["title"]
    assert len(calls) == 2
    assert "title names a source outlet" in calls[1]


def test_generate_seo_checks_entities_against_the_body_not_the_front_matter_the_writer_wrote(tmp_path):
    # The writer lists entities in its own front matter; that list must not make them "mentioned".
    drafted = "---\ntitle: Draft\nentities:\n  - Valve\n  - FromSoftware\n---\n" + DUSK_ARTICLE
    entities_path = tmp_path / "entities.json"
    seo.save_entities([{"canonical": "FromSoftware", "aliases": []}, {"canonical": "Valve", "aliases": []}], entities_path)
    (tmp_path / "articles").mkdir()
    (tmp_path / "drafts").mkdir()
    data = dusk_seo()
    data["entities"] = ["FromSoftware", "Valve"]
    result = seo.generate_seo(
        drafted, dusk_facts(), generate=make_generate(data), entities_path=entities_path,
        articles_dir=tmp_path / "articles", drafts_dir=tmp_path / "drafts",
    )
    assert "Valve" not in result["entities"] and "FromSoftware" in result["entities"]


# --- completing a short tag list --------------------------------------------------------------------------------------


def test_a_tag_list_that_is_long_enough_is_only_normalized_and_capped_never_added_to():
    assert seo.complete_tags(["Game Pass", "xbox", "pc gaming"], entities=["Starfield"], category="xbox") == ["game-pass", "xbox", "pc-gaming"]
    assert seo.complete_tags([f"tag {i}" for i in range(9)], entities=["X Y"]) == [f"tag-{i}" for i in range(seo.MAX_TAGS)]
    assert seo.complete_tags(["a1", "b2", "c3", "d4"], entities=["Extra Thing"]) == ["a1", "b2", "c3", "d4"]


def test_two_tags_are_completed_from_the_stories_entities_in_order():
    assert seo.complete_tags(["nvidia", "gpu"], entities=["NVIDIA", "RTX 5090", "Intel"], category="hardware") == ["nvidia", "gpu", "rtx-5090"]
    assert seo.complete_tags(["gpu"], entities=["NVIDIA", "RTX 5090", "Intel"]) == ["gpu", "nvidia", "rtx-5090"]


def test_no_more_tags_are_added_than_are_needed():
    assert seo.complete_tags([], entities=["One Two", "Three Four", "Five Six", "Seven Eight"]) == ["one-two", "three-four", "five-six"]
    assert seo.complete_tags(["a1", "b2"], entities=["C Three", "D Four"]) == ["a1", "b2", "c-three"]


def test_the_category_fills_in_after_the_entities_and_before_the_last_resort_tags():
    assert seo.complete_tags(["gpu"], entities=["NVIDIA"], category="hardware") == ["gpu", "nvidia", "hardware"]
    assert seo.complete_tags(["gpu"], entities=[], category="nintendo") == ["gpu", "nintendo", "gaming-news"]
    assert seo.complete_tags(["gpu"], entities=[], category="gaming-news") == ["gpu", "gaming-news", "video-games"]


@pytest.mark.parametrize("category", ["sports", "", None, 5, "Hardware"])
def test_a_category_that_is_not_an_allowed_one_is_never_added(category):
    assert seo.complete_tags(["gpu"], entities=[], category=category) == ["gpu", "gaming-news", "video-games"]


def test_a_story_with_nothing_to_fill_from_still_gets_three_valid_tags():
    assert seo.complete_tags([], entities=[], category=None) == list(seo.FALLBACK_TAGS)
    assert len(seo.FALLBACK_TAGS) >= seo.MIN_TAGS and all(seo._TAG_RE.fullmatch(tag) for tag in seo.FALLBACK_TAGS)
    assert seo.complete_tags(None) == list(seo.FALLBACK_TAGS) and seo.complete_tags("xbox") == ["xbox", "gaming-news", "video-games"]


def test_a_tag_that_is_already_there_is_not_added_again_under_another_spelling():
    assert seo.complete_tags(["nvidia", "gpu"], entities=["NVIDIA", "nvidia", "Nvidia "], category="hardware") == ["nvidia", "gpu", "hardware"]
    assert seo.complete_tags(["gaming-news"], entities=[], category=None) == ["gaming-news", "video-games", "gaming"]


def test_an_entity_that_does_not_make_a_valid_short_tag_is_skipped():
    long_name = "The Legend of Some Extremely Long Named Game Subtitle"
    assert len(seo.slugify(long_name)) > seo.MAX_ADDED_TAG_CHARS
    assert seo.complete_tags(["gpu"], entities=[long_name, "", "  ", "\u65e5\u672c\u8a9e", 7, None, "Good Name"]) == ["gpu", "good-name", "gaming-news"]
    assert seo.complete_tags(["gpu"], entities=["A" * seo.MAX_ADDED_TAG_CHARS])[1] == "a" * seo.MAX_ADDED_TAG_CHARS
    assert seo.complete_tags(["gpu"], entities=["A" * (seo.MAX_ADDED_TAG_CHARS + 1)])[1] == "gaming-news"


def test_the_input_is_not_changed_and_the_minimum_and_limit_can_be_set():
    original = ["gpu"]
    assert seo.complete_tags(original, entities=["NVIDIA"]) == ["gpu", "nvidia", "gaming-news"] and original == ["gpu"]
    assert seo.complete_tags(["gpu"], entities=["NVIDIA", "Intel"], minimum=2) == ["gpu", "nvidia"]
    assert seo.complete_tags(["gpu"], entities=["NVIDIA", "Intel"], minimum=5, limit=4) == ["gpu", "nvidia", "intel", "gaming-news"]


def generate_for(tmp_path, data, **kwargs):
    articles, drafts = tmp_path / "articles", tmp_path / "drafts"
    articles.mkdir(exist_ok=True)
    drafts.mkdir(exist_ok=True)
    generate = make_generate(data)
    result = seo.generate_seo(ARTICLE, make_facts(), generate=generate, entities_path=tmp_path / "entities.json",
                              articles_dir=articles, drafts_dir=drafts, **kwargs)
    return result, generate


def test_a_model_that_returns_two_tags_no_longer_costs_a_retry_or_the_story(tmp_path):
    # The live failure: "tags must have 3 to 6 entries, got 2", twice, and the story was dropped.
    result, generate = generate_for(tmp_path, seo_data(tags=["nvidia", "gpu"]))
    assert result["tags"] == ["nvidia", "gpu", seo.slugify(result["entities"][0])] and len(generate.calls) == 1
    assert len(result["tags"]) == 3 and result["tags"][2] == "rtx-5090"


def test_three_tags_that_collapse_into_two_are_completed_too(tmp_path):
    result, generate = generate_for(tmp_path, seo_data(tags=["game pass", "Game Pass", "gpu"]))
    assert result["tags"][:2] == ["game-pass", "gpu"] and len(result["tags"]) == 3 and len(generate.calls) == 1


def test_a_model_that_returns_no_tags_at_all_gets_tags_from_the_entities_then_the_category(tmp_path):
    result, generate = generate_for(tmp_path, seo_data(tags=[]))
    expected = [seo.slugify(entity) for entity in result["entities"]] + ["hardware", "gaming-news"]
    assert result["tags"] == expected[:3] and len(result["tags"]) == 3 and len(generate.calls) == 1


def test_tags_are_only_filled_from_the_entities_that_survive_the_pipelines_own_filtering(tmp_path):
    result, _generate = generate_for(tmp_path, seo_data(tags=["gpu"], entities=["NVIDIA", "Intel Arc"]))
    assert "Intel Arc" not in result["entities"] and "intel-arc" not in result["tags"]       # the article never names it
    assert result["tags"] == ["gpu", seo.slugify(result["entities"][0]), "hardware"]


def test_a_model_that_already_returned_enough_tags_keeps_exactly_its_own(tmp_path):
    result, generate = generate_for(tmp_path, seo_data(tags=["nvidia", "gpu", "rtx-5090"]))
    assert result["tags"] == ["nvidia", "gpu", "rtx-5090"] and len(generate.calls) == 1


def test_a_story_with_a_real_problem_still_retries_and_fails_even_though_its_tags_are_completed(tmp_path):
    with pytest.raises(seo.SeoError, match="value not in the facts sheet"):
        generate_for(tmp_path, seo_data(title="NVIDIA launches a $499 gaming GPU", tags=["gpu"]))


def test_completing_tags_is_logged(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="gamersxpress.pipeline.seo"):
        result, _generate = generate_for(tmp_path, seo_data(tags=["gpu"]))
    assert any(message.startswith("seo completed tags: ['gpu'] -> ['gpu', ") and "rtx-5090" in message for message in caplog.messages)
    assert result["tags"][0] == "gpu"


# --- code repairs the title before validation, so these never cost a model retry ---------------------------------

def test_a_long_title_with_a_subtitle_keeps_its_main_part():
    assert seo.fit_title("Nintendo Switch 2 price rises in Europe: what it means for buyers and rivals now", 65) == \
        "Nintendo Switch 2 price rises in Europe"


def test_a_long_title_without_a_subtitle_is_cut_at_a_whole_word():
    text = "Phantom Blade Zero sells four million copies in the first seventy two hours of release"
    fitted = seo.fit_title(text, 65)
    assert len(fitted) <= 65 and text.startswith(fitted)
    assert text[len(fitted)] == " "                       # the cut falls between words, never inside one


def test_a_title_within_the_limit_is_only_tidied():
    assert seo.fit_title("  Nintendo   Switch 2 price rises  ", 65) == "Nintendo Switch 2 price rises"


def test_a_date_the_body_and_sheet_do_not_state_is_removed_from_the_title():
    article = ARTICLE.replace("Draft description.", "Draft description.") + "\n\nThe update is out now."
    repaired = seo.repair_title(
        "Path of Exile 2 launches on December 2 with a new league",
        article=article, facts=make_facts(), entities=[], config=verify.load_verify_config(),
    )
    assert "December 2" not in repaired and repaired.startswith("Path of Exile 2 launches")


def test_a_date_the_body_states_is_kept():
    article = "---\ntitle: x\n---\n\nThe game launches on December 11."
    repaired = seo.repair_title(
        "Path of Exile 2 launches December 11",
        article=article, facts=make_facts("Path of Exile 2 launches December 11.", "December 11"),
        entities=[], config=verify.load_verify_config(),
    )
    assert repaired == "Path of Exile 2 launches December 11"


def test_numbers_that_are_not_dates_are_left_for_the_model():
    repaired = seo.repair_title(
        "Nintendo Switch 2 price rises", article="---\ntitle: x\n---\n\nThe price rises.",
        facts=make_facts(), entities=[], config=verify.load_verify_config(),
    )
    assert repaired == "Nintendo Switch 2 price rises"


def test_generate_seo_shortens_a_long_title_without_a_retry(tmp_path):
    articles, drafts = tmp_path / "articles", tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    too_long = seo_data(title="NVIDIA launches GeForce RTX 5090 at $1,999 with more memory for high-end gaming PCs and creators")
    generate = make_generate([too_long, seo_data()])
    result = seo.generate_seo(
        ARTICLE, make_facts(), generate=generate, entities_path=tmp_path / "entities.json",
        articles_dir=articles, drafts_dir=drafts,
    )
    assert len(result["title"]) <= 65 and len(generate.calls) == 1


def test_generate_seo_removes_an_unsupported_date_without_a_retry(tmp_path):
    articles, drafts = tmp_path / "articles", tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    dated = seo_data(title="NVIDIA launches GeForce RTX 5090 at $1,999 on December 2")
    generate = make_generate([dated, seo_data()])
    result = seo.generate_seo(
        ARTICLE, make_facts(), generate=generate, entities_path=tmp_path / "entities.json",
        articles_dir=articles, drafts_dir=drafts,
    )
    assert "December" not in result["title"] and len(generate.calls) == 1


# --- code repairs a description that is a few characters too long, so it never costs a model retry -----------------

LO, HI = 70, 160
ORDER_TO = "Square Enix has asked affected PC players to verify their game files on Steam in order to restore the original 50GB install size after an update bug in Nier: Automata."
A_TOTAL_OF = "The studio says Phantom Blade Zero sold more than 4 million copies across PC and PlayStation 5 in 72 hours, a total of three days after its October 29 launch worldwide."
FILLER = "Square Enix is also investigating a very large install size bug in a Nier: Automata update that really inflated the PC download from about 50GB to 366GB for players."
APPROXIMATELY = "Phantom Blade Zero peaked at approximately 833,921 concurrent players on Steam as well as selling over 4 million copies across PC and PlayStation 5 in its first 72 hours."
TWO_SENTENCES = "Phantom Blade Zero sold over 4 million copies in its first 72 hours on PC and PlayStation 5, the studio said. Steam peaked at 833,921 concurrent players, a record for a launch this year."
WITH_CLAUSE = "Nintendo says it has no plans for another Switch 2 price increase in the near term, with prices staying where they are across all of its regions and retailers worldwide."
BUT_ONLY = "Sony will launch the game on PC later this year, but only after the console release has been out for several months in every region of the world, a company spokesperson said."
NOT_CONFIRMED = "Sony says the game launches on PC in March across all major stores worldwide with full controller support. The company has not confirmed pricing, regions or a precise date yet."
MAIN_VERB_LAST = "S-GAME, the Chinese studio behind the action RPG released on PC and PlayStation 5 late last month in several regions, announced that it has now sold more than 4 million copies."


def fit(text):
    return seo.fit_description(text, min_length=LO, max_length=HI)


def test_the_sample_descriptions_really_are_over_the_limit():
    for text in (ORDER_TO, A_TOTAL_OF, FILLER, APPROXIMATELY, TWO_SENTENCES, WITH_CLAUSE, BUT_ONLY, NOT_CONFIRMED, MAIN_VERB_LAST):
        assert len(text) > HI, text[:40]


def test_a_description_within_the_limit_is_only_put_on_one_line():
    assert fit("A short but valid description of an article that is long enough to pass the minimum check.") == \
        "A short but valid description of an article that is long enough to pass the minimum check."
    assert fit("First line of a valid description,\n  second line   of it that makes it long enough to pass.") == \
        "First line of a valid description, second line of it that makes it long enough to pass."


@pytest.mark.parametrize("text", [ORDER_TO, A_TOTAL_OF, FILLER, APPROXIMATELY])
def test_wording_that_says_the_same_in_fewer_characters_brings_it_inside_the_limit(text):
    fitted = fit(text)
    assert LO <= len(fitted) <= HI
    assert fitted.endswith(".") and "  " not in fitted and " ," not in fitted
    added = set(re.findall(r"[a-z']+", fitted.lower())) - set(re.findall(r"[a-z']+", text.lower()))
    assert added <= {"to", "about", "and", "several", "many", "that"}, f"new words appeared: {added}"


def test_each_wording_repair_does_what_it_says():
    assert "in order to" not in fit(ORDER_TO) and " to restore" in fit(ORDER_TO)
    assert "a total of" not in fit(A_TOTAL_OF)
    assert not re.search(r"\b(also|really|very)\b", fit(FILLER))
    assert "approximately" not in fit(APPROXIMATELY) and "about 833,921" in fit(APPROXIMATELY)


def test_a_whole_trailing_sentence_is_dropped_when_that_is_enough():
    assert fit(TWO_SENTENCES) == "Phantom Blade Zero sold over 4 million copies in its first 72 hours on PC and PlayStation 5, the studio said."


def test_a_trailing_clause_that_only_adds_context_is_dropped_and_the_sentence_is_closed():
    fitted = fit(WITH_CLAUSE)
    assert fitted == "Nintendo says it has no plans for another Switch 2 price increase in the near term."
    assert WITH_CLAUSE.startswith(fitted[:-1])                       # what is left is the start of the original, nothing new


@pytest.mark.parametrize("text", [BUT_ONLY, NOT_CONFIRMED, MAIN_VERB_LAST])
def test_anything_that_could_carry_a_qualifier_or_the_main_verb_is_never_cut_off(text):
    assert fit(text) == " ".join(text.split())                       # unchanged: the model's retry handles it, as before


def test_a_cut_that_would_leave_less_than_the_minimum_is_not_made():
    text = "Short opening sentence here. " + "A second sentence that is much longer than the limit allows for any description at all, " * 2
    assert fit(text) == " ".join(text.split())


def test_only_a_repair_that_fully_fits_is_used_otherwise_nothing_is_half_changed():
    # one long sentence: the wording fix saves 8 characters but cannot save the 50 that are needed, so it is not applied
    one_long_sentence = ORDER_TO[:-1] + " and several other Square Enix titles that were also changed by the same update across many regions."
    assert len(one_long_sentence) > HI + 40 and "in order to" in one_long_sentence
    assert fit(one_long_sentence) == " ".join(one_long_sentence.split())
    assert "in order to" in fit(one_long_sentence)


def test_two_safe_repairs_may_combine_when_together_they_fit():
    still_long = ORDER_TO + " " + "The update also affects a great many other players across several regions around the world."
    fitted = fit(still_long)
    assert LO <= len(fitted) <= HI and fitted.endswith("Nier: Automata.") and "in order to" not in fitted


def test_a_description_below_the_minimum_is_left_for_the_model():
    assert fit("Too short.") == "Too short."
    assert fit("") == "" and fit(None) == ""


@pytest.mark.parametrize("text", [ORDER_TO, A_TOTAL_OF, FILLER, APPROXIMATELY, TWO_SENTENCES, WITH_CLAUSE, BUT_ONLY, NOT_CONFIRMED, MAIN_VERB_LAST])
def test_the_result_is_either_the_original_on_one_line_or_inside_the_limit(text):
    fitted = fit(text)
    assert fitted == " ".join(text.split()) or LO <= len(fitted) <= HI
    assert "\n" not in fitted and not fitted.endswith((", ", ","))


def test_the_qualifier_list_catches_denials_and_hedges():
    for word in ("but", "not", "no", "never", "unless", "only", "reportedly", "may", "rumored", "expected", "doesn't", "without"):
        assert seo._QUALIFIER_RE.search(f"the game {word} launch"), word
    assert not seo._QUALIFIER_RE.search("the studio said the game sold four million copies")


def test_generate_seo_fixes_a_description_that_is_a_few_characters_too_long_without_a_retry(tmp_path):
    articles, drafts = tmp_path / "articles", tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    over = "NVIDIA's flagship graphics card arrives with more memory and a higher price for high-end gaming PCs and creators in order to give buyers a faster option for games."
    assert len(over) == 163
    generate = make_generate([seo_data(description=over), seo_data()])
    result = seo.generate_seo(
        ARTICLE, make_facts(), generate=generate, entities_path=tmp_path / "entities.json",
        articles_dir=articles, drafts_dir=drafts,
    )
    assert result["description"] == over.replace(" in order to ", " to ") and len(result["description"]) == 154
    assert len(generate.calls) == 1


def test_generate_seo_puts_a_two_line_description_on_one_line_without_a_retry(tmp_path):
    articles, drafts = tmp_path / "articles", tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    two_lines = "NVIDIA confirms the RTX 5090 launches at $1,999 with more memory.\nIt goes on sale to gamers and creators."
    generate = make_generate([seo_data(description=two_lines), seo_data()])
    result = seo.generate_seo(
        ARTICLE, make_facts(), generate=generate, entities_path=tmp_path / "entities.json",
        articles_dir=articles, drafts_dir=drafts,
    )
    assert "\n" not in result["description"] and len(generate.calls) == 1


def test_generate_seo_still_retries_when_the_description_cannot_be_fixed_safely(tmp_path):
    articles, drafts = tmp_path / "articles", tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    generate = make_generate([seo_data(description=BUT_ONLY), seo_data()])
    result = seo.generate_seo(
        ARTICLE, make_facts(), generate=generate, entities_path=tmp_path / "entities.json",
        articles_dir=articles, drafts_dir=drafts,
    )
    assert len(generate.calls) == 2 and LO <= len(result["description"]) <= HI


def test_a_clause_after_a_safe_connecting_word_is_still_kept_when_it_carries_a_qualifier():
    text = "Nintendo says it has no plans for another Switch 2 price increase in the near term across every region and every retailer that it sells to, and only for the console."
    assert len(text) > HI and len(text[: text.rfind(", ")]) <= HI, "dropping the clause would fit, so only the qualifier check stops it"
    assert fit(text) == text                                          # "and" is a safe starter, but "only" and "not" are not


def test_a_cut_is_not_made_when_what_would_be_left_is_shorter_than_the_minimum():
    text = "Short start, with a long continuation that goes on describing many more details of the story for readers who want them all in one place and well beyond the limit here."
    assert len(text) > HI and text.index(", ") < LO
    assert fit(text) == text


def test_time_words_are_never_dropped_as_filler():
    text = "Square Enix is currently investigating an update bug that recently inflated the Nier: Automata PC download from about 50GB to 366GB and players now see it."
    padded = text + " Check the Steam page for more."
    assert len(padded) > HI
    fitted = fit(padded)
    for word in ("currently", "recently", "now"):
        assert word in fitted or fitted == " ".join(padded.split()), word
    assert fitted == " ".join(padded.split()) or all(word in fitted for word in ("currently", "recently"))
