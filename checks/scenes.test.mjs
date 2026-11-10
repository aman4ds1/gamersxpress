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

test('there is a sky scene, a maze scene and a plain option, chosen by data-scene', () => {
  assert.match(scenes, /class="gx-scene gx-skyscene"/);
  assert.match(scenes, /class="gx-scene gx-mazescene"/);
  assert.match(css, /:root\[data-scene='sky'\] \.gx-skyscene/);
  assert.match(css, /:root:not\(\[data-scene\]\) \.gx-skyscene/);       // the sky is the default without JavaScript
  assert.match(css, /:root\[data-scene='maze'\] \.gx-mazescene/);
  assert.match(css, /\.gx-scene \{ position: absolute; inset: 0; display: none; \}/);
  assert.match(header, /const scenes = \['sky', 'maze', 'plain'\];/);
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

test('the scenes never restyle the theme button icons (a real bug caught in review)', () => {
  assert.doesNotMatch(css, /\.gx-sun\b|\.gx-moon\b/);
  assert.doesNotMatch(scenes, /class="gx-sun"|class="gx-moon"/);
  assert.match(header, /class="gx-moon"/);
  assert.match(header, /class="gx-sun"/);
});

test('the old gradient-blob and grid background is gone and the layout uses the scenes', () => {
  const gx = read('src/styles/gx.css');
  for (const text of [layout, gx]) assert.doesNotMatch(text, /gx-blob|gx-gridlines|gx-shape|gx-drift|gx-gridmove/);
  assert.match(layout, /<Scenes \/>/);
  assert.match(read('src/styles/global.css'), /@import "\.\/scenes\.css";/);
});

test('the choice is saved and applied before the page paints, defaulting to the sky', () => {
  assert.match(layout, /localStorage\.getItem\('gx-scene'\)/);
  assert.match(layout, /scene === 'maze' \|\| scene === 'plain' \? scene : 'sky'/);
  assert.match(layout, /catch \(e\) \{\s*document\.documentElement\.dataset\.scene = 'sky';/);
  assert.match(header, /localStorage\.setItem\('gx-scene', next\)/);
  assert.ok(layout.indexOf("localStorage.getItem('gx-scene')") < layout.indexOf('<Scenes />'));
});

test('the switch is a labelled button with an icon for every scene', () => {
  assert.match(header, /<button type="button" class="gx-iconbtn gx-scenebtn" id="scene-toggle" aria-label="[^"]+">/);
  for (const icon of ['ic-sky', 'ic-maze', 'ic-plain']) assert.match(header, new RegExp(`class="${icon}"`));
  assert.match(header, /aria-label', `Background: \$\{sceneNames\[currentScene\(\)\]\}\. Switch to the \$\{sceneNames\[nextScene\(\)\]\}`/);
  assert.match(scenes, /aria-hidden="true"/);
});

test('the shipped scene code carries no trademarked names', () => {
  const sceneScript = header.slice(header.indexOf('The background switch'));
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

test('the ghosts are frightened blue ones with no delay of their own, so they stay on the chomper\'s clock', () => {
  assert.match(css, /--fright: #3b6df0;/);
  assert.equal((css.match(/--fright: #5b8cff;/g) || []).length, 2);
  assert.match(css, /\.gx-ghost \{ color: var\(--fright\); \}/);
  assert.doesNotMatch(css, /--g[123]|\.gx-gh\d|animation-delay: 1\.5s|animation-delay: 2\.6s/);
  const ghostRules = [...css.matchAll(/\.gx-(?:rev )?\.?gx-f\d+ \{[^}]*\}/g)].map(([rule]) => rule).join('\n');
  assert.doesNotMatch(ghostRules, /animation-delay/);
  assert.match(scenes, /const ghostFace: Rect\[\]/);
  assert.doesNotMatch(scenes, /#1e3a8a|ghostEyes/);                // the old chasing ghosts had pupils
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

test('the colours follow the theme: light by default, dark when chosen or when the system is dark', () => {
  assert.match(css, /:root\[data-theme='dark'\] \.gx-scene \{/);
  assert.match(css, /@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme='light'\]\) \.gx-scene \{/);
  const dark = css.match(/:root\[data-theme='dark'\] \.gx-scene \{([\s\S]*?)\n\}/)[1].replace(/\s+/g, ' ').trim();
  const system = css.match(/:root:not\(\[data-theme='light'\]\) \.gx-scene \{([\s\S]*?)\n  \}/)[1].replace(/\s+/g, ' ').trim();
  assert.equal(dark, system, 'the explicit dark colours and the system-dark colours must be identical');
});
