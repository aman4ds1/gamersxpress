import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const header = read('src/components/Header.astro');
const layout = read('src/layouts/BaseLayout.astro');
const global = read('src/styles/global.css');
const scenes = read('src/styles/scenes.css');
const gx = read('src/styles/gx.css');

test('the header has the light/dark switch, between the background switch and the burger', () => {
  assert.match(header, /<button type="button" class="gx-iconbtn gx-theme" id="theme-toggle" aria-label="Switch theme">/);
  const ids = [...header.slice(header.indexOf('<div class="gx-actions'), header.indexOf('</header>')).matchAll(/<button[^>]*\sid="([^"]+)"/g)].map(([, id]) => id);
  assert.deepEqual(ids, ['search-open', 'scene-toggle', 'theme-toggle', 'menu-toggle']);
});

test('the switch flips between light and dark, saves the choice, and labels what it will do next', () => {
  assert.match(header, /const current = \(\) => document\.documentElement\.dataset\.theme \?\? \(media\.matches \? 'dark' : 'light'\);/);
  assert.match(header, /const next = current\(\) === 'dark' \? 'light' : 'dark';/);
  assert.match(header, /localStorage\.setItem\('gx-theme', next\);/);
  assert.match(header, /`Switch to \$\{current\(\) === 'dark' \? 'light' : 'dark'\} mode`/);
});

test('the switch wipes the page with a circle, and falls back to a plain change when motion is reduced', () => {
  assert.match(header, /if \(!document\.startViewTransition \|\| calm\.matches\) \{\s*apply\(next\);\s*return;/);
  assert.match(header, /document\.startViewTransition\(\(\) => apply\(next\)\)/);
  assert.match(gx, /::view-transition-old\(root\), ::view-transition-new\(root\) \{ animation: none;/);
  assert.match(gx, /\.gx-switching body \{ transition: none; \}/);
});

test('a saved light or dark choice is applied before the first paint; with no choice, the device decides', () => {
  assert.match(layout, /const theme = localStorage\.getItem\('gx-theme'\);\s*if \(theme === 'dark' \|\| theme === 'light'\) document\.documentElement\.dataset\.theme = theme;/);
  assert.ok(layout.indexOf("getItem('gx-theme')") < layout.indexOf('<Scenes'));
  assert.doesNotMatch(layout, /removeItem\('gx-theme'\)/);
});

test('every colour exists for the chosen theme and for the device setting, and they match', () => {
  assert.match(global, /:root\[data-theme='dark'\] \{/);
  assert.match(global, /@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme='light'\]\) \{/);
  // the custom properties inside each dark block (the device one is indented two more spaces)
  const tokens = (selector, close) => {
    const from = global.indexOf(selector) + selector.length;
    return global.slice(from, global.indexOf(close, from));
  };
  const explicit = tokens(":root[data-theme='dark'] {", '\n}');
  const device = tokens(":root:not([data-theme='light']) {", '\n  }');
  assert.equal(explicit.replace(/\s+/g, ' '), device.replace(/\s+/g, ' '), 'the two dark blocks are identical');
  assert.match(scenes, /:root\[data-theme='dark'\] \.gx-scene \{/);
  assert.match(scenes, /@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme='light'\]\) \.gx-scene \{/);
});

test('the theme switch icon shows the mode it will change to', () => {
  assert.match(gx, /:root\[data-theme='dark'\] \.gx-moon \{ opacity: 0;/);
  assert.match(gx, /:root\[data-theme='dark'\] \.gx-sun \{ opacity: 1;/);
  assert.match(gx, /@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme='light'\]\) \.gx-moon/);
});

test('the solid panel is for article pages only, so the animation never runs behind the text being read', () => {
  const news = read('src/pages/news/[id].astro');
  assert.match(news, /<BaseLayout\s+reading\s+title=/);
  assert.match(layout, /class:list=\{\["flex-1", \{ "gx-reading": reading \}\]\}/);
  assert.equal((read('src/pages/index.astro').match(/\breading\b/g) || []).length, 0, 'home page has no panel');
  const panel = /\nmain\.gx-reading \{([^}]*)\}/.exec(gx)[1];
  assert.match(panel, /background: var\(--surface\);/);
  assert.match(panel, /width: min\(1040px, calc\(100% - 24px\)\);/);
  assert.match(panel, /z-index: 1;/);
  assert.match(gx, /@media print \{ main\.gx-reading \{ border: 0; box-shadow: none; \} \}/);
});
