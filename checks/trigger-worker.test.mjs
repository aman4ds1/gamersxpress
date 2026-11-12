import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const worker = await import(pathToFileURL(join(ROOT, 'workers/pipeline-trigger/index.mjs')).href);
const wrangler = JSON.parse(read('workers/pipeline-trigger/wrangler.jsonc'));
const readme = read('workers/pipeline-trigger/README.md');
const workflow = read('.github/workflows/pipeline.yml');
const source = read('workers/pipeline-trigger/index.mjs');

const TOKEN = 'github_pat_SECRET_VALUE_123';
const env = { GITHUB_DISPATCH_TOKEN: TOKEN };

function fakeFetch(answers) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    const answer = answers.shift();
    if (answer instanceof Error) throw answer;
    return answer;
  };
  fn.calls = calls;
  return fn;
}
const reply = (status, body = '') => ({ status, ok: status >= 200 && status < 300, text: async () => body });

test('it asks GitHub to start pipeline.yml on main, with the token and the headers GitHub requires', async () => {
  const fetchFn = fakeFetch([reply(204)]);
  assert.deepEqual(await worker.startPipeline(env, fetchFn), { ok: true });
  const [{ url, init }] = fetchFn.calls;
  assert.equal(url, 'https://api.github.com/repos/aman4ds1/gamersxpress/actions/workflows/pipeline.yml/dispatches');
  assert.equal(init.method, 'POST');
  assert.deepEqual(JSON.parse(init.body), { ref: 'main' });
  assert.equal(init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(init.headers.Accept, 'application/vnd.github+json');
  assert.equal(init.headers['X-GitHub-Api-Version'], '2022-11-28');
  assert.match(init.headers['User-Agent'], /pipeline-trigger/);       // GitHub rejects requests with no User-Agent
});

test('the repository it targets is the one this project is hosted in', () => {
  const pkg = JSON.parse(read('package.json'));
  const slug = /github\.com\/([^/]+\/[^/.]+)/.exec(pkg.repository.url)[1];
  assert.equal(worker.dispatchUrl({}).split('/repos/')[1].split('/actions/')[0], slug);
});

test('the workflow it starts exists and can be started by hand with no inputs', () => {
  assert.match(workflow, /^name: Pipeline$/m);
  assert.match(workflow, /^on:[\s\S]*?^  workflow_dispatch:/m);
  const block = /^  workflow_dispatch:\n([\s\S]*?)^[a-z]/m.exec(workflow)[1];
  const inputs = [...block.matchAll(/^      ([a-z_]+):\n((?:        .*\n)+)/gm)];
  assert.ok(inputs.length >= 3, 'the pipeline has its manual inputs');
  for (const [, name, body] of inputs) assert.match(body, /^\s+default:/m, `input ${name} needs a default, the Worker sends none`);
});

test('every kind of GitHub answer is turned into a plain reason, and a 204 is the only success', async () => {
  for (const [status, words] of [[401, /rejected the token/], [403, /Actions: Read and write/], [404, /could not find/], [422, /workflow_dispatch/], [500, /answered 500/]]) {
    const result = await worker.startPipeline(env, fakeFetch([reply(status, JSON.stringify({ message: 'boom' }))]));
    assert.equal(result.ok, false);
    assert.equal(result.status, status);
    assert.match(result.reason, words);
  }
  for (const status of [200, 201, 202]) assert.equal((await worker.startPipeline(env, fakeFetch([reply(status)]))).ok, false);
});

test('GitHub\'s own message is passed on, and a body that is not JSON does not break it', async () => {
  assert.match((await worker.startPipeline(env, fakeFetch([reply(401, JSON.stringify({ message: 'Bad credentials' }))]))).reason, /GitHub said: Bad credentials/);
  assert.match((await worker.startPipeline(env, fakeFetch([reply(502, '<html>Bad gateway</html>')]))).reason, /GitHub said: <html>Bad gateway/);
  assert.doesNotMatch(worker.explain(500, 'x'.repeat(5000)), /x{201}/);
});

test('a missing token or an unreachable GitHub is a clear failure, not a crash', async () => {
  assert.match((await worker.startPipeline({}, fakeFetch([]))).reason, /GITHUB_DISPATCH_TOKEN secret is not set/);
  const down = await worker.startPipeline(env, fakeFetch([new Error('network down')]));
  assert.equal(down.ok, false);
  assert.match(down.reason, /Could not reach GitHub: network down/);
});

test('the token never appears in a reason, a log line or a Telegram message', async () => {
  const logged = [];
  const original = { log: console.log, error: console.error };
  console.log = (...parts) => logged.push(parts.join(' '));
  console.error = (...parts) => logged.push(parts.join(' '));
  const telegram = { TELEGRAM_BOT_TOKEN: 'tg-secret', TELEGRAM_CHAT_ID: '42' };
  const sent = [];
  try {
    const fetchFn = async (url, init) => {
      if (String(url).startsWith('https://api.telegram.org')) {
        sent.push(init.body);
        return reply(200);
      }
      return reply(401, JSON.stringify({ message: 'Bad credentials' }));
    };
    await assert.rejects(worker.handle({ ...env, ...telegram }, fetchFn));
  } finally {
    console.log = original.log;
    console.error = original.error;
  }
  for (const text of [...logged, ...sent]) assert.ok(!text.includes(TOKEN), 'the GitHub token leaked');
  assert.ok(sent.every((text) => !text.includes('tg-secret')), 'the Telegram token is only in the address, never in the message');
});

test('on success it logs and sends nothing; on failure it sends one Telegram message and fails loudly', async () => {
  const sent = [];
  const make = (githubStatus) => async (url, init) => {
    if (String(url).startsWith('https://api.telegram.org')) {
      sent.push({ url: String(url), body: JSON.parse(init.body) });
      return reply(200);
    }
    return reply(githubStatus);
  };
  const full = { ...env, TELEGRAM_BOT_TOKEN: 'tg-secret', TELEGRAM_CHAT_ID: '42' };
  const quiet = console.log;
  console.log = () => {};
  try {
    assert.deepEqual(await worker.handle(full, make(204)), { ok: true });
    assert.equal(sent.length, 0);
  } finally {
    console.log = quiet;
  }
  const loud = console.error;
  console.error = () => {};
  try {
    await assert.rejects(worker.handle(full, make(401)), /rejected the token/);
  } finally {
    console.error = loud;
  }
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, 'https://api.telegram.org/bottg-secret/sendMessage');
  assert.equal(sent[0].body.chat_id, '42');
  assert.match(sent[0].body.text, /could not start the pipeline/);
  assert.match(sent[0].body.text, /rejected the token/);
  assert.match(sent[0].body.text, /workers\/pipeline-trigger\/README\.md/);
});

test('Telegram is optional, and a Telegram failure never hides the real error', async () => {
  const quiet = console.error;
  console.error = () => {};
  try {
    const noTelegram = fakeFetch([reply(401)]);
    await assert.rejects(worker.handle(env, noTelegram), /rejected the token/);
    assert.equal(noTelegram.calls.length, 1, 'no message is attempted without the Telegram secrets');
    const broken = fakeFetch([reply(401), new Error('telegram down')]);
    await assert.rejects(worker.handle({ ...env, TELEGRAM_BOT_TOKEN: 't', TELEGRAM_CHAT_ID: '1' }, broken), /rejected the token/);
  } finally {
    console.error = quiet;
  }
  assert.equal(await worker.alert({}, 'x', fakeFetch([])), false);
});

test('the scheduled entry point waits for the run, and a web visit can never start one', async () => {
  const pending = [];
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return reply(204);
  };
  const quiet = console.log;
  console.log = () => {};
  try {
    await worker.default.scheduled({}, env, { waitUntil: (promise) => pending.push(promise) });
    await Promise.all(pending);
    assert.equal(pending.length, 1);
    assert.equal(calls.length, 1);
    calls.length = 0;
    const response = await worker.default.fetch(new Request('https://example.com/?run=1', { method: 'POST' }), env);
    assert.equal(response.status, 404);
    assert.equal(calls.length, 0, 'a web request must not reach GitHub');
  } finally {
    globalThis.fetch = original;
    console.log = quiet;
  }
});

test('the Cloudflare config: every 3 hours, no public web address, logs on', () => {
  assert.equal(wrangler.name, 'gamersxpress-pipeline-trigger');
  assert.equal(wrangler.main, 'index.mjs');
  assert.deepEqual(wrangler.triggers.crons, ['7 */3 * * *']);
  assert.equal(wrangler.workers_dev, false);
  assert.equal(wrangler.observability.enabled, true);
  assert.equal(wrangler.routes, undefined);
});

test('the guide, the config and the code agree on the names and the schedule', () => {
  assert.ok(readme.includes(wrangler.triggers.crons[0]), 'the guide gives the same cron expression');
  assert.ok(readme.includes('GITHUB_DISPATCH_TOKEN') && source.includes('GITHUB_DISPATCH_TOKEN'));
  assert.ok(readme.includes('TELEGRAM_BOT_TOKEN') && source.includes('TELEGRAM_BOT_TOKEN'));
  assert.ok(readme.includes('TELEGRAM_CHAT_ID') && source.includes('TELEGRAM_CHAT_ID'));
  assert.match(readme, /Actions:\*\* set to \*\*Read and write\*\*/);
  assert.match(readme, /\*\*Variable name:\*\* `GITHUB_DISPATCH_TOKEN`/, 'step 3 names the secret exactly');
  assert.match(readme, /npx wrangler secret put GITHUB_DISPATCH_TOKEN/, 'the command-line route names it too');
  assert.doesNotMatch(readme, /GITHUB_TOKEN\b/, 'the name is not confused with the Actions GITHUB_TOKEN');
  assert.match(readme, /pipeline\.yml/);
  assert.match(readme, /expires in a year/);
});

test('the Worker has no dependencies, imports nothing, and reads only its own secrets', () => {
  assert.doesNotMatch(source, /^\s*import\s/m);
  assert.doesNotMatch(source, /require\(/);
  const names = [...new Set([...source.matchAll(/env\.([A-Z_]+)/g)].map(([, name]) => name))].sort();
  assert.deepEqual(names, ['BRANCH', 'GITHUB_API_URL', 'GITHUB_DISPATCH_TOKEN', 'GITHUB_REPO', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'WORKFLOW_FILE']);
});

test('the owner\'s exception is recorded under the "no server code" rule, and the CI guide points to the Worker', () => {
  const agents = read('AGENTS.md');
  assert.match(agents, /No database, no server code, no SSR\.\n  - Exception \(owner-approved, 2026-11-12\): `workers\/pipeline-trigger\/` is a Cloudflare Worker that only starts the Pipeline workflow on a cron schedule/);
  assert.match(agents, /so the site itself still has no server code/);
  assert.match(read('docs/CI-SETUP.md'), /`workers\/pipeline-trigger\/` is a small Cloudflare Worker/);
});

test('the site deploy is untouched: the root config still deploys only the built site', () => {
  const root = JSON.parse(read('wrangler.jsonc'));
  assert.equal(root.name, 'gamersxpress');
  assert.deepEqual(root.assets, { directory: './dist' });
  assert.equal(root.main, undefined);
  assert.equal(root.triggers, undefined);
});
