import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const layout = read('src/layouts/BaseLayout.astro');
const privacy = read('src/pages/privacy.astro');
const agents = read('AGENTS.md');
const ID = 'G-NC7DCQ7KT1';

// join() gives backslashes on Windows; compare with forward slashes everywhere
const sourceFiles = (dir) => {
  const found = [];
  for (const entry of readdirSync(join(ROOT, dir), { withFileTypes: true })) {
    const path = join(dir, entry.name).replace(/\\/g, '/');
    if (entry.isDirectory()) found.push(...sourceFiles(path));
    else if (/\.(astro|ts|css|mjs|js)$/.test(entry.name)) found.push(path);
  }
  return found;
};

test('Google Analytics is loaded directly with the measurement ID, inside <head>', () => {
  const head = layout.indexOf('<head>');
  const script = layout.indexOf(`googletagmanager.com/gtag/js?id=${ID}`);
  assert.ok(head > -1 && script > head, 'the tag is inside <head>');
  assert.match(layout, new RegExp(`<script async src="https://www\\.googletagmanager\\.com/gtag/js\\?id=${ID}"></script>`));
  assert.match(layout, new RegExp(`<script is:inline>\\s*gtag\\('js', new Date\\(\\)\\);\\s*gtag\\('config', '${ID}'\\);\\s*</script>`));
});

test('only the consent defaults come before the analytics tag, and gtag is defined exactly once', () => {
  const head = layout.indexOf('<head>');
  const tagStart = layout.lastIndexOf('<script async', layout.indexOf('googletagmanager.com/gtag/js'));
  const between = layout.slice(head + '<head>'.length, tagStart).replace(/<script is:inline>[\s\S]*?<\/script>/, '').replace(/<!--[\s\S]*?-->/g, '');
  assert.doesNotMatch(between, /<meta|<link|<title|<script/, 'nothing but the consent defaults comes before the tag');
  assert.equal((layout.match(/function gtag\(\)/g) || []).length, 1);
  assert.equal((layout.match(/window\.dataLayer = window\.dataLayer \|\| \[\];/g) || []).length, 1);
});

test('the tag is written once, in the one layout every page uses', () => {
  assert.equal((layout.match(new RegExp(`gtag/js\\?id=${ID}`, 'g')) || []).length, 1, 'one script loads Google Analytics');
  assert.equal((layout.match(new RegExp(`gtag\\('config', '${ID}'\\)`, 'g')) || []).length, 1, 'one config call');
  const elsewhere = sourceFiles('src').filter(
    (path) => path !== 'src/layouts/BaseLayout.astro' && path !== 'src/pages/privacy.astro' && /googletagmanager|G-[A-Z0-9]{8,}|dataLayer|gtag\(/.test(read(path)),
  );
  assert.deepEqual(elsewhere, []);
});

test('the old Tag Manager code is gone from the site and the rules', () => {
  const left = sourceFiles('src').filter((path) => /GTM-K377WGXG|gtm\.js/.test(read(path)));
  assert.deepEqual(left, []);
  assert.doesNotMatch(agents, /GTM-K377WGXG/);
  assert.doesNotMatch(privacy, /Tag Manager|GTM-/);
});

test('the privacy page names the measurement ID and says what Google receives and when', () => {
  assert.match(privacy, new RegExp(`Google Analytics 4 \\(measurement ID ${ID}\\)`));
  assert.match(privacy, /Google's script is loaded from Google's servers on every visit/);
  assert.match(privacy, /may receive a signal that a page was viewed/);
  assert.match(privacy, /no analytics cookies on your device until you accept/);
  assert.match(privacy, /names beginning with _ga/);
  assert.doesNotMatch(privacy, /does not currently set any cookies|cookieless Cloudflare|It is cookieless/);
  assert.match(privacy, /const lastUpdated = new Date\('2026-11-12'\);/);
});

test('the privacy page still says the fonts and pictures come from this site', () => {
  assert.match(privacy, /Your browser does not contact Google or any other font service/);
});
