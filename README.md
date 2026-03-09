# Scrapy Web Crawler

A full-stack Python web application that crawls a website using **Scrapy** (with optional **Playwright** JavaScript rendering) and displays the results in a clean, **mobile-responsive** dashboard built with **FastAPI** and **Jinja2 templates**.

---

## Features

- Enter any URL and crawl all internal pages of that domain
- **JavaScript-rendered pages** – automatically detects JS-heavy sites and switches to Playwright (Chromium) rendering
- Extracts: page URL, title, meta description, headings (H1/H2/H3), text content, internal links, and crawl timestamp
- Results stored in `results.json` (no database required)
- Searchable, paginated results table
- Loading indicator with live status polling while a crawl is in progress
- Page detail view showing headings, full text, and all internal links
- **Interactive Site Map** – force-directed graph of page relationships
- **AI Fact Checker** – uses Gemini AI to verify factual claims extracted from crawled pages
- **AI Knowledge Query** – chat-style interface to ask questions about the crawled website; powered by Gemini AI
- **Account Manager** – automated signup with temporary email, email verification, login, and session persistence
- **Fully responsive** – works on desktop, tablet, and mobile with a hamburger navigation menu

---

## Project Structure

```
project/
├── app.py                   # FastAPI application
├── crawler/
│   ├── __init__.py
│   ├── spider.py            # Scrapy + Playwright spider
│   └── auth.py              # Email-verification authentication automation
├── templates/
│   ├── index.html           # Home page
│   ├── results.html         # Results list
│   ├── page.html            # Page detail view
│   ├── factcheck.html       # AI Fact-Check dashboard
│   ├── sitemap.html         # Interactive site graph
│   ├── ask.html             # AI Knowledge Query chat UI
│   └── account_manager.html # Account Manager dashboard
├── static/
│   └── style.css            # Responsive stylesheet
├── results.json             # Crawl output (auto-generated)
├── knowledge_index.json     # Page summaries index (auto-generated)
├── qa_cache.json            # AI Q&A cache (auto-generated)
├── factcheck_results.json   # Fact-check cache (auto-generated)
├── accounts.json            # Created accounts (auto-generated)
├── session_N.json           # Saved browser sessions (auto-generated)
├── requirements.txt
├── Dockerfile
└── README.md
```

---

## Requirements

- Python 3.9+
- pip

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/rohitkumar91131/Scrapy-web-crawler.git
cd Scrapy-web-crawler

# 2. (Optional) Create and activate a virtual environment
python -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright's Chromium browser
playwright install chromium
```

---

## Environment Variables

### Gemini AI (required for Fact Check and Ask AI features)

```bash
export GOOGLE_AI_STUDIO_API_KEY=your_api_key
```

Obtain your key at [https://aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey).

> **Note:** The Fact-Check and Ask AI pages are accessible without the key, but submitting a request will return an error until the variable is set.

---

## Running the Application

```bash
python app.py
```

Or with uvicorn directly:

```bash
uvicorn app:app --reload
```

Then open your browser and navigate to: **http://localhost:8000**

---

## Usage

1. Open **http://localhost:8000** in your browser.
2. Enter a website URL in the input box (e.g. `https://example.com`).
3. Click **Crawl Website**.
4. The Results page shows a loading indicator while the crawl runs.
5. Once finished, browse, search, and paginate through all crawled pages.
6. Click **View** on any row to open the page detail view.
7. Click **🤖 Fact Check** to verify factual claims on any crawled page using Gemini AI.
8. Click **🗺 Site Map** for an interactive force-directed graph of the site structure.
9. Click **🧠 Ask AI** to ask natural-language questions about the crawled website content.
10. Click **🔐 Accounts** to open the Account Manager and create authenticated sessions.

---

## JavaScript Rendering (Playwright)

The spider first fetches pages with plain HTTP for performance. If the response contains a JavaScript wall (e.g. "Enable JavaScript to continue") or has very little visible text, the spider automatically switches to **Playwright Chromium** rendering and re-fetches the page (and all subsequent internal links) with full JS execution and `networkidle` wait.

No configuration needed – the detection is automatic.

---

## Account Manager (`/account-manager`)

The Account Manager automates the full signup-and-login flow for websites that require email verification before allowing login.

### How it works

1. **Temporary email generation** – A random address is generated using the [1secmail](https://www.1secmail.com/) API (no registration required). Example: `abc123xyz@1secmail.com`.

2. **Signup automation** – A headless Chromium browser (Playwright) opens the target site's signup page, fills in the `username`, `email`, and `password` fields, and submits the form.

3. **Inbox polling** – The 1secmail API is polled every 5 seconds until a verification/confirmation email arrives (up to 2 minutes by default).

4. **Verification link extraction** – The email body is parsed and the verification URL (e.g. `https://example.com/verify?token=xxxx`) is extracted using a regular-expression pattern.

5. **Verification completion** – Playwright opens the verification link and waits for the page to load, completing the email-verification step.

6. **Login automation** – Playwright opens the login page, fills in the credentials, and submits the form.

7. **Session persistence** – Cookies and `localStorage` from the authenticated browser context are saved to `session_N.json` (one file per account). These files can be loaded by the spider to crawl authenticated pages.

8. **Failure handling** – If email verification fails (e.g. no email received within the timeout), the flow is automatically retried with a fresh temporary email address (up to 3 retries by default).

### Dashboard

Navigate to **`/account-manager`** to:
- View all created accounts
- See the temporary email address used for each account
- Check email verification status (`pending` / `verified` / `failed`)
- Check login status (`pending` / `success` / `failed`)
- See the path to the saved session file

### API usage

```http
POST /account-manager/create
Content-Type: application/json

{
  "signup_url": "https://example.com/register",
  "login_url":  "https://example.com/login",
  "password":   "SecurePass123!"
}
```

```http
GET /account-manager/status/{account_index}
```

### Programmatic usage

```python
from crawler.auth import (
    generate_temp_email,
    signup_with_playwright,
    wait_for_verification_email,
    extract_verification_link,
    complete_verification,
    login_and_save_session,
    load_session,
    create_account_with_verification,
)

# One-shot helper
result = create_account_with_verification(
    signup_url="https://example.com/register",
    login_url="https://example.com/login",
    password="MyPassword123!",
    session_file="session.json",
)
print(result)
# {'email': 'abc123@1secmail.com', 'verification_status': 'verified', ...}

# Load session for reuse
session = load_session("session.json")
# session['cookies'] and session['localStorage'] are ready to inject
```

---

## AI Knowledge Query (`/ask`)

After every successful crawl the spider builds `knowledge_index.json` — a structured index of each page's URL, title, summary (generated by Gemini), and content. The `/ask` endpoint loads this index, sends it as context to Gemini together with the user's question, and returns a concise answer along with the source page URLs. Answers are cached in `qa_cache.json` to avoid repeated API calls.

---

## Configuration

The spider uses the following default settings (adjustable in `crawler/spider.py`):

| Setting | Default | Description |
|---|---|---|
| `DEPTH_LIMIT` | 3 | Maximum link depth to crawl |
| `CLOSESPIDER_PAGECOUNT` | 100 | Maximum pages per crawl |
| `ROBOTSTXT_OBEY` | True | Respect robots.txt |
| `DOWNLOAD_DELAY` | 0.5 s | Delay between requests |
| `PLAYWRIGHT_BROWSER_TYPE` | chromium | Browser used for JS rendering |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Crawler | [Scrapy](https://scrapy.org/) |
| JS Rendering | [Playwright](https://playwright.dev/python/) via [scrapy-playwright](https://github.com/scrapy-plugins/scrapy-playwright) |
| Backend | [FastAPI](https://fastapi.tiangolo.com/) |
| Templates | [Jinja2](https://jinja.palletsprojects.com/) |
| Server | [Uvicorn](https://www.uvicorn.org/) |
| AI | [Gemini API (google-genai)](https://ai.google.dev/) |
| Temp Email | [1secmail API](https://www.1secmail.com/api/) |
| Storage | JSON files |

---

## Docker

### Build the image

```bash
docker build -t scrapy-web-crawler .
```

### Run the container locally

```bash
docker run -p 10000:10000 -e GOOGLE_AI_STUDIO_API_KEY=your_api_key scrapy-web-crawler
```

Then open your browser and navigate to: **http://localhost:10000**

> The Dockerfile automatically installs Playwright's Chromium binary (`RUN playwright install chromium`) and all required system libraries.

### Deploy on Render

1. Push the repository (including the `Dockerfile`) to GitHub.
2. Create a new **Web Service** on [Render](https://render.com) and connect your repository.
3. Render will detect the `Dockerfile` automatically and build the image.
4. Set the `GOOGLE_AI_STUDIO_API_KEY` environment variable in the Render service settings.
5. Deploy — Render will run the container using the `CMD` defined in the `Dockerfile`.

