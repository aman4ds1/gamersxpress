import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const worker = await import(pathToFileURL(join(ROOT, 'workers/telegram-topic/index.mjs')).href);
const workflow = readFileSync(join(ROOT, '.github/workflows/pipeline.yml'), 'utf8').split('\r\n').join('\n');

const env = {
  GITHUB_DISPATCH_TOKEN: 'github_pat_SECRET',
  TELEGRAM_BOT_TOKEN: 'bot-token',
  TELEGRAM_CHAT_ID: '42',
  TELEGRAM_WEBHOOK_SECRET: 'hook-secret',
};

function fakeFetch(statuses = [204]) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    const status = url.includes('api.github.com') ? statuses.shift() ?? 204 : 200;
    return { status, ok: status >= 200 && status < 300, text: async () => '' };
  };
  fn.calls = calls;
  return fn;
}

function call(body, { secret = 'hook-secret', method = 'POST' } = {}) {
  const headers = new Headers({ 'content-type': 'application/json' });
  if (secret !== null) headers.set('X-Telegram-Bot-Api-Secret-Token', secret);
  return new Request('https://example.test/', { method, headers, body: method === 'POST' ? JSON.stringify(body) : undefined });
}
const message = (text, chat = 42) => ({ message: { chat: { id: chat }, text } });

test('parseCommand reads topic, idea and urls', () => {
  const got = worker.parseCommand('/blog GTA 6 | why the price matters | https://a.example/x, https://b.example/y.');
  assert.deepEqual(got, {
    kind: 'blog',
    topic: 'GTA 6',
    idea: 'why the price matters',
    urls: ['https://a.example/x', 'https://b.example/y'],
  });
});

test('parseCommand handles help, a missing topic, bot suffixes and ordinary text', () => {
  assert.equal(worker.parseCommand('/help').kind, 'help');
  assert.equal(worker.parseCommand('/blog').kind, 'bad');
  assert.equal(worker.parseCommand('/blog@MyBot Elden Ring').topic, 'Elden Ring');
  assert.equal(worker.parseCommand('hello there'), null);
});

test('parseCommand caps lengths and the number of urls', () => {
  const urls = Array.from({ length: 9 }, (_, i) => `https://s.example/${i}`).join(' ');
  const got = worker.parseCommand(`/blog ${'t'.repeat(500)} | ${'i'.repeat(900)} ${urls}`);
  assert.equal(got.topic.length, 200);
  assert.equal(got.idea.length, 600);
  assert.equal(got.urls.length, 5);
});

test('a genuine owner message starts the workflow with the topic as inputs and confirms on Telegram', async () => {
  const fetchFn = fakeFetch();
  const res = await worker.handle(call(message('/blog GTA 6 | price angle')), env, fetchFn);
  assert.equal(res.status, 200);
  const gh = fetchFn.calls.find((c) => c.url.includes('api.github.com'));
  assert.match(gh.url, /\/actions\/workflows\/pipeline\.yml\/dispatches$/);
  const body = JSON.parse(gh.init.body);
  assert.deepEqual(body.inputs, { topic: 'GTA 6', idea: 'price angle', urls: '' });
  assert.equal(body.ref, 'main');
  const tg = fetchFn.calls.find((c) => c.url.includes('api.telegram.org'));
  assert.match(JSON.parse(tg.init.body).text, /Started: "GTA 6"/);
});

test('a missing or wrong secret header gets 404 and starts nothing', async () => {
  for (const secret of [null, 'wrong', '']) {
    const fetchFn = fakeFetch();
    const res = await worker.handle(call(message('/blog GTA 6'), { secret }), env, fetchFn);
    assert.equal(res.status, 404);
    assert.equal(fetchFn.calls.length, 0);
  }
  const res = await worker.handle(call(null, { method: 'GET' }), env, fakeFetch());
  assert.equal(res.status, 404);
});

test('messages from any other chat are ignored silently', async () => {
  const fetchFn = fakeFetch();
  const res = await worker.handle(call(message('/blog GTA 6', 999)), env, fetchFn);
  assert.equal(res.status, 200);
  assert.equal(fetchFn.calls.length, 0);
});

test('without TELEGRAM_CHAT_ID or the webhook secret configured nothing can run', async () => {
  const fetchFn = fakeFetch();
  assert.equal((await worker.handle(call(message('/blog X')), { ...env, TELEGRAM_CHAT_ID: '' }, fetchFn)).status, 200);
  assert.equal((await worker.handle(call(message('/blog X')), { ...env, TELEGRAM_WEBHOOK_SECRET: '' }, fetchFn)).status, 404);
  assert.equal(fetchFn.calls.length, 0);
});

test('a GitHub failure is reported to the owner without leaking the token', async () => {
  const fetchFn = fakeFetch([401]);
  const res = await worker.handle(call(message('/blog GTA 6')), env, fetchFn);
  assert.equal(res.status, 200);
  const tg = fetchFn.calls.find((c) => c.url.includes('api.telegram.org'));
  const text = JSON.parse(tg.init.body).text;
  assert.match(text, /Could not start/);
  assert.ok(!text.includes('github_pat_SECRET'));
});

test('the Pipeline workflow accepts the topic inputs and passes them only through env', () => {
  for (const name of ['topic', 'idea', 'urls']) assert.match(workflow, new RegExp(`\\n      ${name}:\\n`));
  assert.match(workflow, /MANUAL_TOPIC: \$\{\{ inputs\.topic \}\}/);
  assert.ok(!/run:[^\n]*inputs\.(topic|idea|urls)/.test(workflow), 'inputs must not be interpolated into shell');
});
