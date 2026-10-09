# Prompt design

Prompts are files, not Python strings. Editing a prompt never changes pipeline
code, and reviewing a prompt diff in a pull request shows the whole instruction
the model receives.

| File | Role | Used by |
|---|---|---|
| `writer.md` | `writer` | `pipeline/write.py` |

`verify.py` (verifier) and `facts.py` (fast role) still carry short system
instructions inline; they move here when they grow enough to need review.

## How a prompt runs

1. `pipeline/write.py` reads `writer.md` as text.
2. It replaces the one placeholder, `{{facts_sheet}}`, with the JSON facts
   sheet. A template without the placeholder is a hard error, so a botched edit
   fails the run instead of silently sending a prompt with no facts.
3. The resulting string is sent to the `writer` role, whose provider/model come
   from `pipeline/config.yaml`.

That is the whole mechanism: no templating language, no conditionals, no
partials. One placeholder, one substitution, one call.

## Design rules

**The writer gets the facts sheet and nothing else.** `write(facts, ...)` takes
one argument. Source text is never in it (the facts sheet stores claims and
source metadata, no prose), and `write.py` additionally rejects any input that
contains a `text` or `gathered_at` field with `WriterInputError` before the
prompt is built. This is PLAN.md principle 3 enforced in code rather than
trusted to the model.

**The prompt states the contract; code enforces it.** Every limit in
`writer.md` mirrors a constant that a gate already checks:

| Prompt rule | Enforced by |
|---|---|
| `title` ≤ 65 characters | `src/content.config.ts`, `checks/check-articles.mjs` |
| `description` 70-160 characters | same |
| `sources` ≥ 2, from the sheet | same |
| body ≥ 300 words | `checks/check-articles.mjs` |
| no `#` in the body, no skipped heading levels | `checks/check-articles.mjs`, `checks/seo-dist.mjs` |

When you change a limit in one place, change both. The prompt is a request, the
gate is the decision (PLAN.md principle 2: code decides, models assist).

**The body starts at `##` because the renderer owns the `<h1>`.**
`src/pages/news/[id].astro` renders front matter `title` as the page's only
`<h1>`, and `checks/seo-dist.mjs` requires exactly one `<h1>` per built page. A
`#` heading in the body would produce two.

**Deterministic values are pinned, not improvised.** `pubDate` is copied from
the facts sheet's `generated_at`, `sources` are copied verbatim, `image` /
`imageAlt` / `updatedDate` are omitted, and the category list is spelled out.
The writer never invents an SEO date or a source; the later SEO stage may
rewrite title and description in code anyway.

**Rumors are named as a category.** The prompt tells the writer exactly what to
do with `is_rumor: true` and low-confidence claims, because "be careful" is not
an instruction (PLAN.md principle 6). Prices, dates and numbers get the same
treatment: copy, never convert.

**Output is one Markdown document in one call.** Front matter plus body in a
single response keeps the document internally consistent (title, description
and body all describe the same facts) and gives the verifier one artifact to
check. Markdown, not JSON: the site renders Markdown, and JSON would add an
extraction step for no benefit.

**No secrets or private data in prompts.** Free-tier providers may log
requests (PLAN.md section 8). Facts sheets contain public news claims only.

## Changing a prompt

1. Edit the file. Keep `{{facts_sheet}}` in `writer.md`.
2. Keep every stated limit identical to the constants in
   `src/content.config.ts` and `checks/check-articles.mjs`.
3. Run `python -m pytest pipeline\ -q` (the tests assert the prompt file
   exists, has the placeholder, and still names each required section and
   front matter field).
4. For a real look, run a dry run: set `DRY_RUN=1` and run the pipeline. The
   mock provider echoes the full prompt back (`[mock:model] ...`) instead of
   calling a provider, so you can read exactly what the writer would receive.

Prompt text is instructions for a model, so keep it imperative and specific:
name the field, the count, the heading. Vague adjectives ("write a great
article") do not survive contact with a model; constants do.
