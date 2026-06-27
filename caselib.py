"""
caselib — shared library for the pleading-to-proof pipeline.

Parses the Markdown produced by convert_pdfs.py, classifies documents
(pleading / witness statement / exhibit), and provides the Claude API
helpers, JSON schemas, prompts, quote verification, and a dependency-free
BM25 retriever used by extract.py and build_matrix.py.

Only external dependency: the `anthropic` SDK (see requirements.txt).
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Iterable

MODEL = "claude-sonnet-4-6"


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency). Real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("\"'"))


load_dotenv()   # load ./.env at import so ANTHROPIC_API_KEY / VOYAGE_API_KEY are set

# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------
_FM_DELIM = "---"
_PAGE_RE = re.compile(r"^<!--\s*page\s+(\d+)\s*-->\s*$")
# A paragraph's leading label: "12." / "a." / "(a)" / "iv." etc.
_LABEL_RE = re.compile(r"^\s*(\d+\.|\(?[a-z]\)|[a-z]\.|\(?(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\))",
                       re.IGNORECASE)


@dataclass
class Paragraph:
    page: int
    label: str          # "12." / "a." / "" if none
    text: str


@dataclass
class Document:
    path: str
    doc_id: str
    meta: dict
    paragraphs: list[Paragraph]
    kind: str = "unknown"    # pleading | witness_statement | exhibit | other

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)


def _parse_frontmatter(lines: list[str]) -> tuple[dict, int]:
    """Return (meta, index_of_first_body_line)."""
    if not lines or lines[0].strip() != _FM_DELIM:
        return {}, 0
    meta: dict = {}
    i = 1
    while i < len(lines) and lines[i].strip() != _FM_DELIM:
        line = lines[i]
        if ":" in line:
            key, _, val = line.partition(":")
            val = val.strip()
            if val and val[0] in "\"'" and val[-1:] == val[0]:
                try:
                    val = json.loads(val)
                except Exception:
                    val = val.strip("\"'")
            meta[key.strip()] = val
        i += 1
    return meta, (i + 1 if i < len(lines) else i)


def parse_markdown(path: str) -> Document:
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    meta, start = _parse_frontmatter(lines)
    doc_id = str(meta.get("doc_id") or os.path.splitext(os.path.basename(path))[0])

    paragraphs: list[Paragraph] = []
    page = 1
    buf: list[str] = []

    def flush():
        if not buf:
            return
        text = " ".join(s.strip() for s in buf if s.strip()).strip()
        if text:
            m = _LABEL_RE.match(text)
            label = m.group(1) if m else ""
            paragraphs.append(Paragraph(page=page, label=label, text=text))
        buf.clear()

    for line in lines[start:]:
        pm = _PAGE_RE.match(line.strip())
        if pm:
            flush()
            page = int(pm.group(1))
            continue
        if line.startswith("# "):          # the H1 title — skip, it's in meta
            continue
        if not line.strip():
            flush()
        else:
            buf.append(line)
    flush()
    return Document(path=path, doc_id=doc_id, meta=meta, paragraphs=paragraphs)


# ---------------------------------------------------------------------------
# Document classification (heuristic — cheap, no API call)
# ---------------------------------------------------------------------------
_PLEADING_MARKERS = [
    "particulars of claim", "statement of case", "defence and counterclaim",
    "re-amended defence", "amended defence", "the defence", "counterclaim",
    "reply to defence", "rejoinder", "generic particulars",
]
_WITNESS_MARKERS = ["witness statement of", "will say as follows", "i believe that the facts"]
_EXHIBIT_MARKERS = ["exhibit", "this is the exhibit marked", "annexed hereto"]


def classify(doc: Document) -> str:
    blob = (doc.full_text[:4000] + " " + json.dumps(doc.meta)).lower()
    title = str(doc.meta.get("title", "")).lower()

    def hit(markers):
        return sum(1 for m in markers if m in blob or m in title)

    pl, wt, ex = hit(_PLEADING_MARKERS), hit(_WITNESS_MARKERS), hit(_EXHIBIT_MARKERS)
    # Witness statements in this dataset reliably say "WITNESS STATEMENT OF".
    if wt and wt >= pl:
        return "witness_statement"
    if pl:
        return "pleading"
    if ex and not wt:
        return "exhibit"
    return "other"


# ---------------------------------------------------------------------------
# Quote verification (anti-hallucination gate)
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = (s or "").lower()
    s = (s.replace("’", "'").replace("‘", "'")
           .replace("“", '"').replace("”", '"'))
    s = re.sub(r"[\"'`]", "", s)          # drop quotes/apostrophes (common mismatch)
    return re.sub(r"\s+", " ", s).strip()


def verify_quote(quote: str, source_text: str) -> bool:
    q = _norm(quote)
    if len(q) < 8:                       # too short to be a meaningful citation
        return False
    return q in _norm(source_text)


# ---------------------------------------------------------------------------
# BM25 retriever (pure Python — no sklearn / numpy needed)
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = set("the a an and or of to in on for with that this is are was were be been "
            "as at by it its from has have had not no but which who whom whose will "
            "would shall should may might can could i he she they we you my his her "
            "their our your me him them us do does did so if then than".split())


def _tok(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP and len(t) > 1]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.corpus = [_tok(d) for d in docs]
        self.N = len(self.corpus)
        self.avgdl = (sum(len(d) for d in self.corpus) / self.N) if self.N else 0.0
        self.df: Counter = Counter()
        for d in self.corpus:
            self.df.update(set(d))
        self.idf = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for t, n in self.df.items()
        }
        self.tf = [Counter(d) for d in self.corpus]

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        q = _tok(query)
        scores = []
        for i in range(self.N):
            dl = len(self.corpus[i]) or 1
            s = 0.0
            tf = self.tf[i]
            for t in q:
                if t not in tf:
                    continue
                idf = self.idf.get(t, 0.0)
                freq = tf[t]
                s += idf * (freq * (self.k1 + 1)) / (
                    freq + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                )
            if s > 0:
                scores.append((i, s))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


# ---------------------------------------------------------------------------
# Embeddings retriever (Voyage AI) + hybrid fusion
# ---------------------------------------------------------------------------
import hashlib

DEFAULT_EMBED_MODEL = "voyage-law-2"   # legal-domain tuned; good for this task


def voyage_embed(texts: list[str], model: str, input_type: str,
                 batch: int = 128) -> list[list[float]]:
    """Embed texts with Voyage AI. input_type is 'document' or 'query'."""
    try:
        import voyageai
    except ImportError as e:
        raise SystemExit("Embeddings retrieval needs the 'voyageai' package and "
                         "VOYAGE_API_KEY. Run: pip install voyageai") from e
    vo = voyageai.Client()    # reads VOYAGE_API_KEY from env
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        out.extend(vo.embed(chunk, model=model, input_type=input_type).embeddings)
    return out


class EmbeddingRetriever:
    """Dense retriever over the evidence corpus, with on-disk vector caching
    so re-runs don't re-embed unchanged text."""

    def __init__(self, texts: list[str], model: str = DEFAULT_EMBED_MODEL,
                 cache_path: str | None = None, batch: int = 128):
        import numpy as np
        self.np = np
        self.model = model
        self.batch = batch

        hashes = [hashlib.sha1(f"{model}\x00{t}".encode("utf-8")).hexdigest()
                  for t in texts]
        cache: dict[str, "np.ndarray"] = {}
        if cache_path and os.path.exists(cache_path):
            z = np.load(cache_path, allow_pickle=True)
            for k, v in zip(z["hashes"], z["vectors"]):
                cache[str(k)] = v

        missing = [(t, h) for t, h in zip(texts, hashes) if h not in cache]
        if missing:
            print(f"  embedding {len(missing)} new texts with {model}...")
            vecs = voyage_embed([t for t, _ in missing], model, "document", batch)
            for (_, h), v in zip(missing, vecs):
                cache[h] = np.asarray(v, dtype="float32")
            if cache_path:
                np.savez(cache_path,
                         hashes=np.array(list(cache.keys())),
                         vectors=np.vstack(list(cache.values())))

        M = np.vstack([cache[h] for h in hashes]).astype("float32")
        norms = np.linalg.norm(M, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.M = M / norms                       # row-normalised → dot = cosine

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        v = self.np.asarray(
            voyage_embed([query], self.model, "query", self.batch)[0],
            dtype="float32")
        n = self.np.linalg.norm(v) or 1.0
        sims = self.M @ (v / n)
        idx = self.np.argsort(-sims)[:top_k]
        return [(int(i), float(sims[i])) for i in idx]


class HybridRetriever:
    """Reciprocal-rank fusion of several retrievers (e.g. BM25 + embeddings).
    Combines each retriever's top-`pool` ranking, robust to score-scale
    differences between lexical and dense methods."""

    def __init__(self, retrievers: list, pool: int = 100, k: int = 60):
        self.retrievers = retrievers
        self.pool = pool
        self.k = k

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        fused: dict[int, float] = defaultdict(float)
        for r in self.retrievers:
            for rank, (idx, _) in enumerate(r.search(query, self.pool)):
                fused[idx] += 1.0 / (self.k + rank + 1)
        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]


# ---------------------------------------------------------------------------
# JSON Schemas for structured outputs (no recursion / no numeric bounds —
# per structured-output constraints; additionalProperties:false everywhere)
# ---------------------------------------------------------------------------
PROPOSITIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "propositions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["allegation", "denial"]},
                    "text": {"type": "string"},
                    "party": {"type": "string"},
                    "responds_to": {"type": "string"},
                    "page": {"type": "integer"},
                    "paragraph": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["type", "text", "party", "responds_to",
                             "page", "paragraph", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["propositions"],
    "additionalProperties": False,
}

EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "assertion": {"type": "string"},
                    "witness": {"type": "string"},
                    "page": {"type": "integer"},
                    "paragraph": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["assertion", "witness", "page", "paragraph", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["evidence"],
    "additionalProperties": False,
}

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string"},
                    "relation": {"type": "string",
                                 "enum": ["supportive", "adverse", "neutral"]},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["evidence_id", "relation", "confidence",
                             "rationale", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
PROPOSITIONS_SYSTEM = """\
You are a litigation analyst extracting the pleaded case from a statement of \
case (Particulars of Claim, Defence, Counterclaim, Reply, etc.) in an English \
commercial dispute.

Extract every PLEADED PROPOSITION as an atomic record:
- type: "allegation" (a positive assertion a party must prove) or "denial" \
(a denial, non-admission, or positive rebuttal of the other side's allegation).
- ATOMISE: one factual proposition per record. Split compound paragraphs \
("X was reliable AND the defendant knew of bugs") into separate records.
- party: who advances it (e.g. "Claimant", "Defendant"), or "" if unclear.
- responds_to: if a denial, briefly identify the allegation it answers; else "".
- page / paragraph: use the `<!-- page N -->` markers and the leading paragraph \
number/label present in the text. paragraph is the label string (e.g. "14." or "14(a)").
- quote: a SHORT VERBATIM span copied exactly from the text that anchors the \
proposition. Do not paraphrase the quote.

Only extract genuinely pleaded propositions. Do not invent. Return JSON only."""

EVIDENCE_SYSTEM = """\
You are a litigation analyst extracting EVIDENCE from a witness statement / \
exhibit in an English commercial dispute. The goal is to later test pleaded \
allegations and denials against this evidence.

Extract every materially probative FACTUAL ASSERTION as an atomic record:
- assertion: a single, self-contained factual claim the witness makes \
(what happened, who knew what, when). One fact per record; split compound sentences.
- Skip pure narrative throat-clearing, formalities, and legal argument.
- witness: the witness's name (from the metadata/title) or "".
- page / paragraph: use the `<!-- page N -->` markers and the leading paragraph \
number/label present in the text.
- quote: a SHORT VERBATIM span copied exactly from the text. Do not paraphrase.

Do not invent facts not stated. Return JSON only."""

CLASSIFY_SYSTEM = """\
You are a litigation analyst building a pleading-to-proof matrix. You are given \
ONE pleaded proposition and a list of candidate EVIDENCE items. For each \
candidate, classify its relationship to the proposition:

- "supportive": the evidence makes the proposition more likely true / helps \
prove it (or, for a denial, supports the denial).
- "adverse": the evidence contradicts or undermines the proposition.
- "neutral": on-topic but does not move the proposition either way.

For each candidate return: evidence_id (exactly as given), relation, confidence \
(0.0-1.0), a one-sentence rationale, and a SHORT VERBATIM quote copied from that \
evidence item's text. Judge only on the evidence shown. Return JSON only."""


def build_classify_user(prop: dict, candidates: list[dict]) -> str:
    lines = [
        f'PLEADED PROPOSITION ({prop.get("type")}, {prop.get("party","?")}):',
        prop["text"],
        "",
        "CANDIDATE EVIDENCE ITEMS:",
    ]
    for c in candidates:
        w = f' [{c.get("witness")}]' if c.get("witness") else ""
        lines.append(f'- evidence_id={c["id"]}{w}: {c["assertion"]}')
        if c.get("quote"):
            lines.append(f'    quote: "{c["quote"]}"')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Heuristic (no-LLM) extraction — a zero-cost, offline fallback / preview path.
#
# Splits each document into atomic spans straight from the parsed paragraph
# structure, with no model call. It can't judge intent the way the LLM extractor
# does (allegation-vs-denial nuance, dropping boilerplate and legal argument), so
# recall/precision are lower — but every record is provenance-anchored and its
# quote is verbatim by construction (it IS the span). Use it for a dry run, in
# CI, or when no API key / credits are available. Output records match the shapes
# of PROPOSITIONS_SCHEMA / EVIDENCE_SCHEMA so the rest of extract.py is unchanged.
# ---------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[.;])\s+(?=[\"'(A-Z])")
_DENIAL_RE = re.compile(
    r"\b(denie[sd]|deny|denying|not admitted|no admission|is denied|are denied|"
    r"disput\w+|reject\w+|refut\w+)\b", re.IGNORECASE)
_HEURISTIC_MIN_CHARS = 40


def _sentences(text: str) -> list[str]:
    """Rough sentence split that keeps short trailing fragments attached, so a
    stray "Ltd." or "(a)" doesn't become its own record."""
    parts = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
    merged: list[str] = []
    for s in parts:
        if merged and len(s) < 20:
            merged[-1] = f"{merged[-1]} {s}"
        else:
            merged.append(s)
    return merged or ([text.strip()] if text.strip() else [])


def _atomic_spans(doc: "Document", min_chars: int = _HEURISTIC_MIN_CHARS):
    """Yield (page, paragraph_label, span) for substantive paragraphs. Each span
    is a verbatim substring of the source, so verify_quote() passes on it."""
    for p in doc.paragraphs:
        body = p.text
        if p.label and body.startswith(p.label):
            body = body[len(p.label):].strip()     # drop the repeated "12." label
        if len(body) < min_chars:
            continue                               # skip headings / one-liners
        for sent in _sentences(body):
            if len(sent) >= min_chars:
                yield p.page, p.label, sent


def _guess_party(doc: "Document") -> str:
    blob = (str(doc.meta.get("title", "")) + " " + doc.doc_id).lower()
    if "defence" in blob or "defendant" in blob or "counterclaim" in blob:
        return "Defendant"
    if "claim" in blob or "claimant" in blob or "particulars" in blob:
        return "Claimant"
    return ""


def heuristic_propositions(doc: "Document") -> list[dict]:
    """No-LLM proposition records, shaped like PROPOSITIONS_SCHEMA items."""
    party = _guess_party(doc)
    return [{
        "type": "denial" if _DENIAL_RE.search(span) else "allegation",
        "text": span,
        "party": party,
        "responds_to": "",
        "page": page,
        "paragraph": label,
        "quote": span,
    } for page, label, span in _atomic_spans(doc)]


def heuristic_evidence(doc: "Document") -> list[dict]:
    """No-LLM evidence records, shaped like EVIDENCE_SCHEMA items."""
    wit = doc.meta.get("witness_name", "") or ""
    return [{
        "assertion": span,
        "witness": wit,
        "page": page,
        "paragraph": label,
        "quote": span,
    } for page, label, span in _atomic_spans(doc)]


# ---------------------------------------------------------------------------
# Anthropic helpers
# ---------------------------------------------------------------------------
def get_client():
    try:
        import anthropic
    except ImportError as e:
        raise SystemExit("The 'anthropic' package is required. "
                         "Run: pip install -r requirements.txt") from e
    return anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env


def llm_unavailable_reason(exc: Exception) -> str | None:
    """If exc means the LLM simply can't be used right now (no key, no credits,
    no network), return a short human reason for the log. Return None for a
    genuine bug (e.g. a malformed request) that we should NOT silently mask by
    falling back to the heuristic extractor.

    Callers use this to decide whether to fall back: a missing API key or an
    exhausted credit balance is a reason to switch to the free heuristic path; a
    schema error is not."""
    try:
        import anthropic
    except ImportError:
        return "anthropic package not installed"
    if isinstance(exc, (anthropic.AuthenticationError,
                        anthropic.PermissionDeniedError)):
        return "authentication/permission failed (check ANTHROPIC_API_KEY)"
    if isinstance(exc, anthropic.APIConnectionError):
        return "cannot reach the Anthropic API (offline?)"
    msg = str(exc).lower()
    if "api_key" in msg or "anthropic_api_key" in msg:
        return "no API key set"
    if "credit" in msg or "billing" in msg or "quota" in msg or "insufficient" in msg:
        return "no credits / quota exhausted"
    return None


# ---------------------------------------------------------------------------
# On-disk result cache — skip re-calling the LLM for inputs we've seen before.
# Keyed by a hash of (model, system, user, schema): identical inputs => cache
# hit, so re-running extract.py / build_matrix.py over an unchanged bundle costs
# no tokens. Bust it by deleting the cache directory (default out/cache, which
# is gitignored along with the rest of out/).
# ---------------------------------------------------------------------------
CACHE_DIR_DEFAULT = os.path.join("out", "cache")


def llm_cache_key(system: str, user: str, schema: dict, model: str = MODEL) -> str:
    payload = json.dumps(
        {"model": model, "system": system, "user": user, "schema": schema},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_load(cache_dir: str | None, key: str):
    """Cached value for key, or None on miss / disabled / unreadable file."""
    if not cache_dir:
        return None
    path = os.path.join(cache_dir, key + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None        # a corrupt cache file is a miss, not a crash


def cache_store(cache_dir: str | None, key: str, value) -> None:
    """Persist a successful result. Failures (None) are never cached, so they
    are retried on the next run rather than baked in."""
    if not cache_dir or value is None:
        return
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, key + ".json"), "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False)


def _text_block(message) -> str:
    """Return the JSON text from a response (skipping any thinking blocks)."""
    for block in message.content:
        if block.type == "text":
            return block.text
    return ""


def _params(system: str, user: str, schema: dict, max_tokens: int) -> dict:
    """Build a Messages params dict shared by sync + batch paths."""
    return dict(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": "high",
                       "format": {"type": "json_schema", "schema": schema}},
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )


def run_sync(client, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
    msg = client.messages.create(**_params(system, user, schema, max_tokens))
    return json.loads(_text_block(msg))


def run_batch(client, jobs: list[dict], poll_seconds: int = 30) -> dict[str, dict | None]:
    """
    jobs: [{custom_id, system, user, schema, max_tokens}].
    Returns {custom_id: parsed_json | None}.  None means the request errored.
    """
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    requests = [
        Request(
            custom_id=j["custom_id"],
            params=MessageCreateParamsNonStreaming(
                **_params(j["system"], j["user"], j["schema"], j.get("max_tokens", 16000))
            ),
        )
        for j in jobs
    ]
    batch = client.messages.batches.create(requests=requests)
    print(f"  batch {batch.id} submitted ({len(requests)} requests); polling...")
    return collect_batch(client, batch.id, poll_seconds)


def _resilient(fn, *, tries: int = 8, base: float = 5.0):
    """Call fn(), retrying transient network/connection errors with backoff.
    A poll-time DNS blip should not throw away an in-flight (paid) batch."""
    import anthropic
    last = None
    for attempt in range(tries):
        try:
            return fn()
        except (anthropic.APIConnectionError, anthropic.InternalServerError,
                anthropic.RateLimitError) as e:
            last = e
            delay = min(base * (2 ** attempt), 120.0)
            print(f"    transient error ({type(e).__name__}); retry in {delay:.0f}s")
            time.sleep(delay)
    raise last


def collect_batch(client, batch_id: str, poll_seconds: int = 30) -> dict[str, dict | None]:
    """Poll an existing batch to completion and return {custom_id: parsed|None}.
    Resilient to transient connection errors so a network blip mid-poll doesn't
    discard the (already-submitted, already-billed) batch. Resume any crashed run
    with: collect_batch(client, '<msgbatch_id>')."""
    while True:
        b = _resilient(lambda: client.messages.batches.retrieve(batch_id))
        if b.processing_status == "ended":
            break
        c = b.request_counts
        print(f"    status={b.processing_status} "
              f"processing={c.processing} succeeded={c.succeeded} errored={c.errored}")
        time.sleep(poll_seconds)

    out: dict[str, dict | None] = {}
    for result in _resilient(lambda: list(client.messages.batches.results(batch_id))):
        if result.result.type == "succeeded":
            try:
                out[result.custom_id] = json.loads(_text_block(result.result.message))
            except Exception as e:
                print(f"    parse error for {result.custom_id}: {e}")
                out[result.custom_id] = None
        else:
            print(f"    {result.custom_id}: {result.result.type}")
            out[result.custom_id] = None
    return out


# ---------------------------------------------------------------------------
# Small IO helpers
# ---------------------------------------------------------------------------
def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
