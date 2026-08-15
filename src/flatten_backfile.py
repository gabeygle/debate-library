#!/usr/bin/env python3
"""
Flatten and dedupe a nested backfile tree into one folder ready for
CardMirror conversion.

Rules:
  1. Byte-identical duplicates -> keep one copy, discard the rest. No
     information is lost, so these are removed outright.
  2. Same filename, different content -> these are versions. Keep the copy
     with the MOST HIGHLIGHTING, since highlighting marks what was actually
     read aloud; an unhighlighted twin is the unused draft. Losers go to a
     review folder rather than being deleted.
  3. Different filename, different content -> everything survives.

Highlighting is counted straight from word/document.xml rather than through
python-docx: we only need a comparable number, and reading the raw XML is
roughly an order of magnitude faster across 8,500 files.

Usage: python3 flatten_backfile.py --src TREE --out FLAT [--review DIR] [--apply]
Without --apply it runs as a dry run and writes only the report.
"""

import argparse
import collections
import hashlib
import os
import re
import shutil
import sys
import time
import zipfile

WORD_EXTS = (".docx", ".docm", ".doc")
HL = re.compile(rb"<w:highlight[^>]*w:val=\"(?!none)")
SHD = re.compile(rb"<w:shd[^>]*w:fill=\"(?!auto|FFFFFF)")


def scan_file(path):
    """Return (md5, highlight_runs, words_est, readable)."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None, 0, 0, False
    h.update(data)

    hl = 0
    words = 0
    ok = True
    if path.lower().endswith((".docx", ".docm")):
        try:
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist()
                         if n.startswith("word/") and n.endswith(".xml")]
                for n in names:
                    if "document" not in n and "footnotes" not in n:
                        continue
                    xml = z.read(n)
                    hl += len(HL.findall(xml)) + len(SHD.findall(xml))
                    words += xml.count(b"</w:t>")
        except Exception:
            ok = False
    else:
        ok = False          # legacy .doc: cannot inspect, treat conservatively
    return h.hexdigest(), hl, words, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True,
                    help="nested backfile tree to flatten")
    ap.add_argument("--out", required=True, help="flat output folder")
    ap.add_argument("--review", default=None,
                    help="where superseded versions go (default: OUT/../superseded)")
    ap.add_argument("--report", default=None,
                    help="dedupe report path (default: OUT/_DEDUPE_REPORT.txt)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    out = os.path.abspath(args.out)
    review = os.path.abspath(args.review or os.path.join(out, os.pardir, "superseded"))
    args.report = args.report or os.path.join(out, "_DEDUPE_REPORT.txt")

    files = []
    for dp, dn, fn in os.walk(src):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            if f.lower().endswith(WORD_EXTS) and not f.startswith("~$"):
                files.append(os.path.join(dp, f))
    files.sort()
    print(f"found {len(files):,} Word documents")

    t0 = time.time()
    info = {}
    for i, p in enumerate(files, 1):
        info[p] = scan_file(p)
        if i % 2000 == 0:
            print(f"  scanned {i:,}/{len(files):,}  ({time.time()-t0:.0f}s)")
    print(f"scanned all in {time.time()-t0:.0f}s")

    # ---- pass 1: exact duplicates -------------------------------------
    by_hash = collections.defaultdict(list)
    for p, (h, hl, w, ok) in info.items():
        if h:
            by_hash[h].append(p)

    def rank(p):
        # Prefer curated "Completed files" over per-team tubs, then shallower
        # paths, then shorter names -- a stable, explainable preference.
        rel = os.path.relpath(p, src)
        return (0 if "Completed files" in rel else 1,
                rel.count(os.sep), len(rel), rel)

    exact_removed = []
    survivors = []
    for h, ps in by_hash.items():
        ordered = sorted(ps, key=rank)
        survivors.append(ordered[0])
        exact_removed.extend(ordered[1:])

    print(f"exact duplicate copies dropped: {len(exact_removed):,}")
    print(f"survivors after exact dedupe  : {len(survivors):,}")

    # ---- pass 2: same name, different content --------------------------
    by_name = collections.defaultdict(list)
    for p in survivors:
        by_name[os.path.basename(p).lower()].append(p)

    keep, to_review = [], []
    version_groups = 0
    for name, ps in by_name.items():
        if len(ps) == 1:
            keep.append(ps[0])
            continue
        version_groups += 1
        # most highlighting wins; ties fall back to more text, then rank
        ps_sorted = sorted(
            ps, key=lambda p: (-info[p][1], -info[p][2]) + rank(p)[:1]
        )
        keep.append(ps_sorted[0])
        to_review.extend(ps_sorted[1:])

    print(f"same-name version groups      : {version_groups:,}")
    print(f"kept (most highlighted)       : {version_groups:,}")
    print(f"moved to review               : {len(to_review):,}")
    print(f"FINAL flattened set           : {len(keep):,}")

    if not args.apply:
        print("\n(dry run -- pass --apply to move files)")

    # ---- write report --------------------------------------------------
    os.makedirs(out, exist_ok=True)
    lines = []
    W = lines.append
    W("HEALTHCARE BACKFILE FLATTEN + DEDUPE REPORT")
    W("=" * 78)
    W(f"source : {src}")
    W(f"scanned: {len(files):,} Word documents")
    W("")
    W(f"exact byte-identical copies removed : {len(exact_removed):,}")
    W(f"same-name version conflicts         : {version_groups:,}")
    W(f"  -> kept the most-highlighted copy : {version_groups:,}")
    W(f"  -> moved to review folder         : {len(to_review):,}")
    W(f"FINAL unique documents              : {len(keep):,}")
    W("")
    W("RULE: highlighting marks what was read aloud, so among identically")
    W("named versions the most-highlighted copy is treated as the live one.")
    W("Losing versions are MOVED, never deleted.")
    W("")
    W("=" * 78)
    W("VERSION CONFLICTS -- what was kept and why")
    W("=" * 78)
    shown = 0
    for name, ps in sorted(by_name.items()):
        if len(ps) == 1:
            continue
        shown += 1
        ps_sorted = sorted(ps, key=lambda p: (-info[p][1], -info[p][2]) + rank(p)[:1])
        W("")
        W(f"{os.path.basename(ps_sorted[0])}")
        for j, p in enumerate(ps_sorted):
            tag = "KEEP  " if j == 0 else "review"
            W(f"  {tag} hl={info[p][1]:>6,} textruns={info[p][2]:>7,}  "
              f"{os.path.relpath(p, src)}")
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nreport -> {args.report}")

    # ---- move ----------------------------------------------------------
    if args.apply:
        os.makedirs(review, exist_ok=True)
        manifest = [("ORIGINAL PATH", "NEW NAME", "DISPOSITION")]

        def unique_path(d, name):
            base, ext = os.path.splitext(name)
            cand = os.path.join(d, name)
            k = 2
            while os.path.exists(cand):
                cand = os.path.join(d, f"{base}_{k}{ext}")
                k += 1
            return cand

        for p in keep:
            dst = unique_path(out, os.path.basename(p))
            shutil.move(p, dst)
            manifest.append((os.path.relpath(p, src), os.path.basename(dst), "kept"))
        for p in to_review:
            dst = unique_path(review, os.path.basename(p))
            shutil.move(p, dst)
            manifest.append((os.path.relpath(p, src), os.path.basename(dst),
                             "version superseded"))
        for p in exact_removed:
            manifest.append((os.path.relpath(p, src), "", "exact duplicate removed"))
            try:
                os.remove(p)
            except OSError:
                pass
        with open(os.path.join(out, "_ORIGINAL_LOCATIONS.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("Backfile flatten -- original locations\n")
            fh.write("=" * 78 + "\n\n")
            for a, b, c in manifest[1:]:
                fh.write(f"{a}\n    -> {b or '(removed)'}   [{c}]\n")
        print(f"moved {len(keep):,} -> {out}")
        print(f"moved {len(to_review):,} -> {review}")
        print(f"deleted {len(exact_removed):,} exact duplicates")


if __name__ == "__main__":
    main()
