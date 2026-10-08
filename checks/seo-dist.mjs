import { readdir, readFile } from 'node:fs/promises';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const distDir = fileURLToPath(new URL('../dist', import.meta.url));

async function findHtmlFiles(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) files.push(...(await findHtmlFiles(full)));
    else if (entry.name.endsWith('.html')) files.push(full);
  }
  return files;
}

const files = await findHtmlFiles(distDir);
if (files.length === 0) {
  console.error('seo-dist: no HTML files found in dist/ (run `npm run build` first)');
  process.exit(1);
}

const errors = [];
for (const file of files) {
  const html = await readFile(file, 'utf8');
  const rel = relative(distDir, file).replaceAll('\\', '/');

  const h1Count = (html.match(/<h1\b/g) ?? []).length;
  if (h1Count !== 1) errors.push(`${rel}: expected exactly 1 <h1>, found ${h1Count}`);

  const canonicalCount = (html.match(/<link\b[^>]*rel="canonical"/g) ?? []).length;
  if (canonicalCount !== 1)
    errors.push(`${rel}: expected exactly 1 canonical tag, found ${canonicalCount}`);

  const jsonLdBlocks = [
    ...html.matchAll(
      /<script\b[^>]*type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/g,
    ),
  ];
  if (jsonLdBlocks.length === 0) errors.push(`${rel}: no JSON-LD block`);
  jsonLdBlocks.forEach((match, index) => {
    try {
      JSON.parse(match[1]);
    } catch (error) {
      errors.push(`${rel}: JSON-LD #${index + 1} unparseable: ${error.message}`);
    }
  });
}

if (errors.length > 0) {
  for (const error of errors) console.error(`✗ ${error}`);
  console.error(`seo-dist: ${errors.length} problem(s) in ${files.length} page(s)`);
  process.exit(1);
}
console.log(`seo-dist: ${files.length} page(s) OK — 1 H1, 1 canonical, parseable JSON-LD`);
