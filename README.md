# Substack Scraper API

A FastAPI service that scrapes Substack newsletters using [Crawl4AI](https://github.com/unclecode/crawl4ai) and optionally summarizes content via [Featherless AI](https://featherless.ai) (OpenAI-compatible LLM API).

- ✅ Auto-detects page type (homepage vs. article) and applies the right CSS selectors
- ✅ Dismisses Substack subscription popups automatically
- ✅ Batch-scrapes all articles from a newsletter archive
- ✅ Summarizes each article with a configurable LLM prompt
- ✅ Runs headless Chromium with stealth mode (anti-bot fingerprinting)

---

## Project Structure

```
.
├── main.py            # FastAPI app with all routes and scraping logic
├── requirements.txt   # Python dependencies
├── run.sh             # One-shot script: creates venv, installs deps, starts server
├── .env.example       # Template for required environment variables
└── .gitignore
```

---

## Prerequisites

- Python 3.10+
- A [Featherless AI](https://featherless.ai) API key (for LLM summarization)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-username/crawl4AI-substack-scraper.git
cd crawl4AI-substack-scraper
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```env
FEATHERLESS_BASE_URL=https://api.featherless.ai/v1
FEATHERLESS_API_KEY=your_featherless_api_key_here
FEATHERLESS_MODEL=Qwen/Qwen3-8B
```

> Any OpenAI-compatible model hosted on Featherless AI will work. [Browse available models →](https://featherless.ai/models)

### 3. Start the server

#### macOS / Linux

```bash
bash run.sh
```

`run.sh` automatically:
- Creates a `.venv` virtual environment if one doesn't exist
- Installs dependencies from `requirements.txt` (skips if already up-to-date)
- Starts the server at `http://localhost:8000`

> **Note:** `run.sh` is a Bash script and only works on **macOS and Linux**. Windows users should follow the manual steps below.

#### Windows / Manual setup

```bash
python3 -m venv .venv

# macOS/Linux
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

---

## API Reference

Interactive docs are available at **`http://localhost:8000/docs`** once the server is running.

### `GET /health`

Health check.

```
GET /health
```

**Response:**
```json
{ "status": "ok", "version": "0.2.0" }
```

---

### `GET /scrape`

Scrape a single Substack URL (homepage or article) and return the content as Markdown.

```
GET /scrape?url=https://example.substack.com/p/some-article
```

| Parameter | Type   | Required | Description                       |
|-----------|--------|----------|-----------------------------------|
| `url`     | string | ✅       | Substack URL (homepage or article) |

**Response:** `ScrapeResult` — includes title, markdown content, links, media, and timing.

---

### `GET /scrape-and-summarize`

Scrape a URL and then summarize the content with an LLM.

```
GET /scrape-and-summarize?url=https://example.substack.com/p/some-article
```

| Parameter    | Type    | Required | Default                                                    | Description                   |
|--------------|---------|----------|------------------------------------------------------------|-------------------------------|
| `url`        | string  | ✅       | —                                                          | Substack URL                  |
| `prompt`     | string  | ❌       | `"Summarize the following newsletter content..."` | Custom LLM prompt             |
| `max_tokens` | integer | ❌       | `1024`                                                     | Max tokens in LLM response (64–4096) |

**Response:** `ScrapeAndSummarizeResult` — everything from `/scrape` plus `summary` and `llm_model`.

---

### `GET /extract-links`

Scrape a Substack homepage and extract all article links.

```
GET /extract-links?url=https://example.substack.com
```

| Parameter | Type   | Required | Description              |
|-----------|--------|----------|--------------------------|
| `url`     | string | ✅       | Substack newsletter root URL |

**Response:** JSON with `article_links`, `all_internal_links`, and `all_external_links`.

---

### `GET /scrape-all`

Scrape and optionally summarize the N most recent articles from a newsletter, in one request.

```
GET /scrape-all?newsletter_url=https://example.substack.com&limit=10&summarize=true
```

| Parameter        | Type    | Required | Default                                             | Description                              |
|------------------|---------|----------|-----------------------------------------------------|------------------------------------------|
| `newsletter_url` | string  | ✅       | —                                                   | Base newsletter URL                      |
| `limit`          | integer | ❌       | `5`                                                 | Number of articles to scrape (1–100)     |
| `summarize`      | boolean | ❌       | `true`                                              | Run LLM summarization on each article    |
| `prompt`         | string  | ❌       | `"Summarize this newsletter article..."` | Custom prompt used for every article     |
| `max_tokens`     | integer | ❌       | `1024`                                              | Max tokens per summary (64–4096)         |

**Response:** `ScrapeAllResult` — list of articles with markdown and summaries, plus aggregate stats.

---

## How It Works

1. **Page detection** — The URL path is inspected to classify it as an `article` (`/p/…`), `homepage`/`archive`, or `unknown`.
2. **CSS selector targeting** — Articles use `.available-content`; homepages use `.portable-archive-list`. Unknown pages fall back to full-page scraping.
3. **Popup dismissal** — A JavaScript snippet runs after page load to click close buttons and remove subscription overlay elements.
4. **Stealth crawling** — Crawl4AI launches a headless Chromium instance with a realistic user-agent and stealth/anti-fingerprinting settings.
5. **LLM summarization** — The scraped Markdown is passed to the configured Featherless AI model. Content is capped at ~24,000 characters (~6,000 tokens) to stay within context limits.

---

## Dependencies

| Package          | Purpose                              |
|------------------|--------------------------------------|
| `fastapi`        | Web framework                        |
| `uvicorn`        | ASGI server                          |
| `crawl4ai`       | Headless browser scraping            |
| `openai`         | OpenAI-compatible LLM client         |
| `pydantic`       | Request/response validation          |
| `python-dotenv`  | `.env` file loading                  |

---

## License

MIT
