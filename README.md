# JobMan

A local job-search assistant. JobMan scrapes recently posted jobs, filters out
obvious mismatches, uses Claude to score how well each job fits your resume, and
generates a tailored resume and cover letter (as PDFs) for the jobs you choose to
apply to. Everything runs on your own machine; your data lives in `data/`.

## How it works

```
Scrape ──▶ Stage 1 filters ──▶ Stage 2 Claude scoring ──▶ You review ──▶ Tailored resume + cover letter
(LinkedIn,   (rules: title,      (0–100 fit score,          (New Jobs     (Claude writes both, rendered
 Greenhouse)  location, salary,   strengths, gaps;           tab)          to PDF)
              clearance, dupes)   < 70 auto-rejected)
```

1. **Scrape.** Two buttons in the header:
   - **Scrape LinkedIn** uses [JobSpy](https://github.com/speedyapply/JobSpy) to
     search LinkedIn for jobs posted in the last 24 hours. Results are fetched
     one page (10 jobs) at a time, and each page appears in the New Jobs tab
     and starts scoring while the next page loads.
   - **Scrape ATS's** reads Greenhouse postings from the free hosted dataset
     published by [ats-scrapers](https://github.com/kalil0321/ats-scrapers),
     keeping jobs posted in the last 48 hours whose title and location match.
     Greenhouse job titles are shown in mint green.
   - **Scan HN Thread** takes a Hacker News "Who is hiring?" link (e.g.
     `https://news.ycombinator.com/item?id=49922569`). Top-level comments that
     mention a project/product/program manager or PM role are sent to Claude
     Haiku 5.5, which pulls out each role's title, company, location, salary and
     apply link; roles with a matching title are kept. A job's title links to its
     apply URL, and its HN comment is kept as the description. Each comment's
     roles appear and start scoring as soon as they're extracted.
2. **Stage 1 filters (rules, free).** Drops jobs with excluded title words
   (Intern, Junior, VP, …), non-US locations, security-clearance requirements, a
   listed salary below your floor, and duplicates of jobs you already have
   (including the same role posted on more than one board).
3. **Stage 2 scoring (Claude).** A background worker sends each new job, with your
   resume and job preferences, to Claude Haiku 5.5. It gets back a 0–100 fit
   score plus strengths, gaps and dealbreakers. Jobs scoring under 70 move to
   Rejected automatically.
4. **Review.** The **New Jobs** tab lists the remaining jobs sorted best-fit
   first. Expand a row to see the full description and why it scored the way it
   did. Select jobs and click **Start Application** or **Reject**. To skip
   generating a tailored resume and cover letter (e.g. an application you'll
   fill in with your regular resume), click **Start App Without Resume / Cover**
   instead.
5. **Generate.** For each job you start an application for, Claude Sonnet 5
   writes a tailored resume and cover letter from your real resume (it's told
   never to invent experience). Both are saved as PDFs in
   `data/applications/<job id>/`.
6. **Track.** Once you've applied on the employer's site, click
   **Mark as Applied**.

### Tabs

| Tab | Shows |
|---|---|
| 🗑️ (Rejected) | Jobs you rejected and jobs auto-rejected for scoring under 70. **Permanently deleted 48 hours after being rejected.** |
| New Jobs | Scraped, unreviewed jobs, sorted by fit score. |
| Applications | Jobs you've started an application for, plus any marked as applied today. |
| Applied | Jobs marked as applied before today. |
| Profile | Your contact details, work-eligibility answers and job preferences. |

The page updates itself live (over a WebSocket) as scrapes finish, scores
arrive and documents are generated.

## Requirements

- **Linux or macOS** with **Python 3.12** (3.11+ works; ats-scrapers needs 3.11
  or newer). [uv](https://docs.astral.sh/uv/) is recommended for managing the
  environment.
- **An Anthropic API key**, for scoring and document generation. See
  [Environment variables](#environment-variables).
- **WeasyPrint's system libraries** (Pango), for rendering PDFs. On Debian or
  Ubuntu: `sudo apt install libpango-1.0-0 libpangoft2-1.0-0`; on Arch:
  `sudo pacman -S pango`; on macOS: `brew install pango`. See the
  [WeasyPrint install docs](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html)
  for other systems.
- **About 1 GB free disk and 2 GB free RAM** while scraping. The Greenhouse
  dataset is cached on disk (~130 MB).

## Setup

```bash
git clone <this repo> JobMan
cd JobMan

# 1. Create the virtual environment
uv venv --python 3.12 .venv

# 2. The two scraping libraries are installed from local clones in Tools/
#    (Tools/ is in .gitignore, so clone them yourself)
mkdir -p Tools
git clone https://github.com/speedyapply/JobSpy Tools/JobSpy
git clone https://github.com/kalil0321/ats-scrapers Tools/ats-scrapers

# 3. Install everything, including those two clones (run from the repo root)
uv pip install --python .venv/bin/python -r requirements.txt
```

With plain pip instead of uv: `python3.12 -m venv .venv` and then
`.venv/bin/pip install -r requirements.txt`.

## Running it

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./start.sh
```

Then open <http://127.0.0.1:8000>.

`start.sh` runs `uvicorn main:app --reload` from `backend/`, so the server
restarts itself when you edit backend code.

### First run

1. Click **Attach resume** and upload your resume as Markdown (`.md`) or plain
   text (`.txt`). It's saved to `data/resume.md` and used for both scoring and
   tailoring.
2. Open **Profile**, fill it in, and **write your Job Preferences** before
   scraping. For example: target level, industries you want or want to avoid,
   remote/hybrid limits, minimum salary. Scores are much more useful with them.
   Jobs that already have a score keep it when you change your preferences.
3. Edit the search settings in `backend/scrapers.py` (below) to match the roles
   and locations you're looking for. **The defaults are one person's search**
   (Technical Project/Product/Program Manager around San Diego, Hawaii or
   remote).
4. Click **Scrape LinkedIn** and/or **Scrape ATS's**.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Authenticates the Claude calls for scoring (`backend/scorer.py`) and resume/cover-letter generation (`backend/generator.py`). The Anthropic SDK also accepts `ANTHROPIC_AUTH_TOKEN`, or a profile from `ant auth login`, instead. |
| `ANTHROPIC_BASE_URL` | No | Sends Claude requests through a proxy or alternate endpoint. Read by the Anthropic SDK. |
| `ANTHROPIC_LOG` | No | Set to `info` or `debug` to log Claude API requests. |

No other environment variables are read. Everything else is a constant in the
code, listed below.

## Settings you can change

All of these are module-level constants. Edit the file and the server reloads
itself.

### Searching — `backend/scrapers.py`

**LinkedIn (JobSpy)**

| Setting | Default | What it does |
|---|---|---|
| `SEARCH_TERM` | Technical Project/Product Manager in San Diego County, Carlsbad or Hawaii | The LinkedIn search query. |
| `LOCATION` | `"San Diego County, CA"` | The LinkedIn search location. |
| `HOURS_OLD` | `24` | Only jobs posted within this many hours. |
| `results_wanted` (argument of `scrape_daily_jobs`) | `200` | Maximum jobs per LinkedIn scrape. |

**Greenhouse (ats-scrapers dataset)**

| Setting | Default | What it does |
|---|---|---|
| `ATS_TITLE_PATTERN` | `Technical Pro(?:ject\|duct\|gram) Manager` | Regular expression (case-insensitive) a job title must match. |
| `ATS_LOCATION_PATTERN` | `San Diego\|Carlsbad\|Hawaii\|Honolulu\|Remote` | Regular expression the location must match. Any location containing "Remote" passes. |
| `ATS_HOURS_OLD` | `48` | Only jobs posted within this many hours. The hosted dataset is refreshed about once a day and can lag, so a 24-hour window often returns nothing. Raise this to 72 if scrapes come back empty. |
| `ATS_CACHE_DIR` | `data/ats_cache/` | Where downloaded dataset files are cached. A file is re-downloaded only when the dataset publishes a new version, and the old copy is then deleted. |

### Stage 1 filters — `backend/scrapers.py`

These apply to every scrape, LinkedIn included, before anything is stored or
scored. All patterns are case-insensitive regular expressions.

| Setting | Default | What it does |
|---|---|---|
| `EXCLUDE_TITLE_PATTERN` | Intern, Internship, Junior, Jr., VP, Vice President, Associate (but not "Associate Director") | Drops jobs whose title matches. |
| `NON_US_LOCATION_PATTERN` | Canada, UK, India, Europe, … | Drops jobs whose location names another country… |
| `US_LOCATION_PATTERN` | United States, US, USA, California, CA, Hawaii, HI | …unless the location also matches this. So "Remote – US or Canada" is kept, and "Canada Remote" is dropped. |
| `EXCLUDE_DESCRIPTION_PATTERN` | TS/SCI, active clearance, clearance required | Drops jobs whose description matches. |
| `MIN_SALARY` | `130_000` | Drops jobs listing a yearly maximum below this. Jobs with no salary listed, or an hourly rate, are kept. |

Duplicate detection (`_dedupe_key` in `backend/store.py`) treats two jobs as the
same if they have the same title and the same first ~300 characters of
description.

### Scoring — `backend/scorer.py`

| Setting | Default | What it does |
|---|---|---|
| `MODEL` | `"claude-haiku-5-5"` | The Claude model used to score jobs. |
| `AUTO_REJECT_BELOW` | `70` | New jobs scoring below this move to Rejected automatically. |
| `RUBRIC` | — | The scoring instructions and score bands. Edit this to change what counts as a good fit. |

The prompt uses prompt caching on your resume and preferences. Haiku 5.5 caches
prompts of 512 tokens or more, so after the first job in a run the resume and
rubric are read from cache. Scoring costs well under a tenth of a cent per job.

### Resume and cover letter generation — `backend/generator.py`

| Setting | Default | What it does |
|---|---|---|
| `MODEL` | `"claude-sonnet-5"` | The Claude model that writes tailored resumes and cover letters. |
| `max_tokens`, `effort` (in `_complete`) | `4000`, `"medium"` | Output length limit and how much effort the model spends. |
| The prompts in `generate_resume` / `generate_cover_letter` | — | Tone, length (cover letters are under 400 words) and truthfulness rules. |

PDF styling (page size, margins, fonts) is the `CSS` string in `backend/pdf.py`.

### Housekeeping — `backend/main.py`

| Setting | Default | What it does |
|---|---|---|
| `DELETE_REJECTED_AFTER` | `timedelta(hours=48)` | Rejected jobs are **permanently deleted** this long after being rejected. |
| `FORGET_DELETED_AFTER` | `timedelta(days=30)` | Deleted jobs are remembered (in `data/deleted_jobs.json`) this long, so later scrapes don't add them back. |
| `HOUSEKEEPING_INTERVAL_SECONDS` | `600` | How often the auto-reject, delete and move-to-Applied rules run. They also run at server start. |

## Your data

Everything is stored as plain files in `data/`:

| File | Contents |
|---|---|
| `jobs_store.json` | Every job and its status, score and application state. |
| `resume.md` | Your uploaded resume. |
| `profile.json` | Your Profile page: contact details, work-eligibility and self-identification answers, job preferences. |
| `applications/<job id>/` | Generated `resume.pdf` and `cover_letter.pdf` for each job. |
| `deleted_jobs.json` | IDs of recently deleted jobs, to stop them being re-added. |
| `ats_cache/` | Cached copies of the public Greenhouse dataset. Safe to delete; it's re-downloaded when needed. |

`resume.md` and `profile.json` contain personal information. Keep them out of
version control if you share or publish this repository.

What leaves your machine:
- **Anthropic:** your resume, job preferences and job postings, for scoring and
  generation.
- **LinkedIn:** search requests (via JobSpy).
- **The ats-scrapers dataset host (`storage.stapply.ai`):** dataset downloads.
  Nothing about you is sent.

## Project layout

```
backend/
  main.py        FastAPI app: API routes, background workers (scoring,
                 generation, housekeeping), WebSocket live updates
  scrapers.py    LinkedIn + Greenhouse scraping, Stage 1 filters, dataset cache
  scorer.py      Stage 2 Claude fit scoring
  generator.py   Tailored resume / cover letter generation
  pdf.py         Markdown → PDF rendering (WeasyPrint)
  store.py       JSON file storage, duplicate detection
frontend/
  index.html     The whole UI (single page, no build step)
data/            Your jobs, resume, profile and generated documents
Tools/           Local clones of JobSpy and ats-scrapers (not in git)
start.sh         Starts the server
requirements.txt Python dependencies
PROJECT_BRIEF.md The original project brief
```

## Troubleshooting

- **Jobs sit at "…" in the Fit column.** Scoring hasn't run or is still in
  progress. Check that a resume is attached and that `ANTHROPIC_API_KEY` was set
  in the terminal that started the server. A "!" means scoring failed; hover to
  see why. Failed jobs are retried after the next scrape or server restart.
- **Scrape ATS's finds nothing.** The hosted dataset may not have refreshed
  recently. Try raising `ATS_HOURS_OLD`.
- **PDF generation fails with a Pango or cairo error.** Install WeasyPrint's
  system libraries (see [Requirements](#requirements)).
- **The first ATS scrape of the day is slow.** It's downloading the day's
  dataset (~130 MB). Later scrapes use the cached copy and take a few seconds.
