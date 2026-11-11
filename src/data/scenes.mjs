// Which animated background a page gets. The home page and the information pages (about, privacy, terms...) get the
// sky scene; every other page (articles, category and topic listings, search...) gets the maze scene.
// A plain .mjs module so the build checks and the tests can import the very same function the layout uses.

/** The pages that get the sky scene. A page that is not listed here gets the maze. */
export const SKY_PATHS = ['/', '/about', '/privacy', '/terms', '/contact', '/corrections', '/editorial-policy', '/ai-disclosure'];

/**
 * @param {string} pathname a URL path such as "/about/" or "/news/some-article/"
 * @returns {'sky' | 'maze'}
 */
export function sceneForPath(pathname) {
  const path = String(pathname || '/').replace(/\/+$/, '') || '/';
  return SKY_PATHS.includes(path) ? 'sky' : 'maze';
}
