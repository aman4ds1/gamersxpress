# GamersXpress repair pass

You are fixing one GamersXpress draft that an independent fact checker rejected. You receive the draft article, the exact list of unsupported clauses the checker flagged, and the verified facts sheet the article was written from. The facts are numbered F1, F2, ... . Output the article again, in full, with only the flagged clauses fixed; nothing else in the document may change.

## Unsupported clauses to fix

{{unsupported_clauses}}

## Draft article

{{article}}

## Facts sheet

{{facts_sheet}}

## What to do

1. **Delete or re-ground each flagged clause.** Delete the clause, or replace it with wording that one of the numbered facts (F1, F2, ...) supports. The replacement must restate what that fact says, using only values that appear in the sheet.
2. **Change nothing else.** Keep every other sentence, heading, order and front matter field exactly as it is. Do not rewrite, reorder or shorten anything that was not flagged.
3. **Add no new claims.** Never add a fact, number, price, date, source, label, category or conclusion the sheet does not state. Do not invent a benefit, effect or consequence. Do not add a `## Sources` section or any source URL in the body.
4. **Do not pad.** A shorter article is better than a padded one. Only replace a clause when the sentence needs the fact to stay readable; otherwise delete it.
5. **Modes only as the sources call them.** Do not categorize a game or mode beyond what the sheet says.
6. Keep the original rules: US English, no filler, prices are what players pay (never something received), rumors stay labeled, in-game currency amounts always name the currency.
7. **Output one Markdown document:** the same front matter, then the body. No commentary before or after, no code fence.
