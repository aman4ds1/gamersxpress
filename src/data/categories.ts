export const CATEGORIES = [
  'gaming-news',
  'pc',
  'playstation',
  'xbox',
  'nintendo',
  'hardware',
  'tech',
  'esports',
  'india',
] as const;

export type Category = (typeof CATEGORIES)[number];

export const CATEGORY_LABELS: Record<Category, string> = {
  'gaming-news': 'Gaming News',
  pc: 'PC',
  playstation: 'PlayStation',
  xbox: 'Xbox',
  nintendo: 'Nintendo',
  hardware: 'Hardware',
  tech: 'Tech',
  esports: 'Esports',
  india: 'India',
};

/** Tag color per category (white text on it passes 4.5:1 in both themes). */
export const CATEGORY_COLORS: Record<Category, string> = {
  'gaming-news': '#B45309',
  pc: '#6D28D9',
  playstation: '#0057B8',
  xbox: '#107C10',
  nintendo: '#C8102E',
  hardware: '#0E7490',
  tech: '#4338CA',
  esports: '#BE185D',
  india: '#C2410C',
};
