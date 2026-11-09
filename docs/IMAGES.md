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

## Automatic Steam art (PC game stories)

When a story's headline names a game that has a Steam store page, the pipeline fetches that game's
key art from Valve's public store data and attaches it as the hero, credited to the developer with
the license `Steam store` and a link to the store page. It happens on its own, with no extra model
calls and no keys.

- **Strict matching.** The Steam app's name must appear in the headline as a whole phrase, it must
  be a game (not DLC, a demo, a soundtrack or a bundle), and it must not carry an adult-only
  sexual-content descriptor. A one-word game name must also be a proper name the headline stands on.
- **Best picture first.** The wide library art, then up to three screenshots, then the page
  backdrop; the first one that is at least 800 px wide is used.
- **Never in the way.** If there is no match, no usable picture or a network problem, the article is
  published without a picture. A dry run never contacts Steam.
- **Safe fetching.** Only https links on Steam's own hosts are fetched; redirects and files over
  8 MB are refused.
- **Not a formal license.** The art belongs to the developers and publishers. Gaming sites use it to
  illustrate coverage of the game, but if you would rather not, set `images.steam.enabled: false`
  in `pipeline/config.yaml`. An article's picture can also be replaced by hand (see above) or removed.

The picture is committed with the article (draft pull request or direct publish) and removed again
by the Unpublish workflow.

### Check that the Steam art works

**1. Try any headline by hand** (needs internet; run it from the project folder):

```powershell
python -m pipeline.steam check "Path of Exile 2 launches on December 11, 2026" --entity "Path of Exile 2"
```

It prints every step (what it searched for, which Steam app matched, whether it is a game, each
picture it tried) and ends with one of:

| Last line | Meaning |
|---|---|
| `RESULT: working` | A picture was found and passes the publish check. It is saved under a temporary folder (or `--out`). |
| `RESULT: no picture for this headline` | Steam has no matching game, or no usable picture. Read the lines above to see why. |
| `RESULT: Steam could not be reached` | A network problem on your machine. Try again. |

The exit code is 0, 1 and 2 for those three. Try a headline that should match (a game on Steam)
and one that should not (a hardware story): the second must say "no picture".

**2. Look at a real run.** In GitHub open Actions, then Pipeline, then a run that wrote an article,
and search the "Run the pipeline" log for `steam:`. Each article logs its searches and the outcome.
The article itself then has a `hero:` block in its front matter, and its commit or draft pull
request contains `public/images/<slug>.webp`.

**3. Look at the live page.** An article with a hero shows the picture under its headline with
"Image: <developer> · Steam store".

If a story about a Steam game has no picture, run the command from step 1 with that article's
headline and send me the output.

## Planned

Automatic Wikimedia Commons images for hardware stories.
