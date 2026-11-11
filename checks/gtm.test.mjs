import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const layout = read('src/layouts/BaseLayout.astro');
const privacy = read('src/pages/privacy.astro');

test('the Google tag is the supplied snippet, placed directly after <head>', () => {
  const head = layout.indexOf('<head>');
  const tag = layout.indexOf('googletagmanager.com/gtm.js?id=GTM-K377WGXG');
  assert.ok(head > -1 && tag > head, 'the tag is inside <head>');
  const between = layout.slice(head + '<head>'.length, tag);
  assert.doesNotMatch(between, /<meta|<link|<title|<script is:inline>/, 'nothing but the tag comes before the other head elements');
  assert.match(layout, /<script is:inline>\s*window\.dataLayer/);
  assert.match(layout, /<script async src="https:\/\/www\.googletagmanager\.com\/gtm\.js\?id=GTM-K377WGXG"><\/script>/);
  assert.match(layout, /window\.dataLayer = window\.dataLayer \|\| \[\];\s*function gtag\(\)\{dataLayer\.push\(arguments\);\}\s*gtag\('js', new Date\(\)\);/);
});

test('the tag is written once, in the one layout every page uses', () => {
  assert.equal((layout.match(/gtm\.js\?id=GTM-K377WGXG/g) || []).length, 1, 'one script loads the container');
  const elsewhere = [];
  const walk = (dir) => {
    for (const entry of readdirSync(join(ROOT, dir), { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      // the privacy page describes the tag in words; only code that loads or defines it counts
      else if (/\.astro$/.test(entry.name) && path.replace(/\\/g, '/') !== 'src/layouts/BaseLayout.astro' && path.replace(/\\/g, '/') !== 'src/pages/privacy.astro' && /googletagmanager|GTM-|dataLayer|gtag\(/.test(read(path))) elsewhere.push(path);
    }
  };
  walk('src');
  assert.deepEqual(elsewhere, []);
});

test('the privacy page no longer claims there are no cookies or that only Cloudflare measures visits', () => {
  assert.doesNotMatch(privacy, /does not currently set any cookies|cookieless Cloudflare|It is cookieless/);
  assert.match(privacy, /Google Tag Manager \(container GTM-K377WGXG\)/);
  assert.match(privacy, /sets cookies to\s+tell visits apart/);
  assert.match(privacy, /names beginning with _ga/);
  assert.match(privacy, /const lastUpdated = new Date\('2026-11-11'\);/);
});

test('the privacy page still says the fonts and pictures come from this site', () => {
  assert.match(privacy, /Your browser does not contact Google or any other font service/);
});
