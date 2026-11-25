// Small pixel sprites for the static decorations (PixelDecor, the RSS banner and the footer). Original artwork drawn as
// SVG rectangles, the same cloud and coin as the animated scenes in src/components/Scenes.astro, so there are no image
// files and no network requests. A rectangle is [x, y, width, height, colour?]; with no colour it uses currentColor.
export type Rect = [number, number, number, number, string?];

export interface Sprite {
  width: number;
  height: number;
  rects: Rect[];
}

export const CLOUD: Sprite = {
  width: 20,
  height: 7,
  rects: [[5, 0, 5, 1], [11, 0, 4, 1], [4, 1, 13, 1], [2, 2, 16, 1], [1, 3, 18, 1], [0, 4, 20, 1], [1, 5, 18, 1], [3, 6, 14, 1]],
};

export const COIN: Sprite = {
  width: 10,
  height: 14,
  rects: [
    [3, 0, 4, 1, '#FFD044'], [2, 1, 6, 1, '#FFD044'], [1, 2, 8, 10, '#FFD044'], [2, 12, 6, 1, '#FFD044'], [3, 13, 4, 1, '#FFD044'],
    [4, 3, 2, 8, '#E0A800'],
  ],
};

/** A round pixel sun, like the one in the mockup's top-right corner. */
export const SUN: Sprite = {
  width: 12,
  height: 12,
  rects: [[4, 0, 4, 1], [2, 1, 8, 1], [1, 2, 10, 1], [0, 3, 12, 6], [1, 9, 10, 1], [2, 10, 8, 1], [4, 11, 4, 1]],
};

/** A loose cluster of squares for corners. */
export const BITS: Sprite = {
  width: 12,
  height: 12,
  rects: [[0, 8, 4, 4], [4, 4, 4, 4], [8, 8, 4, 4], [0, 0, 2, 2], [10, 2, 2, 2]],
};
