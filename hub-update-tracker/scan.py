#!/usr/bin/env python3
"""
HoyaDocs Endocrine — Hub Update Tracker
=======================================

Scans the endocrine study-hub folder and flags which hub pages (lecture-guide,
active-recall, hub1, hub2, hub3) need rebuilding when lecture source files
(PDF slides + Learning Objective .docx files) have changed since the last build.

Detection method: SHA-256 content hashes compared against a baseline.json.

Usage
-----
    # Scan + regenerate dashboard (also runs --init the first time)
    python3 scan.py

    # Force (re)initialize baseline = accept current state as up-to-date
    python3 scan.py --init

    # Mark specific lectures as freshly built (updates their baseline)
    python3 scan.py --accept end01 end02

    # Mark everything as up-to-date
    python3 scan.py --accept-all

Writes
------
    baseline.json   — stored source-file hashes per lecture
    status.json     — latest scan results
    dashboard.html  — self-contained interactive dashboard (data embedded)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Local module — content extraction + LO coverage scoring
import content as content_mod

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENDO_ROOT = SCRIPT_DIR.parent  # .../endocrine/

PER_LECTURE_DIR = ENDO_ROOT / "Per Lecture"
LO_DIR = PER_LECTURE_DIR / "Learning Objectives"
LECTURE_GUIDES_DIR = ENDO_ROOT / "lecture-guides"
ACTIVE_RECALL_DIR = ENDO_ROOT / "active-lesson-drills"
TESTING_DRILLS_DIR = ENDO_ROOT / "testing-drills"

BASELINE_FILE = SCRIPT_DIR / "baseline.json"
STATUS_FILE = SCRIPT_DIR / "status.json"
DASHBOARD_FILE = SCRIPT_DIR / "dashboard.html"
REPORT_FILE = SCRIPT_DIR / "hub-status-report.html"
CONTENT_CACHE_FILE = SCRIPT_DIR / "content-cache.json"

# LO coverage threshold: fraction of content tokens that must appear in a hub
# page for an LO to count as "covered". Anything below is a gap.
LO_COVERAGE_THRESHOLD = 0.6

# ---------------------------------------------------------------------------
# Hub page types
# ---------------------------------------------------------------------------

HUB_TYPES = ["lecture-guide", "active-recall", "hub1", "hub2", "hub3"]

# Lectures end01..end25 (pad to 2 digits)
LECTURE_IDS = [f"end{i:02d}" for i in range(1, 26)]

LECTURE_PREFIX_RE = re.compile(r"^end(\d{2})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def rel_to_endo(p: Path) -> str:
    try:
        return str(p.relative_to(ENDO_ROOT))
    except ValueError:
        return str(p)


def lecture_id_from_name(name: str) -> Optional[str]:
    m = LECTURE_PREFIX_RE.match(name)
    if not m:
        return None
    return f"end{int(m.group(1)):02d}"


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

@dataclass
class SourceFile:
    path: str            # relative path (nice for display)
    sha256: str
    mtime: str
    mtime_ts: float
    size: int
    kind: str            # "pdf" | "lo" | "other"


@dataclass
class HubPage:
    type: str            # lecture-guide / active-recall / hub1 / hub2 / hub3
    path: Optional[str]  # relative path, or None if missing
    mtime: Optional[str] = None
    mtime_ts: Optional[float] = None
    stale_reason: Optional[str] = None  # "hash-changed" | "sources-newer" | None
    # Content-gap info: LOs from the sources that aren't yet reflected here
    lo_total: int = 0
    lo_covered: int = 0
    lo_missing: List[dict] = field(default_factory=list)  # [{"lo","score","missing_tokens":[...]}]


@dataclass
class LectureStatus:
    lecture_id: str
    title: str
    sources: List[SourceFile] = field(default_factory=list)
    hub_pages: List[HubPage] = field(default_factory=list)
    current_source_hash: str = ""
    baseline_source_hash: str = ""
    state: str = "unknown"   # "fresh" | "stale" | "new" | "missing-sources"
    stale_pages: List[str] = field(default_factory=list)
    missing_pages: List[str] = field(default_factory=list)
    # Learning objectives pulled from source docx files
    los: List[str] = field(default_factory=list)
    # Aggregated content gaps: LOs that are missing from EVERY existing hub page
    orphan_los: List[str] = field(default_factory=list)


def discover_sources_for_lecture(lecture_id: str) -> List[SourceFile]:
    """Find all source files (PDF slides + LO docx) that belong to a lecture."""
    num = int(lecture_id[3:])
    prefix_patterns = [
        f"end{num:02d}",          # end01, end02, ...
        f"{num:02d}_",            # 01_..., 02_... (file prefix style)
    ]

    sources: List[SourceFile] = []

    # 1. Per Lecture/endNN_*/ folders
    if PER_LECTURE_DIR.exists():
        for child in PER_LECTURE_DIR.iterdir():
            if not child.is_dir():
                continue
            cname = child.name.strip()
            if not cname.lower().startswith(f"end{num:02d}"):
                continue
            for f in child.rglob("*"):
                if not f.is_file():
                    continue
                if f.name.startswith("."):
                    continue
                kind = _kind_of(f)
                if kind == "other":
                    continue
                sources.append(_file_to_source(f, kind))

    # 2. Per Lecture/Learning Objectives/ (files may match endNN_*, NN-LO-*, or endNN-LO-*)
    if LO_DIR.exists():
        for f in LO_DIR.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            lname = f.name.lower()
            # Match endNN_ or endNN- at the start
            if (lname.startswith(f"end{num:02d}_") or
                lname.startswith(f"end{num:02d}-") or
                lname.startswith(f"end{num:02d} ")):
                kind = _kind_of(f)
                if kind == "other":
                    continue
                # Skip if we already found this file (same path) under Per Lecture
                if any(s.path == rel_to_endo(f) for s in sources):
                    continue
                sources.append(_file_to_source(f, kind))

    # Stable sort by path
    sources.sort(key=lambda s: s.path)
    return sources


def _kind_of(f: Path) -> str:
    suf = f.suffix.lower()
    if suf == ".pdf":
        # PDFs in Learning Objectives are LO; PDFs elsewhere are slides
        if LO_DIR in f.parents:
            return "lo"
        return "pdf"
    if suf in (".docx", ".doc"):
        return "lo"
    return "other"


def _file_to_source(f: Path, kind: str) -> SourceFile:
    st = f.stat()
    return SourceFile(
        path=rel_to_endo(f),
        sha256=sha256_of(f),
        mtime=iso(st.st_mtime),
        mtime_ts=st.st_mtime,
        size=st.st_size,
        kind=kind,
    )


def discover_hub_pages(lecture_id: str) -> List[HubPage]:
    """Find each of the 5 hub page types for a lecture (if present)."""
    num = int(lecture_id[3:])
    prefix_hyphen = f"end{num:02d}-"   # lecture-guide style: end01-...
    prefix_uscore = f"end{num:02d}_"   # active-recall style: end01_active_recall.html

    pages: List[HubPage] = []

    # lecture-guide: lecture-guides/endNN-*.html  (anything matching prefix)
    pages.append(_find_hub_page(
        type_="lecture-guide",
        directory=LECTURE_GUIDES_DIR,
        patterns=[f"{prefix_hyphen}*.html", f"{prefix_uscore}*.html"],
        exclude_patterns=[],
    ))

    # active-recall: active-lesson-drills/endNN_active_recall.html (also accept endNN-active-recall.html)
    pages.append(_find_hub_page(
        type_="active-recall",
        directory=ACTIVE_RECALL_DIR,
        patterns=[f"{prefix_uscore}active_recall.html", f"{prefix_hyphen}active-recall.html"],
        exclude_patterns=["*_key.html", "*-key.html"],
    ))

    # hub1/hub2/hub3: testing-drills/endNN-hub[1|2|3]-*.html
    for n in (1, 2, 3):
        pages.append(_find_hub_page(
            type_=f"hub{n}",
            directory=TESTING_DRILLS_DIR,
            patterns=[f"{prefix_hyphen}hub{n}-*.html", f"{prefix_hyphen}hub{n}.html"],
            exclude_patterns=[],
        ))

    return pages


def _find_hub_page(type_: str, directory: Path, patterns: List[str],
                   exclude_patterns: List[str]) -> HubPage:
    if not directory.exists():
        return HubPage(type=type_, path=None)

    import fnmatch

    candidates: List[Path] = []
    for pat in patterns:
        for f in directory.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            if fnmatch.fnmatch(f.name, pat):
                if any(fnmatch.fnmatch(f.name, ex) for ex in exclude_patterns):
                    continue
                candidates.append(f)

    if not candidates:
        return HubPage(type=type_, path=None)

    # If multiple, pick the newest (should be one, but be defensive)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    chosen = candidates[0]
    st = chosen.stat()
    return HubPage(
        type=type_,
        path=rel_to_endo(chosen),
        mtime=iso(st.st_mtime),
        mtime_ts=st.st_mtime,
    )


# ---------------------------------------------------------------------------
# Hashing logic
# ---------------------------------------------------------------------------

def lecture_source_fingerprint(sources: List[SourceFile]) -> str:
    """A deterministic hash over all source files in a lecture (path + content)."""
    if not sources:
        return ""
    h = hashlib.sha256()
    for s in sorted(sources, key=lambda x: x.path):
        h.update(s.path.encode("utf-8"))
        h.update(b"\x00")
        h.update(s.sha256.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def lecture_title(lecture_id: str, sources: List[SourceFile],
                  hub_pages: List[HubPage]) -> str:
    """Best-effort human title for the lecture."""
    # Prefer the Per Lecture folder name
    num = int(lecture_id[3:])
    if PER_LECTURE_DIR.exists():
        for child in PER_LECTURE_DIR.iterdir():
            if child.is_dir() and child.name.strip().lower().startswith(f"end{num:02d}"):
                return child.name.strip().replace(f"end{num:02d}_", "").replace("-", " ").strip()
    # Fall back to any source file name
    for s in sources:
        name = Path(s.path).stem
        name = re.sub(r"^(?:end)?\d+[-_ ]*", "", name, count=1, flags=re.I)
        name = re.sub(r"(Objectives|video slides|video).*$", "", name, flags=re.I).strip()
        if name:
            return name[:80]
    # Otherwise take from a hub page
    for hp in hub_pages:
        if hp.path:
            stem = Path(hp.path).stem
            stem = re.sub(r"^end\d{2}[-_]", "", stem)
            stem = re.sub(r"^hub\d-", "", stem)
            return stem.replace("-", " ")
    return lecture_id


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def load_baseline() -> dict:
    if BASELINE_FILE.exists():
        try:
            return json.loads(BASELINE_FILE.read_text())
        except Exception as e:
            print(f"warning: could not read baseline.json ({e}); treating as empty")
    return {"lectures": {}, "meta": {}}


def save_baseline(baseline: dict) -> None:
    baseline.setdefault("meta", {})["updated_at"] = iso(datetime.now().timestamp())
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2))


def scan() -> dict:
    baseline = load_baseline()
    b_lectures = baseline.get("lectures", {})
    cache = content_mod.ContentCache(CONTENT_CACHE_FILE)

    lectures: List[LectureStatus] = []
    any_sources_found = False

    for lid in LECTURE_IDS:
        sources = discover_sources_for_lecture(lid)
        hub_pages = discover_hub_pages(lid)

        if sources:
            any_sources_found = True

        # If no sources AND no hub pages, skip this lecture (not applicable)
        if not sources and all(hp.path is None for hp in hub_pages):
            continue

        title = lecture_title(lid, sources, hub_pages)

        # --- Extract LOs from source docx files (cached by sha256) ---
        lecture_los: List[str] = []
        for s in sources:
            if s.kind == "lo":
                extracted = content_mod.extract_source(
                    ENDO_ROOT / s.path, s.sha256, cache
                )
                for lo in extracted.get("los", []):
                    if lo and lo not in lecture_los:
                        lecture_los.append(lo)

        # --- Compute coverage of LOs in each existing hub page ---
        per_page_missing_los: Dict[str, List[str]] = {}
        for hp in hub_pages:
            if hp.path is None or not lecture_los:
                continue
            hub_text = content_mod.extract_hub(ENDO_ROOT / hp.path)["text"]
            rep = content_mod.coverage_report(
                lecture_los, hub_text, threshold=LO_COVERAGE_THRESHOLD
            )
            hp.lo_total = rep["total"]
            hp.lo_covered = len(rep["covered"])
            hp.lo_missing = rep["missing"]
            per_page_missing_los[hp.type] = [m["lo"] for m in rep["missing"]]

        # Orphan LOs = LOs missing from EVERY existing hub page (or with no hub pages at all)
        existing_hubs = [hp for hp in hub_pages if hp.path is not None]
        orphan_los: List[str] = []
        if lecture_los:
            if not existing_hubs:
                orphan_los = list(lecture_los)
            else:
                for lo in lecture_los:
                    if all(lo in per_page_missing_los.get(hp.type, []) for hp in existing_hubs):
                        orphan_los.append(lo)
        cur_hash = lecture_source_fingerprint(sources)
        base_entry = b_lectures.get(lid, {})
        base_hash = base_entry.get("source_hash", "")

        # Compute hash-based state
        hash_stale = bool(base_hash) and cur_hash != base_hash
        if not sources:
            hash_state = "missing-sources"
        elif not base_hash:
            hash_state = "new"  # never baselined
        elif hash_stale:
            hash_state = "stale"
        else:
            hash_state = "fresh"

        # Per-page stale evaluation: hash signal OR mtime heuristic
        newest_source_ts = max((s.mtime_ts for s in sources), default=0.0)
        for hp in hub_pages:
            if hp.path is None:
                continue
            if hash_stale:
                hp.stale_reason = "hash-changed"
            elif hp.mtime_ts is not None and newest_source_ts > hp.mtime_ts:
                hp.stale_reason = "sources-newer"
            else:
                hp.stale_reason = None

        missing = [hp.type for hp in hub_pages if hp.path is None]
        stale = [hp.type for hp in hub_pages if hp.stale_reason is not None]

        # Overall lecture state (prefer hash, fall back to mtime signal)
        if hash_state in ("stale", "missing-sources"):
            state = hash_state
        elif hash_state == "new":
            # Only surface as "stale" if we have an mtime-based reason for any hub page
            state = "stale" if stale else "new"
        else:  # fresh
            state = "stale" if stale else "fresh"

        lectures.append(LectureStatus(
            lecture_id=lid,
            title=title,
            sources=sources,
            hub_pages=hub_pages,
            current_source_hash=cur_hash,
            baseline_source_hash=base_hash,
            state=state,
            stale_pages=stale,
            missing_pages=missing,
            los=lecture_los,
            orphan_los=orphan_los,
        ))

    cache.save()

    status = {
        "scanned_at": iso(datetime.now().timestamp()),
        "endo_root": str(ENDO_ROOT),
        "lectures": [asdict(l) for l in lectures],
        "summary": summarize(lectures),
        "any_sources_found": any_sources_found,
    }
    STATUS_FILE.write_text(json.dumps(status, indent=2))
    return status


def summarize(lectures: List[LectureStatus]) -> dict:
    total = len(lectures)
    fresh = sum(1 for l in lectures if l.state == "fresh")
    stale = sum(1 for l in lectures if l.state == "stale")
    new = sum(1 for l in lectures if l.state == "new")
    missing_src = sum(1 for l in lectures if l.state == "missing-sources")
    stale_pages = sum(len(l.stale_pages) for l in lectures)
    missing_pages = sum(len(l.missing_pages) for l in lectures)
    total_los = sum(len(l.los) for l in lectures)
    orphan_los = sum(len(l.orphan_los) for l in lectures)
    any_gap_pages = sum(
        sum(1 for hp in l.hub_pages if hp.path and hp.lo_missing)
        for l in lectures
    )
    return {
        "total_lectures": total,
        "fresh": fresh,
        "stale": stale,
        "new": new,
        "missing_sources": missing_src,
        "stale_hub_pages": stale_pages,
        "missing_hub_pages": missing_pages,
        "total_los": total_los,
        "orphan_los": orphan_los,
        "hub_pages_with_gaps": any_gap_pages,
    }


# ---------------------------------------------------------------------------
# Baseline commands
# ---------------------------------------------------------------------------

def cmd_init(force: bool = False) -> None:
    """Accept current state as baseline for all lectures that have sources."""
    status = scan()
    baseline = load_baseline()
    baseline.setdefault("lectures", {})
    updated = 0
    for lec in status["lectures"]:
        if not lec["sources"]:
            continue
        lid = lec["lecture_id"]
        if lid in baseline["lectures"] and not force:
            continue
        baseline["lectures"][lid] = {
            "source_hash": lec["current_source_hash"],
            "accepted_at": status["scanned_at"],
            "file_hashes": {s["path"]: s["sha256"] for s in lec["sources"]},
        }
        updated += 1
    save_baseline(baseline)
    print(f"initialized baseline for {updated} lecture(s)")


def cmd_accept(lecture_ids: List[str]) -> None:
    status = scan()
    baseline = load_baseline()
    baseline.setdefault("lectures", {})
    wanted = {lid.lower() for lid in lecture_ids}
    updated: List[str] = []
    for lec in status["lectures"]:
        if lec["lecture_id"].lower() not in wanted:
            continue
        if not lec["sources"]:
            print(f"skip {lec['lecture_id']}: no source files present")
            continue
        baseline["lectures"][lec["lecture_id"]] = {
            "source_hash": lec["current_source_hash"],
            "accepted_at": status["scanned_at"],
            "file_hashes": {s["path"]: s["sha256"] for s in lec["sources"]},
        }
        updated.append(lec["lecture_id"])
    save_baseline(baseline)
    print(f"marked baseline as current for: {', '.join(updated) if updated else 'nothing'}")


def cmd_accept_all() -> None:
    status = scan()
    all_ids = [l["lecture_id"] for l in status["lectures"] if l["sources"]]
    cmd_accept(all_ids)


# ---------------------------------------------------------------------------
# Dashboard rendering
# ---------------------------------------------------------------------------

def render_dashboard(status: dict) -> str:
    data_json = json.dumps(status)
    # Escape </script> defensively
    data_json = data_json.replace("</", "<\\/")
    return DASHBOARD_TEMPLATE.replace("__STATUS_JSON__", data_json)


DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Endocrine Hub Update Tracker</title>
<style>
  :root {
    --amber-50:#FFFBEB;
    --amber-100:#FEF3C7;
    --amber-200:#FDE68A;
    --amber-500:#F59E0B;
    --amber-600:#D97706;
    --amber-700:#B45309;
    --amber-900:#78350F;
    --slate-50:#F8FAFC;
    --slate-100:#F1F5F9;
    --slate-200:#E2E8F0;
    --slate-300:#CBD5E1;
    --slate-500:#64748B;
    --slate-600:#475569;
    --slate-700:#334155;
    --slate-900:#0F172A;
    --green-100:#DCFCE7;
    --green-600:#16A34A;
    --green-700:#15803D;
    --red-100:#FEE2E2;
    --red-600:#DC2626;
    --red-700:#B91C1C;
    --blue-100:#DBEAFE;
    --blue-600:#2563EB;
    --blue-700:#1D4ED8;
    --yellow-100:#FEF9C3;
    --yellow-700:#A16207;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen,
                 Ubuntu, Cantarell, sans-serif;
    background: linear-gradient(180deg, var(--amber-50) 0%, #fff 280px);
    color: var(--slate-900);
  }
  header {
    padding: 28px 32px 20px;
    border-bottom: 1px solid var(--amber-200);
    background: #fff;
  }
  header h1 {
    margin: 0 0 4px;
    font-size: 28px;
    color: var(--amber-900);
    display: flex;
    align-items: center;
    gap: 10px;
  }
  header .sub {
    color: var(--slate-600);
    font-size: 14px;
  }
  header .scanned {
    color: var(--slate-500);
    font-size: 12px;
    margin-top: 6px;
  }
  main { padding: 24px 32px 60px; max-width: 1400px; margin: 0 auto; }
.topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; flex-wrap: wrap; }
.topbar .btn-row { display: flex; gap: 10px; align-items: center; }
.refresh-hint { margin-left: auto; color: var(--slate-500); font-size: 12px; }
.refresh-hint code { background: var(--slate-100); color: var(--slate-700); padding: 2px 6px; border-radius: 4px; font-size: 11px; }
.jv-link {
  padding: 8px 14px;
  border: 1px solid var(--amber-600);
  background: #fff;
  color: var(--amber-700);
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  text-decoration: none;
  display: inline-block;
}
.jv-link:hover { background: var(--amber-50); }
.jv-link.active-link { background: var(--amber-600); color: #fff; }
.jv-link.active-link:hover { background: var(--amber-700); }
.rerun-btn {
  padding: 8px 14px;
  border: 1px solid var(--amber-600);
  background: var(--amber-600);
  color: #fff;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.rerun-btn:hover:not([disabled]) { background: var(--amber-700); border-color: var(--amber-700); }
.rerun-btn[disabled] { opacity: 0.65; cursor: wait; }
.spin { display: inline-block; }
@keyframes hut-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }
.spin.on { animation: hut-spin 0.8s linear infinite; }

  .summary {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .stat {
    background: #fff;
    border: 1px solid var(--slate-200);
    border-radius: 10px;
    padding: 14px 16px;
  }
  .stat .n { font-size: 28px; font-weight: 700; line-height: 1; }
  .stat .label { font-size: 12px; color: var(--slate-500); margin-top: 6px; text-transform: uppercase; letter-spacing: 0.04em; }
  .stat.fresh  .n { color: var(--green-600); }
  .stat.stale  .n { color: var(--red-600); }
  .stat.new    .n { color: var(--blue-600); }
  .stat.missing .n { color: var(--yellow-700); }

  .toolbar {
    display: flex;
    gap: 10px;
    align-items: center;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }
  .toolbar input[type=search] {
    flex: 1;
    min-width: 220px;
    padding: 9px 12px;
    border: 1px solid var(--slate-300);
    border-radius: 8px;
    font-size: 14px;
  }
  .rescan-btn {
    padding: 8px 14px;
    border: 1px solid var(--amber-600);
    background: var(--amber-600);
    color: #fff;
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .rescan-btn:hover:not([disabled]) { background: var(--amber-700); border-color: var(--amber-700); }
  .rescan-btn[disabled] { opacity: 0.6; cursor: wait; }
  .banner {
    margin-bottom: 14px;
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 13px;
  }
  .banner.info { background: var(--amber-50); border: 1px solid var(--amber-200); color: var(--amber-900); }
  .banner.ok   { background: var(--green-100); border: 1px solid #86EFAC; color: var(--green-700); }
  .banner.warn { background: var(--yellow-100); border: 1px solid #FDE68A; color: var(--yellow-700); }
  .banner a { color: inherit; font-weight: 600; }
  .filter-btn {
    padding: 8px 14px;
    border: 1px solid var(--slate-300);
    background: #fff;
    color: var(--slate-700);
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
  }
  .filter-btn.active {
    background: var(--amber-600);
    color: #fff;
    border-color: var(--amber-600);
  }

  table.matrix {
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    border: 1px solid var(--slate-200);
    border-radius: 10px;
    overflow: hidden;
    font-size: 14px;
  }
  table.matrix th, table.matrix td {
    padding: 10px 12px;
    text-align: left;
    border-bottom: 1px solid var(--slate-100);
  }
  table.matrix th {
    background: var(--slate-50);
    font-weight: 600;
    color: var(--slate-600);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  table.matrix th.ctr, table.matrix td.ctr { text-align: center; }
  table.matrix tr.lecture-row { cursor: pointer; }
  table.matrix tr.lecture-row:hover { background: var(--amber-50); }
  table.matrix tr.detail-row td {
    background: var(--slate-50);
    padding: 14px 22px;
    border-bottom: 2px solid var(--slate-200);
  }
  .lid { font-family: ui-monospace, Menlo, monospace; font-weight: 600; color: var(--amber-700); }
  .title { color: var(--slate-900); }

  .state-pill {
    display: inline-block;
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .state-fresh { background: var(--green-100); color: var(--green-700); }
  .state-stale { background: var(--red-100); color: var(--red-700); }
  .state-new { background: var(--blue-100); color: var(--blue-700); }
  .state-missing-sources { background: var(--yellow-100); color: var(--yellow-700); }

  .hub-cell {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px; height: 28px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
  }
  .hub-fresh { background: var(--green-100); color: var(--green-700); }
  .hub-stale { background: var(--red-100); color: var(--red-700); }
  .hub-gap   { background: var(--amber-100); color: var(--amber-700); }
  .hub-missing { background: var(--slate-100); color: var(--slate-300); }

  .details h3 { margin: 0 0 10px; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--slate-600); }
  .details .file-list { margin: 0; padding: 0; list-style: none; }
  .details .file-list li {
    font-family: ui-monospace, Menlo, monospace;
    font-size: 12px;
    padding: 4px 0;
    color: var(--slate-700);
  }
  .details .file-list .hash { color: var(--slate-400); font-size: 11px; margin-left: 8px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }
  .badge-kind {
    display: inline-block;
    padding: 1px 6px;
    margin-right: 6px;
    background: var(--amber-100);
    color: var(--amber-700);
    border-radius: 4px;
    font-size: 10px;
    text-transform: uppercase;
  }
  .action-hint {
    margin-top: 12px;
    padding: 10px 14px;
    background: #fff;
    border: 1px dashed var(--amber-200);
    border-radius: 8px;
    font-size: 13px;
    color: var(--slate-700);
  }
  .action-hint code {
    background: var(--slate-900);
    color: #fff;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 12px;
  }
  .empty {
    padding: 40px; text-align: center; color: var(--slate-500);
    background: #fff; border: 1px dashed var(--slate-200); border-radius: 10px;
  }
  .gaps {
    margin-top: 18px;
    padding: 14px 16px;
    background: #fff;
    border: 1px solid var(--amber-200);
    border-radius: 10px;
  }
  .gaps h3 {
    margin: 0 0 10px;
    font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--amber-900);
  }
  .gap-empty { color: var(--slate-500); font-size: 13px; padding: 4px 0; }
  .gap-empty.ok { color: var(--green-700); }
  .gap-block { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--slate-100); }
  .gap-block:first-of-type { border-top: none; padding-top: 0; margin-top: 0; }
  .gap-heading { font-size: 13px; color: var(--slate-700); margin-bottom: 6px; }
  .lo-list {
    margin: 4px 0 0;
    padding: 0 0 0 6px;
    list-style: none;
    font-size: 13px;
  }
  .lo-list li {
    padding: 3px 0;
    color: var(--slate-700);
    line-height: 1.4;
  }
  .lo-list .lo-dot {
    display: inline-block;
    width: 18px;
    font-weight: 700;
  }
  .lo-orphan { color: var(--red-700); }
  .lo-orphan .lo-dot { color: var(--red-600); }
  .lo-missing { color: var(--amber-900); }
  .lo-missing .lo-dot { color: var(--amber-600); }
  .legend {
    display: flex; gap: 18px; margin-top: 14px; padding: 10px 14px;
    background: var(--slate-50); border-radius: 8px;
    font-size: 12px; color: var(--slate-600);
    flex-wrap: wrap;
  }
  .legend .item { display: inline-flex; align-items: center; gap: 6px; }
</style>
</head>
<body>
<header>
  <h1>Endocrine Hub Update Tracker</h1>
  <div class="sub">Flags which hub pages need rebuilding when lecture sources change.</div>
  <div class="scanned">Scanned: <span id="scanned-at"></span></div>
</header>
<main>
  <div class="topbar">
    <div class="btn-row">
      <button id="rerun-btn" class="rerun-btn" type="button">
        <span id="rerun-spinner" class="spin" style="display:none;">⟳</span>
        <span id="rerun-label">Rerun scan</span>
      </button>
      <a class="jv-link active-link" href="dashboard.html">Hub Update Tracker</a>
      <a class="jv-link" href="jv-dashboard.html">JV Tool</a>
    </div>
    <div class="refresh-hint">Rerun needs the tracker server running. Start it by double-clicking <code>run.command</code>.</div>
  </div>
  <div id="banner"></div>
  <section class="summary" id="summary"></section>

  <div class="toolbar">
    <input type="search" id="search" placeholder="Search lectures, titles, file names…" />
    <button class="filter-btn active" data-filter="all">All</button>
    <button class="filter-btn" data-filter="needs-update">Needs update</button>
    <button class="filter-btn" data-filter="stale">Stale only</button>
    <button class="filter-btn" data-filter="has-gaps">Has LO gaps</button>
    <button class="filter-btn" data-filter="orphan">Orphan LOs</button>
    <button class="filter-btn" data-filter="missing">Missing hubs</button>
    <button class="filter-btn" data-filter="fresh">Fresh only</button>
  </div>

  <table class="matrix" id="matrix">
    <thead>
      <tr>
        <th style="width:80px;">Lecture</th>
        <th>Title</th>
        <th style="width:120px;">State</th>
        <th class="ctr" style="width:70px;">Lec Guide</th>
        <th class="ctr" style="width:70px;">Active Recall</th>
        <th class="ctr" style="width:60px;">Hub 1</th>
        <th class="ctr" style="width:60px;">Hub 2</th>
        <th class="ctr" style="width:60px;">Hub 3</th>
        <th class="ctr" style="width:70px;">Sources</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>

  <div class="legend">
    <span class="item"><span class="hub-cell hub-fresh">✓</span> Fresh — built from current sources, all LOs covered</span>
    <span class="item"><span class="hub-cell hub-stale">!</span> Stale — sources changed since last build</span>
    <span class="item"><span class="hub-cell hub-gap">N</span> Has gaps — N learning objectives not found on the page</span>
    <span class="item"><span class="hub-cell hub-missing">—</span> Missing — hub page does not exist yet</span>
  </div>
</main>

<script>
const STATUS = __STATUS_JSON__;
const HUB_ORDER = ["lecture-guide","active-recall","hub1","hub2","hub3"];

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.append(c.nodeType ? c : document.createTextNode(c));
  }
  return e;
}

function renderSummary() {
  const s = STATUS.summary;
  const box = document.getElementById("summary");
  box.innerHTML = "";
  const stats = [
    ["fresh", "Fresh", s.fresh],
    ["stale", "Stale (content changed)", s.stale],
    ["new", "New (unbaselined)", s.new],
    ["missing", "Missing hub pages", s.missing_hub_pages],
    ["fresh", "Total LOs tracked", s.total_los],
    ["stale", "Hub pages with LO gaps", s.hub_pages_with_gaps],
    ["missing", "Orphan LOs (nowhere covered)", s.orphan_los],
  ];
  for (const [cls,label,n] of stats) {
    box.append(el("div", {class:"stat "+cls},
      el("div", {class:"n"}, String(n)),
      el("div", {class:"label"}, label)
    ));
  }
}

function hubCell(hubType, lec) {
  const hp = lec.hub_pages.find(h => h.type === hubType);
  if (!hp || !hp.path) {
    return el("span", {class:"hub-cell hub-missing", title:"missing"}, "—");
  }
  const missingCount = (hp.lo_missing || []).length;
  const stale = !!hp.stale_reason;

  // Determine cell class: stale > gap > fresh
  let cls = "hub-fresh", label = "✓";
  const tip = [];
  if (stale) {
    cls = "hub-stale";
    label = "!";
    tip.push(hp.stale_reason === "hash-changed"
      ? "stale: source content changed since last build"
      : "stale: source file is newer than hub page");
  } else if (missingCount > 0) {
    cls = "hub-gap";
    label = String(missingCount);
    tip.push(`${missingCount} LO${missingCount===1?'':'s'} not covered on this page`);
  } else if (hp.lo_total > 0) {
    tip.push(`all ${hp.lo_total} LOs covered`);
  }
  tip.push(hp.path);
  tip.push("updated " + hp.mtime);
  if (missingCount > 0 && !stale) {
    // Also show count on a stale cell? no, keep stale as `!`
  }
  return el("span", {class:"hub-cell "+cls, title: tip.join("\n")}, label);
}

function renderRows(filter="all", query="") {
  const tbody = document.getElementById("rows");
  tbody.innerHTML = "";
  const q = query.trim().toLowerCase();

  const visible = STATUS.lectures.filter(lec => {
    if (q) {
      const hay = [
        lec.lecture_id, lec.title, lec.state,
        ...lec.sources.map(s => s.path),
        ...lec.hub_pages.map(h => h.path || "")
      ].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    const anyGaps = lec.hub_pages.some(h => h.path && (h.lo_missing||[]).length > 0);
    const hasOrphans = (lec.orphan_los||[]).length > 0;
    switch (filter) {
      case "needs-update":
        return lec.state === "stale" || lec.state === "new" || lec.missing_pages.length > 0 || anyGaps;
      case "stale":    return lec.state === "stale";
      case "has-gaps": return anyGaps;
      case "orphan":   return hasOrphans;
      case "missing":  return lec.missing_pages.length > 0;
      case "fresh":    return lec.state === "fresh" && !anyGaps;
      default: return true;
    }
  });

  if (!visible.length) {
    tbody.append(el("tr", {}, el("td", {colspan:9, class:"empty"}, "No matches.")));
    return;
  }

  for (const lec of visible) {
    const row = el("tr", {class:"lecture-row", dataset:{id: lec.lecture_id}},
      el("td", {class:"lid"}, lec.lecture_id.toUpperCase()),
      el("td", {class:"title"}, lec.title),
      el("td", {}, el("span", {class:"state-pill state-"+lec.state}, lec.state.replace("-"," "))),
      ...HUB_ORDER.map(t => el("td", {class:"ctr"}, hubCell(t, lec))),
      el("td", {class:"ctr"}, String(lec.sources.length))
    );
    row.addEventListener("click", () => toggleDetail(row, lec));
    tbody.append(row);
  }
}

function toggleDetail(row, lec) {
  const next = row.nextElementSibling;
  if (next && next.classList.contains("detail-row")) {
    next.remove();
    return;
  }
  // Build the content-gaps block
  const gapsBlock = renderContentGaps(lec);

  const anyGaps = lec.hub_pages.some(h => h.path && (h.lo_missing||[]).length > 0);
  const needsRebuild = (lec.state === "stale" || lec.state === "new"
                        || lec.missing_pages.length > 0 || anyGaps);

  const detail = el("tr", {class:"detail-row"},
    el("td", {colspan:9},
      el("div", {class:"details grid-2"},
        el("div", {},
          el("h3", {}, `Sources (${lec.sources.length})`),
          el("ul", {class:"file-list"},
            ...lec.sources.map(s => el("li", {title: s.path},
              el("span", {class:"badge-kind"}, s.kind),
              (s.path || "").split("/").pop(),
              el("span", {class:"hash"}, " " + s.sha256.slice(0,10) + "…")
            ))
          )
        ),
        el("div", {},
          el("h3", {}, "Hub pages"),
          el("ul", {class:"file-list"},
            ...lec.hub_pages.map(h => {
              const gapCount = (h.lo_missing||[]).length;
              const statusStr = h.path
                ? (h.stale_reason
                    ? ` — stale (${h.stale_reason.replace("-"," ")})`
                    : (gapCount > 0
                        ? ` — ${gapCount} LO${gapCount===1?'':'s'} not covered`
                        : (h.lo_total ? ` — all ${h.lo_total} LOs covered` : " — fresh")))
                : "";
              const color = h.path
                ? (h.stale_reason ? "color:var(--red-600)"
                                  : (gapCount ? "color:var(--amber-700)"
                                              : "color:var(--green-700)"))
                : "color:var(--slate-400)";
              return el("li", {},
                el("span", {class:"badge-kind"}, h.type),
                h.path || "(missing)",
                h.mtime ? el("span", {class:"hash"}, " updated " + h.mtime) : null,
                statusStr ? el("span", {class:"hash", style: color}, statusStr) : null
              );
            })
          )
        )
      ),
      gapsBlock,
      needsRebuild
        ? el("div", {class:"action-hint"},
            el("strong", {}, "Needs attention. "),
            lec.stale_pages.length ? `Stale: ${lec.stale_pages.join(", ")}.  ` : "",
            lec.missing_pages.length ? `Missing: ${lec.missing_pages.join(", ")}.  ` : "",
            anyGaps ? `Has LO gaps (see above).  ` : "",
            "After rebuilding, mark as current: ",
            el("code", {}, `python3 scan.py --accept ${lec.lecture_id}`)
          )
        : el("div", {class:"action-hint"},
            el("strong", {}, "Up to date."),
            " No action needed."
          )
    )
  );
  row.after(detail);
}

function renderContentGaps(lec) {
  const orphans = lec.orphan_los || [];
  const pagesWithGaps = lec.hub_pages.filter(h => h.path && (h.lo_missing||[]).length > 0);

  if (!lec.los || !lec.los.length) {
    return el("div", {class:"gaps"},
      el("h3", {}, "Content gaps"),
      el("div", {class:"gap-empty"}, "No learning objectives parsed for this lecture (source LO docx missing or unreadable)."));
  }

  if (!orphans.length && !pagesWithGaps.length) {
    return el("div", {class:"gaps"},
      el("h3", {}, `Content coverage (${lec.los.length} LOs)`),
      el("div", {class:"gap-empty ok"}, `✓ Every LO is covered on at least one hub page.`));
  }

  const children = [
    el("h3", {}, `Content gaps (${lec.los.length} LOs total)`)
  ];

  if (orphans.length) {
    const list = el("ul", {class:"lo-list"},
      ...orphans.map(lo => el("li", {class:"lo-orphan"},
        el("span", {class:"lo-dot"}, "⚠"),
        lo
      ))
    );
    children.push(
      el("div", {class:"gap-block"},
        el("div", {class:"gap-heading"},
          el("strong", {}, `${orphans.length} LO${orphans.length===1?'':'s'} not reflected on ANY existing hub page`),
          lec.hub_pages.every(h => !h.path)
            ? el("span", {class:"hash"}, " (no hub pages exist yet)")
            : null
        ),
        list
      )
    );
  }

  for (const hp of pagesWithGaps) {
    const list = el("ul", {class:"lo-list"},
      ...hp.lo_missing.map(m => el("li", {class:"lo-missing"},
        el("span", {class:"lo-dot"}, "•"),
        m.lo,
        el("span", {class:"hash"}, ` (coverage ${Math.round((m.score||0)*100)}%)`)
      ))
    );
    children.push(
      el("div", {class:"gap-block"},
        el("div", {class:"gap-heading"},
          el("span", {class:"badge-kind"}, hp.type),
          el("strong", {}, `${hp.lo_missing.length} of ${hp.lo_total} LO${hp.lo_total===1?'':'s'} not covered`)
        ),
        list
      )
    );
  }

  return el("div", {class:"gaps"}, ...children);
}

document.getElementById("scanned-at").textContent = STATUS.scanned_at + "  ·  " + STATUS.endo_root;

function showBanner(kind, html) {
  const b = document.getElementById("banner");
  if (!b) return;
  b.className = "banner " + kind;
  b.innerHTML = html;
}

const rerunBtn = document.getElementById("rerun-btn");
const rerunLabel = document.getElementById("rerun-label");
const rerunSpin = document.getElementById("rerun-spinner");

if (rerunBtn) {
  rerunBtn.addEventListener("click", async () => {
    if (window.location.protocol === "file:") {
      showBanner("warn",
        "The Rerun button needs the tracker server running. Double-click " +
        "<code>run.command</code> (or run <code>python3 server.py</code>) and open the " +
        "dashboard at <a href='http://127.0.0.1:8765/dashboard.html'>http://127.0.0.1:8765/dashboard.html</a>."
      );
      return;
    }
    rerunBtn.disabled = true;
    rerunLabel.textContent = "Scanning…";
    rerunSpin.style.display = "inline-block";
    rerunSpin.classList.add("on");
    try {
      const res = await fetch("/api/rescan", { method: "POST" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "rescan failed");
      showBanner("ok", `Scan complete in ${data.duration_sec}s — reloading…`);
      setTimeout(() => window.location.reload(), 500);
    } catch (e) {
      showBanner("warn",
        "Rerun failed: " + (e.message || e) +
        ". Make sure the server is running (double-click <code>run.command</code>)."
      );
      rerunBtn.disabled = false;
      rerunLabel.textContent = "Rerun scan";
      rerunSpin.style.display = "none";
      rerunSpin.classList.remove("on");
    }
  });
}

let currentFilter = "all";
let currentQuery = "";

for (const btn of document.querySelectorAll(".filter-btn")) {
  btn.addEventListener("click", () => {
    for (const b of document.querySelectorAll(".filter-btn")) b.classList.remove("active");
    btn.classList.add("active");
    currentFilter = btn.dataset.filter;
    renderRows(currentFilter, currentQuery);
  });
}
document.getElementById("search").addEventListener("input", (e) => {
  currentQuery = e.target.value;
  renderRows(currentFilter, currentQuery);
});

renderSummary();
renderRows();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# JV dashboard (standalone single-file HTML)
# ---------------------------------------------------------------------------

JV_TEMPLATE_FILE = SCRIPT_DIR / "jv-dashboard.html"


JV_DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>JV Hub Content Check — Endocrine</title>
<style>
  :root {
    --amber-50:#FFFBEB; --amber-100:#FEF3C7; --amber-200:#FDE68A;
    --amber-500:#F59E0B; --amber-600:#D97706; --amber-700:#B45309;
    --amber-900:#78350F;
    --slate-50:#F8FAFC; --slate-100:#F1F5F9; --slate-200:#E2E8F0;
    --slate-300:#CBD5E1; --slate-500:#64748B; --slate-600:#475569;
    --slate-700:#334155; --slate-900:#0F172A;
    --green-100:#DCFCE7; --green-600:#16A34A; --green-700:#15803D;
    --red-100:#FEE2E2; --red-600:#DC2626; --red-700:#B91C1C;
    --blue-100:#DBEAFE; --blue-600:#2563EB; --blue-700:#1D4ED8;
    --yellow-100:#FEF9C3; --yellow-700:#A16207;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: linear-gradient(180deg, var(--amber-50) 0%, #fff 280px);
    color: var(--slate-900);
  }
  header {
    padding: 24px 32px 18px;
    border-bottom: 1px solid var(--amber-200);
    background: #fff;
  }
  header h1 { margin: 0 0 4px; font-size: 24px; color: var(--amber-900); }
  header .sub { color: var(--slate-600); font-size: 14px; }
  header .topnav { margin-top: 8px; font-size: 13px; color: var(--slate-500); }
  header .topnav a { color: var(--amber-700); font-weight: 600; text-decoration: none; margin-right: 14px; }
  header .topnav a:hover { text-decoration: underline; }
  header .meta { float: right; font-size: 11px; color: var(--slate-400); }

  main { padding: 24px 32px 60px; max-width: 1000px; margin: 0 auto; }

  .card { background: #fff; border: 1px solid var(--slate-200); border-radius: 10px; padding: 18px 22px; margin-bottom: 18px; }

  .field { display: block; margin-bottom: 14px; font-size: 13px; color: var(--slate-700); }
  .field > .label {
    display: block; font-weight: 600; margin-bottom: 6px;
    text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em;
    color: var(--slate-600);
  }
  .field select, .field input[type=file] {
    width: 100%; padding: 9px 12px; border: 1px solid var(--slate-300);
    border-radius: 8px; font-size: 14px; background: #fff;
  }
  .submit-row { display: flex; gap: 10px; align-items: center; }
  button.primary {
    padding: 10px 18px; border: none; background: var(--amber-600); color: #fff;
    border-radius: 8px; font-weight: 600; font-size: 14px; cursor: pointer;
  }
  button.primary:hover:not([disabled]) { background: var(--amber-700); }
  button.primary[disabled] { opacity: 0.6; cursor: wait; }
  .hint { font-size: 12px; color: var(--slate-500); }

  .banner { padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 14px; }
  .banner.info { background: var(--amber-50); border: 1px solid var(--amber-200); color: var(--amber-900); }
  .banner.ok   { background: var(--green-100); border: 1px solid #86EFAC; color: var(--green-700); }
  .banner.err  { background: var(--red-100);   border: 1px solid #FCA5A5; color: var(--red-700); }

  .results h2 { font-size: 18px; margin: 6px 0 12px; color: var(--amber-900); }
  .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 18px; }
  .stat {
    background: var(--slate-50); border: 1px solid var(--slate-200);
    border-radius: 10px; padding: 12px 14px;
  }
  .stat .n { font-size: 26px; font-weight: 700; line-height: 1; }
  .stat .label { font-size: 11px; color: var(--slate-500); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 4px; }

  table.hubs { width: 100%; border-collapse: collapse; margin-bottom: 18px; font-size: 13px; }
  table.hubs th, table.hubs td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--slate-100); }
  table.hubs th { background: var(--slate-50); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--slate-500); }
  table.hubs .ctr { text-align: center; width: 80px; }
  table.hubs tr.hub-row { cursor: pointer; }
  table.hubs tr.hub-row:hover td { background: var(--amber-50); }

  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .pill.exist { background: var(--green-100); color: var(--green-700); }
  .pill.missing { background: var(--slate-100); color: var(--slate-500); }
  .coverage-bar { display: inline-block; width: 120px; height: 8px; background: var(--slate-100); border-radius: 999px; vertical-align: middle; overflow: hidden; }
  .coverage-bar > span { display: block; height: 100%; background: var(--green-600); }
  .coverage-bar.low > span { background: var(--red-600); }
  .coverage-bar.mid > span { background: var(--amber-600); }

  .lo-list { margin: 6px 0 0; padding-left: 16px; list-style: none; }
  .lo-list li { padding: 3px 0; font-size: 13px; line-height: 1.45; }
  .lo-list li.missing { color: var(--red-700); }
  .lo-list li.covered { color: var(--green-700); }
  .lo-list .badge { display: inline-block; width: 20px; font-weight: 700; text-align: center; }
  .lo-list li.missing .badge { color: var(--red-600); }
  .lo-list li.covered .badge { color: var(--green-600); }
  .lo-list .score { margin-left: 6px; font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: var(--slate-500); }
  .detail-block { margin-top: 12px; padding: 12px 14px; background: var(--slate-50); border-radius: 8px; }
  .detail-block h3 { margin: 0 0 8px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--slate-600); }
</style>
</head>
<body>
<header>
  <span class="meta" id="generated"></span>
  <h1>JV Hub Content Check</h1>
  <div class="sub">Standalone tool. Upload an updated learning-objectives <code>.docx</code> for a single lecture — the tool parses the LOs in your browser and tells you which ones are already covered by each existing hub page.</div>
  <div class="topnav">
    <a href="dashboard.html">← Hub Update Tracker</a>
  </div>
</header>

<main>
  <div class="card">
    <form id="jv-form">
      <div class="field">
        <span class="label">Lecture</span>
        <select id="lecture" required></select>
        <div class="hint">Hub-page text is baked into this file — it's a snapshot from when <code>scan.py</code> last ran.</div>
      </div>

      <div class="field">
        <span class="label">Updated Learning Objectives file (.docx)</span>
        <input type="file" id="loFile" accept=".docx" required />
        <div class="hint">Must be a Word <code>.docx</code> with a heading "At the end of the lecture students should be able to:" followed by the objective paragraphs.</div>
      </div>

      <div class="submit-row">
        <button type="submit" class="primary">Analyze</button>
        <span class="hint">Results appear below. Nothing is uploaded — parsing happens in your browser.</span>
      </div>
    </form>
  </div>

  <div id="results"></div>
</main>

<script>
const JV_DATA = __JV_DATA__;
const THRESHOLD = JV_DATA.threshold;
const HUB_ORDER = ["lecture-guide","active-recall","hub1","hub2","hub3"];

document.getElementById("generated").textContent = "snapshot: " + JV_DATA.generated_at;

// ---------------------------------------------------------------------------
// Populate lecture dropdown
// ---------------------------------------------------------------------------
(function() {
  const sel = document.getElementById("lecture");
  sel.innerHTML = '<option value="">— select lecture —</option>';
  for (const l of JV_DATA.lectures) {
    const opt = document.createElement("option");
    opt.value = l.lecture_id;
    const present = l.hub_pages.filter(h => h.path).length;
    opt.textContent = l.lecture_id.toUpperCase() + " — " + l.title + " · " + present + "/5 hub pages";
    sel.appendChild(opt);
  }
})();

// ---------------------------------------------------------------------------
// Minimal ZIP (DEFLATE) extractor for DOCX word/document.xml
// ---------------------------------------------------------------------------
async function extractDocxXml(arrayBuffer) {
  const view = new DataView(arrayBuffer);
  const bytes = new Uint8Array(arrayBuffer);
  const len = bytes.length;
  // Find End-of-Central-Directory Record (signature 0x06054b50)
  let eocd = -1;
  const maxSearch = Math.min(len, 65557 + 22);
  for (let i = len - 22; i >= len - maxSearch; i--) {
    if (i < 0) break;
    if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("Not a valid ZIP / DOCX file.");
  const entries = view.getUint16(eocd + 10, true);
  let cdPos = view.getUint32(eocd + 16, true);

  const decoder = new TextDecoder("utf-8");
  for (let i = 0; i < entries; i++) {
    if (view.getUint32(cdPos, true) !== 0x02014b50) throw new Error("Bad ZIP central directory.");
    const compression = view.getUint16(cdPos + 10, true);
    const csize = view.getUint32(cdPos + 20, true);
    const nameLen = view.getUint16(cdPos + 28, true);
    const extraLen = view.getUint16(cdPos + 30, true);
    const commentLen = view.getUint16(cdPos + 32, true);
    const localOff = view.getUint32(cdPos + 42, true);
    const name = decoder.decode(bytes.subarray(cdPos + 46, cdPos + 46 + nameLen));

    if (name === "word/document.xml") {
      if (view.getUint32(localOff, true) !== 0x04034b50) throw new Error("Bad local header.");
      const lNameLen = view.getUint16(localOff + 26, true);
      const lExtraLen = view.getUint16(localOff + 28, true);
      const dataStart = localOff + 30 + lNameLen + lExtraLen;
      const compressed = bytes.subarray(dataStart, dataStart + csize);

      if (compression === 0) return decoder.decode(compressed);
      if (compression === 8) {
        const ds = new DecompressionStream("deflate-raw");
        const writer = ds.writable.getWriter();
        writer.write(compressed);
        writer.close();
        const buf = await new Response(ds.readable).arrayBuffer();
        return decoder.decode(buf);
      }
      throw new Error("Unsupported compression method: " + compression);
    }
    cdPos += 46 + nameLen + extraLen + commentLen;
  }
  throw new Error("word/document.xml not found in the uploaded file.");
}

// ---------------------------------------------------------------------------
// DOCX XML → [{style, text}]
// ---------------------------------------------------------------------------
function parseDocxParagraphs(xmlText) {
  const dom = new DOMParser().parseFromString(xmlText, "application/xml");
  // Parse errors show up as a <parsererror> node
  if (dom.getElementsByTagName("parsererror").length) {
    throw new Error("Couldn't parse document.xml (malformed XML)");
  }
  const ps = dom.getElementsByTagNameNS("*", "p");
  const out = [];
  for (const p of ps) {
    const styleEl = p.getElementsByTagNameNS("*", "pStyle")[0];
    let style = "Normal";
    if (styleEl) {
      // val attribute is namespaced
      const v = styleEl.getAttribute("w:val") || styleEl.getAttributeNS("*","val")
                || styleEl.getAttribute("val");
      if (v) style = v;
    }
    const ts = p.getElementsByTagNameNS("*", "t");
    let text = "";
    for (const t of ts) text += t.textContent || "";
    text = text.trim();
    if (text) out.push({ style, text });
  }
  return out;
}

// ---------------------------------------------------------------------------
// LO parsing (port of Python _parse_los)
// ---------------------------------------------------------------------------
const LO_PREAMBLE_RE = /at the end of the (lecture|video|session|module|activity|class|reading)s?\s+students should be able to/i;
const LO_SKIP_PATTERNS = [/^(textbook|reading|reference|assessment|homework|assignment)s?\b/i, /^(suggested )?readings?\b/i];
const LO_VERBS = new Set(["describe","identify","explain","understand","know","discuss","list","state","define","recognize","predict","distinguish","compare","classify","demonstrate","outline","summarize","illustrate","apply","evaluate","analyze","interpret","assess","diagram","differentiate","relate","derive","solve","determine","calculate","draw","characterize","contrast","label"]);
const LO_MIN_LEN = 20;

function startsWithLoVerb(text) {
  const m = text.match(/^\s*(\w+)/);
  return !!(m && LO_VERBS.has(m[1].toLowerCase()));
}

function cleanLo(text) {
  return text.replace(/^\s*(?:\d+[.)]\s+|[•·●○◦‣▪▫⁃\-–—]\s+|\[\s*\]\s+)/, "").trim();
}

function parseLOs(paragraphs) {
  const los = [];
  let afterPreamble = false;
  for (const { style, text } of paragraphs) {
    if (LO_PREAMBLE_RE.test(text)) { afterPreamble = true; continue; }
    if (!afterPreamble) continue;
    if (style && style.toLowerCase().startsWith("heading")) continue;
    if (LO_SKIP_PATTERNS.some(re => re.test(text.trim()))) continue;
    if (text.trimEnd().endsWith(":") && !startsWithLoVerb(text)) continue;
    if (text.length < LO_MIN_LEN) continue;
    los.push(cleanLo(text));
  }
  const seen = new Set();
  const out = [];
  for (const lo of los) {
    const k = lo.toLowerCase();
    if (!seen.has(k)) { seen.add(k); out.push(lo); }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Coverage scoring (port of Python coverage_report)
// ---------------------------------------------------------------------------
const STOPWORDS = new Set(("a an the and or but of in on for to with by as at from into onto upon about "
  + "is are was were be been being am do does did doing have has had having "
  + "this that these those it its their there here "
  + "can may might must should would will shall could "
  + "if then so than also more less most least very such not no nor "
  + "all any each every some many few several both either neither one two three "
  + "how why what which who whom when where "
  + "describe identify explain understand know discuss list state define recognize "
  + "predict distinguish compare classify demonstrate outline summarize illustrate "
  + "between among including include includes eg ie etc").split(/\s+/));

const WORD_RE = /[A-Za-z][A-Za-z\-']{2,}/g;
const STEM_SUFFIXES = ["ing","ed","es","s"];

function tokenize(text) { return (text.match(WORD_RE) || []).map(w => w.toLowerCase()); }
function contentTokens(text) {
  return tokenize(text).filter(t => t.length >= 4 && !STOPWORDS.has(t));
}
function lightStem(tok) {
  for (const suf of STEM_SUFFIXES) {
    if (tok.length > suf.length + 3 && tok.endsWith(suf)) return tok.slice(0, -suf.length);
  }
  return tok;
}
function normalizeBlob(text) { return text.toLowerCase().replace(/\s+/g, " "); }

function coverageForLo(lo, hubBlob, hubTokens) {
  const toks = contentTokens(lo);
  if (!toks.length) return { lo, score: 1.0, matched_tokens: [], missing_tokens: [] };
  const matched = [];
  const missing = [];
  for (const t of toks) {
    if (hubTokens.has(t) || hubBlob.indexOf(t) >= 0) { matched.push(t); continue; }
    const root = lightStem(t);
    if (root !== t && (hubTokens.has(root) || hubBlob.indexOf(root) >= 0)) { matched.push(t); continue; }
    missing.push(t);
  }
  return {
    lo,
    score: +(matched.length / toks.length).toFixed(2),
    matched_tokens: matched,
    missing_tokens: missing,
  };
}

function coverageReport(los, hubText, threshold = THRESHOLD) {
  const blob = normalizeBlob(hubText);
  const hubTokens = new Set(contentTokens(hubText));
  const covered = [], missing = [];
  for (const lo of los) {
    const r = coverageForLo(lo, blob, hubTokens);
    (r.score >= threshold ? covered : missing).push(r);
  }
  return { threshold, total: los.length, covered, missing };
}

// ---------------------------------------------------------------------------
// Analyze + render
// ---------------------------------------------------------------------------
const results = document.getElementById("results");

document.getElementById("jv-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const lid = document.getElementById("lecture").value;
  const file = document.getElementById("loFile").files[0];
  if (!lid || !file) return;

  results.innerHTML = '<div class="banner info">Parsing ' + escapeHtml(file.name) + ' and comparing against ' + lid.toUpperCase() + '…</div>';

  const lec = JV_DATA.lectures.find(l => l.lecture_id === lid);
  if (!lec) {
    results.innerHTML = '<div class="banner err">Lecture not found in embedded data.</div>';
    return;
  }

  let los;
  try {
    const buf = await file.arrayBuffer();
    const xml = await extractDocxXml(buf);
    const paras = parseDocxParagraphs(xml);
    los = parseLOs(paras);
  } catch (err) {
    results.innerHTML = '<div class="banner err">Could not parse file: ' + escapeHtml(err.message) + '</div>';
    return;
  }

  // Build per-hub results
  const hubResults = [];
  for (const t of HUB_ORDER) {
    const hp = lec.hub_pages.find(h => h.type === t) || { type: t, path: null, text: "" };
    if (!hp.path) {
      hubResults.push({
        type: t, exists: false, path: null, mtime: null,
        total: los.length, covered_count: 0, covered: [],
        missing: los.map(lo => ({ lo, score: 0, matched_tokens: [], missing_tokens: [] }))
      });
      continue;
    }
    const rep = coverageReport(los, hp.text);
    hubResults.push({
      type: t, exists: true, path: hp.path, mtime: hp.mtime,
      total: rep.total, covered_count: rep.covered.length,
      covered: rep.covered, missing: rep.missing,
    });
  }

  // Orphans: LOs missing from every EXISTING hub page
  const existing = hubResults.filter(h => h.exists);
  const orphans = !los.length ? [] :
    (!existing.length ? los.slice() :
      los.filter(lo => existing.every(h => h.missing.some(m => m.lo === lo))));

  renderResults({
    lecture_id: lid, uploaded_filename: file.name,
    los, hub_pages: hubResults, orphan_los: orphans,
  });
});

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k === "style") e.setAttribute("style", v);
    else e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function renderResults(data) {
  results.innerHTML = "";
  const wrap = el("div", {class:"card results"});

  wrap.append(el("h2", {},
    data.lecture_id.toUpperCase() + " — " + data.los.length + " LO" +
    (data.los.length === 1 ? "" : "s") + " parsed from " + data.uploaded_filename
  ));

  const existing = data.hub_pages.filter(h => h.exists);
  const fullyCovered = existing.filter(h => h.covered_count === h.total).length;
  const withGaps = existing.filter(h => h.covered_count < h.total).length;

  const summary = el("div", {class:"summary-grid"},
    el("div", {class:"stat"}, el("div", {class:"n"}, String(data.los.length)), el("div", {class:"label"}, "LOs parsed")),
    el("div", {class:"stat"}, el("div", {class:"n"}, existing.length + "/" + data.hub_pages.length), el("div", {class:"label"}, "Hub pages present")),
    el("div", {class:"stat"}, el("div", {class:"n"}, String(fullyCovered)), el("div", {class:"label"}, "Hub pages covering all LOs")),
    el("div", {class:"stat"}, el("div", {class:"n"}, String(withGaps)), el("div", {class:"label"}, "Hub pages with gaps")),
    el("div", {class:"stat"}, el("div", {class:"n"}, String(data.orphan_los.length)), el("div", {class:"label"}, "Orphan LOs (no hub)")),
  );
  wrap.append(summary);

  if (!data.los.length) {
    wrap.append(el("div", {class:"banner err"},
      "No learning objectives could be parsed. Make sure the docx contains a heading like " +
      "\"At the end of the lecture students should be able to:\" followed by the objective paragraphs."));
    results.append(wrap);
    return;
  }

  const table = el("table", {class:"hubs"});
  table.innerHTML = '<thead><tr><th>Hub page</th><th>Status</th><th>Coverage</th><th class="ctr">Covered</th><th class="ctr">Missing</th></tr></thead><tbody></tbody>';
  const tbody = table.querySelector("tbody");

  for (const hp of data.hub_pages) {
    const pct = hp.total ? Math.round(100 * hp.covered_count / hp.total) : 0;
    const barCls = pct < 34 ? "low" : (pct < 80 ? "mid" : "");
    const statusPill = hp.exists
      ? el("span", {class:"pill exist"}, "exists")
      : el("span", {class:"pill missing"}, "missing");

    const bar = el("div", {},
      el("span", {class:"coverage-bar " + barCls}, el("span", {style:"width:"+pct+"%"})),
      el("span", {style:"margin-left:8px;font-size:12px;"}, pct + "%")
    );

    const row = el("tr", {class:"hub-row"},
      el("td", {},
        el("span", {style:"font-weight:600"}, hp.type),
        hp.path ? el("div", {style:"font-size:11px;color:var(--slate-500);margin-top:2px"}, hp.path) : null
      ),
      el("td", {}, statusPill),
      el("td", {}, bar),
      el("td", {class:"ctr"}, String(hp.covered_count)),
      el("td", {class:"ctr"}, String(hp.missing.length))
    );
    tbody.append(row);

    const detail = el("tr", {style:"display:none"},
      el("td", {colspan:5},
        el("div", {class:"detail-block"},
          el("h3", {}, hp.exists
            ? "Missing LOs on " + hp.type
            : hp.type + " does not exist yet — all LOs listed as missing"),
          hp.missing.length
            ? el("ul", {class:"lo-list"}, ...hp.missing.map(m => el("li", {class:"missing"},
                el("span", {class:"badge"}, "•"),
                m.lo,
                el("span", {class:"score"}, "  coverage " + Math.round((m.score||0)*100) + "%")
              )))
            : el("div", {class:"hint"}, "All LOs covered on this page."),
          (hp.covered && hp.covered.length)
            ? el("details", {style:"margin-top:10px"},
                el("summary", {style:"cursor:pointer;font-size:12px;color:var(--slate-500)"},
                  "Show " + hp.covered.length + " covered LO" + (hp.covered.length===1?'':'s')),
                el("ul", {class:"lo-list"},
                  ...hp.covered.map(c => el("li", {class:"covered"},
                    el("span", {class:"badge"}, "✓"),
                    c.lo,
                    el("span", {class:"score"}, "  " + Math.round((c.score||0)*100) + "%")
                  ))
                )
              )
            : null
        )
      )
    );
    row.addEventListener("click", () => {
      detail.style.display = detail.style.display === "none" ? "" : "none";
    });
    tbody.append(detail);
  }
  wrap.append(table);

  if (data.orphan_los.length) {
    wrap.append(el("div", {class:"detail-block"},
      el("h3", {}, "Orphan LOs — not reflected on ANY existing hub page (" + data.orphan_los.length + ")"),
      el("ul", {class:"lo-list"},
        ...data.orphan_los.map(lo => el("li", {class:"missing"},
          el("span", {class:"badge"}, "⚠"), lo
        ))
      )
    ));
  }

  wrap.append(el("details", {style:"margin-top:14px"},
    el("summary", {style:"cursor:pointer;font-size:13px;color:var(--slate-500)"},
      "All " + data.los.length + " LO" + (data.los.length===1?'':'s') + " parsed from the file"),
    el("ul", {class:"lo-list"},
      ...data.los.map((lo, i) => el("li", {},
        el("span", {class:"badge"}, (i+1) + "."),
        lo
      ))
    )
  ));

  results.append(wrap);
  wrap.scrollIntoView({ behavior:"smooth", block:"start" });
}
</script>
</body>
</html>
"""


def render_jv_dashboard(status: dict) -> str:
    """Build a fully standalone JV dashboard HTML with hub-page text baked in."""
    cache = content_mod.ContentCache(CONTENT_CACHE_FILE)
    jv_lectures = []
    for lec in status["lectures"]:
        lid = lec["lecture_id"]
        entry = {
            "lecture_id": lid,
            "title": lec["title"],
            "hub_pages": []
        }
        for hp in lec["hub_pages"]:
            page = {"type": hp["type"], "path": hp.get("path"), "mtime": hp.get("mtime")}
            if hp.get("path"):
                try:
                    text = content_mod.extract_hub(ENDO_ROOT / hp["path"])["text"]
                except Exception:
                    text = ""
                page["text"] = text
            else:
                page["text"] = ""
            entry["hub_pages"].append(page)
        jv_lectures.append(entry)

    data = {
        "generated_at": status["scanned_at"],
        "threshold": LO_COVERAGE_THRESHOLD,
        "endo_root": status["endo_root"],
        "lectures": jv_lectures,
    }
    data_json = json.dumps(data)
    data_json = data_json.replace("</", "<\\/")
    return JV_DASHBOARD_TEMPLATE.replace("__JV_DATA__", data_json)


REPORT_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8" />
<title>Endocrine Hub Update Tracker — Report</title>
<style>
  :root{color-scheme:light;--amber-50:#FFFBEB;--amber-100:#FEF3C7;--amber-200:#FDE68A;--amber-600:#D97706;--amber-700:#B45309;--amber-900:#78350F;--slate-50:#F8FAFC;--slate-100:#F1F5F9;--slate-200:#E2E8F0;--slate-300:#CBD5E1;--slate-400:#94A3B8;--slate-500:#64748B;--slate-600:#475569;--slate-700:#334155;--slate-900:#0F172A;--green-100:#DCFCE7;--green-600:#16A34A;--green-700:#15803D;--red-100:#FEE2E2;--red-600:#DC2626;--red-700:#B91C1C;--blue-100:#DBEAFE;--blue-700:#1D4ED8;--yellow-100:#FEF9C3;--yellow-700:#A16207}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:linear-gradient(180deg,var(--amber-50) 0%,#fff 240px);color:var(--slate-900);font-size:14px}
  header{padding:20px 24px 14px;border-bottom:1px solid var(--amber-200);background:#fff}
  header h1{margin:0 0 4px;font-size:22px;color:var(--amber-900)}
  header .scanned{color:var(--slate-500);font-size:11px;margin-top:6px;font-family:ui-monospace,Menlo,monospace}
  main{padding:20px 24px 60px;max-width:1400px;margin:0 auto}
  .summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:18px}
  .stat{background:#fff;border:1px solid var(--slate-200);border-radius:10px;padding:12px 14px}
  .stat .n{font-size:24px;font-weight:700;line-height:1}
  .stat .label{font-size:11px;color:var(--slate-500);margin-top:6px;text-transform:uppercase;letter-spacing:.04em}
  .stat.fresh .n{color:var(--green-600)}.stat.stale .n{color:var(--red-600)}.stat.new .n{color:var(--blue-700)}.stat.missing .n{color:var(--yellow-700)}
  .toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  .toolbar input[type=search]{flex:1;min-width:180px;padding:8px 11px;border:1px solid var(--slate-300);border-radius:8px;font-size:13px}
  .filter-btn{padding:7px 11px;border:1px solid var(--slate-300);background:#fff;color:var(--slate-700);border-radius:8px;cursor:pointer;font-size:12px;font-weight:500}
  .filter-btn.active{background:var(--amber-600);color:#fff;border-color:var(--amber-600)}
  table.matrix{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--slate-200);border-radius:10px;overflow:hidden;font-size:13px}
  table.matrix th,table.matrix td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--slate-100)}
  table.matrix th{background:var(--slate-50);font-weight:600;color:var(--slate-600);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
  table.matrix th.ctr,table.matrix td.ctr{text-align:center}
  table.matrix tr.lecture-row{cursor:pointer}
  table.matrix tr.lecture-row:hover{background:var(--amber-50)}
  table.matrix tr.detail-row td{background:var(--slate-50);padding:12px 18px;border-bottom:2px solid var(--slate-200)}
  .lid{font-family:ui-monospace,Menlo,monospace;font-weight:600;color:var(--amber-700)}
  .state-pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
  .state-fresh{background:var(--green-100);color:var(--green-700)}.state-stale{background:var(--red-100);color:var(--red-700)}.state-new{background:var(--blue-100);color:var(--blue-700)}.state-missing-sources{background:var(--yellow-100);color:var(--yellow-700)}
  .hub-cell{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:7px;font-size:12px;font-weight:600}
  .hub-fresh{background:var(--green-100);color:var(--green-700)}
  .hub-stale{background:var(--red-100);color:var(--red-700)}
  .hub-gap{background:var(--amber-100);color:var(--amber-700)}
  .hub-missing{background:var(--slate-100);color:var(--slate-300)}
  .details h3{margin:0 0 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--slate-600)}
  .details .file-list{margin:0;padding:0;list-style:none}
  .details .file-list li{font-family:ui-monospace,Menlo,monospace;font-size:11px;padding:3px 0;color:var(--slate-700)}
  .details .hash{color:var(--slate-400);font-size:10px;margin-left:6px}
  .grid-2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  .badge-kind{display:inline-block;padding:1px 6px;margin-right:6px;background:var(--amber-100);color:var(--amber-700);border-radius:4px;font-size:9px;text-transform:uppercase}
  .action-hint{margin-top:10px;padding:9px 12px;background:#fff;border:1px dashed var(--amber-200);border-radius:8px;font-size:12px;color:var(--slate-700)}
  .legend{display:flex;gap:14px;margin-top:12px;padding:8px 12px;background:var(--slate-50);border-radius:8px;font-size:11px;color:var(--slate-600);flex-wrap:wrap}
  .legend .item{display:inline-flex;align-items:center;gap:5px}
  .gaps{margin-top:14px;padding:12px 14px;background:#fff;border:1px solid var(--amber-200);border-radius:10px}
  .gaps h3{margin:0 0 8px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--amber-900)}
  .gap-empty{color:var(--slate-500);font-size:12px;padding:4px 0}
  .gap-empty.ok{color:var(--green-700)}
  .gap-block{margin-top:8px;padding-top:8px;border-top:1px solid var(--slate-100)}
  .gap-block:first-of-type{border-top:none;padding-top:0;margin-top:0}
  .gap-heading{font-size:12px;color:var(--slate-700);margin-bottom:4px}
  .lo-list{margin:3px 0 0;padding:0 0 0 4px;list-style:none;font-size:12px}
  .lo-list li{padding:2px 0;color:var(--slate-700);line-height:1.4}
  .lo-list .lo-dot{display:inline-block;width:16px;font-weight:700}
  .lo-orphan{color:var(--red-700)}.lo-orphan .lo-dot{color:var(--red-600)}
  .lo-missing{color:var(--amber-900)}.lo-missing .lo-dot{color:var(--amber-600)}
</style></head><body>
<header>
  <h1>Endocrine Hub Update Tracker</h1>
  <div class="scanned" id="scanned-at"></div>
</header>
<main>
  <section class="summary" id="summary"></section>
  <div class="toolbar">
    <input type="search" id="search" placeholder="Search lectures, titles, file names…" />
    <button class="filter-btn active" data-filter="all">All</button>
    <button class="filter-btn" data-filter="needs-update">Needs update</button>
    <button class="filter-btn" data-filter="stale">Stale only</button>
    <button class="filter-btn" data-filter="has-gaps">Has LO gaps</button>
    <button class="filter-btn" data-filter="orphan">Orphan LOs</button>
    <button class="filter-btn" data-filter="missing">Missing hubs</button>
    <button class="filter-btn" data-filter="fresh">Fresh only</button>
  </div>
  <table class="matrix" id="matrix">
    <thead><tr>
      <th style="width:72px;">Lecture</th>
      <th>Title</th>
      <th style="width:110px;">State</th>
      <th class="ctr" style="width:62px;">Lec Guide</th>
      <th class="ctr" style="width:62px;">Active Rec</th>
      <th class="ctr" style="width:50px;">Hub 1</th>
      <th class="ctr" style="width:50px;">Hub 2</th>
      <th class="ctr" style="width:50px;">Hub 3</th>
      <th class="ctr" style="width:60px;">Sources</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="legend">
    <span class="item"><span class="hub-cell hub-fresh">✓</span> Fresh</span>
    <span class="item"><span class="hub-cell hub-stale">!</span> Stale (sources changed)</span>
    <span class="item"><span class="hub-cell hub-gap">N</span> N LOs not covered</span>
    <span class="item"><span class="hub-cell hub-missing">—</span> Missing</span>
  </div>
</main>
<script id="status-data" type="application/json">__STATUS__</script>
<script>
const STATUS=JSON.parse(document.getElementById("status-data").textContent);
const HUB_ORDER=["lecture-guide","active-recall","hub1","hub2","hub3"];
let currentFilter="all",currentQuery="";
function el(t,a={},...k){const e=document.createElement(t);for(const[K,V]of Object.entries(a)){if(K==="class")e.className=V;else if(K.startsWith("on"))e.addEventListener(K.slice(2),V);else e.setAttribute(K,V);}for(const c of k)if(c!=null)e.append(c.nodeType?c:document.createTextNode(c));return e;}
document.getElementById("scanned-at").textContent="Scanned: "+STATUS.scanned_at;
function renderSummary(){const s=STATUS.summary,b=document.getElementById("summary");b.innerHTML="";const stats=[["fresh","Fresh",s.fresh],["stale","Stale (content changed)",s.stale],["new","New (unbaselined)",s.new],["missing","Missing hub pages",s.missing_hub_pages],["fresh","Total LOs tracked",s.total_los],["stale","Hub pages with LO gaps",s.hub_pages_with_gaps],["missing","Orphan LOs",s.orphan_los]];for(const[c,l,n]of stats)b.append(el("div",{class:"stat "+c},el("div",{class:"n"},String(n)),el("div",{class:"label"},l)));}
function hubCell(type,lec){const hp=lec.hub_pages.find(h=>h.type===type);if(!hp||!hp.path)return el("span",{class:"hub-cell hub-missing",title:"missing"},"—");const miss=(hp.missing_idx||[]).length,stale=!!hp.stale_reason,tip=[];if(stale)tip.push(hp.stale_reason==="hash-changed"?"stale: content changed since build":"stale: source newer than hub page");else if(miss>0)tip.push(miss+" LO"+(miss===1?"":"s")+" not covered");else if(hp.lo_total>0)tip.push("all "+hp.lo_total+" LOs covered");if(hp.path)tip.push(hp.path);if(hp.mtime)tip.push("updated "+hp.mtime);if(stale)return el("span",{class:"hub-cell hub-stale",title:tip.join("\n")},"!");if(miss>0)return el("span",{class:"hub-cell hub-gap",title:tip.join("\n")},String(miss));return el("span",{class:"hub-cell hub-fresh",title:tip.join("\n")},"✓");}
function renderRows(){const tb=document.getElementById("rows");tb.innerHTML="";const q=currentQuery.trim().toLowerCase();const visible=STATUS.lectures.filter(lec=>{if(q){const hay=[lec.lecture_id,lec.title,lec.state,...(lec.sources||[]),...lec.hub_pages.map(h=>h.path||"")].join(" ").toLowerCase();if(!hay.includes(q))return false;}const anyG=lec.hub_pages.some(h=>h.path&&(h.missing_idx||[]).length>0);const hasO=(lec.orphan_lo_idx||[]).length>0;switch(currentFilter){case"needs-update":return lec.state==="stale"||lec.state==="new"||lec.missing_pages.length>0||anyG;case"stale":return lec.state==="stale";case"has-gaps":return anyG;case"orphan":return hasO;case"missing":return lec.missing_pages.length>0;case"fresh":return lec.state==="fresh"&&!anyG;default:return true;}});if(!visible.length){tb.append(el("tr",{},el("td",{colspan:9,style:"text-align:center;padding:24px;color:var(--slate-500)"},"No matches.")));return;}for(const lec of visible){const row=el("tr",{class:"lecture-row"},el("td",{class:"lid"},lec.lecture_id.toUpperCase()),el("td",{},lec.title),el("td",{},el("span",{class:"state-pill state-"+lec.state},lec.state.replace("-"," "))),...HUB_ORDER.map(t=>el("td",{class:"ctr"},hubCell(t,lec))),el("td",{class:"ctr"},String((lec.sources||[]).length)));row.addEventListener("click",()=>toggleDetail(row,lec));tb.append(row);}}
function toggleDetail(row,lec){const nx=row.nextElementSibling;if(nx&&nx.classList.contains("detail-row")){nx.remove();return;}const anyG=lec.hub_pages.some(h=>h.path&&(h.missing_idx||[]).length>0);const need=(lec.state==="stale"||lec.state==="new"||lec.missing_pages.length>0||anyG);const d=el("tr",{class:"detail-row"},el("td",{colspan:9},el("div",{class:"details grid-2"},el("div",{},el("h3",{},"In House Sources ("+(lec.sources||[]).length+")"),el("ul",{class:"file-list"},...(lec.sources||[]).map(name=>el("li",{},name)))),el("div",{},el("h3",{},"Hub pages"),el("ul",{class:"file-list"},...lec.hub_pages.map(h=>{const gc=(h.missing_idx||[]).length;const st=h.path?(h.stale_reason?" — stale ("+h.stale_reason.replace("-"," ")+")":(gc?" — "+gc+" LO"+(gc===1?"":"s")+" not covered":(h.lo_total?" — all "+h.lo_total+" LOs covered":" — fresh"))):"";const cl=h.path?(h.stale_reason?"color:var(--red-600)":(gc?"color:var(--amber-700)":"color:var(--green-700)")):"color:var(--slate-400)";return el("li",{},el("span",{class:"badge-kind"},h.type),h.path||"(missing)",h.mtime?el("span",{class:"hash"}," updated "+h.mtime):null,st?el("span",{class:"hash",style:cl},st):null);})))),renderGaps(lec),need?el("div",{class:"action-hint"},el("strong",{},"Needs attention. "),lec.stale_pages.length?"Stale: "+lec.stale_pages.join(", ")+". ":"",lec.missing_pages.length?"Missing: "+lec.missing_pages.join(", ")+". ":"",anyG?"Has LO gaps (see above). ":""):el("div",{class:"action-hint"},el("strong",{},"Up to date."))));row.after(d);}
function renderGaps(lec){const orph=(lec.orphan_lo_idx||[]).map(i=>lec.los[i]).filter(Boolean);const pg=lec.hub_pages.filter(h=>h.path&&(h.missing_idx||[]).length>0);if(!lec.los||!lec.los.length)return el("div",{class:"gaps"},el("h3",{},"Content gaps"),el("div",{class:"gap-empty"},"No LOs parsed for this lecture."));if(!orph.length&&!pg.length)return el("div",{class:"gaps"},el("h3",{},"Content coverage ("+lec.los.length+" LOs)"),el("div",{class:"gap-empty ok"},"✓ Every LO is covered on at least one hub page."));const ch=[el("h3",{},"Content gaps ("+lec.los.length+" LOs total)")];if(orph.length)ch.push(el("div",{class:"gap-block"},el("div",{class:"gap-heading"},el("strong",{},orph.length+" LO"+(orph.length===1?"":"s")+" not reflected on ANY existing hub page"),lec.hub_pages.every(h=>!h.path)?el("span",{class:"hash"}," (no hub pages exist yet)"):null),el("ul",{class:"lo-list"},...orph.map(lo=>el("li",{class:"lo-orphan"},el("span",{class:"lo-dot"},"⚠"),lo)))));for(const hp of pg)ch.push(el("div",{class:"gap-block"},el("div",{class:"gap-heading"},el("span",{class:"badge-kind"},hp.type),el("strong",{},hp.missing_idx.length+" of "+hp.lo_total+" LO"+(hp.lo_total===1?"":"s")+" not covered")),el("ul",{class:"lo-list"},...hp.missing_idx.map(([idx,score])=>el("li",{class:"lo-missing"},el("span",{class:"lo-dot"},"•"),lec.los[idx]||"(unknown)",el("span",{class:"hash"}," (coverage "+Math.round((score||0)*100)+"%)"))))));return el("div",{class:"gaps"},...ch);}
for(const b of document.querySelectorAll(".filter-btn"))b.addEventListener("click",()=>{for(const x of document.querySelectorAll(".filter-btn"))x.classList.remove("active");b.classList.add("active");currentFilter=b.getAttribute("data-filter");renderRows();});
document.getElementById("search").addEventListener("input",e=>{currentQuery=e.target.value;renderRows();});
renderSummary();renderRows();
</script></body></html>"""


def _slim_for_report(status: dict) -> dict:
    """Transform status.json into the compact shape the report dashboard expects."""
    slim = {
        "scanned_at": status["scanned_at"],
        "summary": status["summary"],
        "lectures": [],
    }
    for lec in status["lectures"]:
        los = lec.get("los", [])
        lo_idx = {lo: i for i, lo in enumerate(los)}
        # In-house sources = Learning Objective files from Learning Objectives folder only,
        # filenames only (no paths).
        seen, src_names = set(), []
        for s in lec.get("sources", []):
            if s.get("kind") != "lo":
                continue
            if "Learning Objectives" not in (s.get("path") or ""):
                continue
            name = s["path"].rsplit("/", 1)[-1]
            if name not in seen:
                seen.add(name)
                src_names.append(name)
        slim["lectures"].append({
            "lecture_id": lec["lecture_id"],
            "title": lec["title"],
            "state": lec["state"],
            "stale_pages": lec.get("stale_pages", []),
            "missing_pages": lec.get("missing_pages", []),
            "los": los,
            "orphan_lo_idx": [lo_idx[lo] for lo in lec.get("orphan_los", []) if lo in lo_idx],
            "sources": src_names,
            "hub_pages": [
                {
                    "type": h["type"],
                    "path": h.get("path"),
                    "mtime": h.get("mtime"),
                    "stale_reason": h.get("stale_reason"),
                    "lo_total": h.get("lo_total", 0),
                    "lo_covered": h.get("lo_covered", 0),
                    "missing_idx": [[lo_idx[m["lo"]], round(m.get("score", 0), 2)]
                                    for m in h.get("lo_missing", []) if m["lo"] in lo_idx],
                } for h in lec.get("hub_pages", [])
            ],
        })
    return slim


def render_report(status: dict) -> str:
    """Downloadable clean report — sources are LO filenames only, no path/JV/rebuild hints."""
    data_json = json.dumps(_slim_for_report(status), ensure_ascii=False).replace("</", "<\\/")
    return REPORT_TEMPLATE.replace("__STATUS__", data_json)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--init", action="store_true",
                    help="Initialize baseline (accept current state as up-to-date)")
    ap.add_argument("--accept", nargs="+", metavar="endNN",
                    help="Mark specific lectures as freshly built")
    ap.add_argument("--accept-all", action="store_true",
                    help="Mark all lectures with sources as up-to-date")
    args = ap.parse_args()

    if args.accept_all:
        cmd_accept_all()
    elif args.accept:
        cmd_accept(args.accept)
    elif args.init:
        cmd_init(force=True)
    # Always scan + render all HTML outputs at the end
    status = scan()
    DASHBOARD_FILE.write_text(render_dashboard(status))
    JV_TEMPLATE_FILE.write_text(render_jv_dashboard(status))
    REPORT_FILE.write_text(render_report(status))

    s = status["summary"]
    print()
    print("Scanned:", status["scanned_at"])
    print(f"  Fresh:               {s['fresh']}")
    print(f"  Stale (changed):     {s['stale']}")
    print(f"  New (unbaselined):   {s['new']}")
    print(f"  Missing sources:     {s['missing_sources']}")
    print(f"  Hub pages stale:     {s['stale_hub_pages']}")
    print(f"  Hub pages missing:   {s['missing_hub_pages']}")
    print()
    print(f"Hub dashboard:  {DASHBOARD_FILE}")
    print(f"Hub report:     {REPORT_FILE}")
    print(f"JV tool:        {JV_TEMPLATE_FILE}")
    print(f"Baseline:       {BASELINE_FILE}")
    print(f"Status:         {STATUS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
