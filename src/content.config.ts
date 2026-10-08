import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import z from 'astro/zod';
import { CATEGORIES } from './data/categories';

const articles = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/articles' }),
  schema: z
    .object({
      title: z.string().max(65),
      description: z.string().min(70).max(160),
      pubDate: z.coerce.date(),
      updatedDate: z.coerce.date().optional(),
      category: z.enum(CATEGORIES),
      tags: z.array(z.string()),
      entities: z.array(z.string()),
      sources: z.array(z.object({ name: z.string(), url: z.url() })).min(2),
      image: z.string().optional(),
      imageAlt: z.string().optional(),
    })
    .refine((data) => !data.image || Boolean(data.imageAlt), {
      message: 'imageAlt is required when image is set',
      path: ['imageAlt'],
    }),
});

export const collections = { articles };
