import datetime as dt

import link

NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)


def article(id, title, category, pub_date, entities):
    return {
        "id": id,
        "title": title,
        "category": category,
        "pubDate": pub_date,
        "entities": entities,
    }


def base_articles():
    return [
        article("a0", "Roundup of the week", "hardware", "2026-10-09T12:00:00+00:00", ["NVIDIA", "GeForce RTX 5090", "PlayStation 5"]),
        article("a1", "NVIDIA RTX 5090 review: the fastest GPU", "hardware", "2026-10-01T12:00:00+00:00", ["NVIDIA", "GeForce RTX 5090"]),
        article("a2", "PlayStation 5 Pro price and release details", "playstation", "2026-10-05T12:00:00+00:00", ["PlayStation 5"]),
        article("a3", "Xbox Series X gets a new dashboard", "xbox", "2026-10-08T12:00:00+00:00", ["Xbox Series X"]),
    ]


SELF = """---
title: Roundup of the week
---

## What happened

The GeForce RTX 5090 and the PlayStation 5 dominate the news, while the Xbox Series X waits.
"""


# --- index -------------------------------------------------------------------


def test_build_index_records_entities_and_keywords(tmp_path):
    path = tmp_path / "index.json"
    index = link.build_index(base_articles(), path)
    assert path.is_file()
    assert link.load_index(path)["by_entity"]["nvidia"] == ["a0", "a1"]
    assert "geforce rtx 5090" in index["by_entity"]
    assert index["articles"][1]["url"] == "/news/a1"


# --- related -----------------------------------------------------------------


def test_select_related_ranks_shared_entity_and_category():
    index = link.build_index(base_articles())
    related = link.select_related(index, "a0", now=NOW)
    assert related[0]["id"] == "a1"
    assert all(item["id"] != "a0" for item in related)
    assert len(related) <= link.RELATED_LIMIT


def test_select_related_skips_articles_younger_than_a_day():
    articles = base_articles() + [
        article("a4", "NVIDIA teaser", "hardware", "2026-10-09T06:00:00+00:00", ["NVIDIA"])
    ]
    related = link.select_related(link.build_index(articles), "a0", now=NOW)
    assert all(item["id"] != "a4" for item in related)


# --- inline links ------------------------------------------------------------


def test_add_links_inserts_one_link_per_target():
    index = link.build_index(base_articles())
    result = link.add_links(SELF, index, self_id="a0", now=NOW)
    ids = [item["id"] for item in result.links]
    assert set(ids) == {"a1", "a2", "a3"}
    assert len(ids) == len(set(ids))
    assert result.article.count("/news/a1") == 1


def test_add_links_never_touches_headings():
    body = "---\ntitle: t\n---\n\n## GeForce RTX 5090 goes on sale\n\nThe card is here.\n"
    index = link.build_index(base_articles())
    result = link.add_links(body, index, self_id="a0", now=NOW)
    assert result.links == []
    assert "## GeForce RTX 5090 goes on sale" in result.article


def test_add_links_caps_at_max_links():
    articles = [
        article("s", "Self", "pc", "2026-10-09T00:00:00+00:00", []),
    ]
    names = ["Alpha One", "Beta Two", "Gamma Three", "Delta Four", "Epsilon Five", "Zeta Six"]
    for i, name in enumerate(names):
        articles.append(article(f"t{i}", f"{name} story", "pc", "2026-10-01T00:00:00+00:00", [name]))
    self_body = "---\ntitle: s\n---\n\n## Body\n\n" + " and ".join(names) + " all happened.\n"
    result = link.add_links(self_body, link.build_index(articles), self_id="s", now=NOW)
    assert len(result.links) == link.MAX_LINKS


def test_add_links_never_inside_existing_links_or_urls():
    body = "---\ntitle: t\n---\n\n## Body\n\nSee [GeForce RTX 5090 review](/news/a1) and https://example.com/GeForce-RTX-5090 here.\n"
    index = link.build_index(base_articles())
    result = link.add_links(body, index, self_id="a0", now=NOW)
    assert all(item["id"] != "a1" for item in result.links)


def test_add_links_skips_code_fences():
    body = "---\ntitle: t\n---\n\n## Body\n\n```\nGeForce RTX 5090\n```\n"
    index = link.build_index(base_articles())
    result = link.add_links(body, index, self_id="a0", now=NOW)
    assert result.links == []


def test_add_links_skips_sources_section_and_adds_none():
    body = "---\ntitle: t\n---\n\n## Body\n\nNothing here.\n\n## Sources\n\n- GeForce RTX 5090 launch\n"
    index = link.build_index(base_articles())
    result = link.add_links(body, index, self_id="a0", now=NOW)
    assert result.links == []
    assert result.article.count("## Sources") == 1
    assert link.has_sources_section(result.article)


def test_add_links_no_matches_adds_nothing():
    body = "---\ntitle: t\n---\n\n## Body\n\nNothing relevant in this sentence.\n"
    index = link.build_index(base_articles())
    result = link.add_links(body, index, self_id="a0", now=NOW)
    assert result.links == []


def test_add_links_never_self_links():
    index = link.build_index(base_articles())
    result = link.add_links(SELF, index, self_id="a0", now=NOW)
    assert all(item["id"] != "a0" for item in result.links)


def test_add_links_preserves_front_matter():
    index = link.build_index(base_articles())
    result = link.add_links(SELF, index, self_id="a0", now=NOW)
    assert result.article.startswith("---\ntitle: Roundup of the week\n---\n")
