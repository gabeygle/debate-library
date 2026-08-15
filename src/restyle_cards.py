#!/usr/bin/env python3
"""
Recover card structure in .docx files whose taglines lost their Word styles.

The problem: CardMirror (like Verbatim) identifies a card by paragraph style.
Heading 4 marks the tag; a cite style marks the citation. Two files in this
corpus kept their section headings but lost the tag styling, so every card
collapsed into unstyled body text and the converter found zero cards:

  Baudrillard.docx        102,147 words, 25,674 underlined, 1 Heading 4
  Impacts_Dedev_Ian.docx   90,238 words, 18,438 underlined, nonstandard styles
                                          ("card", "evidencetext", "cardtext")

The evidence and the warrant marking are intact in both. Only the labels are
missing.

DETECTION -- deliberately conservative. A card is emitted only on the full
three-part rhythm:

    short bold paragraph        -> tag
    short paragraph w/ Author+Year -> cite
    long paragraph w/ underline or highlight -> body

Requiring all three means genuine cards are missed rather than fabricated.
That trade is intentional: a fabricated card is evidence a student might read
aloud in a round believing it is real. Under-recovery is recoverable; invented
evidence is not.

Usage:
    python3 restyle_cards.py --file /path/to/Broken.docx --dry-run
    python3 restyle_cards.py --file /path/to/Broken.docx --apply
"""

import argparse
import os
import re
import shutil

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: F401  (import validates docx)

# "Coulter 11 (Gerry, founding editor...", "Baudrillard 1993 (Transparency...",
# "Jason Dana et al. 23. Ph.D., ..." -- an author token followed by a year.
CITE_RE = re.compile(
    r"^[^a-z]{0,4}[A-Z][A-Za-z.\-']+(?:\s+(?:et\s+al\.?|and|&|[A-Z][A-Za-z.\-']+)){0,3}"
    r"[\s,]*(?:'|’)?\d{2,4}\b"
)
URL_RE = re.compile(r"https?://|www\.")

MAX_TAG_WORDS = 60
MAX_CITE_WORDS = 90
MIN_BODY_WORDS = 40


def features(p):
    t = p.text.strip()
    words = len(t.split())
    runs = p.runs
    bold = any(r.bold for r in runs if r.bold is not None)
    ul = sum(1 for r in runs if r.underline)
    hl = sum(1 for r in runs if r.font.highlight_color is not None)
    return dict(text=t, words=words, bold=bold, ul=ul, hl=hl,
                style=p.style.name, para=p)


def classify(f):
    """Return 'tag' | 'cite' | 'body' | None."""
    t, w = f["text"], f["words"]
    if not t or URL_RE.search(t):
        return None
    if f["style"].startswith("Heading 1") or f["style"].startswith("Heading 2") \
       or f["style"].startswith("Heading 3"):
        return None                      # already a section heading, leave alone
    if w >= MIN_BODY_WORDS and (f["ul"] > 0 or f["hl"] > 0):
        return "body"
    if w <= MAX_CITE_WORDS and CITE_RE.match(t):
        return "cite"
    if w <= MAX_TAG_WORDS and f["bold"]:
        return "tag"
    return None


def find_cards(feats):
    """Scan for tag -> cite -> body, allowing filler between the parts."""
    kinds = [classify(f) for f in feats]
    cards = []
    i = 0
    n = len(feats)
    while i < n:
        if kinds[i] != "tag":
            i += 1
            continue
        j = i + 1
        # a cite must follow within a couple of paragraphs
        cite = None
        while j < min(i + 3, n):
            if kinds[j] == "cite":
                cite = j
                break
            if kinds[j] == "tag":
                break
            j += 1
        if cite is None:
            i += 1
            continue
        # a marked body must follow the cite
        k = cite + 1
        body = None
        while k < min(cite + 4, n):
            if kinds[k] == "body":
                body = k
                break
            if kinds[k] in ("tag", "cite"):
                break
            k += 1
        if body is None:
            i = cite + 1
            continue
        cards.append((i, cite, body))
        i = body + 1
    return cards, kinds


def _ensure_cite_char_style(doc):
    """Return the Style13ptBold character style, creating it if absent."""
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml.ns import qn
    for st in doc.styles:
        if st.style_id == "Style13ptBold":
            return st
    st = doc.styles.add_style("Style13ptBold", WD_STYLE_TYPE.CHARACTER)
    st.element.set(qn("w:styleId"), "Style13ptBold")
    st.font.bold = True
    st.font.size = None
    return st


def ensure_style(doc, name, base="Normal"):
    try:
        return doc.styles[name]
    except KeyError:
        return doc.styles[base]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", type=int, default=6)
    args = ap.parse_args()

    src = os.path.abspath(args.file)
    doc = docx.Document(src)
    feats = [features(p) for p in doc.paragraphs]
    cards, kinds = find_cards(feats)

    print(f"{os.path.basename(src)}")
    print(f"  paragraphs      : {len(feats):,}")
    print(f"  classified tag  : {kinds.count('tag'):,}")
    print(f"  classified cite : {kinds.count('cite'):,}")
    print(f"  classified body : {kinds.count('body'):,}")
    print(f"  COMPLETE CARDS  : {len(cards):,}   <- tag+cite+body in sequence")

    if cards:
        print(f"\n  first {min(args.show, len(cards))} detected cards:")
        for (ti, ci, bi) in cards[:args.show]:
            print(f"    TAG  {feats[ti]['text'][:66]}")
            print(f"    CITE {feats[ci]['text'][:66]}")
            print(f"    BODY {feats[bi]['words']:,}w  ul={feats[bi]['ul']} "
                  f"hl={feats[bi]['hl']}  {feats[bi]['text'][:44]}...")
            print()

    if not args.apply or args.dry_run:
        print("  (dry run -- pass --apply to write a restyled copy)")
        return

    h4 = ensure_style(doc, "Heading 4")

    # Citations are NOT identified by a paragraph style -- CardMirror maps
    # cite_paragraph to null pStyle and detects the citation from the
    # cite_mark CHARACTER style, whose styleId is Style13ptBold
    # (src/ooxml/styles.ts: MARK_TO_RSTYLE.cite_mark). Verified against 484
    # real cite_paragraphs in the corpus, 550 of whose runs carry cite_mark.
    cite_style = _ensure_cite_char_style(doc)

    for (ti, ci, bi) in cards:
        feats[ti]["para"].style = h4
        for r in feats[ci]["para"].runs:
            try:
                r.style = cite_style
            except Exception:
                pass
        # Body paragraphs are left untouched: rewriting them risks disturbing
        # the underline/highlight runs that carry the actual marking.

    out = args.out or os.path.join(
        os.path.dirname(src), "restyled_" + os.path.basename(src))
    doc.save(out)
    print(f"\n  wrote {out}")
    print(f"  {len(cards):,} tag paragraphs restyled to Heading 4")
    print("  next: convert this file in CardMirror (or via cardmirror-read)"
          " and confirm the card count matches")


if __name__ == "__main__":
    main()
