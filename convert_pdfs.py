#!/usr/bin/env python3
"""
Convert witness-statement PDFs into an AI-readable format
(Markdown by default) so Claude can extract allegations and denials.

Why Markdown (the recommended/default format):
  * Lowest token overhead of the candidates (no JSON key/escaping bloat).
  * Preserves the numbered-paragraph structure that allegations/denials live in,
    so Claude can cite "paragraph 14" precisely.
  * YAML frontmatter carries metadata (witness, statement no, date, exhibits,
    doc id) so every extracted allegation can be attributed to a source.
  * Page markers (HTML comments) let Claude cite page numbers without polluting
    the prose.
  * Chunks cleanly if a statement is longer than the context window.

JSON output is also supported (--format json) for programmatic pipelines, and
plain text (--format txt) for the simplest possible ingestion.

Usage:
    python convert_pdfs.py                          # md, default in/out dirs
    python convert_pdfs.py --format json
    python convert_pdfs.py --input "<dir>" --output out --workers 8
    python convert_pdfs.py --no-clean               # keep raw extracted text

Requires: pdfplumber (already installed).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict

import pdfplumber

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_INPUT = r"dataset\witness_statements"
DEFAULT_OUTPUT = "converted"

# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

# Glyphs the embedded fonts fail to map, with best-effort replacements.
_CHAR_FIXES = {
    "�": "'",   # replacement char -> apostrophe / quote (most common case)
    "‘": "'", "’": "'",   # curly single quotes
    "“": '"', "”": '"',   # curly double quotes
    "–": "-", "—": "-",   # en/em dash
    " ": " ",                   # nbsp
}

# Lines that are page furniture, not content.
_DOCUSIGN_RE = re.compile(r"^\s*DocuSign Envelope ID:.*$", re.IGNORECASE)
_PAGE_FOOTER_RE = re.compile(r"^\s*Page\s+\d+\s+of\s+\d+\s*$", re.IGNORECASE)
# Standalone "I" mis-rendered as a pipe, e.g. "|, Alan" / "| make" / "| am".
_PIPE_I_RE = re.compile(r"(^|[\s(])\|(?=[\s,.;:)])")
# A line that begins a new logical paragraph (numbered item, lettered sub-item,
# roman sub-item, or a Question heading) -> do not merge into the previous line.
_PARA_START_RE = re.compile(
    r"^\s*("
    r"\d+\.|"                       # 1.  2.  12.
    r"\(?[a-z]\)|[a-z]\.|"          # a.  (a)  b)
    r"\(?(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\)|"  # roman sub-items
    r"Question\b|Answer\b"
    r")",
    re.IGNORECASE,
)


def fix_chars(text: str) -> str:
    for bad, good in _CHAR_FIXES.items():
        text = text.replace(bad, good)
    text = _PIPE_I_RE.sub(r"\1I", text)
    return text


def make_header_detector(pages_lines: list[list[str]]) -> set[str]:
    """Find short lines that repeat at the top of most pages (the doc-id stamps,
    e.g. 'WITN03380100' printed twice per page). These are header furniture."""
    from collections import Counter
    top_counts: Counter[str] = Counter()
    for lines in pages_lines:
        for ln in lines[:4]:               # only the top of each page
            s = ln.strip()
            if 0 < len(s) <= 30:
                top_counts[s] += 1
    threshold = max(2, int(0.5 * len(pages_lines)))
    return {s for s, c in top_counts.items() if c >= threshold}


def clean_page(raw: str, header_lines: set[str], do_clean: bool) -> list[str]:
    """Return a cleaned list of content lines for one page."""
    lines = raw.split("\n")
    if not do_clean:
        return [ln.rstrip() for ln in lines]
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            out.append("")
            continue
        if s in header_lines:
            continue
        if _PAGE_FOOTER_RE.match(s) or _DOCUSIGN_RE.match(s):
            continue
        out.append(fix_chars(ln.rstrip()))
    return out


def reflow(lines: list[str]) -> list[str]:
    """Join lines that were wrapped mid-paragraph into single paragraphs, while
    keeping numbered/lettered items and headings as their own paragraphs."""
    paras: list[str] = []
    buf = ""
    for ln in lines:
        s = ln.strip()
        if not s:
            if buf:
                paras.append(buf)
                buf = ""
            continue
        if not buf:
            buf = s
        elif _PARA_START_RE.match(s):
            paras.append(buf)
            buf = s
        else:
            # continuation of the current paragraph; de-hyphenate line breaks
            if buf.endswith("-"):
                buf = buf[:-1] + s
            else:
                buf = buf + " " + s
    if buf:
        paras.append(buf)
    return paras


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------
_META_PATTERNS = {
    "witness_name": re.compile(r"Witness\s*Name\s*:?\s*(.+)", re.IGNORECASE),
    "statement_no": re.compile(r"Statement\s*No\.?\s*:?\s*(.+)", re.IGNORECASE),
    "exhibits": re.compile(r"Exhibits?\s*:?\s*(.+)", re.IGNORECASE),
    "date": re.compile(r"^\s*Date\s*:?\s*(.+)", re.IGNORECASE),
}


def extract_metadata(first_pages_lines: list[str], doc_id: str) -> dict:
    meta = {"doc_id": doc_id}
    for ln in first_pages_lines:
        for key, pat in _META_PATTERNS.items():
            if key in meta:
                continue
            m = pat.match(ln.strip())
            if m:
                val = m.group(1).strip()
                if val:
                    meta[key] = val
    # Inquiry title / statement title (e.g. "FIRST WITNESS STATEMENT OF ...")
    for ln in first_pages_lines:
        if re.search(r"WITNESS STATEMENT OF", ln, re.IGNORECASE):
            meta["title"] = ln.strip()
            break
    return meta


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
@dataclass
class DocResult:
    doc_id: str
    source: str
    status: str            # "ok" | "failed"
    pages: int = 0
    out_path: str = ""
    metadata: dict = field(default_factory=dict)
    error: str = ""


def convert_one(args_tuple) -> DocResult:
    path, out_dir, fmt, do_clean = args_tuple
    fname = os.path.basename(path)
    doc_id = re.sub(r"\.pdf$", "", fname, flags=re.IGNORECASE)

    try:
        if os.path.getsize(path) == 0:
            raise ValueError("empty file (0 bytes)")

        with pdfplumber.open(path) as pdf:
            raw_pages = [(p.extract_text() or "") for p in pdf.pages]

        pages_lines = [pg.split("\n") for pg in raw_pages]
        header_lines = make_header_detector(pages_lines)

        cleaned_pages = [clean_page(pg, header_lines, do_clean) for pg in raw_pages]

        # Metadata from the first two pages' lines.
        first_lines: list[str] = []
        for pg in cleaned_pages[:2]:
            first_lines.extend(pg)
        metadata = extract_metadata(first_lines, doc_id)

        os.makedirs(out_dir, exist_ok=True)
        ext = {"md": ".md", "json": ".json", "txt": ".txt"}[fmt]
        out_path = os.path.join(out_dir, doc_id + ext)

        if fmt == "json":
            doc = {
                "doc_id": doc_id,
                "source_file": fname,
                "metadata": metadata,
                "page_count": len(raw_pages),
                "pages": [
                    {"page": i + 1, "paragraphs": reflow(cleaned_pages[i])}
                    for i in range(len(cleaned_pages))
                ],
            }
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=2)
        elif fmt == "txt":
            blocks = []
            for i, pg in enumerate(cleaned_pages):
                paras = reflow(pg)
                blocks.append("\n\n".join(paras))
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("\n\n".join(blocks).strip() + "\n")
        else:  # markdown
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("---\n")
                fh.write(f"doc_id: {doc_id}\n")
                fh.write(f"source_file: {json.dumps(fname, ensure_ascii=False)}\n")
                for k in ("witness_name", "statement_no", "date", "exhibits", "title"):
                    if k in metadata:
                        fh.write(f"{k}: {json.dumps(metadata[k], ensure_ascii=False)}\n")
                fh.write(f"page_count: {len(raw_pages)}\n")
                fh.write("---\n\n")
                title = metadata.get("title") or metadata.get("witness_name") or doc_id
                fh.write(f"# {title}\n\n")
                for i, pg in enumerate(cleaned_pages):
                    paras = reflow(pg)
                    if not paras:
                        continue
                    fh.write(f"<!-- page {i + 1} -->\n\n")
                    fh.write("\n\n".join(paras))
                    fh.write("\n\n")

        return DocResult(
            doc_id=doc_id, source=fname, status="ok",
            pages=len(raw_pages), out_path=out_path, metadata=metadata,
        )
    except Exception as e:
        return DocResult(
            doc_id=doc_id, source=fname, status="failed",
            error=f"{type(e).__name__}: {e}",
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=DEFAULT_INPUT, help="folder of PDFs")
    ap.add_argument("--output", default=DEFAULT_OUTPUT, help="output folder")
    ap.add_argument("--format", choices=["md", "json", "txt"], default="md")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--no-clean", action="store_true",
                    help="keep raw extracted text (skip artifact removal/reflow cleanup)")
    args = ap.parse_args()

    if not os.path.isdir(args.input):
        print(f"Input folder not found: {args.input}", file=sys.stderr)
        return 2

    pdfs = sorted(
        os.path.join(args.input, f)
        for f in os.listdir(args.input)
        if f.lower().endswith(".pdf")
    )
    if not pdfs:
        print(f"No PDFs found in {args.input}", file=sys.stderr)
        return 2

    os.makedirs(args.output, exist_ok=True)
    do_clean = not args.no_clean
    tasks = [(p, args.output, args.format, do_clean) for p in pdfs]

    print(f"Converting {len(pdfs)} PDFs -> {args.format} in '{args.output}' "
          f"({args.workers} workers)...")
    results: list[DocResult] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(convert_one, t): t for t in tasks}
        done = 0
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            tag = "OK " if r.status == "ok" else "ERR"
            extra = f"{r.pages}p" if r.status == "ok" else r.error
            print(f"  [{done:>3}/{len(pdfs)}] {tag} {r.doc_id:<22} {extra}")

    # Manifest index for the whole batch.
    manifest_path = os.path.join(args.output, "manifest.csv")
    with open(manifest_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["doc_id", "status", "pages", "witness_name", "statement_no",
                    "date", "out_file", "source_file", "error"])
        for r in sorted(results, key=lambda x: x.doc_id):
            w.writerow([
                r.doc_id, r.status, r.pages,
                r.metadata.get("witness_name", ""),
                r.metadata.get("statement_no", ""),
                r.metadata.get("date", ""),
                os.path.basename(r.out_path), r.source, r.error,
            ])

    ok = sum(1 for r in results if r.status == "ok")
    failed = [r for r in results if r.status != "ok"]
    total_pages = sum(r.pages for r in results)
    print(f"\nDone: {ok}/{len(results)} converted, {total_pages} pages total.")
    print(f"Manifest: {manifest_path}")
    if failed:
        print(f"Failed ({len(failed)}):")
        for r in failed:
            print(f"  - {r.source}: {r.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
