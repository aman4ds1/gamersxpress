import { readdir, readFile } from 'node:fs/promises';
import { join, relative } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ARTICLES_DIR = fileURLToPath(new URL('../src/content/articles', import.meta.url));
const MAX_TITLE = 65;
const MIN_DESC = 70;
const MAX_DESC = 160;
const MIN_SOURCES = 2;
const MIN_WORDS = 300;

function scalar(value) {
  const trimmed = value.trim();
  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"') && trimmed.length >= 2) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'") && trimmed.length >= 2)
  ) {
    return trimmed.slice(1, -1);
  }
  if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
    return trimmed
      .slice(1, -1)
      .split(',')
      .map((part) => part.trim())
      .filter((part) => part.length > 0);
  }
  return trimmed;
}

export function parseArticle(raw, slug) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/.exec(raw);
  if (!match) return { slug, frontmatter: {}, body: raw, parseErrors: ['missing frontmatter block'] };

  const frontmatter = {};
  let listKey = null;
  for (const line of match[1].split(/\r?\n/)) {
    if (line.trim() === '' || line.trim().startsWith('#')) continue;
    const pair = /^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$/.exec(line);
    if (pair && !line.startsWith(' ')) {
      const key = pair[1];
      const value = pair[2];
      if (value.trim() === '') {
        listKey = key;
        frontmatter[key] = [];
      } else {
        listKey = null;
        frontmatter[key] = scalar(value);
      }
      continue;
    }
    if (listKey === null) continue;
    const item = /^\s+-\s+(.*)$/.exec(line);
    if (item) {
      const text = item[1];
      const objPair = /^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$/.exec(text);
      if (objPair && objPair[2].trim() !== '') {
        frontmatter[listKey].push({ [objPair[1]]: scalar(objPair[2]) });
      } else if (objPair) {
        frontmatter[listKey].push({ [objPair[1]]: [] });
      } else {
        frontmatter[listKey].push(scalar(text));
      }
      continue;
    }
    const sub = /^\s{2,}([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$/.exec(line);
    const last = frontmatter[listKey][frontmatter[listKey].length - 1];
    if (sub && last && typeof last === 'object' && !Array.isArray(last)) {
      last[sub[1]] = scalar(sub[2]);
    }
  }
  return { slug, frontmatter, body: match[2], parseErrors: [] };
}

export function extractHeadings(body) {
  const headings = [];
  let inFence = false;
  for (const line of body.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    const match = /^(#{1,6})\s+(.+?)\s*$/.exec(line);
    if (match) headings.push({ level: match[1].length, text: match[2] });
  }
  return headings;
}

export function countWords(body) {
  const withoutFences = body.replace(/^\s*```[^\n]*\n[\s\S]*?^\s*```/gm, ' ');
  const plain = withoutFences
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s{0,3}>\s?/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/\*\*|__|~~|`/g, '');
  const words = plain.split(/\s+/).filter((word) => word.length > 0);
  return words.length;
}

export function checkArticle(article) {
  const issues = [];
  const { frontmatter, body, slug, parseErrors = [] } = article;
  for (const error of parseErrors) issues.push(error);

  const title = typeof frontmatter.title === 'string' ? frontmatter.title : '';
  if (title.trim() === '') issues.push('missing title');
  else if (title.length > MAX_TITLE) issues.push(`title is ${title.length} characters (max ${MAX_TITLE})`);

  const description = typeof frontmatter.description === 'string' ? frontmatter.description : '';
  if (description.trim() === '') issues.push('missing description');
  else if (description.length < MIN_DESC || description.length > MAX_DESC) {
    issues.push(`description is ${description.length} characters (must be ${MIN_DESC}-${MAX_DESC})`);
  }

  const sources = Array.isArray(frontmatter.sources) ? frontmatter.sources : [];
  if (sources.length < MIN_SOURCES) issues.push(`has ${sources.length} source(s) (min ${MIN_SOURCES})`);

  if (typeof frontmatter.image === 'string' && frontmatter.image.trim() !== '') {
    const imageAlt = typeof frontmatter.imageAlt === 'string' ? frontmatter.imageAlt.trim() : '';
    if (imageAlt === '') issues.push('image is set without imageAlt');
  }

  const words = countWords(body);
  if (words < MIN_WORDS) issues.push(`body has ${words} words (min ${MIN_WORDS})`);

  const headings = extractHeadings(body);
  const h1Count = headings.filter((heading) => heading.level === 1).length;
  if (h1Count > 1) issues.push(`body has ${h1Count} H1 headings (max 1)`);

  let previous = 1;
  for (const heading of headings) {
    if (heading.level > previous + 1) {
      issues.push(
        `heading levels skip: h${previous} -> h${heading.level} at "${heading.text}"`,
      );
      break;
    }
    previous = heading.level;
  }

  return { slug, title, descLength: description.length, words, sources: sources.length, h1Count, headings: headings.length, issues };
}

export function checkArticles(articles) {
  const results = articles.map((article) => checkArticle(article));
  const bySlug = new Map();
  const byTitle = new Map();
  for (const result of results) {
    const slugKey = result.slug.toLowerCase();
    if (bySlug.has(slugKey)) {
      const other = bySlug.get(slugKey);
      result.issues.push(`duplicate slug with ${other.slug}`);
      other.issues.push(`duplicate slug with ${result.slug}`);
    } else {
      bySlug.set(slugKey, result);
    }
    const titleKey = result.title.trim().toLowerCase();
    if (titleKey !== '' && byTitle.has(titleKey)) {
      const other = byTitle.get(titleKey);
      result.issues.push(`duplicate title with ${other.slug}`);
      other.issues.push(`duplicate title with ${result.slug}`);
    } else if (titleKey !== '') {
      byTitle.set(titleKey, result);
    }
  }
  return results;
}

function printTable(results) {
  const columns = [
    ['Article', (r) => r.slug],
    ['Title', (r) => String(r.title.length)],
    ['Desc', (r) => String(r.descLength)],
    ['Sources', (r) => String(r.sources)],
    ['Words', (r) => String(r.words)],
    ['H1', (r) => String(r.h1Count)],
    ['Headings', (r) => String(r.headings)],
    ['Status', (r) => (r.issues.length === 0 ? 'pass' : 'FAIL')],
  ];
  const rows = results.map((result) => columns.map(([, get]) => get(result)));
  const widths = columns.map(([name], index) =>
    Math.max(name.length, ...rows.map((row) => row[index].length)),
  );
  const line = (cells) => cells.map((cell, index) => cell.padEnd(widths[index])).join('  ');
  console.log(line(columns.map(([name]) => name)));
  console.log(line(widths.map((width) => '-'.repeat(width))));
  for (const row of rows) console.log(line(row));
}

export async function run(articlesDir = ARTICLES_DIR) {
  const files = (await readdir(articlesDir)).filter((name) => name.endsWith('.md')).sort();
  if (files.length === 0) {
    console.log('check-articles: no articles in src/content/articles/ — nothing to check');
    return;
  }
  const articles = [];
  for (const file of files) {
    const raw = await readFile(join(articlesDir, file), 'utf8');
    articles.push(parseArticle(raw, file.replace(/\.md$/, '')));
  }

  const results = checkArticles(articles);
  const failures = results.filter((result) => result.issues.length > 0);

  console.log(`check-articles: ${results.length} article(s) in ${relative(process.cwd(), articlesDir).replaceAll('\\', '/')}\n`);
  printTable(results);

  if (failures.length > 0) {
    console.log('');
    for (const result of failures) {
      for (const issue of result.issues) console.log(`✗ ${result.slug}: ${issue}`);
    }
    console.log(`\ncheck-articles: ${failures.length} of ${results.length} article(s) failed`);
    process.exitCode = 1;
    return;
  }
  console.log(`\ncheck-articles: all ${results.length} article(s) passed`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await run();
}
