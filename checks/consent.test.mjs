import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const layout = read('src/layouts/BaseLayout.astro');
const banner = read('src/components/ConsentBanner.astro');
const footer = read('src/components/Footer.astro');
const privacy = read('src/pages/privacy.astro');
const agents = read('AGENTS.md');
const css = read('src/styles/consent.css');

test('consent defaults to denied before anything loads, and comes directly after <head>', () => {
  const head = layout.indexOf('<head>');
  const defaults = layout.indexOf("gtag('consent', 'default', {");
  const tag = layout.indexOf('googletagmanager.com/gtag/js?id=G-NC7DCQ7KT1');
  assert.ok(head > -1 && head < defaults && defaults < tag, 'head, then consent defaults, then the tag');
  const block = layout.slice(defaults, layout.indexOf('});', defaults));
  for (const key of ['ad_storage', 'ad_user_data', 'ad_personalization']) assert.match(block, new RegExp(`${key}: 'denied'`));
  assert.match(block, /analytics_storage: gxConsent === 'granted' \? 'granted' : 'denied'/);
  assert.match(block, /wait_for_update: 500/);
});

test('a choice saved on the device is applied before the tag loads, and read safely', () => {
  assert.match(layout, /try \{ gxConsent = localStorage\.getItem\('gx-consent'\); \} catch \(e\) \{\}/);
  assert.doesNotMatch(layout, /'granted'\s*:\s*'denied',\s*ad_/);     // ads are never granted by the saved choice
});

test('the analytics tag comes after the defaults, and its config call comes after them too', () => {
  assert.match(layout, /<script async src="https:\/\/www\.googletagmanager\.com\/gtag\/js\?id=G-NC7DCQ7KT1"><\/script>/);
  assert.match(layout, /gtag\('js', new Date\(\)\);\s*gtag\('config', 'G-NC7DCQ7KT1'\);/);
  assert.ok(layout.indexOf("gtag('consent', 'default'") < layout.indexOf("gtag('config'"), 'the config call must not run before consent is set');
});

test('the banner is shown on a first visit only, and never pre-selects a choice', () => {
  assert.match(banner, /<div class="gx-consent" id="gx-consent" role="region" aria-label="Cookie choice" hidden>/);
  assert.match(banner, /if \(saved\(\) !== 'granted' && saved\(\) !== 'denied'\) show\(\);/);
  assert.doesNotMatch(banner, /checked|defaultChecked|selected/);
  assert.match(layout, /<ConsentBanner \/>/);
  assert.equal((layout.match(/<ConsentBanner \/>/g) || []).length, 1);
});

test('Accept and Reject are equally prominent: the same element, class and size', () => {
  const buttons = [...banner.matchAll(/<button type="button" class="([^"]+)" data-choice="(granted|denied)">(\w+)<\/button>/g)];
  assert.equal(buttons.length, 2);
  assert.equal(buttons[0][1], buttons[1][1], 'same class');
  assert.deepEqual(buttons.map((b) => b[3]).sort(), ['Accept', 'Reject']);
  assert.match(css, /\.gx-consent-btn \{[^}]*min-height: 44px; min-width: 96px;/);
  assert.doesNotMatch(css, /\.gx-consent-btn--|primary|\.gx-consent-btn\[data-choice="granted"\]/);
});

test('a choice is remembered, sent to Google, and only then closes the banner', () => {
  assert.match(banner, /localStorage\.setItem\(KEY, choice\);/);
  assert.match(banner, /gtagFn\?\.\('consent', 'update', \{ analytics_storage: choice \}\);/);
  assert.match(banner, /const choice = button\.dataset\.choice === 'granted' \? 'granted' : 'denied';/);   // anything unexpected means denied
  assert.match(banner, /hide\(\);/);
});

test('the banner is reachable again from the footer', () => {
  assert.match(footer, /<button type="button" id="cookie-settings" class="hover:text-ink">Cookie settings<\/button>/);
  assert.match(banner, /settings\?\.addEventListener\('click', \(\) => \{\s*show\(\);/);
});

test('the banner never blocks the page from working without JavaScript', () => {
  assert.match(banner, /hidden>/);                                  // hidden until the script decides
  assert.match(css, /\.gx-consent\[hidden\] \{ display: none; \}/);
});

test('the privacy page describes the choice accurately', () => {
  assert.match(privacy, /On your first visit a banner asks whether Google Analytics/);
  assert.match(privacy, /Until you accept, it\s+sets none/);
  assert.match(privacy, /saved on your device in local browser storage, not\s+as a cookie/);
  assert.match(privacy, /"Cookie settings" in\s+the footer/);
  assert.doesNotMatch(privacy, /does not currently set any cookies/);
});

test('the owner\'s exception is recorded next to the rule it overrides, and ad scripts stay forbidden', () => {
  assert.match(agents, /Do not add ad scripts, analytics cookies, or tracking\./);
  assert.match(agents, /Exception \(owner-approved, 2026-11-11; changed from Tag Manager to a direct tag on 2026-11-12\): Google Analytics 4, measurement ID G-NC7DCQ7KT1/);
  assert.match(agents, /Analytics cookies are set only after a visitor accepts the consent banner/);
  assert.match(agents, /Ad scripts remain forbidden/);
});
