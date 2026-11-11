import { sceneForPath } from '../src/data/scenes.mjs';

/** The URL path a built file is served at: "index.html" -> "/", "about/index.html" -> "/about/", "404.html" -> "/404". */
export function pathForFile(relativePath) {
  return `/${relativePath.replace(/\\/g, '/').replace(/(^|\/)index\.html$/, '$1').replace(/\.html$/, '')}`;
}

/** Problems with the background scene of one built page (empty when it is right). */
export function sceneProblems(relativePath, html) {
  const expected = sceneForPath(pathForFile(relativePath));
  const problems = [];
  const declared = /<html\b[^>]*\sdata-page-scene="([^"]*)"/.exec(html)?.[1];
  if (declared !== expected) problems.push(`${relativePath}: should have the ${expected} scene but data-page-scene is ${declared ? `"${declared}"` : 'missing'}`);
  const sky = (html.match(/class="gx-scene gx-skyscene"/g) ?? []).length;
  const maze = (html.match(/class="gx-scene gx-mazescene"/g) ?? []).length;
  const [wanted, other] = expected === 'sky' ? [sky, maze] : [maze, sky];
  if (wanted !== 1) problems.push(`${relativePath}: expected exactly one ${expected} scene in the page, found ${wanted}`);
  if (other !== 0) problems.push(`${relativePath}: the ${expected === 'sky' ? 'maze' : 'sky'} scene must not be rendered on this page`);
  return problems;
}
