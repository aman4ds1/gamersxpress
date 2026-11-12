// Starts the GamersXpress "Pipeline" workflow on GitHub on a fixed schedule.
//
// Why: GitHub's own `schedule:` trigger is best-effort. It starts only a few of the runs it is asked for, so the
// pipeline looked at news about four times a day instead of hourly. Cloudflare's cron triggers fire on time; this
// Worker's only job is to ask GitHub to start the workflow when Cloudflare says so.
//
// It serves no pages and has no dependencies. Setup and the token it needs: workers/pipeline-trigger/README.md.

const DEFAULTS = {
  repo: 'aman4ds1/gamersxpress',
  workflow: 'pipeline.yml',
  branch: 'main',
  api: 'https://api.github.com',
};

/** The GitHub API address that starts the workflow. */
export function dispatchUrl(env = {}) {
  const api = String(env.GITHUB_API_URL || DEFAULTS.api).replace(/\/+$/, '');
  const repo = env.GITHUB_REPO || DEFAULTS.repo;
  const workflow = env.WORKFLOW_FILE || DEFAULTS.workflow;
  return `${api}/repos/${repo}/actions/workflows/${workflow}/dispatches`;
}

/** A plain-language reason for a GitHub answer that was not "started". Never includes the token. */
export function explain(status, body = '') {
  let detail = '';
  try {
    detail = String(JSON.parse(body).message || '').slice(0, 200);
  } catch {
    detail = String(body || '').slice(0, 200);
  }
  const why = {
    401: 'GitHub rejected the token. It has probably expired, or the wrong one was pasted.',
    403: 'GitHub refused the request. The token may lack "Actions: Read and write" permission, or a rate limit was hit.',
    404: 'GitHub could not find the repository or the workflow, or the token has no access to this repository.',
    422: 'GitHub refused to start the workflow. Check that pipeline.yml has a workflow_dispatch trigger and that the branch exists.',
  }[status];
  const text = why || `GitHub answered ${status}.`;
  return detail ? `${text} (GitHub said: ${detail})` : text;
}

/** Ask GitHub to start the workflow. Returns {ok: true} or {ok: false, status?, reason}. */
export async function startPipeline(env = {}, fetchFn = fetch) {
  const token = env.GITHUB_DISPATCH_TOKEN;
  if (!token) return { ok: false, reason: 'The GITHUB_DISPATCH_TOKEN secret is not set on this Worker.' };
  let response;
  try {
    response = await fetchFn(dispatchUrl(env), {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'gamersxpress-pipeline-trigger',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: env.BRANCH || DEFAULTS.branch }),
    });
  } catch (error) {
    return { ok: false, reason: `Could not reach GitHub: ${String(error?.message || error).slice(0, 200)}` };
  }
  if (response.status === 204) return { ok: true };
  const body = await response.text().catch(() => '');
  return { ok: false, status: response.status, reason: explain(response.status, body) };
}

/** Send a Telegram message, if the optional Telegram secrets are set. Never throws. */
export async function alert(env = {}, text, fetchFn = fetch) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) return false;
  try {
    const response = await fetchFn(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** One scheduled tick: start the pipeline; on failure, say so on Telegram and fail loudly in Cloudflare's logs. */
export async function handle(env = {}, fetchFn = fetch) {
  const result = await startPipeline(env, fetchFn);
  if (result.ok) {
    console.log('pipeline-trigger: the Pipeline workflow was started');
    return result;
  }
  console.error(`pipeline-trigger: ${result.reason}`);
  await alert(
    env,
    `GamersXpress: the pipeline trigger could not start the pipeline.\n${result.reason}\n` +
      'Fix: see workers/pipeline-trigger/README.md in the repository.',
    fetchFn,
  );
  throw new Error(result.reason);
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(handle(env));
  },
  // Nothing here can start a run: the Worker only acts on Cloudflare's schedule.
  async fetch() {
    return new Response('Not found', { status: 404 });
  },
};
