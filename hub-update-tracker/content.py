"""
Content extraction + LO coverage for the Endocrine Hub Update Tracker.

Pulls text and learning-objective items out of source files and hub pages,
and computes which source LOs are not yet reflected in each hub page.

Exports
-------
extract_source(path, sha256) -> {"text": str, "los": [str]}
extract_hub(path) -> {"text": str}
coverage_report(lecture_los, hub_text) -> {"missing": [...], "covered": [...], ...}
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Optional deps — loaded lazily so the tracker still runs without them
try:
    import docx  # python-docx
except ImportError:  # pragma: no cover
    docx = None

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class ContentCache:
    """Disk-backed cache keyed by SHA-256 of the source file."""
    def __init__(self, cache_path: Path):
        self.path = cache_path
        try:
            self.data = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        except Exception:
            self.data = {}

    def get(self, sha: str) -> Optional[dict]:
        return self.data.get(sha)

    def put(self, sha: str, value: dict) -> None:
        value = dict(value)
        value["cached_at"] = datetime.now().isoformat(timespec="seconds")
        self.data[sha] = value

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2))


# ---------------------------------------------------------------------------
# HTML text extraction
# ---------------------------------------------------------------------------

class _HtmlText(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: List[str] = []
        self._skip_depth = 0
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
    def handle_data(self, data):
        if self._skip_depth == 0:
            s = data.strip()
            if s:
                self.chunks.append(s)

def extract_html(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    p = _HtmlText()
    p.feed(raw)
    text = " ".join(p.chunks)
    text = re.sub(r"\s+", " ", text).strip()
    return {"text": text}

def extract_hub(path: Path) -> dict:
    return extract_html(path)


# ---------------------------------------------------------------------------
# DOCX extraction + LO parsing
# ---------------------------------------------------------------------------

# Headings that introduce the LO list
LO_PREAMBLE_RE = re.compile(
    r"at the end of the (lecture|video|session|module|activity|class|reading)s?\s+"
    r"students should be able to",
    re.IGNORECASE,
)

# Minimum length for a paragraph to be treated as an LO
_LO_MIN_LEN = 20

# Skip these obviously-not-LOs
_LO_SKIP_PATTERNS = [
    re.compile(r"^(textbook|reading|reference|assessment|homework|assignment)s?\b", re.I),
    re.compile(r"^(suggested )?readings?\b", re.I),
]


def extract_docx(path: Path) -> dict:
    """Return {"text": full_text, "los": [list of LO strings]}."""
    if docx is None:
        # Fallback: extract raw XML text
        return _extract_docx_fallback(path)

    try:
        d = docx.Document(str(path))
    except Exception:
        return _extract_docx_fallback(path)

    paragraphs: List[Tuple[str, str]] = []  # (style, text)
    for p in d.paragraphs:
        t = (p.text or "").strip()
        if not t:
            continue
        paragraphs.append((p.style.name if p.style else "Normal", t))

    full_text = "\n".join(t for _, t in paragraphs)
    los = _parse_los(paragraphs)
    return {"text": full_text, "los": los}


def _parse_los(paragraphs: List[Tuple[str, str]]) -> List[str]:
    """Collect 'Normal' paragraphs that follow the LO preamble heading."""
    los: List[str] = []
    after_preamble = False
    for style, text in paragraphs:
        if LO_PREAMBLE_RE.search(text):
            after_preamble = True
            continue
        if not after_preamble:
            continue
        if style.lower().startswith("heading"):
            continue
        if _is_skippable(text):
            continue
        # Sub-section headers end with ":" and don't start with an LO verb
        if text.rstrip().endswith(":") and not _starts_with_lo_verb(text):
            continue
        if len(text) < _LO_MIN_LEN:
            continue
        los.append(_clean_lo(text))
    # Dedupe while preserving order
    seen = set()
    out = []
    for lo in los:
        k = lo.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(lo)
    return out


def _clean_lo(text: str) -> str:
    # Strip leading numbers / bullets
    text = re.sub(r"^\s*(?:\d+[.)]\s+|[•·●○◦‣▪▫⁃●\-–—]\s+|\[\s*\]\s+)", "", text)
    return text.strip()


_LO_VERBS = {
    "describe", "identify", "explain", "understand", "know", "discuss",
    "list", "state", "define", "recognize", "predict", "distinguish",
    "compare", "classify", "demonstrate", "outline", "summarize",
    "illustrate", "apply", "evaluate", "analyze", "interpret", "assess",
    "diagram", "differentiate", "relate", "derive", "solve", "determine",
    "calculate", "draw", "characterize", "contrast", "label",
}

def _starts_with_lo_verb(text: str) -> bool:
    m = re.match(r"\s*(\w+)", text)
    return bool(m and m.group(1).lower() in _LO_VERBS)


def _is_skippable(text: str) -> bool:
    t = text.strip()
    for pat in _LO_SKIP_PATTERNS:
        if pat.search(t):
            return True
    return False


def _extract_docx_fallback(path: Path) -> dict:
    """Crude fallback: unzip docx and read document.xml."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception:
        return {"text": "", "los": []}
    # Strip tags, keep text
    text = re.sub(r"<[^>]+>", " ", xml)
    text = re.sub(r"\s+", " ", text).strip()
    return {"text": text, "los": []}  # can't reliably parse LOs without structure


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def extract_pdf(path: Path, max_pages: int = 50) -> dict:
    """
    Extract text from a PDF. We don't try to parse LOs — PDFs are slides.

    Caps extraction at `max_pages` pages to keep first-time scans feasible.
    Slide decks rarely need more than 50 pages for keyword coverage.
    """
    if pdfplumber is None:
        return {"text": ""}
    try:
        pages: List[str] = []
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages):
                if i >= max_pages:
                    break
                t = page.extract_text() or ""
                if t.strip():
                    pages.append(t)
        text = "\n".join(pages)
        text = re.sub(r"\s+", " ", text).strip()
        return {"text": text, "pages": len(pages)}
    except Exception as e:
        return {"text": "", "error": str(e)}


# ---------------------------------------------------------------------------
# Unified source extractor
# ---------------------------------------------------------------------------

def extract_source(path: Path, sha: str, cache: ContentCache) -> dict:
    """
    Extract content from a source file. Uses the cache keyed on sha256.
    Returns {"text": str, "los": [str], "kind": "docx"|"pdf"|"other"}.
    """
    cached = cache.get(sha)
    if cached is not None:
        return cached

    suf = path.suffix.lower()
    if suf == ".docx" or suf == ".doc":
        out = extract_docx(path)
        out.setdefault("los", [])
        out["kind"] = "docx"
    elif suf == ".pdf":
        out = extract_pdf(path)
        out["los"] = []
        out["kind"] = "pdf"
    else:
        out = {"text": "", "los": [], "kind": "other"}

    cache.put(sha, out)
    return out


# ---------------------------------------------------------------------------
# Coverage scoring
# ---------------------------------------------------------------------------

# Light stopword list — we want to keep medical terms even if short.
_STOPWORDS = set("""
a an the and or but of in on for to with by as at from into onto upon about
is are was were be been being am do does did doing have has had having
this that these those it its their there here
can may might must should would will shall could
if then so than also more less most least very such not no nor
all any each every some many few several both either neither one two three
how why what which who whom when where
describe identify explain understand know discuss list state define recognize
predict distinguish compare classify demonstrate outline summarize illustrate
between among including include includes e.g. i.e. etc
""".split())

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']{2,}")  # at least 3 chars

def _tokens(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]

def _content_tokens(text: str) -> List[str]:
    return [t for t in _tokens(text) if t not in _STOPWORDS and len(t) >= 4]

def _normalize_blob(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def coverage_for_lo(lo: str, hub_blob_normalized: str, hub_tokens: set) -> dict:
    """
    Score how well a single LO is represented in a hub page.
    Returns {"lo": str, "score": 0..1, "matched_tokens": [...], "missing_tokens": [...]}
    """
    toks = _content_tokens(lo)
    if not toks:
        return {"lo": lo, "score": 1.0, "matched_tokens": [], "missing_tokens": []}

    matched: List[str] = []
    missing: List[str] = []
    for t in toks:
        if t in hub_tokens:
            matched.append(t)
            continue
        # Fallback: check substring (handles plurals, hyphenation)
        if t in hub_blob_normalized:
            matched.append(t)
        else:
            # Try stemming a bit: drop trailing 's', 'es', 'ing', 'ed'
            root = _light_stem(t)
            if root != t and (root in hub_tokens or root in hub_blob_normalized):
                matched.append(t)
            else:
                missing.append(t)

    score = len(matched) / len(toks)
    return {
        "lo": lo,
        "score": round(score, 2),
        "matched_tokens": matched,
        "missing_tokens": missing,
    }


_STEM_SUFFIXES = ("ing", "ed", "es", "s")

def _light_stem(tok: str) -> str:
    for suf in _STEM_SUFFIXES:
        if len(tok) > len(suf) + 3 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def coverage_report(los: List[str], hub_text: str,
                    threshold: float = 0.6) -> dict:
    """
    For every LO in `los`, decide whether it's "covered" by `hub_text`.
    An LO is missing if fewer than `threshold` of its content tokens are present.

    Returns:
      {
        "threshold": 0.6,
        "total": N,
        "covered": [{"lo","score",...}],
        "missing": [{"lo","score",...}],
      }
    """
    blob = _normalize_blob(hub_text)
    hub_tokens = set(_content_tokens(hub_text))

    covered: List[dict] = []
    missing: List[dict] = []
    for lo in los:
        r = coverage_for_lo(lo, blob, hub_tokens)
        (covered if r["score"] >= threshold else missing).append(r)

    return {
        "threshold": threshold,
        "total": len(los),
        "covered": covered,
        "missing": missing,
    }
