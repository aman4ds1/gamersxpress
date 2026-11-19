// Telegram -> GamersXpress: "write about this topic now".
//
// The owner messages the bot:   /blog GTA 6 | why the price matters | https://source.example/story
// This Worker checks the message really came from Telegram AND from the owner's chat, then asks GitHub to start the
// Pipeline workflow with the topic, idea and URLs as inputs. The pipeline does the writing, verifying and publishing
// exactly as for any other story (see pipeline/manual.py); this Worker never writes or publishes anything itself.
//
// No dependencies, no storage. Setup: workers/telegram-topic/README.md.

const DEFAULTS = {
  repo: 'aman4ds1/gamersxpress',
  workflow: 'pipeline.yml',
  branch: 'main',
  api: 'https://api.github.com',
};

export const HELP =
  'Send a topic for a new article:\n' +
  '/blog GTA 6 | your idea or angle | https://source-url-1 https://source-url-2\n\n' +
  'Only the topic is required. The idea steers the angle; it is not treated as a fact. URLs are extra sources. ' +
  'Without URLs I use matching news the pipeline already collected.';

const LIMITS = { topic: 200, idea: 600, urls: 5 };

/** Compare two strings without leaking where they differ. */
export function safeEqual(a, b) {
  const x = String(a ?? '');
  const y = String(b ?? '');
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
    diff |= (x.charCodeAt(i) || 0) ^ (y.charCodeAt(i) || 0);
  }
  return diff === 0;
}

/**
 * Read a Telegram message. Returns {kind: 'help'}, {kind: 'blog', topic, idea, urls}, {kind: 'bad', reason}
 * or null when the message is not for this bot.
 */
export function parseCommand(text) {
  const raw = String(text ?? '').trim();
  const match = raw.match(/^\/(blog|topic|write|start|help)(?:@\w+)?(?:\s+([\s\S]*))?$/i);
  if (!match) return null;
  const command = match[1].toLowerCase();
  if (command === 'start' || command === 'help') return { kind: 'help' };
  const rest = String(match[2] ?? '');
  const urls = [];
  for (const found of rest.match(/https?:\/\/[^\s<>"')]+/g) ?? []) {
    const url = found.replace(/[.,;]+$/, '');
    if (!urls.includes(url)) urls.push(url);
  }
  const withoutUrls = rest.replace(/https?:\/\/[^\s<>"')]+/g, ' ');
  const parts = withoutUrls
    .split('|')
    .map((part) => part.replace(/\s+/g, ' ').trim())
    .filter(Boolean);
  const topic = (parts[0] ?? '').slice(0, LIMITS.topic);
  if (!topic) return { kind: 'bad', reason: 'Add a topic after /blog.' };
  const idea = parts.slice(1).join(' ').slice(0, LIMITS.idea);
  return { kind: 'blog', topic, idea, urls: urls.slice(0, LIMITS.urls) };
}

/** The GitHub API address that starts the workflow. */
export function dispatchUrl(env = {}) {
  const api = String(env.GITHUB_API_URL || DEFAULTS.api).replace(/\/+$/, '');
  const repo = env.GITHUB_REPO || DEFAULTS.repo;
  const workflow = env.WORKFLOW_FILE || DEFAULTS.workflow;
  return `${api}/repos/${repo}/actions/workflows/${workflow}/dispatches`;
}

/** Ask GitHub to start the workflow for one topic. Returns {ok: true} or {ok: false, reason}. */
export async function startTopicRun(env, request, fetchFn = fetch) {
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
        'User-Agent': 'gamersxpress-telegram-topic',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        ref: env.BRANCH || DEFAULTS.branch,
        inputs: { topic: request.topic, idea: request.idea, urls: request.urls.join(' ') },
      }),
    });
  } catch (error) {
    return { ok: false, reason: `Could not reach GitHub: ${String(error?.message || error).slice(0, 200)}` };
  }
  if (response.status === 204) return { ok: true };
  return { ok: false, reason: `GitHub answered ${response.status}. Check the token and the workflow.` };
}

/** Send a Telegram message. Never throws. */
export async function reply(env, chatId, text, fetchFn = fetch) {
  if (!env.TELEGRAM_BOT_TOKEN) return false;
  try {
    const response = await fetchFn(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Handle one webhook call. Always answers 200 to genuine Telegram calls (so Telegram does not retry), and 404 to
 * anything without the secret header. Messages from any chat other than the owner's are ignored without a reply.
 */
export async function handle(request, env = {}, fetchFn = fetch) {
  if (request.method !== 'POST') return new Response('Not found', { status: 404 });
  const expected = env.TELEGRAM_WEBHOOK_SECRET;
  const given = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
  if (!expected || !safeEqual(given, expected)) return new Response('Not found', { status: 404 });

  let update;
  try {
    update = await request.json();
  } catch {
    return new Response('ok');
  }
  const message = update?.message;
  const chatId = message?.chat?.id;
  if (chatId === undefined || !env.TELEGRAM_CHAT_ID || !safeEqual(String(chatId), String(env.TELEGRAM_CHAT_ID))) {
    return new Response('ok');
  }

  const command = parseCommand(message.text);
  if (!command) return new Response('ok');
  if (command.kind === 'help') {
    await reply(env, chatId, HELP, fetchFn);
  } else if (command.kind === 'bad') {
    await reply(env, chatId, `${command.reason}\n\n${HELP}`, fetchFn);
  } else {
    const result = await startTopicRun(env, command, fetchFn);
    if (result.ok) {
      const sources = command.urls.length ? `${command.urls.length} link(s) you sent` : 'matching news already collected';
      await reply(env, chatId, `Started: "${command.topic}"\nSources: ${sources}.\nI will message you when the draft is ready, or if it cannot be written.`, fetchFn);
    } else {
      console.error(`telegram-topic: ${result.reason}`);
      await reply(env, chatId, `Could not start the article.\n${result.reason}`, fetchFn);
    }
  }
  return new Response('ok');
}

export default {
  async fetch(request, env, ctx) {
    return handle(request, env);
  },
};
