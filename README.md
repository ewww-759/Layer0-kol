# Layer0-kol

## Getting Started

This project is an automated agent for discovering and scraping niche Threads accounts. It uses a hybrid architecture: a Chrome-impersonating scraper for discovering seeds via Meta's search endpoint, and a local RSSHub instance for high-concurrency, anti-ban post scraping.

### 1. Prerequisites
- **Python 3.9+**
- **Docker** (Required for the local RSSHub instance)

### 2. Start RSSHub
We use RSSHub to safely bypass Meta's rate limits and graph API restrictions. A `docker-compose.yml` is included in the `rsshub` directory. Start the local instance on port 1200:
```bash
cd rsshub
docker compose up -d
cd ..
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```
*(Ensure `curl_cffi` is installed to bypass TLS fingerprinting).*

### 4. Configure Authentication
Meta restricts unauthenticated searches. You must provide a valid Threads cookie.
1. Log into [threads.net](https://www.threads.net/).
2. Open Developer Tools (F12) -> Network.
3. Refresh and find the `cookie:` string in the Request Headers.
4. Create a file at `config/cookie.json` and paste your entire raw cookie string into it.

### 5. Configure LLM (Optional but Recommended)
Layers 3-5 use an LLM for deep content analysis and personalized outreach. You can choose between cloud APIs or local Ollama.

1. Copy `config/llm_config.yaml.example` to `config/llm_config.yaml`.
2. Set `backend` to `"openai"`, `"gemini"`, or `"ollama"`.
3. For cloud backends, set the API key in the YAML or via env vars (`OPENAI_API_KEY` / `GEMINI_API_KEY`).
4. For local Ollama, ensure the Ollama server is running at `http://localhost:11434`.

> **Note:** The pipeline runs fine without LLM — it falls back to keyword filtering and template-based outreach.

### 6. Run the Pipeline
Once your configuration is ready, launch the pipeline:
```bash
python src/main.py --mode discover
```

The system will:
1. Read keywords from `config/niche_config.yaml`.
2. Discover seed accounts using your cookie.
3. Rapidly scrape recent posts via your local RSSHub.
4. Filter accounts by niche relevance (keywords + LLM validation).
5. Score accounts using heuristic signals + LLM semantic analysis.
6. Generate personalized outreach messages via LLM.
7. Output the final high-value leads to the `output/` directory.

