# Endocrine Hub Update Tracker

A small tool that tells you **which hub pages need rebuilding** after you upload new
lecture content or learning objectives.

## Fastest way to use it

**Double-click `run.command`.** That launches a tiny local server and opens the
dashboard in your browser. The dashboard has a **Rerun scan** button — click it
whenever you upload new content and the page will rescan + reload.

Leave the Terminal window open while you're using it (Ctrl+C or close the
window to stop).

- `dashboard.html` — main tracker. Every lecture × hub page. Rerun button lives
  here. Falls back to a helpful message if opened directly via `file://`.
- `jv-dashboard.html` — **fully standalone** JV tool. Upload a single updated
  LO `.docx`, pick the lecture, get a focused coverage report. Hub-page text
  and all parsing logic are baked in. No server needed — email this file or
  open it on any machine with a modern browser.

## What it checks

For every lecture `endNN` in the endocrine folder, it looks at:

| Source of truth | Location |
|---|---|
| Lecture slides (PDF) | `Per Lecture/endNN_*/` |
| Learning objectives (.docx/.pdf) | `Per Lecture/Learning Objectives/endNN_*` + the matching `Per Lecture/endNN_*/` folder |

And checks these hub pages:

| Hub page | Location |
|---|---|
| lecture-guide | `lecture-guides/endNN-*.html` |
| active-recall | `active-lesson-drills/endNN_active_recall.html` |
| hub1 | `testing-drills/endNN-hub1-*.html` |
| hub2 | `testing-drills/endNN-hub2-*.html` |
| hub3 | `testing-drills/endNN-hub3-*.html` |

## How it decides a hub page needs updating

Three signals, layered:

1. **Content hash** *(primary)* — SHA-256 of each source file is compared against the
   baseline captured when the hub page was last "accepted". Any hash change → the hub
   page for that lecture is stale.
2. **Modification time** *(fallback when no baseline exists)* — if a source file's
   `mtime` is later than the hub page's `mtime`, the page is flagged.
3. **Learning-objective coverage** *(content-level)* — for each source LO (parsed
   from `.docx` files), the tool checks whether the objective's distinctive tokens
   appear in the hub page's text. An LO is marked **uncovered** if fewer than 60%
   of its content tokens appear on the page. This catches cases where the page
   exists and looks fresh but is actually missing content.

### Orphan LOs

An LO that is not covered on **any** existing hub page for that lecture is called
an **orphan**. The dashboard reports orphans per lecture — these are the exact
pieces of new content that the study hub doesn't yet reflect anywhere.

## Usage

### Main flow

Double-click `run.command`. That regenerates `dashboard.html` + `jv-dashboard.html`
with the latest content from your endocrine folder and opens the main dashboard.
Use the "JV tool" link in the header to switch to the JV page.

### Command line

```bash
cd "hub-update-tracker"

# Scan + regenerate both HTML files
python3 scan.py

# After you've rebuilt one or more lectures, mark them as current
python3 scan.py --accept end01 end02

# Or mark everything as current in one go
python3 scan.py --accept-all

# Force re-initialize the baseline to the current state (overwrites everything)
python3 scan.py --init
```

Each scan (re)writes these files:

- `dashboard.html` — open this in your browser to see the matrix
- `status.json` — the latest scan, raw (includes extracted LOs and per-page coverage)
- `baseline.json` — the stored "known-good" hashes (updated by `--accept` / `--init`)
- `content-cache.json` — extracted text + LOs, keyed by SHA-256 (so re-scans are fast)

## Files in this folder

| File | Purpose |
|---|---|
| `run.command` | Double-click to rescan and open the dashboard |
| `scan.py` | Scanner + generator for both HTML dashboards |
| `content.py` | Content extraction (docx/pdf/html) + LO coverage scoring |
| `dashboard.html` | **Standalone** main dashboard (data embedded) — share or open anywhere |
| `jv-dashboard.html` | **Standalone** JV content-check tool (hub text + parsing logic embedded) |
| `baseline.json` | Accepted source-file hashes per lecture |
| `status.json` | Latest scan output |
| `content-cache.json` | Extraction cache (keyed by SHA-256) |

## Workflow

1. You upload new content into `Per Lecture/endNN_*/` or new LO docs into
   `Per Lecture/Learning Objectives/`.
2. Run `python3 scan.py`. Open `dashboard.html`.
3. The matrix shows each lecture × hub page. Red `!` = stale, grey `—` = missing,
   green `✓` = fresh.
4. Rebuild the flagged hub pages (use the `hub-builder` / `study-planner-generator` /
   `hub3-generator` skills as appropriate).
5. Run `python3 scan.py --accept endNN` for each rebuilt lecture so the dashboard
   goes green.

## Dashboard features

- Summary cards at the top (fresh / stale / missing / LO coverage / orphan LOs)
- Filters: All, Needs update, Stale only, Has LO gaps, Orphan LOs, Missing hubs, Fresh only
- Full-text search across lecture ids, titles, source filenames, hub page filenames
- Click any row to expand. The detail view shows:
  - Source files with hashes
  - Hub pages with timestamps and per-page LO coverage (e.g. "4 of 6 LOs covered")
  - **Content gaps** — the exact LO texts missing from each hub page, and the
    "orphan" LOs not reflected anywhere

Hub-cell legend:

- `✓` fresh, all LOs covered
- `N` fresh but N LOs are not covered on this page (amber)
- `!` stale — sources changed since the page was last built (red)
- `—` missing — the hub page doesn't exist yet
