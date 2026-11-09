# Article images

An article can have a **hero image**: a photo or key art shown at the top of its page (with a
credit line under it), as a thumbnail on its card, and as the picture when the article is shared.
Without one, an article simply has no picture on the page; it still has the generated text cover
for sharing. Nothing about publishing depends on an image.

## The rules

- The picture is **our own copy** in `public/images/<slug>.webp`. It is never a link to someone
  else's server.
- Every hero has a **description** (for screen readers), a **credit** and a **license**.
- The license must be one of the kinds listed in `pipeline/config.yaml` under `images`:
  `Public domain`, `CC0`, `CC BY 4.0`, `CC BY-SA 4.0` (and the 3.0 and 2.0 versions), `Press kit`,
  `Steam store`, `Own work`, `Licensed`.
- **Never use a picture from another news outlet.** It is copyrighted, can bring a takedown notice
  and can get an AdSense application refused.
- The picture must be at least 800 px wide. It is resized to at most 1600 px and compressed to
  under 400 KB.

## Where to find pictures you may use

- **Publisher and manufacturer press pages** (Sony, Microsoft, Nintendo, Capcom, NVIDIA, AMD...).
  They publish images for coverage. Use the license `Press kit` and put the press page in
  `--source-url`.
- **Wikimedia Commons.** Each file page shows its license and the author to credit. Use the
  matching license name, e.g. `CC BY-SA 4.0`, and the author as the credit.
- **Your own photos or screenshots:** license `Own work`.

## Add an image by hand

From the project folder (PowerShell):

```powershell
python -m pipeline.images add `
  --article src\content\articles\<slug>.md `
  --file C:\path\to\photo.jpg `
  --alt "A person holding the new console in a living room" `
  --credit "Nintendo" `
  --license "Press kit" `
  --source-url https://example.com/press-page
```

It resizes and converts the picture, strips camera data, writes `public/images/<slug>.webp` and adds
the `hero:` block to the article. Then check and publish both files:

```powershell
npm run check
git add src/content/articles/<slug>.md public/images/<slug>.webp
git commit -m "Add a hero image to <slug>"
git push origin main
```

The command refuses a license that is not on the list, a picture under 800 px wide, a description
that is too short, and anything that is not a readable picture. To change an image, run it again.
To remove one, delete the `hero:` block from the article and the file from `public/images/`.

## Checks

- `npm run check` (and the site check in GitHub) validates the shape of every `hero`.
- The pipeline's publish gate `hero` also checks that the file exists, its size and width match, its
  license is allowed, and it is small enough. An article that fails is not published.

## Planned

1. Automatic Steam store images for PC game stories (license `Steam store`).
2. Automatic Wikimedia Commons images for hardware stories.
