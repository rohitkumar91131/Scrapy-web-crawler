# Scrapy Web Crawler

A full-stack Python web application that crawls a website using **Scrapy** and displays the results in a clean web dashboard built with **FastAPI** and **Jinja2 templates**.

---

## Features

- Enter any URL and crawl all internal pages of that domain
- Extracts: page URL, title, meta description, headings (H1/H2/H3), text content, internal links, and crawl timestamp
- Results stored in `results.json` (no database required)
- Searchable, paginated results table
- Loading indicator with live status polling while a crawl is in progress
- Page detail view showing headings, full text, and all internal links

---

## Project Structure

```
project/
├── app.py                # FastAPI application
├── crawler/
│   ├── __init__.py
│   └── spider.py         # Scrapy spider
├── templates/
│   ├── index.html        # Home page
│   ├── results.html      # Results list
│   └── page.html         # Page detail view
├── static/
│   └── style.css         # Stylesheet
├── results.json          # Crawl output (auto-generated)
├── requirements.txt
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

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Running the Application

```bash
uvicorn app:app --reload
```

Then open your browser and navigate to: **http://localhost:8000**

---

## Usage

1. Open **http://localhost:8000** in your browser.
2. Enter a website URL in the input box (e.g. `https://example.com`).
3. Click **Crawl Website**.
4. You will be redirected to the Results page, which shows a loading indicator while the crawl is running.
5. Once the crawl finishes, the results table is displayed with URL, title, link count, and crawl time.
6. Use the **Search** box to filter results by URL, title, or page content.
7. Click **View** on any row to open the page detail view (headings, full text, internal links).

---

## Configuration

The spider uses the following default settings (adjustable in `crawler/spider.py`):

| Setting | Default | Description |
|---|---|---|
| `DEPTH_LIMIT` | 3 | Maximum link depth to crawl |
| `CLOSESPIDER_PAGECOUNT` | 100 | Maximum pages per crawl |
| `ROBOTSTXT_OBEY` | True | Respect robots.txt |
| `DOWNLOAD_DELAY` | 0.5 s | Delay between requests |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Crawler | [Scrapy](https://scrapy.org/) |
| Backend | [FastAPI](https://fastapi.tiangolo.com/) |
| Templates | [Jinja2](https://jinja.palletsprojects.com/) |
| Server | [Uvicorn](https://www.uvicorn.org/) |
| Storage | JSON file (`results.json`) |
