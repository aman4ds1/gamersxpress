# GamersXpress: Project Plan

Reference for humans and coding agents. Read this before any pipeline work and follow it. If a task conflicts with this plan, say so instead of improvising.

## 1. Goal

Relaunch gamersxpress.com as a gaming news publication plus a small set of browser tools. Publishing is automated, but the content must be useful, original, and fact-checked. This is not an RSS-rewrite site.

Topics: gaming news, PC, PlayStation, Xbox, Nintendo, hardware, gaming tech (NVIDIA/AMD/Intel, handhelds, VR/AR, AI in gaming, engines), esports, and Indian gaming and esports.

## 2. Principles

1. **Quality over volume.** Start at 2-4 articles per day. Never publish to hit a number.
2. **Code decides, models assist.** SEO output, checks, and publish gates are deterministic code. Models only write text (articles, titles, descriptions, alt text) and extract facts.
3. **Facts first.** The writer sees only a verified facts sheet, never the source articles' prose.
4. **Writer and verifier are different model families.**
5. **When unsure, publish nothing.** Any failed gate, rate limit, or provider outage means skip the run.
6. **Rumors are labeled or skipped.** Never state a rumor as fact. Only confirmed pricing and availability appear as fact.
7. **No secrets in the repo.** API keys live in GitHub Secrets and local `.env` (gitignored).
8. **Free by default.** No paid services without an explicit decision.

## 3. Architecture

```
gamersxpress.com (DNS on Cloudflare, registrar GoDaddy)
  -> Cloudflare Pages (static hosting)
  -> Astro site (static output, no server code, no database)
  -> GitHub repo (content is Markdown files)
  -> GitHub Actions (pipeline, checks, reports)
```

Two sections on one site:

- `/news`, `/category`, `/topic`: article content (automated pipeline)
- `/tools`: static pages with client-side JavaScript. No server calls. The pipeline never touches them.

## 4. Repo layout

```
AGENTS.md                  agent rules (short)
docs/PLAN.md               this file
astro.config.mjs           site: https://gamersxpress.com
src/
  content.config.ts        glob loader + Zod schema (import z from astro/zod)
  content/articles/*.md    published articles
  components/SEO.astro     meta, canonical, OG, Twitter
  components/JsonLd.astro  Organization, WebSite, NewsArticle, BreadcrumbList
  pages/                   home, news/[id], category/[category], topic/[entity],
                           tools/, search, about, editorial-policy,
                           ai-disclosure, corrections, contact
pipeline/
  config.yaml              sources, trust tiers, role->provider/model map, caps
  providers.py             generate(role, prompt) with fallback chain
  ingest.py                fetch feeds
  cluster.py               dedupe and cluster
  score.py                 pick the story
  gather.py                fetch source text
  facts.py                 build and validate facts sheet
  write.py                 generate article from facts sheet only
  verify.py                independent claim check
  seo.py                   title, description, slug, tags, entities, alt text
  link.py                  internal links
  publish.py               gates, commit or draft
checks/                    quality-gate scripts and their tests
data/
  seen.json                covered story clusters
  candidates/              per-run scored candidates
  facts/                   facts sheets (one per article id)
  topic-performance.json   fed by weekly Search Console report
drafts/                    articles that failed gates (never deployed)
.github/workflows/         publish.yml, site-check.yml, weekly.yml
```

## 5. Article schema

Collection `articles` (`src/content.config.ts`, glob loader over `src/content/articles/**/*.md`). URLs use `entry.id`.

| Field | Type |
|---|---|
| title | string, 65 characters max |
| description | string, 70-160 characters |
| pubDate | date |
| updatedDate | date, optional |
| category | enum: gaming-news, pc, playstation, xbox, nintendo, hardware, tech, esports, india |
| tags | string[] |
| entities | string[] (for example NVIDIA, PS5, Xbox, Nintendo Switch 2) |
| sources | {name, url}[], at least 2 for published articles |
| image | string path, optional |
| imageAlt | string, required if image is set |

## 6. Pipeline stages

Each stage reads and writes JSON files so it can be tested alone. A run publishes at most one article.

1. **Pause check.** Stop if `PAUSED=true` or today's published count reached `DAILY_CAP`.
2. **Ingest.** Fetch RSS from `config.yaml` sources. Tier 1: official sources (NVIDIA, AMD, Intel, PlayStation Blog, Xbox Wire, Nintendo, Steam, Epic, Valve, studio blogs). Tier 2: reputable outlets. Include Indian gaming and esports sources.
3. **Dedupe and cluster.** Normalize URLs, group items about one story using fuzzy title match plus entity match. Skip clusters in `data/seen.json` (with a cooldown window).
4. **Score.** Official source present, number of independent outlets, category fit, recency, performance hints from `data/topic-performance.json`. Pick the top cluster above a minimum score, otherwise end the run.
5. **Gather.** Fetch full text of every source in the cluster. Respect robots.txt. Source text is for facts only.
6. **Facts sheet.** Extract claims as JSON: claim, value, source URL, confidence. Require at least one tier-1 source or two independent tier-2 sources. Drop unconfirmed claims or mark them `rumor`.
7. **Write.** Input: facts sheet only. Output: original article covering what happened, why gamers should care, context (for example previous generation comparison), and what is unconfirmed. Include a "Sources" section.
8. **Verify.** A different model family checks each claim in the draft against the facts sheet. Code also checks that every number, price, date, and spec in the article appears in the facts sheet. Any unsupported claim fails the run.
9. **SEO.** Generate title, description, slug, tags, entities, and image alt text. Code validates lengths and formats.
10. **Internal links.** Match entities and keywords against existing articles. Link first mentions only, 3-5 links max, plus a related-articles block.
11. **Quality gates** (section 8).
12. **Publish.** Pass: commit to `main`. Fail: write to `drafts/` and open an Issue.

## 7. Providers and models

Code calls `generate(role, prompt)`. Model names and provider order are read from `pipeline/config.yaml` and never hardcoded, because free-tier eligibility changes.

| Role | Purpose | Default provider |
|---|---|---|
| fast | scoring, extraction, SEO text | Cerebras or Groq (Gemini Flash while starting) |
| writer | article generation | Gemini Flash (Google AI Studio) |
| verifier | independent claim check | Mistral |

Fallback: next provider in the list for that role; last resort OpenRouter free models. If the verifier's providers are all unavailable, skip the run. Never publish unverified.

Secrets: `GEMINI_API_KEY`, `MISTRAL_API_KEY`, optional `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`. Treat free tiers as possibly logged or used for training: never put secrets or private data in prompts. Limits differ by provider and change often; check each provider's dashboard.

## 8. Quality gates (code, always run)

An article must pass all of these to publish:

- Frontmatter matches the schema
- Title at most 65 characters; description 70-160 characters
- Exactly one H1; heading levels do not skip
- Body at least 300 words
- At least 2 sources, all cited and linked in the article
- Image alt text present when an image is set
- Text overlap with source articles below threshold (n-gram check)
- No duplicate slug or title; no near-duplicate of an existing article
- All links resolve
- No banned filler phrases
- Verifier passed

Site-level checks on every push: build, Lighthouse CI, linkinator, and a scan of `dist/` for one H1, a canonical tag, and parseable JSON-LD on every page. If a check fails after an article commit, revert that commit and open an Issue.

## 9. Technical SEO (generated in code, not by models)

Title, meta description, canonical URL, Open Graph and Twitter cards, JSON-LD (Organization, WebSite, NewsArticle with author and publisher, BreadcrumbList), XML sitemap, robots.txt, RSS feed, image alt text, heading structure, internal links, fast static pages. Trust pages are required: About, Editorial Policy, AI disclosure, Corrections, Contact. Articles show an "Updated" date when edited.

After deploy: ping IndexNow (Bing, Yandex), resubmit the sitemap through the Search Console API. Do not use the Google Indexing API for articles.

## 10. Safety layer

- **Kill switch:** repo variable `PAUSED=true` stops all runs.
- **Daily cap:** `DAILY_CAP` (start at 2-4).
- **Circuit breaker:** after 3 consecutive failed runs, set `PAUSED=true` and open an Issue.
- **Concurrency:** one publish run at a time.
- **Publish modes** (`PUBLISH_MODE`): `draft` opens a PR per article for human merge; `auto` commits directly when all gates pass. Stay in `draft` until about 50 articles have been reviewed.
- **Alerts:** failed workflows email the owner; optional Telegram message.
- Batch commits: one commit per run, so Cloudflare Pages stays well under its monthly build limit.

## 11. Workflows

- `publish.yml`: schedule about every 3 hours (schedules can be delayed). Runs the pipeline and commits.
- `site-check.yml`: on every push to `main` and on pull requests. Build, checks, auto-revert on failure.
- `weekly.yml`: pulls Search Console and PageSpeed data, writes a short report, updates `data/topic-performance.json`.

## 12. Hosting limits to respect

Cloudflare Pages free plan: 500 builds per month, one build at a time, 20-minute build timeout, 20,000 files per site, 25 MiB per file. Static bandwidth is unmetered. Keep images small, use one cover image per article, and check the file count occasionally. If builds become a constraint, build in GitHub Actions and deploy with `wrangler`.

## 13. Tools section

Tools live under `/tools` and run fully in the browser, with data in JSON files in the repo. Each tool page has a title, a short explanation, the tool, and a "how it works" section, with `<SEO />` applied. Build order: mouse sensitivity converter, resolution and frame-time calculator, PSU wattage estimator, storage and download-time calculator. "Can my PC run it" comes later and must say "meets the published requirements", never promise performance. Do not scrape benchmark sites.

## 14. Rollout order

1. Astro skeleton, layout, SEO component, JSON-LD, sitemap, RSS, robots.txt
2. Content collection, article and category pages, sample articles
3. Trust pages, topic hubs, search
4. Quality-gate scripts and tests
5. Site-check workflow
6. Provider function and config
7. Ingest, dedupe, score (review by hand for a few days)
8. Facts sheet, writer, verifier
9. SEO generation and internal links
10. Publish workflow in `draft` mode
11. After about 50 reviewed articles, switch to `auto` at 2-4 per day
12. Weekly report, social posting, tools section

## 15. Non-goals

- No WordPress, no database, no paid SEO tools, no paid backlinks
- No agent frameworks in the pipeline
- No scraping of images or benchmark data from other sites
- No guaranteed rankings; automation controls what we publish, not what Google shows
