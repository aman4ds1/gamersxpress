import { getCollection } from 'astro:content';
import type { CollectionEntry } from 'astro:content';
import type { Category } from './categories';
import { entitySlug } from './entities';

export type Article = CollectionEntry<'articles'>;

/** Every published article, newest first, each one once. */
export async function sortedArticles(): Promise<Article[]> {
  const seen = new Set<string>();
  return (await getCollection('articles'))
    .sort((a, b) => b.data.pubDate.valueOf() - a.data.pubDate.valueOf())
    .filter((article) => !seen.has(article.id) && Boolean(seen.add(article.id)));
}

/** Minutes to read an article's body at 220 words a minute (at least 1). Counted from the Markdown at build time. */
export function readingMinutes(body: string | undefined): number {
  const words = (body ?? '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .split(/\s+/)
    .filter((word) => /[\p{L}\p{N}]/u.test(word)).length;
  return Math.max(1, Math.ceil(words / 220));
}

/** One date style everywhere: "Nov 20, 2026". */
export const formatDate = (date: Date) =>
  date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

export interface CardImage {
  src: string;
  width: number;
  height: number;
  /** A portrait or square picture is shown whole instead of cropped. */
  tall: boolean;
}

/** The fallback pictures in public/images/default-<category>.webp are all 1200x675. */
const DEFAULT_SIZE = { width: 1200, height: 675 };

/** The picture for a card: the article's own hero image, or the default picture for its category. */
export function cardImage(article: Article): CardImage {
  const hero = article.data.hero;
  if (hero) return { src: hero.src, width: hero.width, height: hero.height, tall: hero.width / hero.height < 1.2 };
  const category: Category = article.data.category;
  return { src: `/images/default-${category}.webp`, ...DEFAULT_SIZE, tall: false };
}

export interface TopicCount {
  entity: string;
  slug: string;
  count: number;
  /** The articles about this topic, newest first. */
  articles: Article[];
}

/** Topics (entities that have a topic page) ranked by how many articles cover them, then by name. */
export function topicCounts(articles: Article[]): TopicCount[] {
  // Grouped by slug, exactly like the topic pages (src/pages/topic/[entity].astro), so a count matches its page.
  const bySlug = new Map<string, TopicCount>();
  for (const article of articles) {
    const slugs = new Map(article.data.entities.map((entity) => [entitySlug(entity), entity]));
    for (const [slug, entity] of slugs) {
      if (!slug) continue;
      const entry = bySlug.get(slug);
      if (entry) {
        entry.count += 1;
        entry.articles.push(article);
      } else bySlug.set(slug, { entity, slug, count: 1, articles: [article] });
    }
  }
  return [...bySlug.values()].sort((a, b) => b.count - a.count || a.entity.localeCompare(b.entity));
}

/**
 * One picture per topic for a list of topics, each from a different article where possible: the newest article of
 * the topic that has its own picture and was not used by an earlier topic. `articles` of each topic are newest first.
 */
export function distinctThumbnails(topics: TopicCount[]): CardImage[] {
  const used = new Set<string>();
  return topics.map(({ articles }) => {
    const pick =
      articles.find((article) => article.data.hero && !used.has(article.id)) ??
      articles.find((article) => !used.has(article.id)) ??
      articles[0];
    used.add(pick.id);
    return cardImage(pick);
  });
}

/**
 * Up to `n` other articles related to `article`: 3 points per shared entity, 2 per shared tag, 1 for the same
 * category; ties go to the newer one. When fewer than `n` share anything, the newest articles fill the list.
 * `articles` must be sorted newest first.
 */
export function relatedArticles(article: Article, articles: Article[], n = 3): Article[] {
  const lower = (values: string[]) => new Set(values.map((value) => value.toLowerCase()));
  const entities = lower(article.data.entities);
  const tags = lower(article.data.tags);
  const others = articles.filter((other) => other.id !== article.id);
  const scored = others
    .map((other, index) => {
      let score = other.data.category === article.data.category ? 1 : 0;
      for (const entity of lower(other.data.entities)) if (entities.has(entity)) score += 3;
      for (const tag of lower(other.data.tags)) if (tags.has(tag)) score += 2;
      return { other, score, index };
    })
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map(({ other }) => other);
  const picked = scored.slice(0, n);
  for (const other of others) {
    if (picked.length >= n) break;
    if (!picked.includes(other)) picked.push(other);
  }
  return picked;
}
