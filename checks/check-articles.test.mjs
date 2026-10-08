import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseArticle, checkArticle, checkArticles, countWords } from './check-articles.mjs';

const FILLER = Array.from({ length: 320 }, (_, index) => `word${index}`).join(' ');

function rawArticle(overrides = {}) {
  const {
    title = 'A well written sample headline that fits within sixty five',
    description = 'A sample description that is comfortably inside the required seventy to one hundred sixty character range.',
    body = '## Section one\n\n' + FILLER,
    sources = ['One', 'Two'],
    image,
    imageAlt,
  } = overrides;
  const lines = ['---', `title: "${title}"`, `description: "${description}"`, 'pubDate: 2026-10-01'];
  if (image) lines.push(`image: ${image}`);
  if (imageAlt) lines.push(`imageAlt: "${imageAlt}"`);
  lines.push('sources:');
  for (const name of sources) {
    lines.push(`  - name: ${name}`);
    lines.push('    url: https://example.com/x');
  }
  lines.push('---', '', body);
  return lines.join('\n');
}

function check(overrides, slug = 'sample-article') {
  return checkArticle(parseArticle(rawArticle(overrides), slug));
}

test('good article passes all checks', () => {
  const result = check({});
  assert.deepEqual(result.issues, []);
});

test('title at exactly 65 characters passes, 66 fails', () => {
  assert.deepEqual(check({ title: 'a'.repeat(65) }).issues, []);
  const result = check({ title: 'a'.repeat(66) });
  assert.equal(result.issues.length, 1);
  assert.match(result.issues[0], /title is 66 characters \(max 65\)/);
});

test('missing title fails', () => {
  const raw = rawArticle().replace(/^title: .*$/m, '');
  const result = checkArticle(parseArticle(raw, 'sample-article'));
  assert.ok(result.issues.some((issue) => issue === 'missing title'));
});

test('description length boundaries', () => {
  assert.deepEqual(check({ description: 'd'.repeat(70) }).issues, []);
  assert.deepEqual(check({ description: 'd'.repeat(160) }).issues, []);
  assert.match(check({ description: 'd'.repeat(69) }).issues[0], /must be 70-160/);
  assert.match(check({ description: 'd'.repeat(161) }).issues[0], /must be 70-160/);
});

test('fewer than two sources fails', () => {
  const result = check({ sources: ['Only one'] });
  assert.equal(result.issues.length, 1);
  assert.match(result.issues[0], /has 1 source\(s\) \(min 2\)/);
});

test('image without imageAlt fails, with imageAlt passes', () => {
  const withoutAlt = check({ image: '/images/sample.jpg' });
  assert.equal(withoutAlt.issues.length, 1);
  assert.match(withoutAlt.issues[0], /image is set without imageAlt/);
  const withAlt = check({ image: '/images/sample.jpg', imageAlt: 'A sample image' });
  assert.deepEqual(withAlt.issues, []);
});

test('body word count boundaries', () => {
  const threeHundred = Array.from({ length: 300 }, () => 'word').join(' ');
  assert.equal(countWords(threeHundred), 300);
  assert.deepEqual(check({ body: threeHundred }).issues, []);
  const twoNinetyNine = Array.from({ length: 299 }, () => 'word').join(' ');
  assert.match(check({ body: twoNinetyNine }).issues[0], /body has 299 words \(min 300\)/);
});

test('more than one H1 in the body fails', () => {
  const result = check({ body: '# First heading\n\n# Second heading\n\n' + FILLER });
  assert.ok(result.issues.some((issue) => /body has 2 H1 headings/.test(issue)));
  const singleH1 = check({ body: '# Only heading\n\n' + FILLER });
  assert.ok(!singleH1.issues.some((issue) => issue.includes('H1')));
});

test('skipped heading levels fail, consecutive levels pass', () => {
  const skip = check({ body: '## Section\n\n#### Skipped\n\n' + FILLER });
  assert.ok(skip.issues.some((issue) => /heading levels skip: h2 -> h4/.test(issue)));
  const ok = check({ body: '## Section\n\n### Subsection\n\n' + FILLER });
  assert.deepEqual(ok.issues, []);
});

test('duplicate titles and slugs are reported on both articles', () => {
  const same = parseArticle(rawArticle(), 'same-title');
  const results = checkArticles([same, parseArticle(rawArticle(), 'other')]);
  assert.ok(results[0].issues.some((issue) => issue.startsWith('duplicate title')));
  assert.ok(results[1].issues.some((issue) => issue.startsWith('duplicate title')));

  const dupSlug = checkArticles([
    parseArticle(rawArticle(), 'twin'),
    parseArticle(rawArticle({ title: 'A different title entirely for the second article' }), 'twin'),
  ]);
  assert.ok(dupSlug[0].issues.some((issue) => issue.startsWith('duplicate slug')));
  assert.ok(dupSlug[1].issues.some((issue) => issue.startsWith('duplicate slug')));
});

test('missing frontmatter is reported', () => {
  const result = checkArticle(parseArticle('# no frontmatter here', 'broken'));
  assert.ok(result.issues.some((issue) => issue === 'missing frontmatter block'));
});
