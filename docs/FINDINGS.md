# Findings

Measured results from building this against a real corpus, including the cases
where the obvious assumption turned out to be wrong. Recorded because each one
produced numbers that looked entirely plausible.

## Corpus

| | High school | College backfile |
|---|---|---|
| Files | 1,296 | 4,430 |
| Words | 19,248,222 | 78,921,116 |
| Card nodes | 33,686 | 131,463 |
| Evidence cards | 22,972 | 79,884 |
| Highlighted | 7.0% | 9.7% |
| Underlined | 21.0% | 27.2% |

## Verified against CardMirror's own parser

CardMirror ships `cardmirror-read.cjs`, which renders `.cmir` through the
editor's real parsers. Across six random evidence files:

- **Evidence card counts matched exactly on all six** — 6/6, 10/10, 15/15,
  26/26, 13/13, 12/12
- Word counts within 0.1–0.5%
- Highlighted word counts within 0.4%

Independently, `.cmir` word counts were checked against `python-docx` counts of
the same documents: **0.0% difference** on all four files sampled. Two
unrelated parsers, two formats, identical numbers.

### A failed check that looked like a real failure

Underlining appeared to diverge badly between the two methods. It was the
*check* that was broken: the rendered text contains an odd number of `__`
markers (799 in one file), so they cannot be paired by regex. Counting from
JSON is reliable; counting from rendered markup is not.

Worth stating plainly — a verification method can be wrong in ways that look
like a defect in the thing being verified.

## Where the corpus contradicted its own documentation

The format brief this was built from described two behaviours that did not hold:

1. **`underline_direct` is not confined to `tag` nodes.** It also appears in
   `block` (254) and `hat` (6) — section headings. Not in `card_body`, which was
   the actual concern, so excluding it from "underlined" remains correct. But
   "exclusively inside tag nodes" is false here.

2. **Highlight colours are not one dominant colour plus noise.** Measured:
   cyan 784k (58%), yellow 338k (25%), green 202k (15%), darkGray 14k. The
   reference files were 98% cyan. Yellow and green are far too heavy to be
   incidental — CardMirror's "Standardize Highlighting (except Yellow)" defaults
   to protecting yellow, which likely explains it.

## What reading the CardMirror source corrected

1. **`analytic` and `analytic_unit` are their own node types**, not cards. Their
   text — real arguments like *"5. Intelligence deficit. Failure to intel-share
   guarantees vulnerability duplication"* — was in word counts but **not
   searchable at all**. 71 units recovered.

2. **`shading` is a second highlight layer that looks identical on screen.** It
   is how you protect an opponent's highlighting from "standardize". 21,144
   shaded words were uncounted.

3. **`analytic_mark` exists but is unused** in this corpus, so there is no
   native flag distinguishing analytics from evidence. The underlining
   heuristic remains the best available signal.

## Files that lie about their contents

**A "no cards" file is not necessarily card-free.** Three files reported zero
cards while containing intact evidence:

| File | Words | Underlined | Cards |
|---|---|---|---|
| A (kritik backfile) | 197,722 | 51,355 | 0 |
| B (theory backfile) | 102,147 | 25,674 | 0 |
| C (impacts file) | 90,238 | 18,438 | 0 |

Roughly 390,000 words of marked evidence, invisible to card-level search,
because tagline paragraphs lost their Heading 4 style. Two were recovered:

- B: 0 → **55 cards, 54 evidence**
- C: 0 → **27 cards, 27 evidence**

Three recovered cards were spot-checked verbatim against the source: 3/3 real.

The signal to look for is a large word count with heavy underlining and zero
cards. Underlining marks the warrant inside quoted evidence, so a file with
tens of thousands of underlined words and no cards has not lost its evidence —
it has lost its labels.

**"Empty" files may hold images.** Seven files reported zero words. All seven
contain one or two embedded images — two are 2.1 MB photographs of attendance
sheets. Deleting them as junk would have destroyed records.

**Underlining distinguishes evidence from analytics** when citations are
untagged. Eight files had cards but no detected citations, yet carried 3,300+
underlined words each — real quoted evidence whose citations were never
structurally marked. Filing them as "analytics" would have buried 153,000 words.

## Search evaluation

Blind comparison of three ranking configurations across 33 real student
queries, order randomised per query:

| Configuration | Wins |
|---|---|
| Semantic only | 7 |
| Lexical-first | 5 |
| Lexical-first + abbreviations | 2 |
| **None useful** | **19** |

58% failed regardless of ranking — which redirected the work from tuning
weights to finding the actual cause.

### The cause was vocabulary, not ranking

Students type `topicality`. The files say `T`.

| File | Evidence cards | Contains "topicality" |
|---|---|---|
| T_Space Cooperation | 29 | **0 times** |
| 2AC AT T_Fish | 116 | 2 times |
| T_Be the Topic | 63 | 5 times |

The word being searched for is not in the text being searched. No weighting
scheme repairs that.

### A methodological trap worth recording

The first evaluation displayed **semantic-only results** while the app used a
blend. Marks collected on that output became an answer key that only semantic
ranking could score well against — so "semantic wins at 92%" was circular. The
fix was a blind comparison with randomised ordering.

Related: the tokeniser was silently dropping `K` and `T` and turning `2ac` into
`ac`. The most meaningful tokens in the vocabulary were invisible to every
configuration tested, which likely accounts for part of the 58%.

## Cost of analysis

For a corpus this size, deterministic parsing beats a language model on every
axis:

| Approach | Cost | Time |
|---|---|---|
| Script | $0 | ~20 s |
| Text into a model | 31.4M tokens | minutes |
| Raw JSON into a model | 149M tokens | — |

Nearly half the raw JSON is base64 image data carrying no analytical signal.
Everything worth counting here is a deterministic field read — exactly what a
script does correctly and a model approximates.

## Known gaps

- Filename abbreviation matching is not yet folded into the blended ranking, so
  `racism k` and `at liberal ontology K` currently return identical results —
  the `k` token swamps the content words.
- LSA drifts on abstract queries (`china spying on us`).
- Six source files were found to be **100% null bytes** — a failed sync or disk
  copy, unrecoverable, predating this tooling.
- Roughly 197,000 words in one kritik backfile remain uncarded.
