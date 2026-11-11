import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SKY_PATHS, sceneForPath } from '../src/data/scenes.mjs';
import { pathForFile, sceneProblems } from './scenes-dist.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

test('the home page and the information pages get the sky scene', () => {
  for (const path of ['/', '/about', '/privacy', '/terms', '/contact', '/corrections', '/editorial-policy', '/ai-disclosure']) {
    assert.equal(sceneForPath(path), 'sky', path);
    assert.equal(sceneForPath(`${path}/`.replace('//', '/')), 'sky', `${path} with a trailing slash`);
  }
});

test('articles, category and topic pages, search and anything unknown get the maze', () => {
  for (const path of ['/news/some-article/', '/news/nier-automata-update-inflates-pc-file-size-to-366gb/', '/category/pc/', '/category/nintendo', '/topic/capcom/', '/search', '/search/', '/404', '/anything-else/', '/about/team/', '/privacy-extra', '/news']) {
    assert.equal(sceneForPath(path), 'maze', path);
  }
});

test('odd input never breaks the rule', () => {
  assert.equal(sceneForPath(''), 'sky');                       // an empty path is the home page
  assert.equal(sceneForPath(undefined), 'sky');
  assert.equal(sceneForPath('//'), 'sky');
  assert.equal(sceneForPath('/ABOUT'), 'maze');                // paths here are lower-case; anything else is not an information page
  assert.equal(sceneForPath('/about?x=1'), 'maze');            // a path never contains a query
});

test('the list of sky pages is exactly the home page and the information pages', () => {
  assert.deepEqual([...SKY_PATHS].sort(), ['/', '/about', '/ai-disclosure', '/contact', '/corrections', '/editorial-policy', '/privacy', '/terms']);
  assert.equal(new Set(SKY_PATHS).size, SKY_PATHS.length);
  assert.ok(SKY_PATHS.every((path) => path === '/' || (path.startsWith('/') && !path.endsWith('/'))));
});

test('every page of the site has been deliberately given a scene', () => {
  // A new page must be added here (or to SKY_PATHS), so nobody gets a background by accident.
  const MAZE_PAGES = ['/search'];
  const MAZE_FOLDERS = ['category', 'news', 'topic'];
  const NOT_PAGES = ['rss.xml.ts'];
  const known = new Set([...SKY_PATHS, ...MAZE_PAGES]);
  const unclassified = [];
  for (const entry of readdirSync(join(ROOT, 'src/pages'), { withFileTypes: true })) {
    if (NOT_PAGES.includes(entry.name)) continue;
    if (entry.isDirectory()) {
      if (!MAZE_FOLDERS.includes(entry.name)) unclassified.push(`${entry.name}/`);
    } else {
      const route = `/${entry.name.replace(/\.astro$/, '')}`.replace(/^\/index$/, '/');
      if (!known.has(route)) unclassified.push(route);
    }
  }
  assert.deepEqual(unclassified, [], 'give these pages a scene in src/data/scenes.mjs (sky) or in this test (maze)');
  for (const path of MAZE_PAGES) assert.equal(sceneForPath(path), 'maze', path);
  for (const folder of MAZE_FOLDERS) assert.equal(sceneForPath(`/${folder}/x/`), 'maze', folder);
});

test('a built file is mapped back to the path it is served at', () => {
  assert.equal(pathForFile('index.html'), '/');
  assert.equal(pathForFile('about/index.html'), '/about/');
  assert.equal(pathForFile('news/some-article/index.html'), '/news/some-article/');
  assert.equal(pathForFile('404.html'), '/404');
  assert.equal(pathForFile('category\\pc\\index.html'), '/category/pc/');          // Windows paths
});

const page = (scene, { declared = scene, sky = scene === 'sky' ? 1 : 0, maze = scene === 'maze' ? 1 : 0 } = {}) =>
  `<html lang="en" data-page-scene="${declared}"><body>${'<div class="gx-scene gx-skyscene"></div>'.repeat(sky)}${'<div class="gx-scene gx-mazescene"></div>'.repeat(maze)}</body></html>`;

test('the build check accepts a page with its own scene and nothing else', () => {
  assert.deepEqual(sceneProblems('index.html', page('sky')), []);
  assert.deepEqual(sceneProblems('privacy/index.html', page('sky')), []);
  assert.deepEqual(sceneProblems('news/x/index.html', page('maze')), []);
  assert.deepEqual(sceneProblems('search/index.html', page('maze')), []);
});

test('the build check names every kind of mistake', () => {
  assert.match(sceneProblems('index.html', page('maze'))[0], /index\.html: should have the sky scene but data-page-scene is "maze"/);
  assert.match(sceneProblems('news/x/index.html', page('sky'))[0], /should have the maze scene but data-page-scene is "sky"/);
  assert.match(sceneProblems('about/index.html', '<html lang="en"><body></body></html>').join('\n'), /data-page-scene is missing/);
  assert.match(sceneProblems('index.html', page('sky', { sky: 0 })).join('\n'), /expected exactly one sky scene in the page, found 0/);
  assert.match(sceneProblems('index.html', page('sky', { sky: 2 })).join('\n'), /expected exactly one sky scene in the page, found 2/);
  assert.match(sceneProblems('index.html', page('sky', { maze: 1 })).join('\n'), /the maze scene must not be rendered on this page/);
  assert.match(sceneProblems('news/x/index.html', page('maze', { sky: 1 })).join('\n'), /the sky scene must not be rendered on this page/);
});

test('the build check runs on every built page', () => {
  const seo = readFileSync(join(ROOT, 'checks/seo-dist.mjs'), 'utf8');
  assert.match(seo, /import \{ sceneProblems \} from '\.\/scenes-dist\.mjs';/);
  assert.match(seo, /errors\.push\(\.\.\.sceneProblems\(rel, html\)\);/);
});
