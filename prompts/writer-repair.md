# GamersXpress repair pass

You are fixing one GamersXpress draft that an independent fact checker rejected. You receive the draft article, the exact list of unsupported clauses the checker flagged together with the reason each was flagged, and the verified facts the article was written from, numbered F1, F2, ... The facts are **reference material**: use them to check your wording, and never let them into the article. Not one of their words may reach your output as a heading, a numbered line (F1, F2, ...), a key or a block of JSON.

Output the article again, in full, with only the flagged sentences changed; every other sentence, heading, list item and front matter field must stay exactly as it is.

## Flagged clauses to fix

{{unsupported_clauses}}

## Draft article

{{article}}

## Reference material — must never appear in the output

{{facts_sheet}}

## What to do

1. **Delete or re-ground each flagged clause.** Delete the flagged sentence, or replace it with wording that one of the numbered facts (F1, F2, ...) supports. The replacement must restate what that fact says, using only values that appear in the reference material. Never delete part of a sentence and keep the rest: a sentence you do not delete stays a whole sentence.
2. **Change nothing else.** Keep every other sentence, heading, list item, order and front matter field exactly as it is. Do not rewrite, reorder or shorten anything that was not flagged.
3. **Keep every attribution.** When a flagged sentence names its speaker, source or outlet ("X states that", "X says", "according to X", "X claims", "X announced"), a replacement sentence must still attribute the statement to that same source. If you cannot keep the attribution, delete the sentence instead.
4. **Add no new claims.** Never add a fact, number, price, date, source, label, category, conclusion, benefit or effect the reference material does not state. Do not add a `## Sources` section or any source URL in the body.
5. **Do not pad.** A shorter article is better than a padded one. Only replace a clause when the sentence needs the fact to stay readable; otherwise delete it.
6. **Keep the reference material out.** The facts list above is for your checking only: no `## Facts sheet` heading, no F1/F2 lines, no JSON keys and no JSON block may appear anywhere in the article.
7. **Modes only as the sources call them.** Do not categorize a game or mode beyond what the reference material says.
8. Keep the original rules: US English, no filler, prices are what players pay (never something received), rumors stay labeled, in-game currency amounts always name the currency.
9. **Output one Markdown document:** the same front matter, then the complete body. No commentary before or after, no code fence.
