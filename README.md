# Debate Library

A card-level search engine for competitive debate evidence files, built over
[CardMirror](https://github.com/ant981228/cardmirror) `.cmir` documents. Built
with Claude and inspired by [Logos](https://logos-debate.netlify.app/).

Debate evidence lives in enormous Word files — a single backfile can run to
900,000 words — organised into *cards*: a tagline, a citation, and a quoted
passage with the parts read aloud highlighted and the reasoning underlined.
Finding the right card usually means remembering which file it was in.

## Why did I build this?

1. I wanted local search without the internet.
2. I also needed to search debate material other than cards — lesson plans,
   drills, feedback documents.

**No corpus is included in this repository** — only the tooling. Point it at
your own `.cmir` files.

## What it does

- **Card-level search.** Results are individual cards with tag, citation and
  source, not "this 58 MB file contains your term somewhere".
- **Semantic search that runs offline.** Vectors are trained on your own
  corpus, so a query embeds by table lookup with no model and no network. This
  matters because students cannot use the internet during tournaments.
- **Typo tolerance.** `deluze` suggests `deleuze`, `arctik` suggests `arctic`.
- **Debate-aware query handling.** Understands that files say `T` where a
  student types `topicality`, and tells a student when their query is too
  vague to answer instead of guessing.
- **Single-file output.** The whole index compiles into one self-contained
  `index.html` that works on double-click. No install, no server.

## Quick start

```bash
pip install python-docx scikit-learn numpy

cd src
python3 build_index.py --cmir /path/to/cmir --docx /path/to/word   # ~105 s / 1,300 files
python3 fix_facets.py                                              # navigation facets
python3 build_vectors.py                                           # semantic vectors
python3 export_web.py                                              # -> index.html
```

Open the resulting `index.html`.

## The pipeline

```
.cmir files ──> build_index.py ──> library.db  (SQLite + FTS5, card level)
                                        │
                     fix_facets.py ─────┤   navigation metadata
                   build_vectors.py ────┤   vectors.npz  (LSA, int8)
                                        │
                    export_web.py ──────> index.html  (self-contained)
```

| Script | Purpose |
|---|---|
| `build_index.py` | Parse `.cmir` into a card-level SQLite + FTS5 index |
| `fix_facets.py` | Derive format, position, argument type, year, collection |
| `build_vectors.py` | Train offline semantic vectors on the corpus |
| `export_web.py` | Compile the database into one self-contained page |
| `query_intent.py` | Vagueness detection and abbreviation expansion |
| `eval_ranking.py` | Score ranking configurations against marked queries |
| `restyle_cards.py` | Recover cards from `.docx` files with broken tag styles |
| `flatten_backfile.py` | Flatten and dedupe a nested backfile tree |

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the pieces fit, and why
- [`docs/FINDINGS.md`](docs/FINDINGS.md) — what measurement revealed, including
  several cases where the obvious assumption was wrong
- [`docs/CMIR_FORMAT.md`](docs/CMIR_FORMAT.md) — the `.cmir` format and the four
  ways to parse it incorrectly

## Correctness

Counting debate documents is deceptively easy to get wrong, and wrong answers
look plausible. Verified against CardMirror's own parser
(`cardmirror-read.cjs`) across six random files:

- **Evidence card counts matched exactly, 6/6.**
- Word counts within 0.1–0.5%; highlighted-word counts within 0.4%.
- Cross-checked against independent `python-docx` counts: **0.0% difference.**

Three traps this codebase handles, each of which produced believable but wrong
numbers first:

| Trap | Symptom | Handling |
|---|---|---|
| Counting every `card` node | 32% overstatement | Require a `cite_paragraph` |
| Counting words per text node | Every total 1.86% high | Reassemble a character stream, tokenise once |
| Reading `.docx` styles instead of `.cmir` | Card counts off ~10% | Parse the document model |

## Requirements

Python 3.9+, `python-docx`, `scikit-learn`, `numpy`. Node 18+ only if you use
CardMirror's `cardmirror-read.cjs` to convert `.docx` to `.cmir`.

Deliberately avoided: no pretrained models, no embedding API, no server. The
output must work offline on a school laptop during a tournament.

## Status

Working: card-level index, exact/semantic/title search, typo correction,
vagueness detection, card recovery from broken files.

In progress: folding filename abbreviation matching into the blended ranking;
separate teacher and student builds.

See [`docs/FINDINGS.md`](docs/FINDINGS.md) for measured results and known gaps.

## License

MIT — see [`LICENSE`](LICENSE).
