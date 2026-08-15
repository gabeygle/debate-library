#!/usr/bin/env python3
"""
Evaluate and tune the blended ranking against marked student queries.

The first evaluation was flawed: it displayed semantic-only results, so the
22% hit rate measured the weakest configuration rather than what the app does.
This harness replicates the real blend and sweeps its parameters.

Scoring per candidate card:
  lexical   - BM25-ish over tag / cite / body / filename, with field weights
  phrase    - bonus when the full query string appears verbatim
  title     - bonus when query terms appear in the document filename
  semantic  - cosine against corpus-trained LSA vectors

The key fix under test: when a query HAS strong literal matches, those should
dominate. Semantic should rescue queries with no lexical anchor, not outvote
exact matches. "50 state fiat" failed at 45 literal matches -- that is the bug.

Usage: python3 eval_ranking.py [--gt /tmp/gt.json] [--queries ../Student_Queries.cmir]
"""

import argparse
import collections
import gzip
import json
import math
import re
import sqlite3

import numpy as np

TOK = re.compile(r"[a-z][a-z'\-]{1,}")
STOP = {"the", "a", "an", "to", "for", "of", "i", "me", "my", "how", "do",
        "help", "find", "need", "please", "give", "some", "that", "thing",
        "and", "is", "it", "can", "you", "with", "on", "at", "in"}


def load_queries(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        d = json.load(fh)
    out = []

    def walk(n):
        if n.get("type") == "text":
            out.append(n.get("text", ""))
        for c in n.get("content") or []:
            walk(c)
        if n.get("type") in ("tag", "card_body", "paragraph", "block", "hat",
                             "pocket", "analytic", "analytic_unit"):
            out.append("\n")
    walk(d["doc"])
    return [l.strip() for l in "".join(out).split("\n") if len(l.strip()) > 2]


class Engine:
    def __init__(self, db_path, vec_path):
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        rows = self.db.execute(
            "SELECT c.card_id, c.tag, c.cite, c.body, c.is_evidence, f.stem"
            " FROM cards c JOIN files f ON f.file_id = c.file_id"
            " ORDER BY c.card_id").fetchall()
        self.ids = [r["card_id"] for r in rows]
        self.pos = {c: i for i, c in enumerate(self.ids)}
        self.tag = [(r["tag"] or "").lower() for r in rows]
        self.cite = [(r["cite"] or "").lower() for r in rows]
        self.body = [(r["body"] or "").lower() for r in rows]
        self.stem = [(r["stem"] or "") for r in rows]
        self.stemlc = [s.lower() for s in self.stem]
        self.ev = [r["is_evidence"] for r in rows]

        z = np.load(vec_path, allow_pickle=True)
        self.terms = list(z["terms"])
        self.ti = {t: i for i, t in enumerate(self.terms)}
        self.T = z["T"].astype(np.float32) * float(z["T_scale"])
        D = z["D"].astype(np.float32) * float(z["D_scale"])
        order = [self.pos[int(c)] for c in z["card_ids"]]
        self.D = np.zeros_like(D)
        self.D[order] = D

        # document frequency for idf weighting
        self.df = collections.Counter()
        for i in range(len(self.ids)):
            for t in set(TOK.findall(
                    self.tag[i] + " " + self.cite[i] + " " + self.body[i]
                    + " " + self.stemlc[i])):
                self.df[t] += 1
        self.N = len(self.ids)

    def lexical(self, toks, cfg):
        """idf-weighted field-boosted match count."""
        sc = collections.defaultdict(float)
        for t in toks:
            df = self.df.get(t, 0)
            if not df:
                continue
            idf = math.log(1 + (self.N - df + 0.5) / (df + 0.5))
            try:
                hits = [r[0] for r in self.db.execute(
                    "SELECT rowid FROM cards_fts WHERE cards_fts MATCH ?"
                    " LIMIT 6000", (f'"{t}"',))]
            except Exception:
                continue
            for cid in hits:
                i = self.pos.get(cid)
                if i is None:
                    continue
                w = 1.0
                if t in self.tag[i]:
                    w += cfg["w_tag"]
                if t in self.cite[i]:
                    w += cfg["w_cite"]
                if t in self.stemlc[i]:
                    w += cfg["w_title"]
                sc[i] += idf * w
        return sc

    def qvec(self, toks):
        idx = [self.ti[t] for t in toks if t in self.ti]
        if not idx:
            return None
        v = self.T[idx].mean(0)
        n = np.linalg.norm(v)
        return v / n if n else None

    def search(self, q, cfg, k=3):
        raw = TOK.findall(q.lower())
        toks = [t for t in raw if t not in STOP] or raw
        lex = self.lexical(toks, cfg)

        # Phrase and title signals, computed only over lexical candidates
        # (a phrase cannot appear where none of its words do).
        ql = " ".join(toks)
        for i in list(lex.keys()):
            if ql and ql in (self.tag[i] + " " + self.body[i]):
                lex[i] += cfg["w_phrase"]
            if ql and ql in self.stemlc[i]:
                lex[i] += cfg["w_title"] * 2
            if self.ev[i]:
                lex[i] += cfg["w_eviden"]

        maxlex = max(lex.values()) if lex else 0.0
        cand = {}
        for i, v in lex.items():
            cand[i] = cfg["a_lex"] * (v / maxlex) if maxlex else 0.0

        # Semantic contribution is scaled DOWN when lexical evidence is strong.
        # This is the fix: exact matches should not be outvoted by fuzzy ones.
        qv = self.qvec(toks)
        if qv is not None:
            sem = self.D @ qv
            top = np.argpartition(-sem, min(300, len(sem) - 1))[:300]
            top = top[np.argsort(-sem[top])]
            smax = sem[top[0]] if len(top) else 1.0
            strength = min(1.0, len(lex) / cfg["lex_sat"]) if lex else 0.0
            a_sem = cfg["a_sem"] * (1.0 - cfg["sem_damp"] * strength)
            for i in top:
                cand[i] = cand.get(i, 0.0) + a_sem * (sem[i] / (smax or 1))

        ranked = sorted(cand.items(), key=lambda kv: -kv[1])
        out, seen = [], set()
        for i, s in ranked:
            if self.stem[i] in seen:
                continue
            seen.add(self.stem[i])
            out.append((self.stem[i], self.tag[i], s))
            if len(out) >= k:
                break
        return out


CONFIGS = {
    "current (60/40, no damping)": dict(
        a_lex=.6, a_sem=.4, sem_damp=0.0, lex_sat=1, w_tag=10, w_cite=4,
        w_title=8, w_phrase=0, w_eviden=0),
    "semantic only (what you marked)": dict(
        a_lex=0, a_sem=1, sem_damp=0, lex_sat=1, w_tag=0, w_cite=0,
        w_title=0, w_phrase=0, w_eviden=0),
    "lexical-first + damping": dict(
        a_lex=1.0, a_sem=.45, sem_damp=.85, lex_sat=25, w_tag=3, w_cite=1,
        w_title=4, w_phrase=6, w_eviden=.3),
    "lexical-first + phrase + title": dict(
        a_lex=1.0, a_sem=.35, sem_damp=.9, lex_sat=15, w_tag=4, w_cite=1,
        w_title=6, w_phrase=10, w_eviden=.4),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="library.db")
    ap.add_argument("--vec", default="vectors.npz")
    ap.add_argument("--gt", default="/tmp/gt.json")
    ap.add_argument("--queries", default="../Student_Queries.cmir")
    ap.add_argument("--report", default="../QUERY_EVAL_ROUND2.txt")
    args = ap.parse_args()

    eng = Engine(args.db, args.vec)
    gt = json.load(open(args.gt))
    queries = load_queries(args.queries)
    print(f"{len(queries)} queries | {len(gt)} with known-relevant files\n")

    results = {}
    print(f"{'configuration':<34}{'hit@1':>8}{'hit@3':>8}{'MRR':>8}")
    print("=" * 58)
    for name, cfg in CONFIGS.items():
        h1 = h3 = 0
        rr = 0.0
        per = {}
        for q, rel in gt.items():
            top = eng.search(q, cfg, k=3)
            per[q] = top
            names = [t[0] for t in top]
            r = next((i for i, n in enumerate(names) if n in rel), None)
            if r is not None:
                h3 += 1
                rr += 1 / (r + 1)
                if r == 0:
                    h1 += 1
        n = len(gt)
        results[name] = (h1 / n, h3 / n, rr / n, per, cfg)
        print(f"{name:<34}{h1/n*100:>7.0f}%{h3/n*100:>7.0f}%{rr/n:>8.2f}")

    best = max(results.items(), key=lambda kv: (kv[1][2], kv[1][1]))
    print(f"\nbest: {best[0]}")

    # full report over ALL queries using the winning config
    cfg = best[1][4]
    lines = [f"QUERY EVALUATION ROUND 2 -- config: {best[0]}",
             "=" * 84,
             "Ranking now blends exact matching with meaning, and damps the",
             "semantic signal when strong literal matches exist.",
             "Mark GOOD/BAD again on any line that looks wrong.", ""]
    for q in queries:
        top = eng.search(q, cfg, k=3)
        rel = gt.get(q, [])
        lines.append(f"\n{'-'*84}\nQUERY: {q}")
        for stem, tag, s in top:
            flag = "  <-- previously confirmed GOOD" if stem in rel else ""
            lines.append(f"   [{s:.2f}] {(tag or '(untagged)')[:64]}")
            lines.append(f"          -> {stem[:58]}{flag}")
        if not top:
            lines.append("   (nothing found)")
    open(args.report, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"report -> {args.report}")


if __name__ == "__main__":
    main()
