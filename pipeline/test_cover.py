import pytest
from PIL import Image

import cover

HEADLINE = "NVIDIA launches GeForce RTX 5090 at $1,999"


def test_generate_cover_writes_webp_within_limits(tmp_path):
    result = cover.generate_cover("test-slug", HEADLINE, "hardware", output_dir=tmp_path)
    path = tmp_path / "test-slug.webp"
    assert path.is_file()
    assert path.stat().st_size <= cover.MAX_COVER_BYTES
    with Image.open(path) as image:
        assert image.format == "WEBP"
        assert image.size == (cover.WIDTH, cover.HEIGHT)
    assert result["image"] == "/covers/test-slug.webp"


def test_front_matter_and_alt_describe_the_cover():
    result = cover.front_matter("test-slug", HEADLINE, "playstation")
    assert result["image"] == "/covers/test-slug.webp"
    assert "PlayStation" in result["imageAlt"]
    assert HEADLINE in result["imageAlt"]


def test_wrap_text_never_exceeds_width():
    font = cover.load_font(84)
    max_width = cover.WIDTH - 2 * cover.MARGIN
    lines = cover.wrap_text("A very long headline about hardware and games " * 12, font, max_width)
    assert lines
    assert all(font.getlength(line) <= max_width for line in lines)


def test_wrap_text_breaks_a_single_long_word():
    font = cover.load_font(84)
    max_width = 200
    lines = cover.wrap_text("Supercalifragilisticexpialidocious" * 3, font, max_width)
    assert lines
    assert all(font.getlength(line) <= max_width for line in lines)


def test_render_cover_long_headline_does_not_overflow(tmp_path):
    long_headline = "A dramatically long headline about the next generation of gaming hardware " * 4
    path = cover.render_cover(long_headline, "tech", slug="long", output_dir=tmp_path)
    with Image.open(path) as image:
        assert image.size == (cover.WIDTH, cover.HEIGHT)


def test_palette_is_deterministic(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    cover.render_cover(HEADLINE, "hardware", slug="same", output_dir=first)
    cover.render_cover(HEADLINE, "hardware", slug="same", output_dir=second)
    assert (first / "same.webp").read_bytes() == (second / "same.webp").read_bytes()


def test_category_label_known_and_fallback():
    assert cover.category_label("gaming-news") == "Gaming News"
    assert cover.category_label("weird-cat") == "Weird Cat"


def test_missing_font_raises(tmp_path):
    with pytest.raises(cover.CoverError):
        cover.load_font(20, tmp_path)
