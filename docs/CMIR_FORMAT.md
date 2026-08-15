# Analysing debate files in CardMirror `.cmir` format

> Originally published at
> [burdenofproof.education](https://burdenofproof.education/audience-adaptation.html),
> where the two sample files and three working scripts referenced below are
> also available. Reproduced here because this repository's parsing code is
> built directly on it.

A handoff brief. Everything below was learned by taking apart two real policy debate files and
checking the results against a human hand count. Written 11 August 2026.

If you are an agent picking this up: **read the "Four ways to get this wrong" section before you
write any code.** Every one of those mistakes was made in this project by a different system, and
each produced numbers that looked entirely plausible.

---

## 1. What a `.cmir` file is

A `.cmir` is **gzip-compressed JSON**. No dependencies are needed to read one:

```python
import gzip, json
with gzip.open(path, 'rt', encoding='utf-8') as fh:
    data = json.load(fh)
```

Top level:

```json
{
  "format": "cardmirror-doc",
  "formatVersion": 1,
  "createdBy": "CardMirror",
  "createdAt": "2026-08-09T12:41:25.848Z",
  "doc": { "type": "doc", "content": [ ... ] }
}
```

`data["doc"]["content"]` is a **flat list** of top-level nodes. In both files examined, cards are
never nested inside heading nodes — the document is a sequence, not a tree of sections. Do not
assume nesting; verify it.

---

## 2. Why this format matters

A `.docx` stores **appearance**. It knows a paragraph is styled "Heading 4"; it does not know
whether that paragraph is a card tag, an analytic, or a heading in someone's notes. All three look
identical on paper, so they are formatted identically.

A `.cmir` stores the **document model**. A card is typed as a card, a citation as a citation. This
is the difference between reading a field and guessing from formatting, and it is the reason the
card count in this project only became correct after switching formats.

---

## 3. Node types

| Type | What it is |
|---|---|
| `doc` | root |
| `hat` | top-level heading (e.g. "1AC—AI Love") |
| `pocket` | groups a set of blocks (e.g. "1AC Script") |
| `block` | a section (e.g. "1AC—Canada", "2AC—NORAD !") |
| `card` | a card-shaped unit — **not necessarily evidence, see §5** |
| `tag` | the card's tagline, in the debater's own words |
| `cite_paragraph` | the citation line: author, date, qualifications, source |
| `card_body` | the quoted passage. A card can have several |
| `paragraph` | loose prose outside any card |
| `text` | the leaf. **All words live here** |
| `image` | embedded image; carries `data`, `contentType`, `widthEmu`, `heightEmu`, `alt` |

`hat` / `pocket` / `block` together give you the file's outline, which is how you isolate a
section such as the 1AC. Walking the flat top-level list and remembering the most recent heading
rebuilds the structure.

Node `attrs` seen: `id`, `indent`, `spacing`, `alignment`, plus the image fields.

---

## 4. Marks

Marks live on `text` nodes as a list of `{"type": ..., "attrs": {...}}`.

| Mark | Meaning | Where it appears |
|---|---|---|
| `highlight` | the words read aloud in the round | `card_body`, occasionally `cite_paragraph` |
| `underline_mark` | the warrant — reasoning inside quoted evidence | `card_body`, `cite_paragraph`, `paragraph` |
| `emphasis_mark` | visual emphasis inside evidence | `card_body` |
| `cite_mark` | formatting of the citation line | `cite_paragraph` only |
| `underline_direct` | **tagline emphasis, not warrant marking** — see §6 | `tag` only, in both files |
| `bold`, `italic`, `strikethrough`, `superscript` | typography | various |
| `font_size` (`halfPoints`), `font_family` (`name`) | typography | various |
| `link` (`href`) | hyperlink | citation lines mostly |
| `pilcrow_marker`, `bold_off` | artefacts of the Word conversion | rare |

**`highlight` carries a `color` attribute.** In the files examined, `cyan` is the real
highlighting (2,154 of 2,197 occurrences in one file), with `darkGray` and `black` appearing as a
small minority on ordinary-looking words. If a project needs precision about what is actually
read aloud, filter on colour rather than treating every highlight as equivalent — and check what
the minority colour is being used for in your files before deciding.

---

## 5. Telling evidence cards from analytics

**This is the single most important thing in this document.**

A `card` node is *not* necessarily a piece of evidence. CardMirror uses the same node type for:

- **evidence cards** — tag, citation, quoted passage from a source
- **analytics** — arguments the debater makes in their own voice, no source underneath
- **documentation headings** — in one file, the Notes section's questions were card-shaped

They share a type because they look alike on the page and are read aloud alike.

**The test:** a card is an evidence card if it contains a `cite_paragraph`.

```python
def is_evidence_card(node):
    return node.get('type') == 'card' and contains(node, 'cite_paragraph')
```

Measured on the two files:

| | card nodes | evidence cards | analytics |
|---|---|---|---|
| File A (2022) | 21 | 20 | 1 |
| File B (2025) | 93 | **56** | 37 |

A human hand count found 20 and 56. **Every automated attempt that skipped this test returned 93
for File B** — Claude's first `.cmir` pass, ChatGPT's script, and GLM's script all did. Report both
numbers so the gap is visible rather than hidden.

Note `cite_mark` counts do not equal `cite_paragraph` counts (75 marks across 56 citation
paragraphs in File B), so **do not use `cite_mark` as a proxy for counting cards.**

---

## 6. `underline_direct` is not underlining in the sense you want

It looks like it should count toward "underlined words". In both files it appears **exclusively
inside `tag` nodes** — 62 text nodes in File A, 75 in File B, none in a card body. The words
carrying it are the debater emphasising a verb in their own tagline: *stalled*, *poisons*,
*collapses*, *invites*.

That is a different act from underlining the warrant inside quoted evidence. Excluding it is
correct **for these files**. Including it would add 80 words (+0.6%) and 100 words (+1.3%).

**Verify this before relying on it.** A file where `underline_direct` appears inside `card_body`
means something else, and excluding it would undercount. Print the mark inventory with parent
node types first:

```python
import collections
ctx = collections.defaultdict(collections.Counter)
def scan(n, parent=None):
    if n.get('type') == 'text':
        for m in n.get('marks', []) or []:
            ctx[m.get('type')][parent] += 1
    for c in n.get('content', []) or []:
        scan(c, n.get('type') if n.get('type') != 'text' else parent)
```

---

## 7. Counting words correctly

**Text nodes split at every formatting boundary.** Highlight the "deter" in "deterrence" and the
file stores two adjacent text nodes, `"deter"` and `"rence"`. Counting `len(node["text"].split())`
per node reports two words where the document contains one.

This inflates every total by roughly **1.9%**. It is not a rounding difference; it is a wrong
answer that looks right.

**The fix:** flatten the document to a stream of `(character, marks_on_that_character)` pairs,
emit a space after every block-level node so the last word of one paragraph cannot fuse with the
first word of the next, then split on whitespace **once**.

```python
BREAK_TYPES = {'tag', 'card_body', 'cite_paragraph', 'block', 'hat', 'pocket', 'paragraph'}

def _flatten(node, out):
    if node.get('type') == 'text':
        marks = frozenset(m.get('type') for m in node.get('marks', []) or [])
        for ch in node.get('text', ''):
            out.append((ch, marks))
    for child in node.get('content', []) or []:
        _flatten(child, out)
    if node.get('type') in BREAK_TYPES:
        out.append((' ', frozenset()))

def count_words(node, mark=None):
    stream = []
    _flatten(node, stream)
    total, in_word, word_marks = 0, False, set()
    for ch, marks in stream:
        if ch.isspace():
            if in_word and (mark is None or mark in word_marks):
                total += 1
            in_word, word_marks = False, set()
        else:
            in_word = True
            word_marks |= marks
    if in_word and (mark is None or mark in word_marks):
        total += 1
    return total
```

A word crossing a boundary becomes one word carrying the **union** of the marks that touched it.
That is also the right semantics: a debater reading "deterrence" aloud reads the whole word, not
the highlighted half.

**Performance note.** The version above materialises the whole document as a list of tuples and
must be called once per mark. A better implementation accumulates every counter in a single pass
and holds only the current word's marks — roughly 8× faster and effectively zero memory. Prefer
that if you are processing many files.

---

## 8. How to know your numbers are right

Do **not** validate a script against itself, or against another run of the same method. Both fail
silently. Three checks that actually work:

1. **Hand count.** Have a person who reads these files count the cards in one document. This is
   the only ground truth available, and it is what caught the 93-vs-56 error.
2. **Cross-format.** Convert the same document to `.docx` and count it paragraph-by-paragraph with
   `python-docx`. A correct `.cmir` word count matches the `.docx` paragraph count **exactly** —
   62,629 and 66,331 for these two files, from two unrelated parsers over two formats.
3. **Sanity-check the shape.** Highlighted words should be a small fraction of the total, since
   only what is read aloud gets highlighted. If highlighting exceeds ~15% of a file, something is
   miscounted or the file is unusual.

---

## 9. Four ways to get this wrong

All four happened in this project, each to a different system.

| Mistake | Symptom | Fix |
|---|---|---|
| Reading the `.docx` and inferring structure from styles | Card count off by ~10% | Read the `.cmir` |
| Counting every `card` node | 93 instead of 56 | Require a `cite_paragraph` |
| Counting words per text node | Every total ~1.9% high | Reassemble, then tokenise once |
| Not decompressing the file | `UnicodeDecodeError` on byte `0x8b`, or silent zeros | `gzip.open` |

A fifth, subtler one: **declaring a match against numbers that were already wrong.** One system
reported "difference: 0" against a set of totals that had been superseded. Check what you are
comparing to, not just whether it matches.

---

## 10. Reference figures

Two files, for anyone wanting to check an implementation against known-good output.

| | File A — Cognitive Warfare (2022–23) | File B — Fisheries (2025–26) |
|---|---|---|
| Words | 62,629 | 66,331 |
| Highlighted words | 493 (0.8%) | 3,182 (4.8%) |
| Underlined words | 12,732 (20.3%) | 7,457 (11.2%) |
| Card nodes | 21 | 93 |
| Evidence cards | 20 | 56 |
| Median card body | 2,167.5 words | 733.5 words |
| Median tag | 14 words | 7 words |
| Cards in the 1AC | 19 | 30 |

Both files and three working scripts are published at
<https://burdenofproof.education/audience-adaptation.html>.

---

## 11. What is worth measuring, and why

Some context on what these numbers mean, in case the analysis is for a similar purpose.

- **Highlighting** marks what a student says out loud. As a share of the file it measures how much
  of the document is performance-ready versus raw material.
- **Underlining** marks the warrant — the reasoning that supports the claim. It measures analytical
  work done during research.
- The **ratio between them** is the interesting one. A file that is heavily underlined and barely
  highlighted is a record of the writer's reading, not an instrument for a student's speaking.
- **Card count and median card length** together describe how the material is chunked. Same total
  length split into three times as many cards is a different teaching artefact.
- **Cards in the 1AC** — isolated via the `hat`/`pocket`/`block` outline — is what a student
  actually stands up and reads, as opposed to what exists in the file.

Word counts alone say very little. The marking is where the pedagogy is.
