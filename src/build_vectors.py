#!/usr/bin/env python3
"""
Build offline semantic vectors for the Debate Library.

Why this approach: Hugging Face is unreachable from the build environment, so
no pretrained embedding model can be downloaded, and students cannot use the
internet during tournaments -- so nothing may be fetched at runtime either.
That rules out transformer embeddings entirely.

Instead: LSA (TF-IDF -> TruncatedSVD) trained on the corpus itself. Two
properties make this work well here:

  * It learns THIS corpus. "deterrence" lands near "compellence" and "payne"
    (Keith Payne, the deterrence theorist); "fisheries" near "finfish" and
    "npfmc". A generic model knows neither.
  * A query is embedded by looking up its terms in a shipped term-vector table
    and averaging. No model runtime, no download -- which is what keeps the
    app a double-clickable file.

gensim/fastText was tried first and abandoned: gensim 4.4 has a numpy 2.x
incompatibility that kills training.

Outputs vectors.npz consumed by export_web.py.

Usage: python3 build_vectors.py [--db library.db] [--dims 64] [--out vectors.npz]
"""

import argparse
import re
import sqlite3
import time

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize

# Only the opening of each card body feeds the vector model. The semantic gist
# of a card lives in its tag, citation and first paragraph; including all 5,000
# words of a long card adds runtime and dilutes the topic signal.
BODY_CHARS = 1200


def quantize(M):
    """float32 matrix -> int8 + per-matrix scale. Cuts payload 4x.

    Vectors are L2-normalised so every component is within [-1, 1]; a single
    global scale is therefore safe and avoids shipping per-row scales.
    """
    scale = float(np.abs(M).max())
    if scale == 0:
        scale = 1.0
    Q = np.clip(np.round(M / scale * 127), -127, 127).astype(np.int8)
    return Q, scale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="library.db")
    ap.add_argument("--dims", type=int, default=64)
    ap.add_argument("--max-terms", type=int, default=40000)
    ap.add_argument("--out", default="vectors.npz")
    args = ap.parse_args()

    t0 = time.time()
    db = sqlite3.connect(args.db)

    ids, docs = [], []
    for cid, tag, cite, body, stem in db.execute(
        "SELECT c.card_id, c.tag, c.cite, substr(c.body,1,?), f.stem"
        " FROM cards c JOIN files f ON f.file_id = c.file_id"
        " ORDER BY c.card_id", (BODY_CHARS,)
    ):
        ids.append(cid)
        # The filename is included deliberately: it is often the most
        # topically-loaded text attached to a card ("1AC_Fish_Final").
        docs.append(" ".join(x for x in (tag, cite, body, stem) if x).lower())
    print(f"cards loaded : {len(docs):,}  ({time.time()-t0:.0f}s)")

    vec = TfidfVectorizer(
        min_df=4, max_df=0.4, sublinear_tf=True,
        token_pattern=r"[a-z][a-z'\-]{1,}",
        max_features=args.max_terms, dtype=np.float32,
    )
    X = vec.fit_transform(docs)
    print(f"tfidf matrix : {X.shape}  ({time.time()-t0:.0f}s)")

    svd = TruncatedSVD(n_components=args.dims, random_state=0, n_iter=4)
    D = normalize(svd.fit_transform(X)).astype(np.float32)
    print(f"svd          : {args.dims} dims, "
          f"explained variance {svd.explained_variance_ratio_.sum():.3f}")

    # Term vectors: a term's position in concept space, weighted by how
    # informative it is. Averaging these for a query reproduces the document
    # projection closely enough for ranking, without shipping the TF-IDF matrix.
    T = normalize(svd.components_.T * vec.idf_[:, None]).astype(np.float32)

    inv = {i: t for t, i in vec.vocabulary_.items()}
    terms = [inv[i] for i in range(len(inv))]

    QD, sD = quantize(D)
    QT, sT = quantize(T)
    np.savez_compressed(
        args.out,
        card_ids=np.array(ids, dtype=np.int32),
        D=QD, D_scale=sD, T=QT, T_scale=sT,
        terms=np.array(terms, dtype=object),
    )

    import os
    print(f"\nterms  : {T.shape[0]:,} x {T.shape[1]}")
    print(f"cards  : {D.shape[0]:,} x {D.shape[1]}")
    print(f"wrote  : {args.out}  ({os.path.getsize(args.out)/1e6:.1f} MB)")
    print(f"total  : {time.time()-t0:.0f}s")

    # ---- smoke test -----------------------------------------------------
    ti = {t: i for i, t in enumerate(terms)}

    def qv(q):
        w = [ti[x] for x in re.findall(r"[a-z][a-z'\-]{1,}", q.lower()) if x in ti]
        if not w:
            return None
        v = T[w].mean(0)
        n = np.linalg.norm(v)
        return v / n if n else None

    rows = {r[0]: r[1:] for r in db.execute(
        "SELECT c.card_id, c.tag, f.stem FROM cards c"
        " JOIN files f ON f.file_id = c.file_id")}
    print("\nsmoke test -- sloppy queries:")
    for q in ("robots taking peoples jobs", "why condo is bad",
              "stuff about fish in the arctic"):
        v = qv(q)
        if v is None:
            print(f"  {q!r}: no known terms")
            continue
        s = D @ v
        i = int(np.argmax(s))
        tag, stem = rows[ids[i]]
        print(f"  {q!r}\n     [{s[i]:.2f}] {(tag or '')[:62]}  <- {stem[:40]}")


if __name__ == "__main__":
    main()
