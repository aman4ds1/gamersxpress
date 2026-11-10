import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const header = read('src/components/Header.astro');
const popup = read('src/components/SearchPopup.astro');
const css = read('src/styles/searchpopup.css');
const layout = read('src/layouts/BaseLayout.astro');
const script = popup.slice(popup.indexOf('<script>'));
const markup = popup.slice(0, popup.indexOf('<script>'));

test('Home is the first link in the main nav and is current only on the home page', () => {
  assert.match(header, /const navItems = \[\s*\{ label: 'Home', href: '\/' \},\s*\{ label: 'PC'/);
  assert.match(header, /const path = Astro\.url\.pathname\.replace\(\/\\\/\+\$\/, ''\) \|\| '\/';/);
  assert.match(header, /aria-current=\{path === item\.href \? 'page' : undefined\}/);
});

test('the search icon is a button that opens the popup, not a link to another page', () => {
  assert.match(header, /<button type="button" class="gx-iconbtn" id="search-open" aria-label="Search" aria-haspopup="dialog" aria-expanded="false" aria-controls="gx-sp"/);
  const outsideNoscript = header.replace(/<noscript>[\s\S]*?<\/noscript>/g, '');
  assert.doesNotMatch(outsideNoscript, /href="\/search"/);
});

test('without JavaScript the search page is still one click away', () => {
  const noscript = /<noscript>([\s\S]*?)<\/noscript>/.exec(header)[1];
  assert.match(noscript, /#search-open \{ display: none; \}/);
  assert.match(noscript, /<a href="\/search" class="gx-iconbtn" aria-label="Search">/);
  assert.ok(existsSync(join(ROOT, 'src/pages/search.astro')));
});

test('the popup is rendered after the header, outside it and outside the indexed page body', () => {
  // inside the header it would be positioned against the header's backdrop-filter instead of the screen
  assert.ok(header.indexOf('<SearchPopup />') > header.indexOf('</header>'));
  assert.ok(layout.indexOf('<Header />') < layout.indexOf('<main'));
  assert.match(layout, /<main class="flex-1" data-pagefind-body>/);
  assert.match(header, /import SearchPopup from '\.\/SearchPopup\.astro';/);
});

test('the popup markup is an accessible combobox with a listbox and a polite status line', () => {
  assert.match(markup, /<div class="gx-sp" id="gx-sp" role="dialog" aria-label="Search articles" hidden>/);
  assert.match(markup, /<div class="gx-sp-backdrop" id="gx-sp-backdrop" hidden><\/div>/);
  for (const attribute of ['type="search"', 'role="combobox"', 'aria-expanded="false"', 'aria-controls="gx-sp-list"', 'aria-autocomplete="list"', 'autocomplete="off"']) {
    assert.ok(markup.includes(attribute), attribute);
  }
  assert.match(markup, /<ul class="gx-sp-list" id="gx-sp-list" role="listbox"/);
  assert.match(markup, /<p class="gx-sp-status" id="gx-sp-status" role="status" aria-live="polite">/);
  assert.match(markup, /<button type="button" class="gx-sp-close" id="gx-sp-close" aria-label="Close search">/);
});

test('the popup sits above the header and the page, and the backdrop between them', () => {
  const z = (selector) => Number(new RegExp(`${selector.replace(/[.[\]]/g, '\\$&')} \\{[^}]*z-index: (\\d+)`).exec(css)?.[1]);
  const gx = read('src/styles/gx.css');
  const bar = Number(/\.gx-bar \{[^}]*z-index: (\d+)/.exec(gx)[1]);
  assert.ok(bar < z('.gx-sp-backdrop') && z('.gx-sp-backdrop') < z('.gx-sp'), `${bar} < ${z('.gx-sp-backdrop')} < ${z('.gx-sp')}`);
  assert.match(css, /\.gx-sp \{ position: fixed;/);
  assert.match(css, /\.gx-sp-backdrop \{ position: fixed; inset: 0;/);
  assert.match(css, /\.gx-sp-backdrop\[hidden\], \.gx-sp\[hidden\] \{ display: none; \}/);
});

test('the input is 16px so phones do not zoom in, and the styles load nothing from outside', () => {
  assert.match(css, /\.gx-sp-input \{[^}]*font-size: 16px;/);
  assert.doesNotMatch(css, /https?:\/\/|@import|url\(/);
  assert.match(read('src/styles/global.css'), /@import "\.\/searchpopup\.css";/);
});

test('motion is skipped for visitors who prefer less, and nothing is printed', () => {
  assert.match(css, /@media \(prefers-reduced-motion: no-preference\) \{ \.gx-sp:not\(\[hidden\]\) \{ animation: gx-sp-in/);
  assert.match(css, /@media print \{ \.gx-sp, \.gx-sp-backdrop \{ display: none; \} \}/);
});

test('the search index is only loaded the first time the popup opens', () => {
  const topLevel = script.slice(0, script.indexOf('const loadIndex'));
  assert.doesNotMatch(topLevel, /pagefind\.js/);
  assert.match(script, /const url = '\/pagefind\/pagefind\.js';\s*pagefind = await import\(\/\* @vite-ignore \*\/ url\);/);
  assert.match(script, /const open = \(\) => \{[\s\S]*?void run\(input\.value\.trim\(\)\);/);
  assert.match(script, /loading \?\?= /);
});

test('results are built from text, never from raw HTML', () => {
  const assignments = [...script.matchAll(/\.innerHTML\s*=/g)];
  assert.equal(assignments.length, 1, 'the only innerHTML is the entity decoder');
  assert.match(script, /const decoder = document\.createElement\('textarea'\);\s*const decode = \(html: string\) => \{\s*decoder\.innerHTML = html;/);
  assert.match(script, /part\.replace\(\/<\[\^>\]\*>\/g, ''\)/);          // any tag other than <mark> is stripped
  assert.match(script, /document\.createElement\('mark'\)/);
  assert.doesNotMatch(script, /eval\(|new Function|insertAdjacentHTML|outerHTML|document\.write/);
});

test('the popup lists articles first and falls back to every page when there is no article', () => {
  assert.match(script, /articlesOnly = Number\(filters\?\.type\?\.article \?\? 0\) > 0;/);
  assert.match(script, /debouncedSearch\(query, articlesOnly \? \{ filters: \{ type: 'article' \} \} : \{\}, 120\)/);
  assert.match(script, /if \(articlesOnly && found\.results\.length === 0\) \{\s*found = await pagefind\.debouncedSearch\(query, \{\}, 120\);/);
  assert.match(script, /const MAX_RESULTS = 8;/);
});

test('only article pages carry the article tag the popup filters on', () => {
  const tag = /<span hidden data-pagefind-filter="type\[data-value\]" data-value="article"><\/span>/;
  assert.match(read('src/pages/news/[id].astro'), tag);
  const others = [];
  const walk = (dir) => {
    for (const entry of readdirSync(join(ROOT, dir), { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (/\.astro$/.test(entry.name) && !path.split(/[\\/]/).join('/').endsWith('news/[id].astro') && tag.test(read(path))) others.push(path);
    }
  };
  walk('src');
  assert.deepEqual(others, []);
});

test('a slow answer never overwrites a newer one', () => {
  assert.match(script, /const mine = \+\+token;/);
  // every await in run() is followed by a check that this search is still the newest
  assert.equal((script.match(/if \(mine !== token\) return;/g) || []).length, 2);
  assert.equal((script.match(/found === null \|\| mine !== token/g) || []).length, 2);
  assert.match(script, /if \(found === null \|\| mine !== token\) return; \/\/ a newer search replaced this one/);
  assert.match(script, /token \+= 1;\s*panel\.hidden = true;/);               // closing also cancels a search in flight
});

test('keyboard: arrows move, Enter opens, Escape closes, / and Ctrl/Cmd+K open (but not while typing)', () => {
  assert.match(script, /event\.key === 'ArrowDown' \|\| event\.key === 'ArrowUp'/);
  assert.match(script, /\(active \+ step \+ hits\.length\) % hits\.length/);
  assert.match(script, /event\.key === 'Enter'[\s\S]*?window\.location\.assign\(target\.url\)/);
  assert.match(script, /event\.key === 'Escape' && isOpen\(\)/);
  assert.match(script, /\(event\.key === 'k' \|\| event\.key === 'K'\) && \(event\.ctrlKey \|\| event\.metaKey\)/);
  assert.match(script, /event\.key === '\/' && !event\.ctrlKey && !event\.metaKey && !event\.altKey && !typing/);
  assert.match(script, /closest\?\.\('input, textarea, select, \[contenteditable="true"\]'\)/);
});

test('the popup opens right under the search button, lined up with its right edge, and stays on the screen', () => {
  assert.match(script, /const top = box\.bottom \+ 10;/);
  assert.match(script, /window\.innerWidth - box\.right/);
  assert.match(script, /window\.innerWidth - width - 12\)/);
  assert.match(script, /const width = Math\.min\(600, window\.innerWidth - 24\);/);
  assert.match(script, /panel\.style\.maxHeight = `\$\{Math\.max\(220, window\.innerHeight - top - 16\)\}px`;/);
  assert.match(script, /window\.addEventListener\('resize', replace\);/);
  assert.match(script, /window\.addEventListener\('scroll', replace, \{ passive: true \}\);/);
});

test('focus goes into the box on opening and back to the button on Escape, and the state is announced', () => {
  assert.match(script, /opener\.setAttribute\('aria-expanded', 'true'\);\s*input\.focus\(\);\s*input\.select\(\);/);
  assert.match(script, /opener\.setAttribute\('aria-expanded', 'false'\);\s*if \(returnFocus\) opener\.focus\(\);/);
  assert.match(script, /backdrop\.addEventListener\('pointerdown', \(\) => close\(false\)\);/);
  assert.match(script, /input\.setAttribute\('aria-activedescendant', `gx-sp-opt-\$\{index\}`\)/);
});

test('every result is a labelled option with a real link', () => {
  assert.match(script, /item\.setAttribute\('role', 'option'\);/);
  assert.match(script, /link\.href = hit\.url;\s*link\.tabIndex = -1;/);
  assert.match(script, /item\.id = `gx-sp-opt-\$\{index\}`;/);
  assert.match(script, /No results for “\$\{query\}”\./);
  assert.match(script, /Search is not available yet\. It is created when the site is built\./);
});
