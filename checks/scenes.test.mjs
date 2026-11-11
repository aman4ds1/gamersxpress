import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (path) => readFileSync(join(ROOT, path), 'utf8');
const css = read('src/styles/scenes.css');
const scenes = read('src/components/Scenes.astro');
const header = read('src/components/Header.astro');
const layout = read('src/layouts/BaseLayout.astro');

test('the scenes load nothing from the network', () => {
  // The only "http://" inside the inline tiles is the standard SVG namespace name, which is not a request.
  const outsideTheTiles = css.replace(/url\("data:image\/svg\+xml,[^"]*"\)/g, 'url(TILE)');
  assert.doesNotMatch(outsideTheTiles, /https?:\/\//);
  assert.doesNotMatch(css.replace(/xmlns=%22http:\/\/www\.w3\.org\/2000\/svg%22/g, ''), /https?:\/\//);
  assert.doesNotMatch(css, /@import/);
  const urls = [...css.matchAll(/url\(([^)]*)\)/g)].map(([, value]) => value.replace(/^["']/, ''));
  assert.ok(urls.length >= 3, 'the hills, bricks and maze tiles');
  for (const value of urls) assert.match(value, /^data:image\/svg\+xml,/, value.slice(0, 40));
  assert.doesNotMatch(scenes, /<img|src=|href=|<image/);
});

test('the page picks its scene and only that scene is drawn; the background can be switched off', () => {
  assert.match(scenes, /interface Props \{\s*scene: 'sky' \| 'maze';\s*\}/);
  assert.match(scenes, /\{scene === 'sky' && \(\s*<div class="gx-scene gx-skyscene">/);
  assert.match(scenes, /\{scene === 'maze' && \(\s*<div class="gx-scene gx-mazescene">/);
  assert.match(css, /\.gx-scene \{ position: absolute; inset: 0; \}/);
  assert.match(css, /:root\[data-scene='plain'\] \.gx-bg \{ display: none; \}/);
  assert.doesNotMatch(css, /\.gx-scene \{[^}]*display: none/);                      // no scene starts hidden: the page only renders its own
});

test('every animation the scenes use has its keyframes', () => {
  const used = new Set([...css.matchAll(/animation(?:-name)?:\s*([a-z-]+)/g)].map(([, name]) => name).filter((name) => name.startsWith('gx-')));
  const defined = new Set([...css.matchAll(/@keyframes\s+(gx-[a-z-]+)/g)].map(([, name]) => name));
  assert.ok(used.size >= 12);
  for (const name of used) assert.ok(defined.has(name), `${name} has no @keyframes`);
});

test('reduced motion switches every scene animation off and leaves the sprites in place', () => {
  const block = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'), css.indexOf('@media print'));
  assert.match(block, /\.gx-scene \*, \.gx-scene \*::before \{ animation: none !important; \}/);
  assert.match(block, /\.gx-chomper-run \{ transform: translateX\(/);
  assert.match(block, /\.gx-ghost-run \{ transform: translateX\(/);
  assert.match(block, /\.gx-cl1 \{ transform: translateX\(/);
});

test('the old gradient-blob and grid background is gone and the layout uses the scenes', () => {
  const gx = read('src/styles/gx.css');
  for (const text of [layout, gx]) assert.doesNotMatch(text, /gx-blob|gx-gridlines|gx-shape|gx-drift|gx-gridmove/);
  assert.match(layout, /<Scenes scene=\{scene\} \/>/);
  assert.match(read('src/styles/global.css'), /@import "\.\/scenes\.css";/);
});

test('before the page paints it gets its own scene, unless the visitor switched the background off', () => {
  assert.match(layout, /const scene = sceneForPath\(Astro\.url\.pathname\);/);
  assert.match(layout, /<html lang="en" data-page-scene=\{scene\}>/);
  assert.match(layout, /const page = document\.documentElement\.dataset\.pageScene;/);
  assert.match(layout, /document\.documentElement\.dataset\.scene = localStorage\.getItem\('gx-scene'\) === 'plain' \? 'plain' : page;/);
  assert.match(layout, /catch \(e\) \{\s*document\.documentElement\.dataset\.scene = document\.documentElement\.dataset\.pageScene;/);
  assert.ok(layout.indexOf("localStorage.getItem('gx-scene')") < layout.indexOf('<Scenes'));
  // only "off" is remembered: turning it on forgets the setting so each page starts with its own scene
  assert.match(header, /if \(next === 'plain'\) localStorage\.setItem\('gx-scene', 'plain'\);\s*else localStorage\.removeItem\('gx-scene'\);/);
  assert.match(header, /const next = backgroundOn\(\) \? 'plain' : \(document\.documentElement\.dataset\.pageScene \?\? 'sky'\);/);
});

test('the background switch is a labelled on/off button with an icon for each state', () => {
  // there are two of these buttons: one in the bar (wide screens) and one inside the menu (small screens)
  assert.match(header, /<button type="button" class="gx-iconbtn gx-scenebtn" id="scene-toggle" data-scene-toggle aria-label="[^"]+">/);
  assert.match(header, /<button type="button" class="gx-scenebtn gx-menu-scene" data-scene-toggle aria-label="[^"]+">/);
  for (const icon of ['ic-sky', 'ic-maze', 'ic-plain']) assert.equal((header.match(new RegExp(`class="${icon}"`, 'g')) || []).length, 2, icon);
  assert.match(header, /const text = `Background: \$\{backgroundOn\(\) \? 'on' : 'off'\}\. Turn it \$\{backgroundOn\(\) \? 'off' : 'on'\}`;\s*sceneButtons\.forEach\(\(button\) => button\.setAttribute\('aria-label', text\)\);/);
  // the icon shows the state: the page's scene while it is on, the plain square while it is off
  assert.match(css, /:root\[data-scene='sky'\] \.gx-scenebtn \.ic-sky,\s*:root:not\(\[data-scene\]\) \.gx-scenebtn \.ic-sky,\s*:root\[data-scene='maze'\] \.gx-scenebtn \.ic-maze,\s*:root\[data-scene='plain'\] \.gx-scenebtn \.ic-plain \{ display: block; \}/);
  assert.match(scenes, /aria-hidden="true"/);
});

test('the shipped scene code carries no trademarked names', () => {
  const sceneScript = header.slice(header.indexOf('The background switch'), header.indexOf('The menu:'));
  for (const text of [scenes, css, layout.slice(layout.indexOf('<script is:inline>'), layout.indexOf('</script>')), sceneScript]) {
    assert.doesNotMatch(text, /mario|pac-?man|luigi|goomba|bowser|blinky|pinky|inky|clyde/i);
  }
});

test('each pellet lane is clipped and run on the same clock so the chomper really eats what it passes', () => {
  assert.match(css, /\.gx-pellets \{[^}]*animation: gx-eat var\(--t\) linear infinite/);
  assert.match(css, /\.gx-runner \{[^}]*animation: gx-run var\(--t\) linear infinite/);
  assert.match(css, /\.gx-rev \.gx-pellets \{ animation-name: gx-eat-rev; \}/);
  assert.match(css, /\.gx-rev \.gx-chomper-run \{ animation-name: gx-run-rev; \}/);
  assert.doesNotMatch(css, /\.gx-rev \.gx-runner \{/);          // that would also turn the ghosts round
  // the lane spans 124vw starting 12vw off the left edge, and the chomper travels exactly that far
  assert.match(css, /\.gx-lane \{[^}]*left: -12vw; width: 124vw;/);
  assert.match(css, /@keyframes gx-run \{ from \{ transform: translateX\(0\); \} to \{ transform: translateX\(124vw\); \} \}/);
  assert.match(css, /@keyframes gx-run-rev \{ from \{ transform: translateX\(124vw\); \} to \{ transform: translateX\(0\); \} \}/);
});

test('pellets vanish where the chomper\'s mouth is, not where its tail passes', () => {
  // the sprite is 28px wide: going right the mouth is at its right edge, going left at its left edge
  assert.match(css, /@keyframes gx-eat \{ from \{ clip-path: inset\(0 0 0 20px\); \} to \{ clip-path: inset\(0 0 0 calc\(100% \+ 20px\)\); \} \}/);
  assert.match(css, /@keyframes gx-eat-rev \{ from \{ clip-path: inset\(0 -8px 0 0\); \} to \{ clip-path: inset\(0 calc\(100% - 8px\) 0 0\); \} \}/);
  assert.match(css, /\.gx-chomper \{[^}]*width: 28px;/);
});

// ---- the chomper eats the ghosts; the ghosts never chase it ----------------------------------------------------------------

const LANE_WIDTH = 124; // vw, from .gx-lane
const lanes = [...scenes.matchAll(/\{ lane: '(gx-l\d)', ghosts: \[([^\]]*)\], reverse: (true|false) \}/g)].map(([, lane, ghosts, reverse]) => ({
  lane,
  reverse: reverse === 'true',
  ghosts: [...ghosts.matchAll(/'(gx-f(\d+))'/g)].map(([, name, percent]) => ({ name, percent: Number(percent) })),
}));
const number = (text) => Number(/-?[\d.]+/.exec(text)[0]);
function flight(name, reverse) {
  const selector = reverse ? `\\.gx-rev \\.${name}` : `\\.${name}`;
  const rule = new RegExp(`${selector} \\{([^}]*)\\}`).exec(css);
  assert.ok(rule, `${reverse ? '.gx-rev ' : ''}.${name} has no rule`);
  const x0 = /--x0: ([^;]+);/.exec(rule[1])[1];
  const end = /--end: ([^;]+);/.exec(rule[1])[1];
  const animation = /animation: (gx-flee-\d+) var\(--t\) linear infinite;/.exec(rule[1]);
  assert.ok(animation, `${name} must use a gx-flee animation on the lane clock`);
  const vw = (text) => number(/(-?[\d.]+)vw/.exec(text)[1]);
  const startVw = reverse ? LANE_WIDTH - vw(/calc\(124vw - ([\d.]+vw)\)/.exec(x0)?.[1] ?? '0vw') : vw(x0);
  const endVw = Number(/calc\(124vw \* ([\d.]+) [+-] \d+px\)/.exec(end)[1]) * LANE_WIDTH;
  return { x0, end, startVw, endVw, keyframes: animation[1] };
}

test('the lanes are described in the markup: every ghost has a catch point', () => {
  assert.equal(lanes.length, 3);
  assert.deepEqual(lanes.map((lane) => lane.reverse), [false, true, false]);
  assert.ok(lanes.every((lane) => lane.ghosts.length >= 1));
});

test('every ghost starts ahead of the chomper and is caught before the lane ends', () => {
  for (const { reverse, ghosts } of lanes) {
    for (const { name, percent } of ghosts) {
      const { startVw, endVw } = flight(name, reverse);
      if (!reverse) {
        assert.ok(startVw > 0, `${name} must start ahead (to the right of the chomper's start)`);
        assert.ok(endVw > startVw, `${name} runs forward`);
        assert.ok(endVw < LANE_WIDTH, `${name} is caught inside the lane`);
        // slower than the chomper, or it could never be caught
        assert.ok((endVw - startVw) / (percent / 100) < LANE_WIDTH, `${name} must be slower than the chomper`);
      } else {
        assert.ok(startVw < LANE_WIDTH, `${name} must start ahead (to the left of the chomper's start)`);
        assert.ok(endVw < startVw, `${name} runs left`);
        assert.ok(endVw > 0, `${name} is caught inside the lane`);
        assert.ok((startVw - endVw) / (percent / 100) < LANE_WIDTH, `${name} must be slower than the chomper`);
      }
    }
  }
});

test('a ghost is caught exactly where the chomper is when its keyframes say it is eaten', () => {
  for (const { reverse, ghosts } of lanes) {
    for (const { name, percent } of ghosts) {
      const { keyframes, endVw } = flight(name, reverse);
      assert.equal(keyframes, `gx-flee-${percent}`, `${name} must use the keyframes of its catch point`);
      const frames = new RegExp(`@keyframes ${keyframes} \\{([\\s\\S]*?)\\n\\}`).exec(css)[1];
      assert.match(frames, new RegExp(`${percent}% \\{ transform: translateX\\(var\\(--end\\)\\) scale\\(1\\); opacity: 1; \\}`));
      assert.match(frames, new RegExp(`${percent + 1}% \\{ transform: translateX\\(var\\(--end\\)\\) scale\\(1\\.5\\); opacity: 0; \\}`));
      assert.match(frames, /100% \{ transform: translateX\(var\(--end\)\) scale\(1\.5\); opacity: 0; \}/);   // and stays gone
      // where the chomper's left edge is at that moment (it runs the whole lane in the whole time)
      const chomperAt = reverse ? LANE_WIDTH * (1 - percent / 100) : LANE_WIDTH * (percent / 100);
      assert.ok(Math.abs(endVw - chomperAt) < 0.01, `${name} is caught where the chomper is (${endVw} vs ${chomperAt})`);
    }
  }
});

test('the ghost is met at the mouth: 15px ahead going right, 13px ahead going left', () => {
  for (const { reverse, ghosts } of lanes) {
    for (const { name } of ghosts) {
      const { end } = flight(name, reverse);
      assert.match(end, reverse ? / - 13px\)$/ : / \+ 15px\)$/, `${name} --end ${end}`);
    }
  }
});

test('two ghosts in one lane never cross, so the first caught is always the one behind', () => {
  for (const { reverse, ghosts } of lanes) {
    for (let a = 0; a < ghosts.length; a += 1) {
      for (let b = a + 1; b < ghosts.length; b += 1) {
        const [first, second] = ghosts[a].percent < ghosts[b].percent ? [ghosts[a], ghosts[b]] : [ghosts[b], ghosts[a]];
        const f = flight(first.name, reverse);
        const g = flight(second.name, reverse);
        const secondAt = (share) => g.startVw + (g.endVw - g.startVw) * (share / (second.percent / 100));
        const sign = reverse ? -1 : 1;
        assert.ok(sign * (secondAt(0) - f.startVw) > 0, `${second.name} must start ahead of ${first.name}`);
        assert.ok(sign * (secondAt(first.percent / 100) - f.endVw) > 0, `${second.name} must still be ahead when ${first.name} is caught`);
      }
    }
  }
});

test('Pac-Man is bright yellow in both themes, with an outline only on the pale page', () => {
  assert.equal((css.match(/--chomp: #ffd500;/g) || []).length, 3);                     // light, and the device's dark setting
  assert.doesNotMatch(css, /--chomp: #(?!ffd500)/);
  assert.match(css, /--chomp-edge: drop-shadow\(1px 0 0 #6b5000\) drop-shadow\(-1px 0 0 #6b5000\) drop-shadow\(0 1px 0 #6b5000\) drop-shadow\(0 -1px 0 #6b5000\);/);
  assert.equal((css.match(/--chomp-edge: none;/g) || []).length, 2);
  assert.match(css, /\.gx-chomper \{[^}]*color: var\(--chomp\); filter: var\(--chomp-edge\);/);
});

test('the ghosts are colourful, each its own colour, and run on the chomper\'s clock with no delay of their own', () => {
  assert.match(css, /--g-pink: #ff5fa2; --g-aqua: #22d3ee; --g-orange: #ff9a3c; --g-violet: #a78bfa;/);
  assert.match(css, /\.gx-ghost \{ color: var\(--ghost, var\(--g-pink\)\); \}/);
  const used = {};
  for (const { name } of lanes.flatMap((lane) => lane.ghosts)) {
    const colour = /--ghost: var\((--g-[a-z]+)\);/.exec(new RegExp(`\\.(?:gx-rev )?\\.?${name} \\{([^}]*)\\}`).exec(css)?.[1] ?? '')?.[1];
    assert.ok(colour, `${name} needs its own --ghost colour`);
    used[colour] = (used[colour] || 0) + 1;
  }
  assert.deepEqual(Object.keys(used).sort(), ['--g-aqua', '--g-orange', '--g-pink', '--g-violet']);   // four colours across the ghosts
  assert.doesNotMatch(css, /--fright|--g[123]\b|\.gx-gh\d|#3b6df0|#5b8cff/);                         // no all-blue frightened ghosts any more
  const ghostRules = [...css.matchAll(/\.gx-(?:rev )?\.?gx-f\d+ \{[^}]*\}/g)].map(([rule]) => rule).join('\n');
  assert.doesNotMatch(ghostRules, /animation-delay/);
  assert.match(scenes, /const ghostEyes: Rect\[\] = \[\[3, 4, 3, 4, '#ffffff'\], \[8, 4, 3, 4, '#ffffff'\], \[5, 6, 2, 2, '#1e3a8a'\], \[10, 6, 2, 2, '#1e3a8a'\]\];/);
  assert.doesNotMatch(scenes, /ghostFace|#fde68a/);
});

test('only cheap properties are animated', () => {
  const keyframes = [...css.matchAll(/@keyframes\s+gx-[a-z-]+\s*\{([\s\S]*?)\n?\}\s*(?=@keyframes|\n\/\*|\n\.|$)/g)].map(([, body]) => body).join('\n');
  const properties = new Set([...keyframes.matchAll(/([a-z-]+):\s*[^;{}]+;/g)].map(([, name]) => name));
  for (const name of properties) assert.ok(['transform', 'opacity', 'clip-path'].includes(name), `${name} should not be animated`);
});

test('phones drop the heavier pieces and the maze gets quieter; print shows no scene', () => {
  const small = css.slice(css.indexOf('@media (max-width: 640px)'), css.indexOf('@media (prefers-reduced-motion'));
  for (const name of ['.gx-cl3', '.gx-cl4', '.gx-l3', '.gx-q2']) assert.ok(small.includes(name), name);
  assert.match(small, /\.gx-maze \{ opacity: 0\.7; \}/);
  assert.match(css, /@media print \{\s*\.gx-bg \{ display: none; \}/);
});

test('the colours follow the chosen theme, or the device when nothing is chosen', () => {
  assert.equal((css.match(/@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme='light'\]\) \.gx-scene \{/g) || []).length, 1);
  assert.match(css, /:root\[data-theme='dark'\] \.gx-scene \{/);
  const device = /:root:not\(\[data-theme='light'\]\) \.gx-scene \{([\s\S]*?)\r?\n  \}\r?\n\}/.exec(css)[1];
  assert.ok(device.includes('--sky-top: #070b2a;') && device.includes('--sun-on: 0; --moon-on: 1; --stars-on: 1;'));
  assert.match(css, /--sun-on: 1; --moon-on: 0; --stars-on: 0;/);
});
