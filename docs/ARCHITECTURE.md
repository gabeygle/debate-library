# Architecture

How the pieces fit together, and why each choice was made rather than an
obvious alternative.

## Constraints that shaped everything

1. **Must work offline.** Students cannot use the internet during tournaments.
   This rules out embedding APIs, model downloads at runtime, and any server.
2. **Must not require installation.** The people using it are high school
   students on school laptops. The deliverable is one file they double-click.
3. **Must not fabricate evidence.** A student may read whatever they are shown
   aloud in a round. A hallucinated card is worse than no result.

Constraint 3 is why no language model generates result summaries. Retrieval is
deterministic and auditable; generation is not.

## Why card level, not file level

A debate file can exceed 800,000 words. Knowing that "Deleuze" appears
*somewhere* in it is not an answer. The `.cmir` format stores the document
model — a card is typed as a card, a citation as a citation — so cards can be
extracted as first-class records.

Measured on one corpus: 1,296 files became 33,686 searchable cards.

## Why the files are flat

An evidence file is simultaneously a 2AC, a Fish file, from 2025-26, and a
drill. Folders force one of those to win. Facets do not.

The corpus is stored flat and the organisation lives in metadata, which is why
`fix_facets.py` exists. A pre-flatten manifest preserves the original folder
paths so that hierarchy is recoverable as facet data rather than lost.

## Why SQLite + FTS5

- One portable file, no server
- Sub-10 ms queries over ~19M words
- Directly queryable by SQL, so a language model can use it later with no
  embeddings and no new infrastructure
- FTS5 trigram tokeniser gives substring matching for free

The database is the source of truth. `index.html` is a compiled artifact.

## Why the data is embedded in the HTML

Browsers block `fetch()` of local files under `file://`. A separate data file
would not load without a server, which breaks constraint 2. So the entire index
is gzipped, base64'd, and embedded in a `<script>` tag, decompressed at load
with `DecompressionStream`.

Costs about 25 MB for a 33,686-card corpus and loads in ~440 ms. Card bodies
are truncated to a preview; the full text stays in the database and the source
document.

## Why LSA rather than a transformer

Hugging Face was unreachable from the build environment, and constraint 1
forbids fetching a model at runtime anyway. So the vectors are trained on the
corpus itself: TF-IDF into TruncatedSVD, 64 dimensions, quantised to int8.

This turned out to be a genuine advantage rather than a compromise. The model
learns *this* vocabulary:

```
deterrence -> compellence, credible, payne   (Keith Payne, deterrence theorist)
fisheries  -> finfish, harvest, npfmc        (North Pacific Fishery Mgmt Council)
automation -> robots, artificial
```

A generic model knows neither Payne nor NPFMC.

The decisive property: a query is embedded by looking terms up in a shipped
table and averaging. **No model runs at query time.** That is what keeps the
output a single double-clickable file.

Cost: 37,184 term vectors + 33,686 card vectors ≈ 3.7 MB.

Limitation: LSA matches distributional co-occurrence, not meaning. Abstract or
metaphorical queries drift. An LLM would fix those, and cannot be used here.

`gensim`/fastText was tried first and abandoned — gensim 4.4 has a numpy 2.x
incompatibility that kills training.

## Ranking

Three signals combine:

- **Lexical** — idf-weighted matches, boosted for tag, citation and filename
- **Semantic** — cosine against LSA vectors, over all cards
- **Filename** — abbreviation-aware matching against document titles

Brute-force cosine over 33,686 × 64 int8 vectors takes 4–8 ms, so no
approximate-nearest-neighbour index is needed at this scale.

The semantic weight is **damped when strong literal matches exist**. Semantic
search should rescue queries with no lexical anchor, not outvote exact matches.
A query with 45 literal matches returning none of them is a bug, and was one.

## Query understanding

Two problems that ranking cannot solve, handled in `query_intent.py`:

**Vocabulary mismatch.** Debate files say `T`, students type `topicality`.
Measured: a file with 29 evidence cards about topicality contains the word
zero times. Expanding to `t` inside the body index is useless — a single letter
matches noise — so expansion is applied to *filenames*, where the abbreviation
appears as a real token.

**Unanswerable queries.** `2ac case cards` names a format but no subject. There
is no correct result. Returning the top-scoring card anyway teaches students
the tool is unreliable, so these are detected and answered with a request for
specifics.

The mechanism is a split between *structural* vocabulary (2ac, cards, speech,
blocks) and *content* vocabulary (fish, arctic, federalism). A query made only
of structural terms cannot be answered.

### A tokenising trap

Standard tokenisers break debate text. `[a-z][a-z]+` drops `T` and `K` — single
letters carrying real meaning — and turns `2AC` into `ac`. The two most
important tokens in the vocabulary become invisible. Use `[a-z0-9][a-z0-9']*`.

## Recovering broken files

Some `.docx` files lose their tag styling and convert to zero cards despite
containing intact evidence. `restyle_cards.py` detects the tag → cite → body
rhythm and reapplies styles.

Two details matter:

- The tag maps to **Heading 4**, but the citation has **no paragraph style at
  all** — CardMirror detects it from the `cite_mark` character style
  (`Style13ptBold`). Restyling only tags produces cards with zero citations,
  which then misfile as analytics.
- Detection requires the **full triple** before emitting a card. This
  under-recovers deliberately. Missing a card is recoverable; inventing one is
  not.

## What is deliberately absent

- **No language model in the result path.** See constraint 3.
- **No approximate nearest neighbours.** Brute force is fast enough here and has
  no index to rebuild or tune.
- **No server.** See constraint 2.
- **No embeddings service.** See constraint 1.
