#!/usr/bin/env python3
"""
Build a card-level searchable index over a CardMirror (.cmir) corpus.

Implements the algorithm described in CMIR-ANALYSIS-BRIEF.md. In particular:
  - .cmir is gzip-compressed JSON            (brief S1, S9 trap 4)
  - a card is EVIDENCE only if it contains a cite_paragraph  (brief S5, S9 trap 2)
  - words are counted by reassembling a character stream, not per text node
    (brief S7, S9 trap 3) -- text nodes split at formatting boundaries
  - underline_direct is tagline emphasis, not warrant marking (brief S6),
    and is counted separately rather than folded into "underlined"
  - highlight colours are recorded, since cyan is the real highlighting (brief S4)

Output: library.db (SQLite + FTS5), consumed by index.html.

Usage:  python3 build_index.py [--cmir DIR] [--docx DIR] [--out library.db]
"""

import argparse
import collections
import gzip
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata

# ---------------------------------------------------------------- constants

# Block-level nodes. A word cannot run across one of these, so we emit a
# space after each to stop the last word of a paragraph fusing with the
# first word of the next. (brief S7)
BREAK_TYPES = {
    "tag", "card_body", "cite_paragraph", "block", "hat", "pocket", "paragraph",
    # Confirmed against the CardMirror source: analytics are their own node
    # types, not cards. Their text is real argument content and needs the same
    # word-boundary treatment as any other block-level node.
    "analytic", "analytic_unit",
}

OUTLINE_TYPES = ("hat", "pocket", "block")

# Marks we track per word. underline_direct is deliberately separate from
# underline_mark -- they mean different things. (brief S6)
TRACKED_MARKS = ("highlight", "underline_mark", "underline_direct", "emphasis_mark")

WORD_EXTS = (".docx", ".doc", ".docm")


# ---------------------------------------------------------------- text utils

def node_text(node):
    """Concatenate every text leaf under a node, depth-first, in order."""
    out = []
    stack = [node]
    # Explicit stack, but we need document order, so push children reversed.
    while stack:
        n = stack.pop()
        if n.get("type") == "text":
            out.append(n.get("text", ""))
        kids = n.get("content") or []
        for k in reversed(kids):
            stack.append(k)
    return "".join(out)


def clean(s):
    """Collapse whitespace and normalise unicode for storage and display."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


class WordCounter:
    """
    Single-pass word counter over a character stream.

    A word crossing a formatting boundary is one word carrying the UNION of
    the marks that touched it -- a debater reading "deterrence" aloud reads
    the whole word, not the highlighted half. (brief S7)

    Accumulates every counter in one pass rather than re-walking per mark,
    which the brief notes is ~8x faster.
    """

    __slots__ = ("total", "marks", "colors", "_in_word", "_cur", "_cur_colors")

    def __init__(self):
        self.total = 0
        self.marks = collections.Counter()
        self.colors = collections.Counter()
        self._in_word = False
        self._cur = set()
        self._cur_colors = set()

    def feed(self, text, marks, colors):
        for ch in text:
            if ch.isspace():
                self.flush()
            else:
                self._in_word = True
                if marks:
                    self._cur |= marks
                if colors:
                    self._cur_colors |= colors

    def brk(self):
        """Block boundary -- same effect as whitespace."""
        self.flush()

    def flush(self):
        if self._in_word:
            self.total += 1
            for m in self._cur:
                self.marks[m] += 1
            for c in self._cur_colors:
                self.colors[c] += 1
        self._in_word = False
        self._cur = set()
        self._cur_colors = set()


def walk_counting(node, wc):
    """Depth-first walk feeding the counter, respecting block boundaries."""
    t = node.get("type")
    if t == "text":
        marks = set()
        colors = set()
        for m in node.get("marks") or []:
            mt = m.get("type")
            if mt:
                marks.add(mt)
            # CardMirror has two visually identical layers: highlight and
            # shading (background colour). Per the manual, shading is how you
            # keep an opponent's highlighting safe from "standardize
            # highlighting". Both render as marked text, so both are recorded.
            if mt in ("highlight", "shading"):
                col = (m.get("attrs") or {}).get("color")
                if col:
                    colors.add(col)
        wc.feed(node.get("text", ""), marks, colors)
    for c in node.get("content") or []:
        walk_counting(c, wc)
    if t in BREAK_TYPES:
        wc.brk()


def count_words(node):
    wc = WordCounter()
    walk_counting(node, wc)
    wc.flush()
    return wc


# ---------------------------------------------------------------- card model

def find_first(node, wanted):
    """First descendant of a given type, document order, or None."""
    if node.get("type") == wanted:
        return node
    for c in node.get("content") or []:
        r = find_first(c, wanted)
        if r is not None:
            return r
    return None


def find_all(node, wanted, acc=None):
    if acc is None:
        acc = []
    if node.get("type") == wanted:
        acc.append(node)
    for c in node.get("content") or []:
        find_all(c, wanted, acc)
    return acc


def contains_type(node, wanted):
    """Cheap existence check without building a list."""
    if node.get("type") == wanted:
        return True
    for c in node.get("content") or []:
        if contains_type(c, wanted):
            return True
    return False


CITE_YEAR = re.compile(r"\b((?:19|20)\d{2}|['’]?\d{2})\b")
CITE_AUTHOR = re.compile(r"^\s*([A-Z][A-Za-z.\-']+(?:\s+(?:et\s+al\.?|and|&|[A-Z][A-Za-z.\-']+)){0,3})")


def parse_cite(cite_text):
    """Best-effort author + year from a citation line. ~67% parse cleanly."""
    if not cite_text:
        return None, None
    author = None
    m = CITE_AUTHOR.match(cite_text)
    if m:
        author = m.group(1).strip(" .,-")
    year = None
    m2 = CITE_YEAR.search(cite_text[:80])
    if m2:
        y = m2.group(1).lstrip("'’")
        if len(y) == 2:
            y = ("19" + y) if int(y) > 30 else ("20" + y)
        year = y
    return author, year


# ---------------------------------------------------------------- facets

POSITION_PAT = [
    (re.compile(r"\b(1AC)\b", re.I), "1AC"),
    (re.compile(r"\b(2AC)\b", re.I), "2AC"),
    (re.compile(r"\b(1NC)\b", re.I), "1NC"),
    (re.compile(r"\b(2NC)\b", re.I), "2NC"),
    (re.compile(r"\b(1AR)\b", re.I), "1AR"),
    (re.compile(r"\b(2AR)\b", re.I), "2AR"),
    (re.compile(r"\b(1NR)\b", re.I), "1NR"),
    (re.compile(r"\b(2NR)\b", re.I), "2NR"),
]

ARGTYPE_PAT = [
    (re.compile(r"^DA[_\- ]|\bDA\b", re.I), "Disadvantage"),
    (re.compile(r"^CP[_\- ]|\bCP\b", re.I), "Counterplan"),
    (re.compile(r"^K[_\- ]|\bkritik\b", re.I), "Kritik"),
    (re.compile(r"^T[_\- ]|\btopicality\b", re.I), "Topicality"),
    (re.compile(r"^AT[_\- ]|^A2[_\- ]", re.I), "Answers To"),
    (re.compile(r"^AFF", re.I), "Aff"),
    (re.compile(r"^(NEG|CaseNeg)", re.I), "Neg"),
    (re.compile(r"^Drill|\bdrill\b", re.I), "Drill"),
    (re.compile(r"^Impact", re.I), "Impacts"),
    (re.compile(r"\blesson\b|\blecture\b", re.I), "Teaching"),
    (re.compile(r"\bflow\b|\bflowing\b", re.I), "Flowing"),
]

FORMAT_PAT = [
    (re.compile(r"^PF[_\- ]|\bpublic forum\b", re.I), "Public Forum"),
    (re.compile(r"^SD[_\- ]|\bsmart debate\b", re.I), "Smart Debate"),
    (re.compile(r"\bLD\b|\blincoln[- ]douglas\b", re.I), "Lincoln-Douglas"),
]

YEAR_PAT = re.compile(r"\b(20\d{2})\b|\b(\d{4})-(\d{2})\b|['’](\d{2})[-–]['’](\d{2})")
PAREN_PAT = re.compile(r"\(([^)]{2,60})\)")


def facets_from_name(stem):
    f = {"position": None, "argtype": None, "format": "Policy", "year": None, "attribution": None}
    for pat, val in POSITION_PAT:
        if pat.search(stem):
            f["position"] = val
            break
    for pat, val in ARGTYPE_PAT:
        if pat.search(stem):
            f["argtype"] = val
            break
    for pat, val in FORMAT_PAT:
        if pat.search(stem):
            f["format"] = val
            break
    m = YEAR_PAT.search(stem)
    if m:
        if m.group(1):
            f["year"] = m.group(1)
        elif m.group(2):
            f["year"] = m.group(2)
        elif m.group(4):
            f["year"] = "20" + m.group(4)
    m = PAREN_PAT.search(stem)
    if m:
        f["attribution"] = clean(m.group(1))
    return f


# ---------------------------------------------------------------- schema

SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous  = OFF;

CREATE TABLE files (
    file_id       INTEGER PRIMARY KEY,
    stem          TEXT NOT NULL,
    cmir_path     TEXT NOT NULL,
    docx_path     TEXT,
    format        TEXT,
    position      TEXT,
    argtype       TEXT,
    year          TEXT,
    attribution   TEXT,
    words         INTEGER,
    highlighted   INTEGER,
    underlined    INTEGER,
    tagline_emph  INTEGER,
    shaded        INTEGER,
    card_nodes    INTEGER,
    analytics_n   INTEGER,
    evidence      INTEGER,
    analytics     INTEGER,
    outline       TEXT
);

CREATE TABLE cards (
    card_id     INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(file_id),
    ordinal     INTEGER,
    section     TEXT,
    is_evidence INTEGER,
    is_analytic INTEGER DEFAULT 0,
    tag         TEXT,
    cite        TEXT,
    author      TEXT,
    year        TEXT,
    body        TEXT,
    words       INTEGER,
    highlighted INTEGER,
    underlined  INTEGER
);

-- Full text over the card, which is the unit people actually want back.
-- Body is indexed too: rare terms such as philosopher names appear only in
-- card bodies, never in tags or citations. (measured, not assumed)
CREATE VIRTUAL TABLE cards_fts USING fts5(
    tag, cite, body, stem,
    content='',
    tokenize='unicode61 remove_diacritics 2'
);

-- Trigram index for typo-tolerant / substring lookup, so "deluze" still
-- reaches "Deleuze". FTS5 trigram needs SQLite >= 3.34.
CREATE VIRTUAL TABLE cards_trg USING fts5(
    blob,
    content='',
    tokenize='trigram'
);

CREATE INDEX idx_cards_file ON cards(file_id);
CREATE INDEX idx_cards_ev   ON cards(is_evidence);
CREATE INDEX idx_files_year ON files(year);
CREATE INDEX idx_files_arg  ON files(argtype);
"""


# ---------------------------------------------------------------- extraction

def process_file(path, file_id, docx_lookup):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    doc = data.get("doc") or {}

    stem = os.path.splitext(os.path.basename(path))[0]
    f = facets_from_name(stem)

    whole = count_words(doc)

    cards = []
    section_stack = {"hat": None, "pocket": None, "block": None}
    ordinal = 0

    def current_section():
        parts = [section_stack[k] for k in OUTLINE_TYPES if section_stack[k]]
        return " > ".join(parts) if parts else None

    outline_seen = []

    def walk(node):
        nonlocal ordinal
        t = node.get("type")

        if t in OUTLINE_TYPES:
            label = clean(node_text(node))[:120]
            if label:
                section_stack[t] = label
                # reset finer levels when a coarser one changes
                if t == "hat":
                    section_stack["pocket"] = None
                    section_stack["block"] = None
                elif t == "pocket":
                    section_stack["block"] = None
                outline_seen.append((t, label))

        if t == "card":
            ordinal += 1
            cite_node = find_first(node, "cite_paragraph")
            is_ev = cite_node is not None          # THE test (brief S5)

            tag_node = find_first(node, "tag")
            tag = clean(node_text(tag_node)) if tag_node is not None else ""
            cite = clean(node_text(cite_node)) if cite_node is not None else ""

            bodies = find_all(node, "card_body")
            body = clean(" ".join(node_text(b) for b in bodies))

            cwc = count_words(node)
            author, year = parse_cite(cite)

            cards.append({
                "ordinal": ordinal,
                "section": current_section(),
                "is_evidence": 1 if is_ev else 0,
                "is_analytic": 0,
                "tag": tag,
                "cite": cite,
                "author": author,
                "year": year,
                "body": body,
                "words": cwc.total,
                "highlighted": cwc.marks.get("highlight", 0)
                               + cwc.marks.get("shading", 0),
                "underlined": cwc.marks.get("underline_mark", 0),
            })
            # Cards are not nested inside other cards; skip re-walking.
            return

        # Analytics are a distinct node type in CardMirror, not cards. They
        # carry real argument text ("5. Intelligence deficit. Failure to
        # intel-share...") that is otherwise unreachable by card-level search.
        # analytic_unit wraps an analytic (and sometimes a card_body); index
        # the wrapper where present so the text is not counted twice.
        if t in ("analytic_unit", "analytic"):
            ordinal += 1
            body = clean(node_text(node))
            if body:
                awc = count_words(node)
                cards.append({
                    "ordinal": ordinal,
                    "section": current_section(),
                    "is_evidence": 0,
                    "is_analytic": 1,
                    "tag": body[:160],
                    "cite": "",
                    "author": None,
                    "year": None,
                    "body": body,
                    "words": awc.total,
                    "highlighted": awc.marks.get("highlight", 0)
                                   + awc.marks.get("shading", 0),
                    "underlined": awc.marks.get("underline_mark", 0),
                })
            return

        for c in node.get("content") or []:
            walk(c)

    walk(doc)

    outline_txt = " | ".join(f"{t}:{l}" for t, l in outline_seen[:60])
    ev = sum(c["is_evidence"] for c in cards)
    an = sum(c["is_analytic"] for c in cards)

    frow = (
        file_id, stem, path, docx_lookup.get(stem),
        f["format"], f["position"], f["argtype"], f["year"], f["attribution"],
        whole.total,
        whole.marks.get("highlight", 0) + whole.marks.get("shading", 0),
        whole.marks.get("underline_mark", 0),
        whole.marks.get("underline_direct", 0),
        whole.marks.get("shading", 0),
        len(cards), an, ev, len(cards) - ev - an,
        outline_txt,
    )
    return frow, cards, whole


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmir", default="../CardMirror")
    ap.add_argument("--docx", default="..")
    ap.add_argument("--out", default="library.db")
    args = ap.parse_args()

    cmir_dir = os.path.abspath(args.cmir)
    docx_dir = os.path.abspath(args.docx)
    out = os.path.abspath(args.out)

    # .cmir files now live in sorted subfolders (Has Evidence/, Analytics/,
    # No Cards/, Needs Attention/), so walk the tree rather than one flat dir.
    files = []
    for dp, _dn, fn in os.walk(cmir_dir):
        for f in fn:
            if f.lower().endswith(".cmir"):
                files.append(os.path.join(dp, f))
    files.sort()
    if not files:
        sys.exit(f"no .cmir files found in {cmir_dir}")

    docx_lookup = {}
    for f in os.listdir(docx_dir):
        s, e = os.path.splitext(f)
        if e.lower() in WORD_EXTS:
            docx_lookup[s] = f

    if os.path.exists(out):
        os.remove(out)
    db = sqlite3.connect(out)
    db.executescript(SCHEMA)

    t0 = time.time()
    n_cards = 0
    agg = collections.Counter()
    colors = collections.Counter()
    mark_ctx = collections.Counter()

    for i, path in enumerate(files, 1):
        try:
            frow, cards, whole = process_file(path, i, docx_lookup)
        except Exception as exc:                      # keep going, report later
            print(f"  !! {os.path.basename(path)}: {exc}", file=sys.stderr)
            continue

        db.execute(
            "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", frow
        )
        for c in cards:
            cur = db.execute(
                "INSERT INTO cards (file_id, ordinal, section, is_evidence, is_analytic,"
                " tag, cite, author, year, body, words, highlighted, underlined)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (i, c["ordinal"], c["section"], c["is_evidence"], c["is_analytic"], c["tag"],
                 c["cite"], c["author"], c["year"], c["body"], c["words"],
                 c["highlighted"], c["underlined"]),
            )
            rid = cur.lastrowid
            db.execute(
                "INSERT INTO cards_fts (rowid, tag, cite, body, stem)"
                " VALUES (?,?,?,?,?)",
                (rid, c["tag"], c["cite"], c["body"], frow[1]),
            )
            db.execute(
                "INSERT INTO cards_trg (rowid, blob) VALUES (?,?)",
                (rid, f"{c['tag']} {c['cite']} {frow[1]}"),
            )
        n_cards += len(cards)

        agg["words"] += whole.total
        agg["hl"] += whole.marks.get("highlight", 0)
        agg["ul"] += whole.marks.get("underline_mark", 0)
        agg["ud"] += whole.marks.get("underline_direct", 0)
        agg["cards"] += frow[14]
        agg["ev"] += frow[16]
        colors.update(whole.colors)

        if i % 200 == 0:
            print(f"  {i}/{len(files)} files, {n_cards:,} cards, {time.time()-t0:.0f}s")

    db.commit()
    db.execute("INSERT INTO cards_fts(cards_fts) VALUES('optimize')")
    db.execute("INSERT INTO cards_trg(cards_trg) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()

    el = time.time() - t0
    size = os.path.getsize(out)
    print(f"\nindexed {len(files):,} files / {n_cards:,} cards in {el:.1f}s")
    print(f"db: {out}  ({size/1e6:.0f} MB)")
    print(f"words {agg['words']:,} | highlighted {agg['hl']:,} "
          f"({agg['hl']/max(agg['words'],1)*100:.1f}%) | underlined {agg['ul']:,} "
          f"({agg['ul']/max(agg['words'],1)*100:.1f}%)")
    print(f"card nodes {agg['cards']:,} -> evidence {agg['ev']:,}, "
          f"analytics {agg['cards']-agg['ev']:,}")
    print(f"highlight colours: {dict(colors.most_common(6))}")


if __name__ == "__main__":
    main()
