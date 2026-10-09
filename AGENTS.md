# GamersXpress project rules

- Stack: Astro (static output), TypeScript, Tailwind. No database, no server code, no SSR.
- Site URL: https://gamersxpress.com. Keep `site` in astro.config set to it.
- Articles are Markdown in src/content/articles/ and must match the Zod schema.
- Never commit secrets. API keys come from environment variables only; keep .env gitignored.
- Do not add ad scripts, analytics cookies, or tracking. Ad slots stay behind ADS_ENABLED=false until the owner turns them on.
- After every change: run `npm run build` and `npm run check`, and fix errors before finishing.
- Hosting is Cloudflare Workers static assets (not Pages). Deploy with `npm run deploy` (builds the site, then `wrangler deploy` of `dist/`). The publish workflow must build and deploy itself using the `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` GitHub secrets.
- Keep changes small: one task per session, no unrelated refactors.
- Do not add dependencies without saying why. Prefer built-in Astro features.
- SEO is generated in code (layout, JSON-LD, sitemap). Never let a model write HTML meta tags directly.
- Tools under /tools run entirely in the browser. No server calls.
- Never state rumors as fact in article-generation prompts; sources are always cited.
- For any Astro config, content collection, routing, or integration question, look it up in the Astro docs via the MCP before writing code. Don't rely on memory for Astro APIs.
- Read docs/PLAN.md before pipeline work and follow it. If a task conflicts with it, say so instead of improvising.