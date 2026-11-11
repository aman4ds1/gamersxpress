import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const header = read('src/components/Header.astro');
const layout = read('src/layouts/BaseLayout.astro');
const global = read('src/styles/global.css');
const gx = read('src/styles/gx.css');

function sourceFiles(dir) {
  const found = [];
  for (const entry of readdirSync(join(ROOT, dir), { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...sourceFiles(path));
    else if (/\.(astro|css|ts|mjs|js)$/.test(entry.name)) found.push(path);
  }
  return found;
}
const stripCommentsAndStrings = (text) => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '').replace(/^\s*\/\/.*$/gm, '');

test('the header has no theme button, icons or label', () => {
  for (const text of ['theme-toggle', 'gx-theme', 'gx-moon', 'gx-sun', 'Switch theme', 'Switch to', 'dark mode', 'light mode']) {
    assert.ok(!header.includes(text), `the header still mentions "${text}"`);
  }
  assert.doesNotMatch(header, /startViewTransition|prefers-color-scheme|dataset\.theme/);
});

test('the bar holds exactly three buttons: search, the background switch and the burger', () => {
  const actions = header.slice(header.indexOf('<div class="gx-actions'), header.indexOf('</header>')).replace(/<noscript>[\s\S]*?<\/noscript>/g, '');
  const ids = [...actions.matchAll(/<button[^>]*\sid="([^"]+)"/g)].map(([, id]) => id);
  assert.deepEqual(ids, ['search-open', 'scene-toggle', 'menu-toggle']);
});

test('nothing in the site sets or styles a chosen theme any more', () => {
  const offenders = [];
  for (const file of [...sourceFiles('src')]) {
    const text = stripCommentsAndStrings(read(file));
    if (/data-theme|dataset\.theme|gx-switching|view-transition|startViewTransition|\.gx-moon|\.gx-sun\b/.test(text)) offenders.push(file);
  }
  assert.deepEqual(offenders, []);
});

test('a theme saved by the old toggle is forgotten, and no saved theme is ever applied', () => {
  assert.match(layout, /localStorage\.removeItem\('gx-theme'\);/);
  assert.doesNotMatch(layout, /localStorage\.getItem\('gx-theme'\)|dataset\.theme/);
  const pre = layout.slice(layout.indexOf('<script is:inline>'), layout.indexOf('</script>'));
  assert.ok(pre.indexOf("removeItem('gx-theme')") < pre.indexOf("getItem('gx-scene')"));
  assert.match(pre, /try \{[\s\S]*removeItem\('gx-theme'\)[\s\S]*\} catch \(e\)/);          // storage can be blocked; the page must still load
});

test('the browser\'s own colours still follow the device', () => {
  assert.match(layout, /<meta name="theme-color" content="#ffffff" media="\(prefers-color-scheme: light\)" \/>/);
  assert.match(layout, /<meta name="theme-color" content="#0b0c10" media="\(prefers-color-scheme: dark\)" \/>/);
});

test('the dark colours exist once, for the device\'s dark setting, and only override tokens that have a light value', () => {
  const darkBlocks = [...global.matchAll(/@media \(prefers-color-scheme: dark\) \{\s*:root \{([\s\S]*?)\r?\n  \}\r?\n\}/g)];
  assert.equal(darkBlocks.length, 1);
  const dark = darkBlocks[0][1];
  assert.match(dark, /color-scheme: dark;/);
  const base = /:root \{([\s\S]*?)\n\}/.exec(global.slice(global.indexOf('\n:root {')))[1];
  assert.match(base, /color-scheme: light;/);
  const overridden = dark.match(/--[a-z0-9-]+(?=:)/g);
  assert.ok(overridden.length >= 25, `${overridden.length} tokens`);
  for (const token of overridden) assert.ok(base.includes(`${token}:`), `${token} has a dark value but no light one`);
  assert.doesNotMatch(global, /data-theme/);
});

test('when the device switches between light and dark, the page fades its colours (unless motion is reduced)', () => {
  assert.match(gx, /body \{ transition: background-color 0\.35s ease, color 0\.35s ease; \}/);
  assert.match(gx, /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\n  body \{ transition: none; \}/);
  assert.doesNotMatch(gx, /\.gx-theme|::view-transition/);
});

test('the rest of the header still works: search, background switch and menu are all there', () => {
  for (const id of ['search-open', 'scene-toggle', 'menu-toggle']) assert.ok(header.includes(`id="${id}"`), id);
  assert.match(header, /<SearchPopup \/>/);
  assert.match(header, /const sceneButtons = document\.querySelectorAll<HTMLElement>\('\[data-scene-toggle\]'\);/);
  assert.match(header, /const menuButton = document\.getElementById\('menu-toggle'\);/);
});
