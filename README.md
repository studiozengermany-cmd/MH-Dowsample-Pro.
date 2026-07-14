# 🎧 MH-Dowsample (Audio Organizer Pro v4.1)

A production-grade, CLI-first audio sample organizer tailored for music producers. It automatically inspects quality, classifies content (loop/one-shot/fx), estimates BPM & Key, normalizes to tagged PCM WAV, deduplicates with SHA-256, and maintains an SQLite inventory.

## ✨ Key Features
- **Smart Classification**: Uses `librosa` for spectral analysis to detect BPM, Key, and audio type (Loop, One-shot, FX, Ambient, DnB, Trap, House, etc.).
- **Auto-Normalization**: Converts all formats to standard 16/24-bit PCM WAV.
- **Concurrent Pipeline**: Blazing fast processing with multi-threading and an SQLite-backed metadata store.
- **Multi-source Web Crawler**: Accepts direct audio URLs or catalogue pages from any public website. It discovers audio through network responses, JSON APIs, HTML media attributes, resource timing, and play controls; site-specific adapters remain optional optimizations.
- **Telegram Bot Integration**: Trigger processing remotely via a private admin bot.

## 🚀 Getting Started

### 1. Requirements
- Python 3.11+
- Node.js on PATH (recommended on Windows for the Playwright driver)
- `ffmpeg` and `ffprobe` installed and added to system PATH.

### 2. Installation
```bash
# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\activate   # Windows

# Install dependencies
python -m pip install -r requirements.txt -r requirements-dev.txt

# Install the headless Chromium runtime used by the crawler
python -m playwright install chromium --only-shell

# Setup environment variables
copy .env.example .env
```

### 3. Configuration
Edit `.env` to match your local paths.
- Define `OUTPUT_DIR`, `TEMP_DIR`, `DB_PATH`.
- Optionally tune `CRAWL_WAIT_SEC`, `CRAWL_TIMEOUT_SEC`, and `CRAWL_LAUNCH_TIMEOUT_MS`.
- Add `TELEGRAM_TOKEN` and `ADMIN_USER_ID` if using the bot.

## 🛠️ Usage

### Organize Local Samples
```bash
# Basic run
python organize.py --input ./raw_samples --output ./organized

# High-performance run with 8 workers
python organize.py --input ./raw_samples --workers 8

# Dry run (test classification without modifying files)
python organize.py --input ./raw_samples --dry-run

# View database stats
python organize.py --stats

# Upgrade files created by an older version to the clean layout
python organize.py --rebuild-layout
```

**Output Structure:**
The local library is organized for browsing in a DAW, independently of the website it came from:

```text
organized/
├── Loops/<Genre>/Readable Name - 140 BPM - C major.wav
├── One-Shots/Readable Name - C minor.wav
├── FX/Readable Name.wav
└── Unsorted/Readable Name.wav
```

The source website remains searchable metadata in SQLite. Retained originals use the same content-first layout under `downloads/<Source>/`; long CDN hashes are kept out of visible filenames.
The bot checks the layout version on startup and performs any required one-time migration automatically. Every newly downloaded file is named and filed during the normal processing flow.

### Run Telegram Bot
```bash
python bot.py
```
Commands: `/start`, `/organize PATH`, `/stats`. (Only works for the configured `ADMIN_USER_ID`).

## 🧪 Testing & Verification
The project maintains an enforced 68% minimum coverage gate alongside linting, type checks, and security scans:
```bash
# Run test suite with coverage
python -m pytest tests -v --cov=.

# Linter and Type checking
python -m ruff check .
python -m mypy config.py exceptions.py quality_gate.py processor.py organizer.py organize.py crawler.py bot.py utils tools --ignore-missing-imports
```
