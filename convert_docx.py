#!/usr/bin/env python3
"""
Preprocessing: convert the CMS Challenge .docx bundle into LLM-readable Markdown.

This is a PREPROCESSING STEP, not part of the web app. Run it ONCE, before the
pipeline / app, whenever the source bundle changes:

    python convert_docx.py            # dataset bundle -> bundle_md/*.md
    python extract.py                 # bundle_md/     -> out/propositions.json, ...
    python build_matrix.py            # ...            -> out/matrix.json
    python case_ui/snapshot_data.py   # publish matrix snapshot for the app

It replaces the deprecated PDF path (convert_pdfs.py / pdf_to_ai_safe.py): the
official challenge data is a bundle of .docx files, so we parse those directly.

Pure stdlib — a .docx is a zip of XML, so we read word/document.xml with
zipfile + ElementTree (no python-docx dependency). Output Markdown matches the
format caselib.parse_markdown expects: `---` frontmatter, a `# title`, a
`<!-- page N -->` marker, blank-line-separated paragraphs (leading numbers like
"12." preserved for provenance), and tables rendered as Markdown tables.
"""

import argparse
import json
import os
import re
import zipfile
from xml.etree import ElementTree as ET

# WordprocessingML namespace.
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

DEFAULT_INPUT = os.path.join("dataset", "CMS Challenge - Case Bundle (Synthetic)")
DEFAULT_OUTPUT = "bundle_md"


def _para_text(p: ET.Element) -> str:
    """Flatten a <w:p> to plain text, honouring tabs and line breaks."""
    parts: list[str] = []
    for node in p.iter():
        tag = node.tag
        if tag == f"{W}t":
            parts.append(node.text or "")
        elif tag == f"{W}tab":
            parts.append(" ")
        elif tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    return re.sub(r"[ \t]+", " ", "".join(parts)).strip()


def _cell_text(tc: ET.Element) -> str:
    """A table cell may hold several paragraphs; join them on one line."""
    paras = [_para_text(p) for p in tc.findall(f"{W}p")]
    return " ".join(t for t in paras if t).replace("|", "\\|").strip()


def _table_md(tbl: ET.Element) -> str:
    """Render a <w:tbl> as a GitHub-flavoured Markdown table."""
    rows: list[list[str]] = []
    for tr in tbl.findall(f"{W}tr"):
        rows.append([_cell_text(tc) for tc in tr.findall(f"{W}tc")])
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _blocks(path: str) -> list[str]:
    """Walk the document body in order, yielding paragraph and table blocks."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    out: list[str] = []
    if body is None:
        return out
    for child in body:
        if child.tag == f"{W}p":
            text = _para_text(child)
            if text:
                out.append(text)
        elif child.tag == f"{W}tbl":
            md = _table_md(child)
            if md:
                out.append(md)
    return out


def _title_from_name(doc_id: str) -> str:
    """'02_Particulars_of_Claim' -> 'Particulars of Claim'."""
    name = re.sub(r"^\d+[_\s-]*", "", doc_id)
    return name.replace("_", " ").strip() or doc_id


def convert_one(path: str, out_dir: str) -> str:
    fname = os.path.basename(path)
    doc_id = re.sub(r"\.docx$", "", fname, flags=re.IGNORECASE)
    title = _title_from_name(doc_id)
    blocks = _blocks(path)

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, doc_id + ".md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("---\n")
        fh.write(f"doc_id: {doc_id}\n")
        fh.write(f"source_file: {json.dumps(fname, ensure_ascii=False)}\n")
        fh.write(f"title: {json.dumps(title, ensure_ascii=False)}\n")
        fh.write("page_count: 1\n")  # .docx has no fixed pagination
        fh.write("---\n\n")
        fh.write(f"# {title}\n\n")
        # Single synthetic page marker keeps the downstream provenance schema happy.
        fh.write("<!-- page 1 -->\n\n")
        fh.write("\n\n".join(blocks))
        fh.write("\n")
    return out_path


def find_docx(root: str) -> list[str]:
    if os.path.isfile(root) and root.lower().endswith(".docx"):
        return [root]
    if os.path.isdir(root):
        return sorted(
            os.path.join(dp, f)
            for dp, _dn, fns in os.walk(root)
            for f in fns
            if f.lower().endswith(".docx") and not f.startswith("~$")
        )
    return []


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", default=DEFAULT_INPUT,
                    help="folder of .docx files (the case bundle)")
    ap.add_argument("--output", "-o", default=DEFAULT_OUTPUT,
                    help="output folder for the Markdown")
    args = ap.parse_args()

    docs = find_docx(args.input)
    if not docs:
        print(f"No .docx files found under: {args.input}")
        return
    print(f"Converting {len(docs)} .docx -> {args.output}/")
    ok = 0
    for path in docs:
        try:
            out = convert_one(path, args.output)
            print(f"  ok   {os.path.basename(out)}")
            ok += 1
        except Exception as exc:  # keep going; report the failure
            print(f"  FAIL {os.path.basename(path)}: {type(exc).__name__}: {exc}")
    print(f"Done: {ok}/{len(docs)} converted into {args.output}/")


if __name__ == "__main__":
    main()
