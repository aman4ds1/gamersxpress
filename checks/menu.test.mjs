import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const header = read('src/components/Header.astro');
const css = read('src/styles/menu.css');
const gx = read('src/styles/gx.css');
const layout = read('src/layouts/BaseLayout.astro');
const search = read('src/components/SearchPopup.astro');
const script = header.slice(header.indexOf('<script>'));
const narrowBlock = css.slice(css.indexOf('@media (max-width: 1059px)'), css.indexOf('@media (prefers-reduced-motion: no-preference)'));

test('the burger is a labelled button that controls the menu and is the last button in the bar', () => {
  assert.match(header, /<button type="button" class="gx-iconbtn gx-burger" id="menu-toggle" aria-label="Menu" aria-expanded="false" aria-controls="gx-menu">/);
  assert.match(header, /<span class="gx-burger-bars" aria-hidden="true"><i><\/i><i><\/i><i><\/i><\/span>/);
  const actions = header.slice(header.indexOf('<div class="gx-actions'), header.indexOf('</header>'));
  assert.ok(actions.indexOf('id="search-open"') < actions.indexOf('id="scene-toggle"') && actions.indexOf('id="scene-toggle"') < actions.indexOf('id="menu-toggle"'));
});

test('the main nav is the menu: it has the id the burger controls and keeps its label', () => {
  assert.match(header, /<nav id="gx-menu" class="gx-nav [^"]*" aria-label="Main">/);
  assert.match(header, /<div class="gx-menu-extras">/);
  assert.match(header, /<span>Background: <strong data-scene-name>on<\/strong><\/span>/);
});

test('below 1060px the links collapse, and the JavaScript uses the very same breakpoint', () => {
  assert.match(css, /@media \(max-width: 1059px\) \{/);
  assert.match(script, /window\.matchMedia\('\(max-width: 1059px\)'\)/);
  assert.match(css, /\.gx-burger \{ display: none; \}/);                                   // hidden by default, so wide screens never show it
  assert.match(narrowBlock, /:root\[data-js\] \.gx-burger \{ display: inline-flex; \}/);
});

test('nothing hides a link unless JavaScript is on (every collapsing rule is scoped to data-js)', () => {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [...withoutComments.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => ({ selector: selector.trim(), body }));
  const hiding = rules.filter(({ selector, body }) => /display: none/.test(body) && /gx-nav|gx-iconbtn\.gx-scenebtn/.test(selector));
  assert.ok(hiding.length >= 2, 'the nav and the bar\'s background switch are collapsed');
  for (const { selector } of hiding) assert.ok(selector.startsWith(':root[data-js] '), `${selector} must be scoped to data-js`);
});

test('the data-js flag is set before the page first paints, in the pre-paint script', () => {
  const pre = layout.slice(layout.indexOf('<script is:inline>'), layout.indexOf('</script>'));
  assert.match(pre, /document\.documentElement\.setAttribute\('data-js', ''\);/);
  assert.ok(pre.indexOf("setAttribute('data-js'") < pre.indexOf('localStorage'));
});

test('the open menu is a solid panel directly under the header, above the page and below the search popup', () => {
  const panel = /:root\[data-js\] \.gx-bar\[data-menu-open\] \.gx-nav \{([^}]*)\}/.exec(narrowBlock)[1];
  for (const part of ['display: flex', 'position: absolute', 'left: 0', 'right: 0', 'top: 100%', 'flex-direction: column', 'background: var(--card)', 'overflow-y: auto']) {
    assert.ok(panel.includes(part), part);
  }
  const z = Number(/z-index: (\d+)/.exec(panel)[1]);
  const bar = Number(/\.gx-bar \{[^}]*z-index: (\d+)/.exec(gx)[1]);
  const searchZ = Number(/\.gx-sp \{[^}]*z-index: (\d+)/.exec(read('src/styles/searchpopup.css'))[1]);
  assert.ok(bar < z && z < searchZ, `${bar} < ${z} < ${searchZ}`);
});

test('menu links are tall tap targets and the current page is clearly marked', () => {
  assert.match(narrowBlock, /\.gx-bar \.gx-nav a \{ display: flex; align-items: center; min-height: 48px;/);
  assert.match(narrowBlock, /\.gx-nav a\[aria-current='page'\] \{[^}]*box-shadow: inset 3px 0 0 var\(--a1\);[^}]*font-weight: 700;/);
  assert.match(css, /\.gx-menu-scene \{[^}]*min-height: 48px;/);
});

test('the background switch moves into the menu on small screens only', () => {
  assert.match(css, /\.gx-menu-extras \{ display: none; \}/);
  assert.match(narrowBlock, /:root\[data-js\] \.gx-iconbtn\.gx-scenebtn \{ display: none; \}/);
  assert.match(narrowBlock, /:root\[data-js\] \.gx-bar \.gx-menu-extras \{ display: block;/);
});

test('the bar stays on one row on small screens', () => {
  assert.match(narrowBlock, /:root\[data-js\] \.gx-bar \.gx-wrap \{ flex-wrap: nowrap; \}/);
  const phone = /@media \(max-width: 480px\) \{([\s\S]*?)\n\}/.exec(css)[1];
  assert.match(phone, /\.gx-bar \.gx-iconbtn \{ width: 40px; min-width: 40px; height: 40px; padding: 0; \}/);   // the base style has width 44px and 12px padding
  const tiny = /@media \(max-width: 350px\) \{([\s\S]*?)\n\}/.exec(css)[1];
  assert.match(tiny, /\.gx-bar \.gx-iconbtn \{ width: 36px; min-width: 36px; height: 36px; \}/);
  assert.match(tiny, /\.gx-logo-tag \{ display: none; \}/);
  assert.match(css, /@media \(max-width: 380px\) \{\s*\.gx-logo-name \{ font-size: 10px; \}/);
  for (const name of ['gx-logo', 'gx-logo-name', 'gx-logo-tag', 'gx-actions']) assert.ok(header.includes(name), name);
});

test('the header stays on screen while scrolling on phones too', () => {
  assert.match(gx, /\.gx-bar \{ position: sticky; top: 0;/);
  const phone = /@media \(max-width: 640px\) \{([\s\S]*?)\n\}/.exec(gx)[1];
  assert.doesNotMatch(phone, /\.gx-bar \{ position: relative; \}/);
});

test('the script opens and closes the menu and keeps its state announced', () => {
  assert.match(script, /bar\.toggleAttribute\('data-menu-open', open\);/);
  assert.match(script, /menuButton\.setAttribute\('aria-expanded', String\(open\)\);/);
  assert.match(script, /menuButton\.setAttribute\('aria-label', open \? 'Close menu' : 'Menu'\);/);
  assert.match(script, /menuButton\.addEventListener\('click', \(event\) => \{\s*setMenu\(!menuOpen\(\)\);/);
});

test('Escape closes it and returns focus; tapping outside or a link closes it; a keyboard open moves focus in', () => {
  assert.match(script, /event\.key === 'Escape' && menuOpen\(\)\) \{\s*setMenu\(false\);\s*menuButton\.focus\(\);/);
  assert.match(script, /if \(menuOpen\(\) && !bar\.contains\(event\.target as Node\)\) setMenu\(false\);/);
  assert.match(script, /menu\.addEventListener\('click', \(event\) => \{\s*if \(\(event\.target as HTMLElement\)\.closest\('a'\)\) setMenu\(false\);/);
  assert.match(script, /\(event as MouseEvent\)\.detail === 0\) menu\.querySelector<HTMLElement>\('a'\)\?\.focus\(\);/);
});

test('growing past the breakpoint closes the menu so the inline bar is clean', () => {
  assert.match(script, /narrow\.addEventListener\('change', \(\) => \{\s*if \(!narrow\.matches\) setMenu\(false\);/);
});

test('the menu and the search popup never stay open together, in either direction', () => {
  assert.match(script, /if \(open\) document\.dispatchEvent\(new CustomEvent\('gx-overlay', \{ detail: 'menu' \}\)\);/);
  assert.match(script, /document\.addEventListener\('gx-overlay', \(event\) => \{\s*if \(\(event as CustomEvent\)\.detail !== 'menu' && menuOpen\(\)\) setMenu\(false\);/);
  assert.match(search, /document\.dispatchEvent\(new CustomEvent\('gx-overlay', \{ detail: 'search' \}\)\);/);
  assert.match(search, /document\.addEventListener\('gx-overlay', \(event\) => \{\s*if \(\(event as CustomEvent\)\.detail !== 'search'\) close\(false\);/);
});

test('both background switches stay in step: they share one handler and one label', () => {
  assert.match(script, /const sceneButtons = document\.querySelectorAll<HTMLElement>\('\[data-scene-toggle\]'\);/);
  assert.match(script, /document\.querySelectorAll\('\[data-scene-name\]'\)\.forEach\(\(name\) => \(name\.textContent = backgroundOn\(\) \? 'on' : 'off'\)\);/);
  assert.equal((header.match(/<button[^>]*data-scene-toggle/g) || []).length, 2, 'the bar\'s button and the menu\'s button');
});

test('motion is optional: the burger and the panel animate only when allowed', () => {
  assert.match(css, /@media \(prefers-reduced-motion: no-preference\) \{\s*@keyframes gx-menu-in/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{ \.gx-burger-bars i \{ transition: none; \} \}/);
  assert.match(css, /\.gx-burger-bars i \{[^}]*transition: transform 0\.18s ease, opacity 0\.18s ease, top 0\.18s ease;/);
});

test('the burger turns into an X and the stylesheet loads nothing from outside', () => {
  assert.match(css, /\.gx-burger\[aria-expanded='true'\] \.gx-burger-bars i:nth-child\(1\) \{ top: 6px; transform: rotate\(45deg\); \}/);
  assert.match(css, /\.gx-burger\[aria-expanded='true'\] \.gx-burger-bars i:nth-child\(2\) \{ opacity: 0; \}/);
  assert.match(css, /\.gx-burger\[aria-expanded='true'\] \.gx-burger-bars i:nth-child\(3\) \{ top: 6px; transform: rotate\(-45deg\); \}/);
  assert.doesNotMatch(css, /https?:\/\/|@import|url\(/);
  assert.match(read('src/styles/global.css'), /@import "\.\/menu\.css";/);
  assert.match(css, /@media print \{ \.gx-burger, \.gx-menu-extras \{ display: none; \} \}/);
});
