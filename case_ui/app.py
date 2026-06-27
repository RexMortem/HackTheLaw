"""
Case UI — Proof Matrix web server.
Pure stdlib: no third-party dependencies.

Run:
    python case_ui/app.py

Then open: http://localhost:8001

Data file read (relative to the repo root, i.e. one level above this file):
    out/matrix.json — full matrix (from build_matrix.py)

Goals file written to:
    case_ui/data/goals.json
"""

import hashlib
import json
import os
import pathlib
import re
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Benign on Windows when a browser cancels/aborts a request mid-response.
_DISCONNECT_ERRORS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

# After an LLM call fails (no credits, no network), skip the LLM (and the EU
# Cellar fetch) for this many seconds so the UI answers instantly from the
# grounded offline path. It auto-recovers — the next call after the window
# retries, and a success just lets the window lapse.
_LLM_COOLDOWN_SECS = 60.0
_llm_down_until = 0.0


def _llm_in_cooldown() -> bool:
    return time.monotonic() < _llm_down_until


def _llm_mark_down() -> None:
    global _llm_down_until
    _llm_down_until = time.monotonic() + _LLM_COOLDOWN_SECS


def _log(tag: str, msg: str) -> None:
    """Server-side debug log, flushed so it appears immediately even when stdout
    is buffered (e.g. redirected to a file / running under a process manager)."""
    print(f"  [{tag}] {msg}", flush=True)


def _describe_response(msg) -> str:
    """One-line summary of an Anthropic response for the log: stop reason, the
    content-block types present, and token usage."""
    block_types: dict[str, int] = {}
    for b in getattr(msg, "content", []) or []:
        block_types[b.type] = block_types.get(b.type, 0) + 1
    usage = getattr(msg, "usage", None)
    tin = getattr(usage, "input_tokens", "?")
    tout = getattr(usage, "output_tokens", "?")
    return (f"stop_reason={getattr(msg, 'stop_reason', '?')} "
            f"blocks={block_types or '{}'} tokens(in/out)={tin}/{tout}")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Hosts (e.g. Render) inject the port to bind via $PORT; default for local dev.
PORT = int(os.environ.get("PORT", "8001"))

# Resolve paths relative to the repo root (parent of this file's directory).
_HERE = pathlib.Path(__file__).parent
REPO_ROOT = _HERE.parent
# Launching as `python case_ui/app.py` puts case_ui/ on sys.path, not the repo
# root, so the lazy `import caselib` (in the chat/summary handlers) would fail.
# Put the repo root on the path so caselib is importable from any launch dir.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
OUT_DIR = REPO_ROOT / "out"
STATIC_DIR = _HERE
DATA_DIR = _HERE / "data"

GOALS_FILE = DATA_DIR / "goals.json"
# Cached AI case summary (narrative + retrieved EU legal sources), keyed by a
# signature of the matrix so we only re-call the LLM when the case data changes.
SUMMARY_FILE = DATA_DIR / "summary.json"
# Cached generated arguments (Claude + Perplexity authority) and the saved
# "built case" — both keyed/stored like the summary so the LLM only runs when
# the case changes.
ARGUMENTS_FILE = DATA_DIR / "arguments.json"
CASE_FILE = DATA_DIR / "case.json"
QUANTUM_FILE = DATA_DIR / "quantum.json"
# User-added propositions / evidence, layered onto the matrix at load time.
ADDITIONS_FILE = DATA_DIR / "additions.json"

# EU Publications Office — Cellar SPARQL endpoint (public, no auth). Used to
# retrieve potentially relevant EU legal materials to ground the AI summary.
CELLAR_SPARQL = "http://publications.europa.eu/webapi/rdf/sparql"

# In-memory goals cache — populated on first read or on POST /api/goals.
_GOALS: dict | None = None


def _out(name: str) -> pathlib.Path:
    return OUT_DIR / name


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------
def _load_matrix() -> tuple[list, str | None]:
    """Returns (matrix_list, error_string).

    Prefers the live pipeline output (out/matrix.json). That directory is
    gitignored and absent on deploy hosts, so fall back to the committed
    snapshot at case_ui/data/matrix.json (refresh via snapshot_data.py).
    """
    path = _out("matrix.json")
    if not path.exists():
        path = DATA_DIR / "matrix.json"
    if not path.exists():
        return [], (
            "matrix.json not found. Run the pipeline first:\n"
            "  python extract.py\n"
            "  python build_matrix.py\n"
            "Then snapshot it for deploy:\n"
            "  python case_ui/snapshot_data.py"
        )
    with open(path, encoding="utf-8") as fh:
        matrix = json.load(fh)
    return _apply_additions(matrix), None


def _status_from_links(links: list, min_conf: float = 0.5) -> str:
    """Four-bucket status from a row's links (same rule as build_matrix)."""
    sup = any(l.get("relation") == "supportive" and (l.get("confidence") or 0) >= min_conf
              for l in links)
    adv = any(l.get("relation") == "adverse" and (l.get("confidence") or 0) >= min_conf
              for l in links)
    if sup and adv:
        return "contested"
    if sup:
        return "supported"
    if adv:
        return "undermined"
    return "MISSING"


# ---------------------------------------------------------------------------
# Grounding guard — live, deterministic citation verification for the UI
# ---------------------------------------------------------------------------
# Every citation Claude emitted is checked against the case bundle before the UI
# shows it, so a judge can never click a citation that doesn't hold. This is the
# "accountability layer" / anti-hallucination gate: it is DETERMINISTIC CODE, not
# a prompt — we don't ask the model to grade itself, we check it.
#
#   1. Citation existence — the cited source id (doc + ¶) must really be in the
#      case; a fabricated paragraph/document is caught and flagged.
#   2. Quote verification — the quoted text must actually appear in that source.
#      The full source bundle (bundle_md/) is regenerable and not shipped with
#      the app, so we replay the verdict the pipeline already recorded per
#      citation as `quote_ok` — itself the same grounding-guard quote check run
#      against the real source text at extraction time (caselib.verify_quote).
#   3. Quarantine — anything that fails either check is marked so the UI can
#      surface it as unverified rather than presenting it as solid.
_GG = None


def _grounding_guard():
    """Lazy-import the deterministic verifier from case_ui/quantum (pure stdlib,
    no third-party deps). Cached after first use."""
    global _GG
    if _GG is None:
        gg_dir = str(_HERE / "quantum")
        if gg_dir not in sys.path:
            sys.path.insert(0, gg_dir)
        import grounding_guard as gg
        _GG = gg
    return _GG


def _citation_id(src: dict | None) -> str:
    """Composite source id from a {doc_id, page, paragraph} provenance block,
    e.g. '14_Letter_Notice_of_Termination ¶2' — shown in the verdict tooltip."""
    if not src:
        return ""
    doc = str(src.get("doc_id") or "").strip()
    para = str(src.get("paragraph") or "").strip()
    if doc and para:
        return f"{doc} ¶{para}"
    return doc or para


def _known_doc_ids() -> set:
    """Legitimate case document ids — the ground truth for the citation EXISTENCE
    check. Taken from the pipeline matrix on disk BEFORE user additions are layered
    in, so the set is independent of whatever is being verified: a citation to a
    document that isn't part of the case bundle is genuinely caught rather than
    self-validating. Canonicalised via grounding_guard._canon_id."""
    path = _out("matrix.json")
    if not path.exists():
        path = DATA_DIR / "matrix.json"
    try:
        base = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    try:
        canon = _grounding_guard()._canon_id
    except Exception:
        canon = lambda s: str(s).strip().lower()
    docs: set = set()
    for row in base:
        for obj, key in [(row.get("proposition") or {}, "source")] + \
                [(l, "evidence_source") for l in row.get("links") or []]:
            doc = (obj.get(key) or {}).get("doc_id")
            if doc:
                docs.add(canon(doc))
    return docs


def _verify_matrix(matrix: list) -> dict:
    """Run the grounding guard over every citation and annotate each proposition
    and link in place with a `_verify` verdict the UI renders as a checkmark.

    Two deterministic checks, no model in the loop:
      • existence — the cited document must be a real case document (independent
        ground truth from `_known_doc_ids`); a fabricated doc id is rejected.
      • quote     — the quoted text must match the source. The full source bundle
        is regenerable and not shipped, so we replay the per-citation `quote_ok`
        the pipeline recorded — itself the grounding-guard quote check run against
        the real source text at extraction time.

    Returns a {available, total, verified, flagged} summary. Never raises — if the
    guard can't load, citations are simply left unannotated and the UI falls back
    to the per-link quote_ok flag."""
    try:
        gg = _grounding_guard()
    except Exception as exc:  # never let verification take down the matrix endpoint
        _log("verify", f"grounding guard unavailable: {type(exc).__name__}: {exc}")
        return {"available": False, "total": 0, "verified": 0, "flagged": 0}

    known_docs = _known_doc_ids()
    Status = gg.Status
    counters = {"total": 0, "verified": 0}

    def _verdict(src: dict | None, quote, quote_ok) -> dict:
        src = src or {}
        sid = _citation_id(src)
        doc = gg._canon_id(src.get("doc_id") or "") if src.get("doc_id") else ""
        quote = str(quote or "")
        if not doc or (known_docs and doc not in known_docs):
            res = gg.CheckResult(sid, quote, Status.FABRICATED_SOURCE, 0.0,
                                 f"cited document '{src.get('doc_id') or '?'}' is not in the case bundle")
        elif not quote.strip():
            res = gg.CheckResult(sid, quote, Status.NO_QUOTE, 1.0,
                                 "source exists but no quote was supplied to verify")
        elif quote_ok:
            res = gg.CheckResult(sid, quote, Status.EXACT, 1.0,
                                 "source exists and the quote matches it verbatim")
        else:
            res = gg.CheckResult(sid, quote, Status.MISQUOTE, 0.0,
                                 "quote not found in the cited source")
        counters["total"] += 1
        if res.ok:
            counters["verified"] += 1
        return {"ok": res.ok, "status": res.status.value,
                "score": round(res.score, 3), "detail": res.detail, "source_id": sid}

    for row in matrix:
        prop = row.get("proposition")
        if prop is not None:
            prop["_verify"] = _verdict(prop.get("source"), prop.get("quote"), prop.get("quote_ok"))
        for link in row.get("links") or []:
            link["_verify"] = _verdict(link.get("evidence_source"),
                                       link.get("quote"), link.get("quote_ok"))

    return {"available": True, "total": counters["total"],
            "verified": counters["verified"],
            "flagged": counters["total"] - counters["verified"]}


def _evidence_index(matrix: list) -> dict:
    """evidence_id -> {witness, quote, evidence_source} from existing links, so a
    user-added proposition that cites existing evidence shows its real detail."""
    idx: dict = {}
    for row in matrix:
        for l in row.get("links", []) or []:
            eid = l.get("evidence_id")
            if eid and eid not in idx:
                idx[eid] = {"witness": l.get("witness", ""), "quote": l.get("quote", ""),
                            "evidence_source": l.get("evidence_source", {})}
    return idx


def _apply_additions(matrix: list) -> list:
    """Merge user-added propositions and evidence (from additions.json) into a
    freshly-loaded matrix. Non-destructive: the snapshot on disk is untouched;
    additions are layered in on every load so they flow into the matrix, graph,
    stress test and every downstream feature."""
    additions = _load_additions()
    user_props = additions.get("propositions") or []
    user_evidence = additions.get("evidence") or []
    if not user_props and not user_evidence:
        return matrix

    by_id = {row["proposition"]["id"]: row for row in matrix if row.get("proposition")}
    ev_idx = _evidence_index(matrix)
    user_ev_by_id = {e["id"]: e for e in user_evidence}
    affected: set = set()

    def _link_for_evidence(eid, relation, confidence, rationale):
        meta = ev_idx.get(eid)
        if not meta and eid in user_ev_by_id:
            ue = user_ev_by_id[eid]
            meta = {"witness": ue.get("witness", ""), "quote": ue.get("quote", ""),
                    "evidence_source": {"doc_id": ue.get("doc_id", "User-added"),
                                        "page": None, "paragraph": ""}}
        meta = meta or {}
        return {"evidence_id": eid, "relation": relation,
                "confidence": confidence, "rationale": rationale,
                "quote": meta.get("quote", ""), "quote_ok": True,
                "witness": meta.get("witness", ""),
                "evidence_source": meta.get("evidence_source")
                or {"doc_id": "", "page": None, "paragraph": ""},
                "user_added": True}

    # 1. user evidence -> append a link onto each proposition it references
    for e in user_evidence:
        for lk in e.get("links", []) or []:
            pid = lk.get("proposition_id")
            row = by_id.get(pid)
            if not row:
                continue
            row.setdefault("links", []).append(_link_for_evidence(
                e["id"], lk.get("relation", "supportive"),
                lk.get("confidence", 0.7), lk.get("rationale", "")))
            affected.add(pid)

    # 2. user propositions -> new matrix rows (links may cite existing/user evidence)
    for p in user_props:
        links = [_link_for_evidence(lk.get("evidence_id"), lk.get("relation", "supportive"),
                                    lk.get("confidence", 0.7), lk.get("rationale", ""))
                 for lk in (p.get("links") or []) if lk.get("evidence_id")]
        row = {
            "proposition": {
                "id": p["id"], "type": p.get("type", "allegation"),
                "text": p.get("text", ""), "party": p.get("party", ""),
                "responds_to": p.get("responds_to", ""), "quote": "", "quote_ok": True,
                "source": {"doc_id": "User-added", "page": None, "paragraph": ""},
                "user_added": True,
            },
            "status": _status_from_links(links), "n_candidates": len(links), "links": links,
        }
        matrix.append(row)
        by_id[p["id"]] = row

    # 3. recompute status of existing rows that gained user evidence
    for pid in affected:
        row = by_id.get(pid)
        if row:
            row["status"] = _status_from_links(row.get("links", []))
    return matrix


def _build_summary(matrix: list) -> dict:
    """Compute a quick, deterministic narrative summary of the case from the matrix.

    Pure derivation from the matrix data (no LLM call) so the result is stable
    and cacheable. Returns a dict with a headline, a few narrative paragraphs,
    and the raw stats used to build them.
    """
    counts = {"supported": 0, "contested": 0, "undermined": 0, "MISSING": 0}
    parties: dict[str, int] = {}
    types: dict[str, int] = {}
    for row in matrix:
        s = row.get("status", "MISSING")
        counts[s] = counts.get(s, 0) + 1
        p = row.get("proposition", {})
        party = p.get("party") or "Unspecified"
        ptype = p.get("type") or "proposition"
        parties[party] = parties.get(party, 0) + 1
        types[ptype] = types.get(ptype, 0) + 1

    total = sum(counts.values())
    denom = total or 1
    readiness = round(
        (counts["supported"] * 1.0 + counts["contested"] * 0.5) / denom * 100, 1
    )

    def _phrase(mapping: dict[str, int]) -> str:
        items = sorted(mapping.items(), key=lambda kv: -kv[1])
        return ", ".join(f"{n} {k.lower()}" if k != "Unspecified" else f"{n} unspecified"
                         for k, n in items)

    type_phrase = _phrase(types)
    party_phrase = _phrase(parties)

    at_risk = counts["MISSING"] + counts["undermined"]
    headline = (
        f"{total} pleaded proposition{'' if total == 1 else 's'} analysed — "
        f"trial readiness {readiness}%."
    )

    paragraphs = [
        f"The bundle yields {total} pleaded proposition{'' if total == 1 else 's'} "
        f"({type_phrase}), split across {party_phrase}.",
        f"Of these, {counts['supported']} are supported by the evidence, "
        f"{counts['contested']} are contested, {counts['undermined']} are undermined "
        f"by adverse material, and {counts['MISSING']} have no supporting evidence at all.",
    ]
    if at_risk:
        paragraphs.append(
            f"{at_risk} proposition{'' if at_risk == 1 else 's'} "
            f"({counts['MISSING']} missing, {counts['undermined']} undermined) "
            "carry the most litigation risk and should be prioritised for further "
            "witness, expert or documentary support before trial or settlement."
        )
    else:
        paragraphs.append(
            "No propositions are missing or undermined — the pleaded case is, on its "
            "face, evidenced throughout, though contested points still warrant review."
        )

    return {
        "headline": headline,
        "paragraphs": paragraphs,
        "stats": {
            "total": total,
            "counts": counts,
            "by_party": parties,
            "by_type": types,
            "trial_readiness": readiness,
        },
    }


# ---------------------------------------------------------------------------
# AI case summary — narrative grounded in EU Cellar data, cached to disk.
# ---------------------------------------------------------------------------
# Curated legal-concept vocabulary. We search the EU Cellar repository for the
# subset of these that actually appear in the pleadings, so the retrieval is
# driven by the case rather than hard-coded to one dispute.
_LEGAL_CONCEPTS = [
    "misrepresentation", "breach of contract", "unfair contract terms",
    "consumer protection", "accounting", "software", "electronic evidence",
    "burden of proof", "negligence", "duty of care", "data protection",
    "liability", "good faith", "disclosure",
]


def _case_digest(matrix: list) -> str:
    """One line per pleaded proposition: id, type, party, status, text."""
    return "\n".join(
        f"[{(p := row['proposition']).get('id')}] "
        f"({p.get('type')}, {p.get('party')}, status={row.get('status')}) {p.get('text')}"
        for row in matrix
    )


def _offline_chat_reply(user_msg: str, matrix: list) -> str:
    """Grounded reply when the LLM is unavailable — keyword-match the pleadings
    so the assistant still says something true about the case (and emits [Pxxxx]
    citations the UI turns into chips)."""
    stop = {
        "what", "which", "where", "this", "that", "case", "tell", "show", "find",
        "about", "have", "with", "from", "they", "there", "help", "does", "could",
        "would", "your", "their", "been", "into", "than", "then", "them",
    }
    words = {w for w in re.findall(r"[a-z']{4,}", (user_msg or "").lower()) if w not in stop}
    scored = []
    for row in matrix:
        hay = (row.get("proposition", {}).get("text") or "").lower()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda t: -t[0])
    top = [r for _, r in scored[:3]]
    if not top:  # fall back to the riskiest propositions
        top = [r for r in matrix if r.get("status") in ("MISSING", "undermined")][:3]

    lines = ["Second Chair is offline (LLM unavailable — check the server log "
             "for the cause), but here is what the proof matrix shows on point:"]
    for row in top:
        p = row["proposition"]
        lines.append(f"• [{p.get('id')}] ({row.get('status')}) {p.get('text')}")
    if not top:
        lines.append("• No matching propositions found in the bundle.")
    lines.append("Set ANTHROPIC_API_KEY (and restart the server) to chat with "
                 "the full associate.")
    return "\n".join(lines)


_NAV_VIEWS = {
    "propositions": "Proof Matrix",
    "damages": "Financial overview",
    "argumentation": "Argumentation",
    "graph": "Case Graph",
    "builder": "Case Builder",
    "home": "home screen",
}

# Tool the chat model can call to switch the UI to another view. View ids MUST
# match the go() routes in index.html.
NAVIGATE_TOOL = {
    "name": "navigate",
    "description": (
        "Switch the app to a different view. Call this when the lawyer asks to go "
        "to / open / show a screen, or when another view is the best way to answer "
        "them. Views: propositions = proof matrix of pleaded allegations & denials; "
        "damages = the financial / quantum (money / loss) overview; argumentation = "
        "the generated legal arguments; graph = the case graph linking pleadings to "
        "evidence; builder = the case builder; home = the landing screen. Always "
        "also give a one-line spoken reply confirming where you're taking them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"view": {"type": "string", "enum": list(_NAV_VIEWS)}},
        "required": ["view"],
    },
}

# Keyword fallback so "go to the financial overview" still navigates on the
# offline (no-LLM) path.
_NAV_TRIGGERS = ("go to", "open", "show me", "show the", "take me", "switch to",
                 "navigate", "bring up", "jump to")
_NAV_KEYWORDS = [
    ("damages", ("financial", "damages", "quantum", "money", "loss", "compensation")),
    ("graph", ("graph", "network", "connections", "map of")),
    ("argumentation", ("argument", "argumentation", "case theory")),
    ("builder", ("builder", "build the case", "build my case", "assemble")),
    ("propositions", ("proposition", "proof matrix", "matrix", "allegation", "claims")),
    ("home", ("home", "landing", "start screen", "main screen")),
]


def _offline_navigate(user_msg: str) -> str | None:
    t = (user_msg or "").lower()
    if not any(trig in t for trig in _NAV_TRIGGERS):
        return None
    for view, kws in _NAV_KEYWORDS:
        if any(k in t for k in kws):
            return view
    return None


def _chat_reply(messages: list, matrix: list) -> tuple[str, str, str | None]:
    """Answer as 'Second Chair', grounded in the matrix. Returns
    (reply_text, source, navigate) — source is 'claude'|'offline', navigate is a
    view id to switch the UI to (or None)."""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    last_user = user_msgs[-1].get("content", "") if user_msgs else ""

    # Recently failed — answer instantly from the grounded offline path.
    if _llm_in_cooldown():
        _log("chat", f"in LLM cooldown ({_llm_down_until - time.monotonic():.0f}s "
                     "left) — returning offline reply")
        return _offline_chat_reply(last_user, matrix), "offline", _offline_navigate(last_user)

    try:
        import caselib  # lazy: anthropic SDK + .env
        # No retries on the interactive path: if the call errors (e.g. no credits
        # or no network), fail fast to the grounded offline reply instead of
        # making the user wait through SDK backoff.
        client = caselib.get_client().with_options(max_retries=0)
        stats = _build_summary(matrix)["stats"] if matrix else {"counts": {}}
        c = stats.get("counts", {})
        system = (
            "You are 'Second Chair', an AI litigation associate embedded in a "
            "Proof Matrix tool for a UK commercial dispute. You have memorised the "
            "pleaded propositions below (allegations and denials) and their "
            "evidential status from the proof matrix. Answer the lawyer's questions "
            "about the case, grounded strictly in this data. When you reference a "
            "proposition, cite it inline in square brackets exactly like [P0003]. "
            "Be concise, neutral and practical. When the lawyer asks to go to / "
            "open / show another screen (e.g. 'go to the financial overview'), use "
            "the navigate tool to switch the view.\n\n"
            f"PLEADED PROPOSITIONS:\n{_case_digest(matrix)}\n\n"
            f"PROOF-MATRIX TOTALS: {c.get('supported', 0)} supported, "
            f"{c.get('contested', 0)} contested, {c.get('undermined', 0)} undermined, "
            f"{c.get('MISSING', 0)} missing; trial readiness "
            f"{stats.get('trial_readiness', '?')}%."
        )
        api_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        _log("chat", f"-> calling LLM: model={caselib.MODEL} msgs={len(api_messages)} "
                     f"matrix={len(matrix)} rows last_user={last_user[:80]!r}")
        t0 = time.monotonic()
        msg = client.messages.create(
            model=caselib.MODEL,
            # Generous budget: adaptive thinking can consume most of a small cap
            # and leave no room for the answer (stop_reason=max_tokens, empty
            # text) — which previously showed up as a silent "offline".
            max_tokens=4000,
            thinking={"type": "adaptive"},
            tools=[NAVIGATE_TOOL],
            # Cache the case digest: it's identical across every turn of a chat.
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=api_messages,
        )
        dt = time.monotonic() - t0
        _log("chat", f"<- LLM responded in {dt:.1f}s: {_describe_response(msg)}")
        navigate = None
        for b in msg.content:
            if getattr(b, "type", None) == "tool_use" and getattr(b, "name", "") == "navigate":
                navigate = (b.input or {}).get("view")
        text = "\n".join(b.text for b in msg.content if b.type == "text").strip()
        if not text and navigate:
            text = f"Taking you to the {_NAV_VIEWS.get(navigate, navigate)}."
        if text or navigate:
            _log("chat", f"OK: {len(text)} chars, navigate={navigate} (source=claude)")
            return text, "claude", navigate
        # Reached the model but got no text (e.g. all budget spent on thinking,
        # stop_reason=max_tokens). This was previously silent — log it loudly.
        _log("chat", f"WARNING empty text despite stop_reason="
                     f"{getattr(msg, 'stop_reason', '?')}; falling back to offline. "
                     "Consider raising max_tokens.")
    except Exception as exc:
        _log("chat", f"LLM unavailable: {type(exc).__name__}: {exc}")
        _llm_mark_down()

    return _offline_chat_reply(last_user, matrix), "offline"


def _matrix_signature(matrix: list) -> str:
    """Stable short hash of the case content — changes only when propositions
    or their statuses change, so it makes a good cache key for the summary."""
    h = hashlib.sha256()
    for row in matrix:
        p = row.get("proposition", {})
        h.update(
            f"{p.get('id', '')}|{p.get('text', '')}|{row.get('status', '')}".encode("utf-8")
        )
    return h.hexdigest()[:16]


def _case_keywords(matrix: list, limit: int = 6) -> list[str]:
    """Pick legal-concept search terms that actually appear in the pleadings."""
    hay = " ".join(
        (row.get("proposition", {}).get("text") or "") for row in matrix
    ).lower()
    hits = [c for c in _LEGAL_CONCEPTS if c in hay]
    # Always have something to search on, even for an unusual case.
    if len(hits) < 2:
        hits = list(dict.fromkeys(hits + ["contract", "evidence"]))
    return hits[:limit]


def _cellar_search(terms: list[str], limit: int = 8, timeout: float = 8.0) -> list[dict]:
    """Query the EU Cellar SPARQL endpoint for English-language legal works whose
    title matches any of `terms` (Virtuoso full-text). Returns a list of
    {title, work, celex}. Never raises — retrieval is best-effort enrichment."""
    import urllib.parse
    import urllib.request

    if not terms:
        return []
    # Virtuoso bif:contains OR-query, e.g. '"misrepresentation" OR "accounting"'.
    contains = " OR ".join(f'"{t}"' for t in terms)
    query = (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT DISTINCT ?work ?title ?celex WHERE {\n"
        "  ?exp cdm:expression_title ?title .\n"
        f"  ?title bif:contains '{contains}' .\n"
        "  ?exp cdm:expression_uses_language"
        " <http://publications.europa.eu/resource/authority/language/ENG> .\n"
        "  ?exp cdm:expression_belongs_to_work ?work .\n"
        "  OPTIONAL { ?work cdm:resource_legal_id_celex ?celex }\n"
        f"}} LIMIT {limit}"
    )
    url = CELLAR_SPARQL + "?" + urllib.parse.urlencode({
        "query": query,
        "format": "application/sparql-results+json",
    })
    req = urllib.request.Request(url, headers={
        "Accept": "application/sparql-results+json",
        "User-Agent": "ProofMatrix/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network/SPARQL error — degrade gracefully
        print(f"  [cellar] retrieval skipped: {type(exc).__name__}: {exc}")
        return []

    out, seen = [], set()
    for b in data.get("results", {}).get("bindings", []):
        title = (b.get("title", {}) or {}).get("value", "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        out.append({
            "title": title,
            "work": (b.get("work", {}) or {}).get("value", ""),
            "celex": (b.get("celex", {}) or {}).get("value", ""),
        })
    return out


def _parse_narrative(text: str) -> tuple[str, list[str]]:
    """Parse the model's `HEADLINE: ...` + blank-line-separated paragraphs."""
    idx = text.upper().find("HEADLINE:")
    if idx != -1:
        text = text[idx + len("HEADLINE:"):]
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    if not blocks:
        return "", []
    headline = blocks[0].splitlines()[0].strip()
    # Anything after the headline's first line, plus the remaining blocks.
    rest = blocks[0].splitlines()[1:]
    paragraphs = [" ".join(rest).strip()] if any(rest) else []
    paragraphs += blocks[1:]
    paragraphs = [p for p in paragraphs if p]
    return headline, paragraphs[:6]


def _generate_narrative(matrix: list, stats: dict) -> dict | None:
    """Call Claude (web search + EU Cellar context) to produce a high-level,
    accurate narrative of what the case is about. Returns a dict with headline,
    paragraphs and the EU sources used — or None if the LLM call fails."""
    if _llm_in_cooldown():  # recently failed — skip the LLM (and Cellar) fast
        _log("summary", f"in LLM cooldown ({_llm_down_until - time.monotonic():.0f}s "
                        "left) — skipping LLM, using deterministic fallback")
        return None
    try:
        import caselib  # lazy: pulls in the anthropic SDK + loads ./.env
        client = caselib.get_client().with_options(max_retries=0)
    except Exception as exc:
        _log("summary", f"LLM unavailable: {type(exc).__name__}: {exc}")
        _llm_mark_down()
        return None

    # Compact digest of the pleaded case for the model.
    digest = "\n".join(
        f"[{(p := row['proposition']).get('id')}] "
        f"({p.get('type')}, {p.get('party')}, status={row.get('status')}) {p.get('text')}"
        for row in matrix
    )
    t_cellar = time.monotonic()
    sources = _cellar_search(_case_keywords(matrix))
    _log("summary", f"EU Cellar retrieval: {len(sources)} sources "
                    f"in {time.monotonic() - t_cellar:.1f}s")
    if sources:
        cellar_block = "\n".join(
            f"- {s['title']}" + (f" (CELEX {s['celex']})" if s["celex"] else "")
            for s in sources
        )
    else:
        cellar_block = "(no EU materials retrieved)"

    c = stats["counts"]
    system = (
        "You are a litigation analyst preparing a concise case summary for a UK "
        "commercial litigation team. You receive the pleaded propositions "
        "(allegations and denials) extracted from the pleadings, each tagged with "
        "its evidential status from a proof matrix, plus a list of potentially "
        "relevant EU legal materials retrieved from the EU Publications Office "
        "Cellar repository. Use the web_search tool to identify the real-world "
        "dispute these pleadings correspond to and to ground the background in "
        "accurate facts; if you cannot identify it, summarise from the pleadings "
        "alone without speculating. Be accurate, neutral and concise."
    )
    user = (
        f"PLEADED PROPOSITIONS ({stats['total']} total):\n{digest}\n\n"
        f"PROOF-MATRIX FIGURES: {c['supported']} supported, {c['contested']} "
        f"contested, {c['undermined']} undermined, {c['MISSING']} missing; "
        f"trial readiness {stats['trial_readiness']}%.\n\n"
        f"RELEVANT EU LEGAL MATERIALS (EU Cellar):\n{cellar_block}\n\n"
        "Write the summary in EXACTLY this format and nothing else:\n"
        "HEADLINE: <one sentence naming the dispute and its core issue>\n\n"
        "<Paragraph 1 — high level: what this case is actually about and the "
        "nature of the dispute>\n\n"
        "<Paragraph 2 — high level: who the parties are and the core "
        "claims and defences>\n\n"
        "<Paragraph 3 — the evidential picture: weave in the proof-matrix "
        "figures above and what they imply for trial; note any relevant EU "
        "materials if applicable>\n\n"
        "Keep each paragraph to 2-4 sentences. No markdown headings, no bullet "
        "points, no citation brackets."
    )

    # Cache the prompt: the same system + user prefix is re-sent on every
    # web-search pause-turn resume below.
    user_content = [{"type": "text", "text": user,
                     "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": user_content}]
    _log("summary", f"-> calling LLM: model={caselib.MODEL} matrix={len(matrix)} rows "
                    "(web_search enabled)")
    try:
        final = None
        t0 = time.monotonic()
        for i in range(6):  # resume across web-search server-tool pauses
            msg = client.messages.create(
                model=caselib.MODEL,
                max_tokens=2000,
                thinking={"type": "adaptive"},
                tools=[{"type": "web_search_20260209", "name": "web_search",
                        "max_uses": 5}],
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=messages,
            )
            _log("summary", f"   turn {i + 1}: {_describe_response(msg)}")
            if msg.stop_reason == "pause_turn":
                messages = [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": msg.content},
                ]
                final = msg
                continue
            final = msg
            break
        dt = time.monotonic() - t0
        text = "\n".join(b.text for b in final.content if b.type == "text")
        _log("summary", f"<- LLM done in {dt:.1f}s: {len(text)} chars of text")
    except Exception as exc:
        _log("summary", f"generation failed: {type(exc).__name__}: {exc}")
        _llm_mark_down()
        return None

    headline, paragraphs = _parse_narrative(text)
    if not paragraphs:
        _log("summary", "WARNING parsed 0 paragraphs from the model text; "
                        "using deterministic fallback")
        return None
    _log("summary", f"OK: headline + {len(paragraphs)} paragraphs (source=claude)")
    return {"headline": headline, "paragraphs": paragraphs, "sources": sources}


def _summary_response(matrix: list, refresh: bool = False) -> dict:
    """Return the case summary payload, using the disk cache when the matrix is
    unchanged. Always carries fresh deterministic stats; the narrative is the
    Claude-generated one when available, else the deterministic fallback."""
    stats = _build_summary(matrix)["stats"]
    sig = _matrix_signature(matrix)

    if not refresh and SUMMARY_FILE.exists():
        try:
            with open(SUMMARY_FILE, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("signature") == sig and cached.get("paragraphs"):
                return {**cached, "stats": stats, "cached": True}
        except (json.JSONDecodeError, OSError):
            pass

    narrative = _generate_narrative(matrix, stats)
    if narrative:
        record = {
            "signature": sig,
            "generated_by": "claude",
            "headline": narrative["headline"],
            "paragraphs": narrative["paragraphs"],
            "sources": narrative["sources"],
        }
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(SUMMARY_FILE, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return {**record, "stats": stats, "cached": False}

    # Fallback: deterministic narrative (no LLM). Not persisted, so we retry next time.
    det = _build_summary(matrix)
    return {
        "generated_by": "fallback",
        "headline": det["headline"],
        "paragraphs": det["paragraphs"],
        "sources": [],
        "stats": stats,
        "cached": False,
    }


def _load_goals() -> dict:
    """Load goals from disk (lazy), with in-memory cache. Returns the goals dict."""
    global _GOALS
    if _GOALS is not None:
        return _GOALS
    if GOALS_FILE.exists():
        try:
            with open(GOALS_FILE, encoding="utf-8") as fh:
                _GOALS = json.load(fh)
            return _GOALS
        except (json.JSONDecodeError, OSError):
            pass
    # No file or unreadable — return sentinel indicating no goals saved yet.
    return {}


def _save_goals(payload: dict) -> dict:
    """Persist goals to disk and update in-memory cache."""
    global _GOALS
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(GOALS_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    _GOALS = payload
    return _GOALS


# ---------------------------------------------------------------------------
# Generated arguments (Claude + Perplexity), cached by matrix signature.
# ---------------------------------------------------------------------------
def _arguments_response(matrix: list, refresh: bool = False) -> dict:
    """Return the linked-arguments payload, using the disk cache while the matrix
    is unchanged. The LLM (and Perplexity authority lookups) only run on refresh
    or a case change. Empty/unavailable results are not cached, so they retry."""
    sig = _matrix_signature(matrix)
    if not refresh and ARGUMENTS_FILE.exists():
        try:
            with open(ARGUMENTS_FILE, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("signature") == sig and cached.get("arguments"):
                return {**cached, "cached": True}
        except (json.JSONDecodeError, OSError):
            pass

    try:
        import arguments_gen
        result = arguments_gen.generate_arguments(matrix)
    except Exception as exc:
        _log("arguments", f"generation failed: {type(exc).__name__}: {exc}")
        result = {"arguments": [], "generated_by": "unavailable"}

    record = {"signature": sig, **result}
    if result.get("arguments") and result.get("generated_by") == "claude":
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(ARGUMENTS_FILE, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return {**record, "cached": False}


def _quantum_response(matrix: list, refresh: bool = False) -> dict:
    """Return the quantum-assessment payload, cached by matrix signature. The LLM
    only runs on refresh or a case change; empty results aren't cached."""
    sig = _matrix_signature(matrix)
    if not refresh and QUANTUM_FILE.exists():
        try:
            with open(QUANTUM_FILE, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("signature") == sig and cached.get("methods"):
                return {**cached, "cached": True}
        except (json.JSONDecodeError, OSError):
            pass

    try:
        import quantum_gen
        result = quantum_gen.generate_quantum(matrix)
    except Exception as exc:
        _log("quantum", f"generation failed: {type(exc).__name__}: {exc}")
        result = {"methods": [], "generated_by": "unavailable"}

    record = {"signature": sig, **result}
    if result.get("methods") and result.get("generated_by") == "claude":
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(QUANTUM_FILE, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return {**record, "cached": False}


def _load_arguments_cached() -> list:
    """Return the cached generated arguments (no LLM call) for the stress test
    to attack; [] if none have been generated yet."""
    if ARGUMENTS_FILE.exists():
        try:
            with open(ARGUMENTS_FILE, encoding="utf-8") as fh:
                return (json.load(fh) or {}).get("arguments", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _load_additions() -> dict:
    """User-added propositions/evidence ({} if none)."""
    if ADDITIONS_FILE.exists():
        try:
            with open(ADDITIONS_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_additions(payload: dict) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ADDITIONS_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


def _next_addition_id(existing: list, prefix: str) -> str:
    """Next free id like U0007 / UE0007, scanning existing ids for the max."""
    n = 0
    for item in existing:
        m = re.match(rf"^{prefix}(\d+)$", str(item.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}{n + 1:04d}"


def _add_addition(kind: str, body: dict) -> dict:
    """Append a user proposition or evidence item; returns the stored record.
    Links are normalised; ids are assigned (U#### / UE####)."""
    store = _load_additions()
    store.setdefault("propositions", [])
    store.setdefault("evidence", [])

    def _links(items, ref_key):
        out = []
        for lk in items or []:
            ref = (lk.get(ref_key) or "").strip()
            rel = lk.get("relation", "supportive")
            if ref and rel in ("supportive", "adverse", "neutral"):
                out.append({ref_key: ref, "relation": rel,
                            "confidence": float(lk.get("confidence", 0.7) or 0.7),
                            "rationale": (lk.get("rationale") or "").strip()})
        return out

    if kind == "proposition":
        rec = {
            "id": _next_addition_id(store["propositions"], "U"),
            "type": body.get("type", "allegation"),
            "text": (body.get("text") or "").strip(),
            "party": (body.get("party") or "").strip(),
            "responds_to": (body.get("responds_to") or "").strip(),
            "links": _links(body.get("links"), "evidence_id"),
        }
        store["propositions"].append(rec)
    else:  # evidence
        rec = {
            "id": _next_addition_id(store["evidence"], "UE"),
            "assertion": (body.get("assertion") or body.get("text") or "").strip(),
            "witness": (body.get("witness") or "").strip(),
            "quote": (body.get("quote") or "").strip(),
            "doc_id": (body.get("doc_id") or "User-added").strip(),
            "links": _links(body.get("links"), "proposition_id"),
        }
        store["evidence"].append(rec)
    _save_additions(store)
    return rec


def _load_case() -> dict:
    """Load the saved 'built case' (selected propositions/evidence + notes)."""
    if CASE_FILE.exists():
        try:
            with open(CASE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_case(payload: dict) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CASE_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/", "/index.html"):
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path in ("/timeline", "/timeline.html"):
            self._serve_file(STATIC_DIR / "timeline.html", "text/html; charset=utf-8")
        elif path == "/favicon.svg":
            self._serve_file(STATIC_DIR / "favicon.svg", "image/svg+xml")
        elif path == "/api/matrix":
            self._api_matrix()
        elif path == "/api/verify":
            self._api_verify()
        elif path == "/api/graph":
            self._api_graph()
        elif path == "/api/summary":
            self._api_summary()
        elif path == "/api/arguments":
            self._api_arguments()
        elif path == "/api/quantum":
            self._api_quantum()
        elif path == "/api/stress":
            self._api_stress()
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/goals":
            self._api_goals_get()
        elif path == "/api/case":
            self._api_case_get()
        elif path == "/api/additions":
            self._api_additions_get()
        else:
            self._404()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/goals":
            self._api_goals_post()
        elif path == "/api/chat":
            self._api_chat()
        elif path == "/api/case":
            self._api_case_post()
        elif path == "/api/additions":
            self._api_additions_post()
        else:
            self._404()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/additions":
            _save_additions({"propositions": [], "evidence": []})
            self._json_response(200, json.dumps({"ok": True}))
        else:
            self._404()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ---- static files -------------------------------------------------------

    def _serve_file(self, filepath: pathlib.Path, content_type: str):
        if not filepath.exists():
            self._404()
            return
        data = filepath.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(data)
        except _DISCONNECT_ERRORS:
            pass  # client went away mid-response

    # ---- API ----------------------------------------------------------------

    def _api_matrix(self):
        """GET /api/matrix — full matrix with summary stats."""
        matrix, err = _load_matrix()
        if err:
            self._json_response(503, json.dumps({"ok": False, "error": err}))
            return

        # Compute summary
        counts = {"supported": 0, "contested": 0, "undermined": 0, "MISSING": 0}
        for row in matrix:
            s = row.get("status", "MISSING")
            counts[s] = counts.get(s, 0) + 1

        total = sum(counts.values()) or 1
        # Trial readiness: supported=1.0, contested=0.5, undermined=0.0, MISSING=0.0
        readiness = round(
            (counts["supported"] * 1.0 + counts["contested"] * 0.5) / total * 100,
            1,
        )

        # Grounding guard: verify every citation and annotate each proposition /
        # link in place with a `_verify` verdict the UI renders as a checkmark.
        verification = _verify_matrix(matrix)

        # Cacheable: the matrix is static between pipeline runs, so let the
        # browser cache it (ETag gives a cheap 304 when it hasn't changed).
        self._json_cacheable(json.dumps({
            "ok": True,
            "summary": {
                "total": total,
                "counts": counts,
                "trial_readiness": readiness,
            },
            "verification": verification,
            "matrix": matrix,
        }), max_age=60)

    def _api_verify(self):
        """GET /api/verify — run the grounding guard over the whole matrix and
        return the summary plus the list of any quarantined (failed) citations.
        Lets you inspect the anti-hallucination gate independently of the UI."""
        matrix, err = _load_matrix()
        if err:
            self._json_response(503, json.dumps({"ok": False, "error": err}))
            return
        summary = _verify_matrix(matrix)
        flagged = []
        for row in matrix:
            prop = row.get("proposition") or {}
            for kind, obj in [("proposition", prop)] + \
                    [("evidence", l) for l in row.get("links") or []]:
                v = obj.get("_verify")
                if v and not v["ok"]:
                    flagged.append({
                        "proposition_id": prop.get("id"),
                        "kind": kind,
                        "id": obj.get("id") or obj.get("evidence_id"),
                        "source_id": v["source_id"],
                        "status": v["status"],
                        "detail": v["detail"],
                    })
        self._json_cacheable(json.dumps({
            "ok": True, "summary": summary, "flagged": flagged,
        }), max_age=60)

    def _api_graph(self):
        """GET /api/graph — the case as a graph (propositions + evidence nodes,
        SUPPORTS/UNDERMINES/NEUTRAL edges) for the Case Graph view and Neo4j.

        Derived live from the matrix via graph_export so it always matches the
        current data; cacheable since it changes only when the matrix changes.
        """
        matrix, err = _load_matrix()
        if err:
            self._json_response(503, json.dumps({"ok": False, "error": err}))
            return
        try:
            import graph_export  # repo root is on sys.path (see top of file)
            graph = graph_export.build_graph(matrix)
        except Exception as exc:  # never let a graph error take down the app
            print(f"  [graph] build failed: {type(exc).__name__}: {exc}")
            self._json_response(500, json.dumps(
                {"ok": False, "error": f"graph build failed: {exc}"}))
            return
        self._json_cacheable(json.dumps({"ok": True, **graph}), max_age=300)

    def _api_summary(self):
        """GET /api/summary — AI case summary.

        Explains what the case is actually about (high-level), grounded by web
        search and EU Cellar legal materials, then folds in the proof-matrix
        stats. The narrative is cached to disk keyed by a matrix signature so we
        only call the LLM when the case changes; the HTTP response additionally
        carries an ETag + max-age for browser/proxy caching. ?refresh=1 forces
        regeneration.
        """
        matrix, err = _load_matrix()
        if err:
            self._json_response(503, json.dumps({"ok": False, "error": err}))
            return
        params = parse_qs(urlparse(self.path).query)
        refresh = params.get("refresh", ["0"])[0] in ("1", "true", "yes")
        _log("summary", f"GET /api/summary received (refresh={refresh})")
        t0 = time.monotonic()
        summary = _summary_response(matrix, refresh=refresh)
        _log("summary", f"GET /api/summary done in {time.monotonic() - t0:.1f}s "
                        f"-> generated_by={summary.get('generated_by')} "
                        f"cached={summary.get('cached')}")
        payload = json.dumps({"ok": True, **summary})
        self._json_cacheable(payload, max_age=300)

    def _api_arguments(self):
        """GET /api/arguments — Claude-drafted arguments linked to the
        propositions and evidence they rely on, with Perplexity authority.
        Cached by matrix signature; ?refresh=1 regenerates."""
        matrix, err = _load_matrix()
        if err:
            self._json_response(503, json.dumps({"ok": False, "error": err}))
            return
        params = parse_qs(urlparse(self.path).query)
        refresh = params.get("refresh", ["0"])[0] in ("1", "true", "yes")
        _log("arguments", f"GET /api/arguments (refresh={refresh})")
        t0 = time.monotonic()
        result = _arguments_response(matrix, refresh=refresh)
        _log("arguments", f"-> {len(result.get('arguments', []))} arguments "
                          f"({result.get('generated_by')}) in {time.monotonic() - t0:.1f}s")
        self._json_cacheable(json.dumps({"ok": True, **result}), max_age=60)

    def _api_quantum(self):
        """GET /api/quantum — preliminary quantum (damages) assessment: competing
        valuation methodologies grounded in the matrix, each linked to the
        propositions it depends on. Cached by signature; ?refresh=1 regenerates."""
        matrix, err = _load_matrix()
        if err:
            self._json_response(503, json.dumps({"ok": False, "error": err}))
            return
        params = parse_qs(urlparse(self.path).query)
        refresh = params.get("refresh", ["0"])[0] in ("1", "true", "yes")
        _log("quantum", f"GET /api/quantum (refresh={refresh})")
        t0 = time.monotonic()
        result = _quantum_response(matrix, refresh=refresh)
        _log("quantum", f"-> {len(result.get('methods', []))} methods "
                        f"({result.get('generated_by')}) in {time.monotonic() - t0:.1f}s")
        self._json_response(200, json.dumps({"ok": True, **result}))

    def _api_stress(self):
        """GET /api/stress — run the case-theory stress-test suite over the
        matrix (and the saved built case, if any). ?adversarial=1 adds the LLM
        red-team + Perplexity contrary-authority checks (slower)."""
        matrix, err = _load_matrix()
        if err:
            self._json_response(503, json.dumps({"ok": False, "error": err}))
            return
        params = parse_qs(urlparse(self.path).query)
        adversarial = params.get("adversarial", ["0"])[0] in ("1", "true", "yes")
        try:
            import stress_test
            report = stress_test.run(matrix, case=_load_case(),
                                     arguments=_load_arguments_cached(),
                                     adversarial=adversarial)
        except Exception as exc:
            _log("stress", f"failed: {type(exc).__name__}: {exc}")
            self._json_response(500, json.dumps(
                {"ok": False, "error": f"stress test failed: {exc}"}))
            return
        self._json_response(200, json.dumps({"ok": True, **report}))

    def _api_case_get(self):
        """GET /api/case — the saved built case (selected propositions/evidence)."""
        case = _load_case()
        self._json_response(200, json.dumps({"ok": True, "has_case": bool(case),
                                             "case": case}))

    def _api_case_post(self):
        """POST /api/case — persist the built case. Body is the case object."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._json_response(400, json.dumps({"ok": False, "error": "Empty body"}))
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json_response(400, json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"}))
            return
        if not isinstance(payload, dict):
            self._json_response(400, json.dumps({"ok": False, "error": "Body must be an object"}))
            return
        payload["saved_at"] = datetime.now(timezone.utc).isoformat()
        _save_case(payload)
        self._json_response(200, json.dumps({"ok": True, "saved": payload}))

    def _api_additions_get(self):
        """GET /api/additions — list user-added propositions and evidence."""
        store = _load_additions()
        self._json_response(200, json.dumps({
            "ok": True,
            "propositions": store.get("propositions", []),
            "evidence": store.get("evidence", []),
        }))

    def _api_additions_post(self):
        """POST /api/additions — add a proposition or evidence item.

        Body: {"kind": "proposition"|"evidence", ...fields..., "links": [...]}.
        For a proposition, links reference evidence: {evidence_id, relation, ...}.
        For evidence, links reference propositions: {proposition_id, relation, ...}.
        """
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._json_response(400, json.dumps({"ok": False, "error": "Empty body"}))
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json_response(400, json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"}))
            return

        kind = body.get("kind")
        if kind not in ("proposition", "evidence"):
            self._json_response(400, json.dumps(
                {"ok": False, "error": "kind must be 'proposition' or 'evidence'"}))
            return
        text = (body.get("text") or body.get("assertion") or "").strip()
        if not text:
            self._json_response(400, json.dumps(
                {"ok": False, "error": "text/assertion is required"}))
            return

        rec = _add_addition(kind, body)
        _log("additions", f"added {kind} {rec['id']} "
                          f"({len(rec.get('links', []))} link(s))")
        self._json_response(200, json.dumps({"ok": True, "id": rec["id"], "record": rec}))

    def _api_chat(self):
        """POST /api/chat — converse with 'Second Chair', grounded in the matrix.

        Body: {"messages": [{"role": "user"|"assistant", "content": "..."}]}.
        Returns {"ok": true, "reply": "...", "source": "claude"|"offline"}.
        """
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._json_response(400, json.dumps({"ok": False, "error": "Empty body"}))
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json_response(400, json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"}))
            return

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._json_response(400, json.dumps(
                {"ok": False, "error": "'messages' must be a non-empty list"}))
            return

        matrix, _err = _load_matrix()
        _log("chat", f"POST /api/chat received: {len(messages)} message(s)")
        t0 = time.monotonic()
        reply, source, navigate = _chat_reply(messages, matrix or [])
        _log("chat", f"POST /api/chat done in {time.monotonic() - t0:.1f}s -> "
                     f"source={source} navigate={navigate}")
        self._json_response(200, json.dumps({
            "ok": True, "reply": reply, "source": source, "navigate": navigate}))

    def _api_status(self):
        """GET /api/status — lightweight health check."""
        self._json_response(200, json.dumps({
            "ok": True,
            "matrix_json_exists": _out("matrix.json").exists()
            or (DATA_DIR / "matrix.json").exists(),
            "goals_exist": GOALS_FILE.exists(),
        }))

    def _api_goals_get(self):
        """GET /api/goals — return current saved goals (or empty sentinel)."""
        goals = _load_goals()
        self._json_response(200, json.dumps({
            "ok": True,
            "has_goals": bool(goals),
            "goals": goals,
        }))

    def _api_goals_post(self):
        """POST /api/goals — accept and persist goals JSON."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._json_response(400, json.dumps({"ok": False, "error": "Empty body"}))
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json_response(400, json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"}))
            return

        goals_list = payload.get("goals")
        if not isinstance(goals_list, list):
            self._json_response(400, json.dumps({"ok": False, "error": "'goals' must be a list"}))
            return

        # Normalise: ensure each entry has id + text.
        normalised = []
        for i, g in enumerate(goals_list):
            if isinstance(g, str):
                normalised.append({"id": f"g{i + 1}", "text": g.strip()})
            elif isinstance(g, dict) and g.get("text"):
                normalised.append({
                    "id": g.get("id", f"g{i + 1}"),
                    "text": g["text"].strip(),
                })
        # Filter empties.
        normalised = [g for g in normalised if g["text"]]

        if not normalised:
            self._json_response(400, json.dumps({"ok": False, "error": "No non-empty goals provided"}))
            return

        record = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "goals": normalised,
        }
        _save_goals(record)

        self._json_response(200, json.dumps({"ok": True, "saved": record}))

    # ---- helpers ------------------------------------------------------------

    def _json_response(self, status: int, payload: str):
        data = payload.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(data)
        except _DISCONNECT_ERRORS:
            pass

    def _json_cacheable(self, payload: str, max_age: int = 300):
        """Send JSON with caching headers (ETag + Cache-Control), honouring
        If-None-Match with a 304 so unchanged content isn't re-sent."""
        data = payload.encode("utf-8")
        etag = '"%s"' % hashlib.sha256(data).hexdigest()[:16]

        try:
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", f"public, max-age={max_age}")
                self._cors_headers()
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", f"public, max-age={max_age}")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(data)
        except _DISCONNECT_ERRORS:
            pass

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _404(self):
        self._json_response(404, json.dumps({"error": "Not found"}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    """Threaded so a slow LLM call (chat/summary) can't block other requests,
    and so a crash in one request thread never takes the process down."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # A client that hangs up mid-response is normal — don't log a traceback.
        if isinstance(sys.exc_info()[1], _DISCONNECT_ERRORS):
            return
        super().handle_error(request, client_address)


def main():
    server = Server(("", PORT), Handler)
    print(f"Proof Matrix running at http://localhost:{PORT}")
    print(f"Data directory: {OUT_DIR.resolve()}")
    print(f"Goals file: {GOALS_FILE.resolve()}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
