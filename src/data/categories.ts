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
