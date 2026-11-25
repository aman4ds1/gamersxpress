// Guards for the 2026-11 redesign: the promises it made (real data only, sources once, no fake newsletter, readable
// tags, a share image) stay true. Tests on built pages need `npm run build` first (CI builds before testing) and skip
// when dist/ is missing.
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const DIST = join(ROOT, 'dist');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const built = { skip: existsSync(join(DIST, 'index.html')) ? false : 'no dist/ (run npm run build first)' };
const global = read('src/styles/global.css');

test('the original palette: gold in light, silver in dark, and the layout roles drawn from it', () => {
  assert.match(global, /:root \{[^}]*--surface: #ffffff;[^}]*--gold: #b8860b;/, 'white page and gold in the light theme');
  assert.equal((global.match(/--surface: #0b0c10;/g) || []).length, 2, 'near-black page in both dark blocks');
  assert.equal((global.match(/--gold: #c9ced6;/g) || []).length, 2, 'silver in both dark blocks');
  for (const role of ['--gx-x: var(--gold);', '--gx-yellow: var(--gold);', '--gx-panel: var(--panel-bg);']) assert.ok(global.includes(role), role);
  const gx = read('src/styles/gx.css');
  assert.match(gx, /\.gx-panel \{[^}]*background: var\(--panel-bg\);[^}]*backdrop-filter: var\(--panel-blur\);/, 'panels are the same glass as the reading panel');
});

test('story cards, the featured story and the side cards wear the animated gold / silver border', () => {
  assert.match(read('src/components/ArticleCard.astro'), /class="gx-lined gx-ncard"/);
  assert.match(read('src/components/HeroStory.astro'), /class="gx-lined gx-feature-wrap"/);
  assert.match(read('src/components/SideStory.astro'), /class="gx-lined gx-side-wrap"/);
  assert.match(read('src/styles/gx.css'), /\.gx-lined \{[^}]*conic-gradient\(from var\(--gx-a\), var\(--l1\)/);
});

test('category tags reach 4.5:1 contrast with their white text', () => {
  const categories = read('src/data/categories.ts');
  const map = (name) => Object.fromEntries([...new RegExp(`${name}[^{]*\\{([^}]*)\\}`).exec(categories)[1].matchAll(/'?([\w-]+)'?\s*:\s*'(#[0-9A-Fa-f]{6})'/g)].map(([, k, v]) => [k, v]));
  const bg = map('CATEGORY_COLORS');
  assert.match(categories, /--chip-ink:#FFFFFF/);
  const lum = (hex) => {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a, b) => (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05);
  assert.ok(Object.keys(bg).length >= 8);
  for (const [category, color] of Object.entries(bg)) assert.ok(ratio(color, '#FFFFFF') >= 4.5, `${category}: ${ratio(color, '#FFFFFF').toFixed(2)}:1`);
});

test('the home page and its sections take their stories from the collection, never hard-coded', () => {
  const dir = join(ROOT, 'src/content/articles');
  const titles = existsSync(dir)
    ? readdirSync(dir).filter((f) => f.endsWith('.md')).map((f) => /^title:\s*["']?(.+?)["']?\s*$/m.exec(readFileSync(join(dir, f), 'utf8'))?.[1]).filter(Boolean)
    : [];
  const sources = ['src/pages/index.astro', 'src/components/HeroStory.astro', 'src/components/SideStory.astro', 'src/components/MostCovered.astro', 'src/components/TopicStrip.astro', 'src/components/ArticleCard.astro'].map(read).join('\n');
  assert.match(read('src/pages/index.astro'), /await sortedArticles\(\)/);
  for (const title of titles) assert.ok(!sources.includes(title), `hard-coded title: ${title}`);
});

test('no invented popularity numbers and no fake newsletter sign-up', () => {
  const banner = read('src/components/FollowBanner.astro');
  assert.doesNotMatch(banner, /<input|<form|type="email"/);
  assert.match(banner, /href="\/rss\.xml"/);
  const covered = read('src/components/MostCovered.astro');
  assert.match(covered, /\{topic\.count\} \{topic\.count === 1 \? 'story' : 'stories'\}/, 'counts are real story counts');
  const markup = covered.slice(covered.indexOf('---', 3) + 3);
  assert.doesNotMatch(markup, /views|reads|trending/i, 'the markup (not the comment) mentions no views');
});

test('the topic strip only offers categories that have stories', () => {
  assert.match(read('src/components/TopicStrip.astro'), /CATEGORIES\.filter\(\(category\) => \(counts\[category\] \?\? 0\) > 0\)/);
});

test('the animated sky and maze backgrounds are on, with the static decorations kept as the alternative', () => {
  for (const file of ['src/components/Scenes.astro', 'src/styles/scenes.css', 'src/data/scenes.mjs', 'src/components/PixelDecor.astro']) assert.ok(existsSync(join(ROOT, file)), file);
  assert.match(read('src/data/scenes.mjs'), /export const SCENES_ENABLED = true;/);
});

test('pages without their own picture share the 1200x630 brand card', () => {
  const png = readFileSync(join(ROOT, 'public/og-default.png'));
  assert.equal(png.toString('ascii', 1, 4), 'PNG');
  assert.deepEqual([png.readUInt32BE(16), png.readUInt32BE(20)], [1200, 630]);
  assert.match(read('src/components/SEO.astro'), /new URL\(image \?\? DEFAULT_IMAGE, site\)/);
});

test('every built article lists its sources exactly once', built, () => {
  const news = join(DIST, 'news');
  const pages = readdirSync(news, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => join(news, d.name, 'index.html')).filter(existsSync);
  assert.ok(pages.length > 0);
  for (const page of pages) {
    const html = readFileSync(page, 'utf8');
    assert.equal((html.match(/id="sources-heading"/g) || []).length, 1, page);
    assert.equal((html.match(/>Sources</g) || []).length, 1, `${page}: one Sources heading, none in the body`);
  }
});

test('every header and footer link leads to a built page', built, () => {
  const home = readFileSync(join(DIST, 'index.html'), 'utf8');
  const chrome = home.slice(home.indexOf('<header'), home.indexOf('</header>')) + home.slice(home.lastIndexOf('<footer'));
  const links = [...new Set([...chrome.matchAll(/href="(\/[^"#?]*)"/g)].map(([, href]) => href))];
  assert.ok(links.length >= 10);
  for (const href of links) {
    const target = href.endsWith('.xml') || /\.\w+$/.test(href) ? join(DIST, href) : join(DIST, href, 'index.html');
    assert.ok(existsSync(target), `${href} has no built page`);
  }
});

test('the built home page shows no view or read counts', built, () => {
  const text = readFileSync(join(DIST, 'index.html'), 'utf8').replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>|<[^>]+>/g, ' ');
  assert.doesNotMatch(text, /\d[\d,.]*\s*[kKmM]?\s+(views|reads|readers|plays)\b/);
});
