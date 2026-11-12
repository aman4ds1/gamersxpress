"""Give articles that were published without a picture one, using the same steps as a new article.

    python -m pipeline.backfill                  # look, show what each article would get, change nothing
    python -m pipeline.backfill --apply          # write the pictures and update the articles
    python -m pipeline.backfill --apply --upgrade

For each article without a ``hero`` it tries, in order, Steam key art for a PC game in the headline, a freely
licensed Wikimedia Commons photo of the product, and then GamersXpress's own picture for the article's category.
Run it on a computer with internet access: Steam and Wikimedia are contacted for the first two steps.

An article that already has a picture is never touched. ``--upgrade`` also looks again at articles whose picture
is only the category picture, in case a real photo can now be found. Nothing is written without ``--apply``.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

# Support `python -m pipeline.backfill` (namespace package): make sibling modules importable by their top-level
# names before any of them are imported.
_PIPELINE_DIR = Path(__file__).resolve().parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import gates as gates_stage
import images as images_stage
import run as run_module

PROJECT_ROOT = _PIPELINE_DIR.parent
ARTICLES_DIR = PROJECT_ROOT / "src" / "content" / "articles"
PUBLIC_DIR = PROJECT_ROOT / "public"

#: The credit and license of GamersXpress's own category pictures (see images.default_hero).
OWN_CREDIT = "GamersXpress"
OWN_LICENSE = "Own work"

Finder = Callable[..., "dict | None"]


def kind_of(hero: dict[str, Any] | None) -> str:
    """A short name for where a picture came from."""
    if not hero:
        return "none"
    license_name = str(hero.get("license") or "")
    if license_name == "Steam store":
        return "Steam art"
    if license_name == OWN_LICENSE and hero.get("credit") == OWN_CREDIT:
        return "category picture"
    return "Commons photo"


def _is_category_picture(hero: Any) -> bool:
    return isinstance(hero, dict) and hero.get("license") == OWN_LICENSE and hero.get("credit") == OWN_CREDIT


def _lookup_folder(public_dir: Path, scratch: Path) -> Path:
    """A throwaway public folder for the lookups, with the category pictures copied in.

    Steam and Commons write the picture they find into ``<public>/images/<slug>.webp`` while looking. Doing that in
    a scratch folder keeps a preview from writing anything into the site.
    """
    images = scratch / "images"
    images.mkdir(parents=True, exist_ok=True)
    for picture in (public_dir / "images").glob("default-*.webp"):
        shutil.copyfile(picture, images / picture.name)
    return scratch


def backfill(
    *,
    articles_dir: Path = ARTICLES_DIR,
    public_dir: Path = PUBLIC_DIR,
    apply: bool = False,
    upgrade: bool = False,
    slugs: list[str] | None = None,
    finder: Finder | None = None,
    say: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Look for a picture for each article that needs one. Returns one record per article looked at.

    Each record has ``slug``, ``action`` (``skip``, ``add``, ``replace``, ``none`` or ``rejected``), ``kind`` and, for
    an add or replace, the ``hero``. Articles and pictures are only written when ``apply`` is true.
    """
    find = finder or run_module.find_any_hero
    say = say or images_stage.safe_print
    articles_dir, public_dir = Path(articles_dir), Path(public_dir)
    paths = sorted(articles_dir.glob("*.md"))
    wanted = set(slugs or [])
    unknown = wanted - {path.stem for path in paths}
    if unknown:
        raise images_stage.ImageError(f"no such article: {', '.join(sorted(unknown))}")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="gx-backfill-") as scratch_name:
        scratch = _lookup_folder(public_dir, Path(scratch_name))
        for path in paths:
            slug = path.stem
            if wanted and slug not in wanted:
                continue
            text = path.read_text(encoding="utf-8")
            front, _body = gates_stage.parse_front_matter(text)
            current = front.get("hero")
            if current and not (upgrade and _is_category_picture(current)):
                results.append({"slug": slug, "action": "skip", "kind": kind_of(current if isinstance(current, dict) else None)})
                say(f"{slug}: already has a picture, left alone")
                continue
            found = find(
                title=str(front.get("title") or ""),
                entities=[str(item) for item in front.get("entities") or []],
                slug=slug,
                public_dir=scratch,
                category=str(front.get("category") or "") or None,
            )
            if not found:
                results.append({"slug": slug, "action": "none", "kind": "none"})
                say(f"{slug}: no picture found")
                continue
            hero = dict(found["hero"])
            kind = kind_of(hero)
            if current and kind == "category picture":
                results.append({"slug": slug, "action": "skip", "kind": "category picture"})
                say(f"{slug}: still only the category picture, left alone")
                continue
            action = "replace" if current else "add"
            record = {"slug": slug, "action": action, "kind": kind, "hero": hero}
            destination = public_dir / "images" / f"{slug}.webp"
            if apply:
                problem = _write(path, text, hero, Path(found["path"]), destination, public_dir)
                if problem:
                    record["action"], record["problem"] = "rejected", problem
                    say(f"{slug}: not used, {problem}")
                    results.append(record)
                    continue
            who = f"{hero.get('credit')}, {hero.get('license')}"
            verb = ("replaced" if apply else "would replace") if action == "replace" else ("added" if apply else "would add")
            say(f"{slug}: {verb} {kind} ({who})")
            results.append(record)
    return results


def _write(path: Path, text: str, hero: dict[str, Any], source: Path, destination: Path, public_dir: Path) -> str | None:
    """Copy the picture in and set the article's hero. Returns a reason when it cannot be used, and writes nothing."""
    previous = destination.read_bytes() if destination.exists() else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
        problems = images_stage.hero_failures(hero, public_dir)
        if problems:
            raise images_stage.ImageError("; ".join(problems))
        path.write_text(images_stage.set_hero(text, hero), encoding="utf-8")
    except (images_stage.ImageError, OSError) as error:
        if previous is None:
            destination.unlink(missing_ok=True)
        else:
            destination.write_bytes(previous)
        return str(error)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.backfill", description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the pictures and update the articles (default: only show)")
    parser.add_argument("--upgrade", action="store_true", help="also look again at articles that only have the category picture")
    parser.add_argument("--slug", action="append", default=None, help="only this article (the file name without .md); repeatable")
    parser.add_argument("--articles-dir", default=str(ARTICLES_DIR), help="default: src/content/articles")
    parser.add_argument("--public-dir", default=str(PUBLIC_DIR), help="default: public")
    parser.add_argument("--verbose", action="store_true", help="show every Steam and Commons step")
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        results = backfill(
            articles_dir=Path(args.articles_dir), public_dir=Path(args.public_dir),
            apply=args.apply, upgrade=args.upgrade, slugs=args.slug,
        )
    except (images_stage.ImageError, OSError) as error:
        images_stage.safe_print(f"error: {error}", file=sys.stderr)
        return 1
    changed = [r for r in results if r["action"] in ("add", "replace")]
    images_stage.safe_print("")
    if args.apply:
        images_stage.safe_print(f"{len(changed)} article(s) updated. Next: npm run check, then commit public\\images and src\\content\\articles.")
    elif changed:
        images_stage.safe_print(f"{len(changed)} article(s) would get a picture. Nothing was changed. Run again with --apply to write them.")
    else:
        images_stage.safe_print("Nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
