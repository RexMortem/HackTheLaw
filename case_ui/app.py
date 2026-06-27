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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Hosts (e.g. Render) inject the port to bind via $PORT; default for local dev.
PORT = int(os.environ.get("PORT", "8001"))

# Resolve paths relative to the repo root (parent of this file's directory).
_HERE = pathlib.Path(__file__).parent
REPO_ROOT = _HERE.parent
OUT_DIR = REPO_ROOT / "out"
STATIC_DIR = _HERE
DATA_DIR = _HERE / "data"

GOALS_FILE = DATA_DIR / "goals.json"
# Cached AI case summary (narrative + retrieved EU legal sources), keyed by a
# signature of the matrix so we only re-call the LLM when the case data changes.
SUMMARY_FILE = DATA_DIR / "summary.json"

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
        return json.load(fh), None


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

    lines = ["Second Chair is offline (no API credits), but here is what the "
             "proof matrix shows on point:"]
    for row in top:
        p = row["proposition"]
        lines.append(f"• [{p.get('id')}] ({row.get('status')}) {p.get('text')}")
    if not top:
        lines.append("• No matching propositions found in the bundle.")
    lines.append("Add Anthropic API credits to chat with the full associate.")
    return "\n".join(lines)


def _chat_reply(messages: list, matrix: list) -> tuple[str, str]:
    """Answer the lawyer's question as 'Second Chair', grounded in the matrix.
    Returns (reply_text, source) where source is 'claude' or 'offline'."""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    last_user = user_msgs[-1].get("content", "") if user_msgs else ""

    try:
        import caselib  # lazy: anthropic SDK + .env
        client = caselib.get_client()
        stats = _build_summary(matrix)["stats"] if matrix else {"counts": {}}
        c = stats.get("counts", {})
        system = (
            "You are 'Second Chair', an AI litigation associate embedded in a "
            "Proof Matrix tool for a UK commercial dispute. You have memorised the "
            "pleaded propositions below (allegations and denials) and their "
            "evidential status from the proof matrix. Answer the lawyer's questions "
            "about the case, grounded strictly in this data. When you reference a "
            "proposition, cite it inline in square brackets exactly like [P0003]. "
            "Be concise, neutral and practical.\n\n"
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
        msg = client.messages.create(
            model=caselib.MODEL,
            max_tokens=1200,
            thinking={"type": "adaptive"},
            # Cache the case digest: it's identical across every turn of a chat.
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=api_messages,
        )
        text = "\n".join(b.text for b in msg.content if b.type == "text").strip()
        if text:
            return text, "claude"
    except Exception as exc:
        print(f"  [chat] LLM unavailable: {type(exc).__name__}: {exc}")

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
    try:
        import caselib  # lazy: pulls in the anthropic SDK + loads ./.env
        client = caselib.get_client()
    except Exception as exc:
        print(f"  [summary] LLM unavailable: {type(exc).__name__}: {exc}")
        return None

    # Compact digest of the pleaded case for the model.
    digest = "\n".join(
        f"[{(p := row['proposition']).get('id')}] "
        f"({p.get('type')}, {p.get('party')}, status={row.get('status')}) {p.get('text')}"
        for row in matrix
    )
    sources = _cellar_search(_case_keywords(matrix))
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
    try:
        final = None
        for _ in range(6):  # resume across web-search server-tool pauses
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
            if msg.stop_reason == "pause_turn":
                messages = [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": msg.content},
                ]
                final = msg
                continue
            final = msg
            break
        text = "\n".join(b.text for b in final.content if b.type == "text")
    except Exception as exc:
        print(f"  [summary] generation failed: {type(exc).__name__}: {exc}")
        return None

    headline, paragraphs = _parse_narrative(text)
    if not paragraphs:
        return None
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
        elif path == "/api/matrix":
            self._api_matrix()
        elif path == "/api/summary":
            self._api_summary()
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/goals":
            self._api_goals_get()
        else:
            self._404()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/goals":
            self._api_goals_post()
        elif path == "/api/chat":
            self._api_chat()
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
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

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

        self._json_response(200, json.dumps({
            "ok": True,
            "summary": {
                "total": total,
                "counts": counts,
                "trial_readiness": readiness,
            },
            "matrix": matrix,
        }))

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
        summary = _summary_response(matrix, refresh=refresh)
        payload = json.dumps({"ok": True, **summary})
        self._json_cacheable(payload, max_age=300)

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
        reply, source = _chat_reply(messages, matrix or [])
        self._json_response(200, json.dumps({"ok": True, "reply": reply, "source": source}))

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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _json_cacheable(self, payload: str, max_age: int = 300):
        """Send JSON with caching headers (ETag + Cache-Control), honouring
        If-None-Match with a 304 so unchanged content isn't re-sent."""
        data = payload.encode("utf-8")
        etag = '"%s"' % hashlib.sha256(data).hexdigest()[:16]

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

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _404(self):
        self._json_response(404, json.dumps({"error": "Not found"}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    server = HTTPServer(("", PORT), Handler)
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
