#!/usr/bin/env python3
"""
Export library.db into a single self-contained index.html.

The whole search index is gzipped, base64'd and embedded in the page, so the
file works when opened directly from disk. That matters: browsers block
fetch() of local files under file://, so a separate data file would not load
without running a server.

Sizing (measured on this corpus):
  33,615 cards, 130k distinct body terms, ~4.9M postings
  -> ~11 MB raw JSON -> ~5 MB gzipped -> ~7 MB base64 in the page.

Body text is truncated to a preview. Storing all 19M words would balloon the
page; the full card is one click away in the source .docx, and library.db
still holds everything for scripted or LLM queries.

Usage: python3 export_web.py [--db library.db] [--template template.html]
                             [--out index.html] [--preview 320]
"""

import argparse
import base64
import collections
import gzip
import json
import os
import re
import sqlite3
import time

TOK = re.compile(r"[a-z][a-z'\-]{2,}")

# Terms in more than this share of cards carry no discriminating power and
# cost the most space. Terms appearing once are usually OCR noise, but we keep
# them: rare proper nouns (philosopher surnames) are exactly the hard queries.
MAX_DF_RATIO = 0.35


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="library.db")
    ap.add_argument("--template", default="template.html")
    ap.add_argument("--out", default="index.html")
    ap.add_argument("--preview", type=int, default=320)
    args = ap.parse_args()

    t0 = time.time()
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row

    # ---- files ----
    files, findex = [], {}
    for r in db.execute("SELECT file_id, stem, docx_path, format, position,"
                        " argtype, year, collection, topic FROM files ORDER BY file_id"):
        findex[r["file_id"]] = len(files)
        files.append([r["stem"], r["docx_path"], r["format"],
                      r["position"], r["argtype"], r["year"],
                      r["collection"], r["topic"]])
    print(f"files    : {len(files):,}")

    # ---- cards + inverted index ----
    cards = []
    card_ids_ordered = []
    inv = collections.defaultdict(list)
    n_ev = 0
    for r in db.execute("SELECT card_id, file_id, section, is_evidence, tag,"
                        " cite, body FROM cards ORDER BY card_id"):
        idx = len(cards)
        card_ids_ordered.append(r["card_id"])
        body = r["body"] or ""
        tag = r["tag"] or ""
        cite = r["cite"] or ""
        if r["is_evidence"]:
            n_ev += 1
        cards.append([
            tag,
            cite,
            findex[r["file_id"]],
            r["section"] or "",
            1 if r["is_evidence"] else 0,
            0,
            body[:args.preview],
        ])
        # index tag + cite + body so rare terms in bodies stay findable
        for term in set(TOK.findall((tag + " " + cite + " " + body).lower())):
            inv[term].append(idx)

    print(f"cards    : {len(cards):,}  ({n_ev:,} evidence)")
    print(f"raw terms: {len(inv):,}")

    # ---- prune + delta-encode postings ----
    cutoff = int(len(cards) * MAX_DF_RATIO)
    terms = {}
    kept_postings = 0
    for term, ids in inv.items():
        if len(ids) > cutoff:
            continue
        ids.sort()
        deltas, prev = [], 0
        for i in ids:
            deltas.append(i - prev)
            prev = i
        terms[term] = deltas
        kept_postings += len(deltas)
    print(f"kept     : {len(terms):,} terms / {kept_postings:,} postings"
          f"  (dropped {len(inv)-len(terms)} terms above df {cutoff:,})")

    # ---- facet vocabularies ----
    def col(name):
        vals = [r[0] for r in db.execute(
            f"SELECT {name}, COUNT(*) c FROM files WHERE {name} IS NOT NULL"
            f" AND {name}!='' GROUP BY {name} ORDER BY c DESC") ]
        return vals

    years = sorted([y for y in col("year") if y and y.isdigit()], reverse=True)[:8]
    payload = {
        "cards": cards,
        "files": files,
        "terms": terms,
        "meta": {
            "evidence": n_ev,
            "formats":     col("format")[:5],
            "argtypes":    col("argtype")[:10],
            "positions":   col("position")[:8],
            "collections": col("collection")[:12],
            "years":       years,
        },
    }

    # ---- semantic vectors (optional) -----------------------------------
    # Shipped as base64 int8. Query embedding is a lookup+average over the
    # term table, so the page needs no model and no network.
    vpath = os.path.join(os.path.dirname(os.path.abspath(args.db)), "vectors.npz")
    if os.path.exists(vpath):
        import numpy as np
        z = np.load(vpath, allow_pickle=True)
        cid_to_idx = {int(c): i for i, c in enumerate(z["card_ids"])}
        # Re-order card vectors to match the payload's card ordering exactly.
        D = z["D"]
        order = np.array([cid_to_idx.get(int(c), -1) for c in card_ids_ordered])
        missing = int((order < 0).sum())
        Dord = np.zeros((len(order), D.shape[1]), dtype=np.int8)
        ok = order >= 0
        Dord[ok] = D[order[ok]]
        payload["vec"] = {
            "dims": int(D.shape[1]),
            "terms": list(z["terms"]),
            "T": base64.b64encode(z["T"].tobytes()).decode("ascii"),
            "D": base64.b64encode(Dord.tobytes()).decode("ascii"),
            "tScale": float(z["T_scale"]),
            "dScale": float(z["D_scale"]),
        }
        print(f"vectors  : {D.shape[1]}d, {len(z['terms']):,} terms,"
              f" {len(order):,} cards ({missing} unmatched)")
    else:
        print("vectors  : none found (semantic search disabled)")

    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    gz = gzip.compress(raw, 9)
    b64 = base64.b64encode(gz).decode("ascii")
    print(f"json     : {len(raw)/1e6:.1f} MB -> gzip {len(gz)/1e6:.1f} MB"
          f" -> base64 {len(b64)/1e6:.1f} MB")

    tpl = open(args.template, encoding="utf-8").read()
    if "__DATA__" not in tpl:
        raise SystemExit("template is missing the __DATA__ placeholder")
    html = tpl.replace("__DATA__", b64)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    size = os.path.getsize(args.out)
    print(f"\nwrote {args.out}  ({size/1e6:.1f} MB)  in {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
