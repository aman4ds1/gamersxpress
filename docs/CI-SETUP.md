# CI setup: scheduled pipeline, deploy and Telegram alerts

Three GitHub workflows (`.github/workflows/`):

| Workflow | When | What it does |
|---|---|---|
| `site-check.yml` | every push to `main`, every pull request | site build and checks, pipeline tests, the three dry runs |
| `pipeline.yml` | hourly, or by hand | runs the pipeline in **draft** mode, opens one pull request per finished article, sends a Telegram alert |
| `deploy.yml` | a site change lands on `main` (including merging a draft) | checks, builds and deploys to Cloudflare Workers |

**How an article gets published:** the pipeline finishes a story, pushes it to a branch
`draft/<slug>-<run id>` and opens a pull request. Telegram tells you. You read it, and
**merging the pull request publishes it** (Deploy runs by itself). Closing the pull
request rejects it.

Only `draft` mode exists here. `auto` mode is not wired up, on purpose
(docs/PLAN.md: stay in draft until about 50 articles are reviewed).

## One-time setup

### 1. Telegram bot

1. In Telegram, open **@BotFather**, send `/newbot`, and follow the steps. It gives you a bot token.
2. Send any message to your new bot.
3. Get your chat id: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser
   (with your token in place of `<TOKEN>`) and read `"chat":{"id": ...}`.

Never paste the token into a chat, an issue or a file in the repo.

### 2. GitHub secrets and variables

Repository **Settings > Secrets and variables > Actions**.

Secrets:

| Name | Used by |
|---|---|
| `GEMINI_API_KEY` | pipeline run |
| `GROQ_API_KEY` | pipeline run |
| `CLOUDFLARE_API_TOKEN` | deploy |
| `CLOUDFLARE_ACCOUNT_ID` | deploy |
| `TELEGRAM_BOT_TOKEN` | alerts |
| `TELEGRAM_CHAT_ID` | alerts |

Variables (the **Variables** tab):

| Name | Value | Meaning |
|---|---|---|
| `PAUSED` | `false` | set to `true` to stop all runs (kill switch) |
| `PUBLISH_MODE` | `draft` | the only supported value |
| `DAILY_CAP` | `2` | most articles drafted per day |

### 3. Repository settings

- **Settings > Actions > General > Workflow permissions:** choose *Read and write permissions* and
  tick **Allow GitHub Actions to create and approve pull requests**. Without this the
  draft branch is pushed but no pull request opens (the run shows a warning).
- **Settings > General > Pull Requests:** tick **Automatically delete head branches**, so
  merged and closed drafts do not pile up.
- **Cloudflare:** if Cloudflare's own Git integration also deploys this repository, turn it
  off. Deploy.yml already deploys on every site change.

## First tests (in this order)

Actions tab > **Pipeline** > **Run workflow**.

1. Tick **telegram_test** only. You should get a Telegram message. Nothing else runs.
2. Tick **dry_run** only. It runs on sample data: no API keys used, nothing saved, no pull request.
   The job should be green.
3. Run it with nothing ticked. This is the first real run. It creates the `pipeline-state`
   branch (see below) and, if a story is confirmed and passes its checks, opens the first
   draft pull request.

## Reviewing a draft

- Telegram links to the pull request. The article file is in **Files changed**.
- The run's **Artifacts** (kept 7 days) hold the facts sheet and reports for that run: check
  claims against `data/facts/<id>.json` and the sources.
- A draft that failed its checks never becomes a pull request. Its files are in the run
  artifact under `drafts/`, and Telegram says so.
- Relayed reports (one named source, several outlets) are always drafts; Telegram flags them.
- Bot-made pull requests do not start the Site check (a GitHub rule for pull requests created
  with the built-in token). Deploy runs the site checks before it publishes, and Site check
  runs on `main` after the merge.

## Stopping, and the circuit breaker

- **Stop everything:** set the variable `PAUSED` to `true`. Set it back to `false` to resume.
- **Circuit breaker:** after three failed runs in a row the pipeline pauses itself, sends a
  Telegram alert and opens an issue labelled `pipeline-breaker`. Fix the cause, then run
  **Pipeline** by hand with **reset_breaker** ticked.

## Where the state lives

The pipeline remembers its item pool, seen and failed lists, circuit breaker, published log
and feed cache on a branch called **`pipeline-state`**, never on `main`. Each run replaces
that branch with a single new commit, so it never grows. You can open it on GitHub to look
at the files. Deleting the branch starts the pipeline from a clean slate.

## Limits to know about

- **Cost of staying public vs going private.** GitHub-hosted runners are free for public
  repositories. A private repository on GitHub Free gets 2,000 Actions minutes a month; a
  real run takes about 10 to 20 minutes, so hourly runs would use far more than that.
  Before making the repository private, cut the schedule (edit the `cron` line in
  `pipeline.yml`), or plan for a paid plan. Check GitHub's current billing page for the numbers.
- **Artifacts are public while the repository is public.** They contain drafts, short quoted
  snippets in the facts sheets, and reports. They are deleted after 7 days.
- **Schedules can run late**, and GitHub may switch scheduled workflows off in a public
  repository after about 60 days without activity. If runs stop, re-enable the workflow on
  the Actions tab.
- **Spend.** The Gemini and Groq free tiers are used by default. Set a budget alert in each
  provider's console before moving to a paid tier.

## Not built yet

- `auto` publish mode (commit straight to `main`)
- the weekly summary message
- Lighthouse, link checking and automatic revert after a failed site check
- Search Console and PageSpeed data
