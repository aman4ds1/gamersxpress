import re
from pathlib import Path

import pytest

import seo
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
            {"source_name": "NVIDIA", "link": "https://nvidia.example/5090", "tier": 1, "owner": "nvidia", "region": "global"}
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


def test_truncate_slug_cuts_64_char_slug_at_word_boundary():
    assert len(LONG_SLUG_64) == 64
    cut = seo.truncate_slug(LONG_SLUG_64)
    assert len(cut) <= seo.MAX_SLUG
    assert seo.SLUG_RE.fullmatch(cut)
    assert LONG_SLUG_64.startswith(cut + "-")


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
        slug="estimated-gears-of-war-e-day-sales-raise-query-about-last-minute",
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
    assert result["slug"] == LONG_SLUG_CUT
    assert len(result["slug"]) <= seo.MAX_SLUG
    assert result["slug"] != data["slug"]
    assert result["tags"] == ["game-pass", "xbox", "pc-gaming"]
    assert len(generate.calls) == 1


def test_generate_seo_avoids_slug_collision(tmp_path):
    articles = tmp_path / "articles"
    drafts = tmp_path / "drafts"
    articles.mkdir()
    drafts.mkdir()
    (articles / f"{LONG_SLUG_CUT}.md").write_text("---\n---\n", encoding="utf-8")
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
    assert result["slug"] == f"{LONG_SLUG_CUT}-2"
    assert len(result["slug"]) <= seo.MAX_SLUG
    assert len(generate.calls) == 1
