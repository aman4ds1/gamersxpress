import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, statSync, openSync, readSync, closeSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');

const EXPECTED = {
  'Chakra Petch': { slug: 'chakra-petch', weights: [500, 600, 700] },
  Inter: { slug: 'inter', weights: [400, 500, 600, 700] },
  'JetBrains Mono': { slug: 'jetbrains-mono', weights: [500, 700] },
  'Press Start 2P': { slug: 'press-start-2p', weights: [400] },
};
const SUBSETS = ['latin', 'latin-ext'];

function sourceFiles(dir) {
  const found = [];
  for (const name of readdirSync(join(ROOT, dir))) {
    const relative = join(dir, name);
    if (statSync(join(ROOT, relative)).isDirectory()) found.push(...sourceFiles(relative));
    else if (/\.(astro|css|ts|mjs|js|json|html)$/.test(name)) found.push(relative);
  }
  return found;
}

function fontFaces() {
  const css = read('src/styles/fonts.css');
  return [...css.matchAll(/@font-face\s*\{([^}]*)\}/g)].map(([, body]) => ({
    family: /font-family:\s*'([^']+)'/.exec(body)?.[1],
    weight: Number(/font-weight:\s*(\d+)/.exec(body)?.[1]),
    display: /font-display:\s*(\w+)/.exec(body)?.[1],
    url: /url\(([^)]+)\)/.exec(body)?.[1],
    format: /format\('([^']+)'\)/.exec(body)?.[1],
    range: /unicode-range:\s*([^;]+);/.exec(body)?.[1]?.trim(),
    body,
  }));
}

test('no page, style or script asks a font service for anything', () => {
  for (const file of [...sourceFiles('src'), ...sourceFiles('public')]) {
    const text = read(file);
    assert.doesNotMatch(text, /fonts\.googleapis\.com|fonts\.gstatic\.com|use\.typekit|fonts\.bunny\.net/i, file);
  }
  assert.doesNotMatch(read('src/layouts/BaseLayout.astro'), /rel="preconnect"/);
});

test('fonts.css declares exactly the expected families and weights, in Latin and Latin Extended', () => {
  const faces = fontFaces();
  const declared = faces.map((f) => `${f.family}|${f.weight}|${/-(latin-ext|latin)-\d+-normal\.woff2/.exec(f.url)?.[1]}`).sort();
  const expected = Object.entries(EXPECTED)
    .flatMap(([family, { weights }]) => weights.flatMap((weight) => SUBSETS.map((subset) => `${family}|${weight}|${subset}`)))
    .sort();
  assert.deepEqual(declared, expected);
});

test('every font face is self-hosted woff2 with swap and its own unicode range', () => {
  for (const face of fontFaces()) {
    assert.match(face.url, /^\/fonts\/[a-z0-9-]+-normal\.woff2$/, face.url);
    assert.equal(face.format, 'woff2');
    assert.equal(face.display, 'swap');
    assert.ok(face.range && face.range.startsWith('U+'), `${face.url} needs a unicode-range`);
  }
});

test('the Latin and Latin Extended files of one weight cover different characters', () => {
  // Without distinct ranges the second declaration would replace the first and break ordinary text.
  const faces = fontFaces();
  for (const [family, { weights }] of Object.entries(EXPECTED)) {
    for (const weight of weights) {
      const ranges = faces.filter((f) => f.family === family && f.weight === weight).map((f) => f.range);
      assert.equal(ranges.length, 2, `${family} ${weight}`);
      assert.notEqual(ranges[0], ranges[1], `${family} ${weight}`);
    }
  }
  const latin = fontFaces().find((f) => f.url.includes('inter-latin-400'));
  assert.match(latin.range, /U\+0000-00FF/);
});

test('every declared font file exists, is a real woff2 and is small', () => {
  for (const face of fontFaces()) {
    const path = join(ROOT, 'public', face.url);
    assert.ok(existsSync(path), `${face.url} is missing`);
    const fd = openSync(path, 'r');
    const magic = Buffer.alloc(4);
    readSync(fd, magic, 0, 4, 0);
    closeSync(fd);
    assert.equal(magic.toString('latin1'), 'wOF2', `${face.url} is not a woff2 file`);
    assert.ok(statSync(path).size < 100_000, `${face.url} is larger than 100 KB`);
  }
});

test('no font file sits in public/fonts without being declared', () => {
  const declared = new Set(fontFaces().map((f) => f.url.replace('/fonts/', '')));
  const files = readdirSync(join(ROOT, 'public/fonts')).filter((name) => name.endsWith('.woff2'));
  assert.deepEqual(files.sort(), [...declared].sort());
});

test('every font family the styles use has a face', () => {
  const css = sourceFiles('src/styles').filter((f) => f.endsWith('.css') && !f.endsWith('fonts.css')).map(read).join('\n');
  const used = new Set([...css.matchAll(/["']([A-Z][A-Za-z0-9 ]+)["']\s*[,;)]/g)].map(([, name]) => name).filter((n) => EXPECTED[n]));
  assert.ok(used.size >= 3, 'the styles should use the site fonts');
  for (const family of used) assert.ok(fontFaces().some((f) => f.family === family), `${family} has no @font-face`);
});

test('the fonts that are preloaded are declared, exist, and are loaded before the page styles', () => {
  const layout = read('src/layouts/BaseLayout.astro');
  const preloads = [...layout.matchAll(/<link rel="preload" href="([^"]+)" as="font" type="font\/woff2" crossorigin \/>/g)].map(([, href]) => href);
  assert.ok(preloads.length >= 2);
  const declared = fontFaces().map((f) => f.url);
  for (const href of preloads) {
    assert.ok(declared.includes(href), `${href} is preloaded but not declared`);
    assert.ok(existsSync(join(ROOT, 'public', href)), `${href} is missing`);
  }
  assert.ok(preloads.some((href) => href.includes('inter-latin-400')) && preloads.some((href) => href.includes('chakra-petch-latin-700')));
});

test('the stylesheet is imported before the site styles', () => {
  const css = read('src/styles/global.css');
  assert.ok(css.indexOf('@import "./fonts.css"') > css.indexOf('@import "tailwindcss"'));
  assert.ok(css.indexOf('@import "./fonts.css"') < css.indexOf('@import "./gx.css"'));
});

test('each font family ships with its license and the README names the packages', () => {
  for (const { slug } of Object.values(EXPECTED)) {
    const path = join(ROOT, 'public/fonts/licenses', `${slug}-OFL.txt`);
    assert.ok(existsSync(path), `${slug} license is missing`);
    assert.match(readFileSync(path, 'utf8'), /SIL OPEN FONT LICENSE/i, slug);
  }
  const readme = read('public/fonts/README.md');
  for (const { slug } of Object.values(EXPECTED)) assert.ok(readme.includes(`@fontsource/${slug}`), slug);
});

test('the privacy page says where fonts and pictures come from and was updated for it', () => {
  const page = read('src/pages/privacy.astro');
  assert.match(page, /<h2[^>]*>Fonts and pictures<\/h2>/);
  const section = page.slice(page.indexOf('Fonts and pictures'), page.indexOf('Third-party links'));
  assert.match(section.replace(/\s+/g, ' '), /served from gamersxpress\.com itself/);
  assert.match(section.replace(/\s+/g, ' '), /does not contact Google or any other font service/);
  assert.match(section.replace(/\s+/g, ' '), /Steam store page or Wikimedia Commons, we keep our own copy and credit it/);
  const updated = /new Date\('(\d{4}-\d{2}-\d{2})'\)/.exec(page)?.[1];
  assert.ok(updated >= '2026-11-10', `privacy page "last updated" is ${updated}`);
});
