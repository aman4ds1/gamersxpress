# Self-hosted fonts

These font files are served from this site's own domain, so a visit never contacts Google or any other
font service. `src/styles/fonts.css` declares them.

| Family | Weights | Package | License |
|---|---|---|---|
| Chakra Petch | 500, 600, 700 | `@fontsource/chakra-petch` 5.3.0 | SIL Open Font License 1.1 (`licenses/chakra-petch-OFL.txt`) |
| Inter | 400, 500, 600, 700 | `@fontsource/inter` 5.3.0 | SIL Open Font License 1.1 (`licenses/inter-OFL.txt`) |
| JetBrains Mono | 500, 700 | `@fontsource/jetbrains-mono` 5.3.0 | SIL Open Font License 1.1 (`licenses/jetbrains-mono-OFL.txt`) |
| Press Start 2P | 400 | `@fontsource/press-start-2p` 5.3.0 | SIL Open Font License 1.1 (`licenses/press-start-2p-OFL.txt`) |

Only the Latin and Latin Extended subsets are included (English and accented Latin text). A character
outside them falls back to the next font in the page's font stack.

To change a font: install the matching `@fontsource/<name>` package in a scratch folder, copy its
`latin` and `latin-ext` `woff2` files for the weights you need into this folder, and update
`src/styles/fonts.css` (copy the `@font-face` blocks from the package's `<weight>.css`, change the `url()`
to `/fonts/<file>`). `checks/fonts.test.mjs` checks that every declared file exists.
