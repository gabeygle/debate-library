#!/usr/bin/env python3
"""
Query understanding for the Debate Library: vagueness detection and
abbreviation matching.

Two failures showed up in blind testing that ranking cannot fix.

1. VOCABULARY MISMATCH. Debaters write "T", not "topicality"; "K", not
   "kritik". Measured: `T_Space Cooperation` holds 29 evidence cards and never
   once contains the word "topicality"; `2AC AT T_Fish` has 284 cards and
   mentions it twice. The word a student types is absent from the text they
   are searching. Expanding "topicality" -> "t" inside the body index is
   useless (a single letter matches noise), so the expansion is applied to
   FILENAMES, where the abbreviation actually appears as a token.

2. UNANSWERABLE QUERIES. "2ac case cards" and "give me a speech" name a
   format but no subject. There is no correct result, so returning the
   top-scoring card is worse than saying so -- it teaches students the tool is
   unreliable. These are detected and answered with a request for specifics.

The distinction is structural vocabulary vs content vocabulary. "2ac", "cards",
"speech", "blocks" describe the shape of a document. "fish", "arctic",
"deleuze", "federalism" describe what it argues about. A query made only of
structural words cannot be answered.
"""

import re

# Debate vocabulary breaks ordinary tokenisers: "2AC" starts with a digit,
# and "T" and "K" are single letters that carry real meaning. A pattern
# requiring two leading letters silently drops exactly the terms that matter.
TOK = re.compile(r"[a-z0-9][a-z0-9']*")

# Words describing document shape rather than subject matter.
STRUCTURAL = {
    "1ac", "2ac", "1nc", "2nc", "1ar", "2ar", "1nr", "2nr",
    "ac", "nc", "ar", "nr",          # what "2ac" leaves after the digit
    "1", "2", "3", "4",
    "aff", "neg", "affirmative", "negative",
    "card", "cards", "block", "blocks", "file", "files", "doc", "docs",
    "speech", "speeches", "case", "evidence", "eviden", "eve",
    "round", "drill", "drills", "summary", "overview", "frontline",
    "answer", "answers", "at", "a2", "help", "find", "need", "want",
    "give", "get", "show", "please", "some", "good", "best", "stuff",
    "thing", "things", "something", "anything", "me", "my", "i", "you",
    "the", "a", "an", "to", "for", "of", "and", "is", "it", "how", "do",
    "can", "with", "on", "in", "about", "that", "this", "there", "prep",
}

# Long form -> the abbreviations that actually appear in filenames.
# Matched against the filename, not the body text.
ABBREV = {
    "topicality":     ["t"],
    "kritik":         ["k"],
    "kritiks":        ["k"],
    "critique":       ["k"],
    "counterplan":    ["cp"],
    "counterplans":   ["cp"],
    "disadvantage":   ["da"],
    "disadvantages":  ["da"],
    "disad":          ["da"],
    "conditionality": ["condo"],
    "framework":      ["fw"],
    "permutation":    ["perm"],
    "theory":         ["theory"],
}

# Reverse, so "T" in a query also reaches files named "topicality".
EXPAND = {}
for long, shorts in ABBREV.items():
    for s in shorts:
        EXPAND.setdefault(s, set()).add(long)
        EXPAND.setdefault(long, set()).add(s)


def tokens(q):
    return TOK.findall(q.lower())


def content_terms(q):
    """Query terms that carry subject matter."""
    return [t for t in tokens(q) if t not in STRUCTURAL]


def assess(q, df=None, n_docs=0):
    """
    Classify a query.

    Returns (kind, message) where kind is one of:
      'ok'      -- has subject matter, run the search
      'vague'   -- only structural words, ask for specifics
      'empty'   -- nothing usable at all
    """
    toks = [t for t in tokens(q) if t]
    if not toks:
        return "empty", "Type something to search for."

    content = content_terms(q)

    # An abbreviation on its own IS subject matter ("at k", "states cp").
    abbrev_present = any(t in EXPAND for t in toks)

    if not content and not abbrev_present:
        return "vague", vague_message(q, toks)

    # Content words exist but every one is extremely common in the corpus,
    # so they cannot discriminate between documents.
    if df and content:
        rare = [t for t in content if df.get(t, 0) < n_docs * 0.25]
        if not rare:
            return "vague", vague_message(q, toks)

    return "ok", None


def vague_message(q, toks):
    fmt = [t for t in toks if t in STRUCTURAL and re.match(r"^[12][an][crm]$", t)]
    bits = []
    bits.append("That's too broad to search — it says what kind of document you "
                "want but not what it argues about.")
    if fmt:
        bits.append(f"Add the topic or argument: instead of \"{q}\", "
                    f"try \"{fmt[0]} answers to the states counterplan\" "
                    f"or \"{fmt[0]} fish arctic\".")
    else:
        bits.append(f"Add a topic or argument name — instead of \"{q}\", "
                    f"try \"nuclear war impacts\" or \"answers to the cap K\".")
    bits.append("Naming the argument, the author, or the topic works best.")
    return " ".join(bits)


def filename_terms(q):
    """
    Terms to match against document filenames, with abbreviations expanded.

    Returns [(term, weight)] -- original terms at full weight, expansions
    slightly lower so a literal filename match still wins.
    """
    out = []
    seen = set()
    for t in tokens(q):
        if t in STRUCTURAL and t not in EXPAND:
            continue
        if t not in seen:
            out.append((t, 1.0))
            seen.add(t)
        for e in EXPAND.get(t, ()):
            if e not in seen:
                out.append((e, 0.85))
                seen.add(e)
    return out


def filename_score(stem, terms):
    """
    Score a filename. Abbreviations are matched as whole tokens so that "t"
    hits "T_Be the Topic" and "2AC AT T_Fish" but not every word containing
    the letter t.
    """
    s = stem.lower()
    parts = set(re.split(r"[^a-z0-9]+", s))
    score = 0.0
    for t, w in terms:
        if len(t) <= 2:
            if t in parts:
                score += 6.0 * w          # short abbreviation, exact token
        elif t in s:
            score += 4.0 * w
    return score


if __name__ == "__main__":
    tests = [
        "2ac case cards", "give me a speech", "1nr single payer speech",
        "topicality", "at k", "states cp cards", "racism k",
        "stuff about fish in the arctic", "cards", "help me find evidence",
        "answer to k aff", "T--USFG speech",
    ]
    print(f"{'query':<34}{'verdict':<10}message / filename terms")
    print("=" * 96)
    for q in tests:
        kind, msg = assess(q)
        if kind == "ok":
            ft = ", ".join(f"{t}({w})" for t, w in filename_terms(q)[:5])
            print(f"{q:<34}{kind:<10}{ft}")
        else:
            print(f"{q:<34}{kind:<10}{(msg or '')[:52]}...")
