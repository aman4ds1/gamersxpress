# Telegram topic bot (Cloudflare)

Send the bot a topic and the pipeline writes an article about it right away, instead of waiting for the next
scheduled run.

```
/blog GTA 6 | why the price matters | https://source.example/story https://other.example/story
```

- **Topic** (required): what to write about.
- **Idea** (optional, after the first `|`): the angle. It steers the writer; it is **never treated as a fact**.
- **URLs** (optional, anywhere in the message): extra sources.

Without URLs the pipeline uses matching news it has already collected (for GTA 6 that is usually dozens of
items from many outlets). The article still has to pass every normal check: facts confirmed by independent
sources, a verifier from a different model family, the quality gates, and `PUBLISH_MODE` (`draft` opens a pull
request, `auto` publishes). If it cannot be confirmed, nothing is published and the bot tells you why.

**What this Worker can do.** Start the one workflow `pipeline.yml` on `main` with a topic, idea and URLs. It
ignores every chat except yours, rejects calls that lack the secret header (404), has no dependencies, stores
nothing, and cannot write or publish anything itself. Messages are capped (topic 200, idea 600 characters, 5 URLs).

## One-time setup

You already have the GitHub token and the Telegram bot from `workers/pipeline-trigger/README.md`.

1. **Create the Worker.** Cloudflare dashboard > Workers & Pages > Create > Worker, name it
   `gamersxpress-telegram-topic`, deploy, then **Edit code** and paste in all of `workers/telegram-topic/index.mjs`.
   Keep the `workers.dev` address on (it is how Telegram reaches it).
2. **Add four secrets** (Settings > Variables and Secrets, type Secret):
   - `GITHUB_DISPATCH_TOKEN`: the same token the trigger Worker uses
   - `TELEGRAM_BOT_TOKEN`: your bot's token
   - `TELEGRAM_CHAT_ID`: your chat id (the same one the pipeline alerts go to)
   - `TELEGRAM_WEBHOOK_SECRET`: make up a long random string (letters and digits only)
3. **Point Telegram at the Worker** (PowerShell; use your real values):

```powershell
$bot = "YOUR_BOT_TOKEN"
$secret = "YOUR_TELEGRAM_WEBHOOK_SECRET"
$url = "https://gamersxpress-telegram-topic.YOUR-SUBDOMAIN.workers.dev"
Invoke-RestMethod -Uri "https://api.telegram.org/bot$bot/setWebhook" -Method Post `
  -Body (@{ url = $url; secret_token = $secret; allowed_updates = @("message") } | ConvertTo-Json) `
  -ContentType "application/json"
```

4. Send `/help` to the bot. It should answer with the usage text. Then send `/blog <topic>`.

Telegram only allows one webhook per bot. If something else already uses this bot's webhook, make a second bot
with @BotFather and use its token and chat.

The pipeline stops at `DAILY_CAP` articles a day (the repository variable `DAILY_CAP`, currently defaulting to 2
in `pipeline.yml`). Raise that variable before sending several topics in one day, or the extra ones will be refused
and the bot will tell you the daily cap was reached.
