# Pipeline trigger (Cloudflare)

A tiny Cloudflare Worker that tells GitHub to start the **Pipeline** workflow every 3 hours.

**Why it exists.** GitHub's own `schedule:` trigger is best-effort: it started only about 4 of the 24 hourly
runs it was asked for, so the pipeline looked at news every 5 to 8 hours. Cloudflare's cron triggers fire on
time. This Worker only asks GitHub to start the workflow; the pipeline itself still runs on GitHub as before.

**What it can do.** Start one workflow, `pipeline.yml`, on branch `main`. It serves no pages (a web visit gets
a 404 and starts nothing), has no dependencies, and uses a token that can only run workflows in this
repository.

## One-time setup (about 15 minutes)

Cloudflare and GitHub change their menus now and then. If a name below does not match what you see, use the
closest one, or ask.

### 1. Make a GitHub token

1. On GitHub, open **Settings** (your profile menu) > **Developer settings** > **Personal access tokens** >
   **Fine-grained tokens** > **Generate new token**.
2. **Token name:** `gamersxpress-pipeline-trigger`. **Expiration:** 1 year.
3. **Repository access:** choose **Only select repositories**, then pick `gamersxpress`.
4. **Permissions > Repository permissions > Actions:** set to **Read and write**. Leave everything else alone.
5. Click **Generate token** and **copy it now** (it starts with `github_pat_`). GitHub shows it only once.

**Test the token before going on.** In PowerShell (this really starts one pipeline run, which takes about 15
minutes and shows in the Actions tab as "Manually run"):

```powershell
$token = "PASTE_THE_TOKEN_HERE"
Invoke-RestMethod -Method Post `
  -Uri "https://api.github.com/repos/aman4ds1/gamersxpress/actions/workflows/pipeline.yml/dispatches" `
  -Headers @{ Authorization = "Bearer $token"; Accept = "application/vnd.github+json"; "X-GitHub-Api-Version" = "2022-11-28"; "User-Agent" = "test" } `
  -Body '{"ref":"main"}' -ContentType "application/json"
```

No output means it worked. An error means the token is wrong or lacks the Actions permission.

### 2. Create the Worker on Cloudflare

1. Open the Cloudflare dashboard > **Workers & Pages** (it may be listed under **Compute**).
2. **Create** > **Worker** (start from "Hello World"). Name it `gamersxpress-pipeline-trigger`. Click **Deploy**.
3. Click **Edit code**. Select everything in the editor, delete it, and paste in the whole of
   `workers/pipeline-trigger/index.mjs` from this repository. Click **Deploy**.

### 3. Add the token as a secret

On the Worker's page: **Settings** > **Variables and Secrets** > **Add**.

- **Type:** Secret
- **Variable name:** `GITHUB_DISPATCH_TOKEN`
- **Value:** the token from step 1

Click **Deploy**.

### 4. Add the schedule

On the Worker's page: **Settings** > **Trigger Events** (or **Triggers**) > **Cron Triggers** > **Add**.
Enter this expression and save:

```
7 */3 * * *
```

That means: at 7 minutes past every third hour, 00:07, 03:07, 06:07 and so on, in UTC (05:37, 08:37, 11:37 and
so on in India).

### 5. Get a Telegram message if it ever fails (recommended)

If the token expires or is removed, the runs would stop quietly. To be told instead, add two more secrets in the
same place, with the **same values** as the `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets you already
set in GitHub: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. A failure then sends a message with the reason.
(At most 8 a day, one for each failed attempt.)

### 6. Check that it works

After the next 3-hour mark, the Actions tab should show a **Pipeline** run described as
"Manually run by" your GitHub name. On the Worker's page, **Observability** (or **Logs**) shows the same moment
and the line `pipeline-trigger: the Pipeline workflow was started`.

## Afterwards

- GitHub's own hourly schedule in `.github/workflows/pipeline.yml` is still on, so for now you will see both.
  Once this Worker is confirmed, reduce that schedule to a once-a-day safety net, or remove it.
- **The token expires in a year.** GitHub emails you beforehand. Make a new one the same way and replace the
  `GITHUB_DISPATCH_TOKEN` secret. Nothing else changes.
- **To change how often it runs,** edit the cron expression in step 4 (and keep `wrangler.jsonc` in step with it).
- **To stop it,** delete the cron trigger on the Worker, or set the repository variable `PAUSED` to `true` in
  GitHub (the pipeline then skips its runs).

## If you prefer the command line

```powershell
cd workers\pipeline-trigger
npx wrangler login
npx wrangler secret put GITHUB_DISPATCH_TOKEN
npx wrangler deploy
```

`wrangler.jsonc` already holds the name, the schedule and "no public web address".
