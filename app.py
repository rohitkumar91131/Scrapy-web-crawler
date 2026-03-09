import json
import os
import subprocess
import sys
import threading

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_FILE = os.path.join(BASE_DIR, "results.json")
SPIDER_FILE = os.path.join(BASE_DIR, "crawler", "spider.py")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Scrapy Web Crawler")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ---------------------------------------------------------------------------
# Crawl state (shared between threads)
# ---------------------------------------------------------------------------
_crawl_state: dict = {
    "status": "idle",   # "idle" | "running" | "complete" | "error"
    "url": None,
    "message": "",
}
_state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Background crawl worker
# ---------------------------------------------------------------------------

def _run_crawl(url: str) -> None:
    """Run Scrapy in a subprocess and update crawl_state when finished."""
    # Remove stale results file so we always get a fresh list
    if os.path.exists(RESULTS_FILE):
        try:
            os.remove(RESULTS_FILE)
        except OSError:
            pass

    cmd = [
        sys.executable, "-m", "scrapy", "runspider", SPIDER_FILE,
        "-a", f"start_url={url}",
        "-o", RESULTS_FILE,
        "--logfile", os.devnull,
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
        with _state_lock:
            if result.returncode == 0:
                _crawl_state["status"] = "complete"
                _crawl_state["message"] = "Crawl completed successfully."
            else:
                _crawl_state["status"] = "error"
                err = (result.stderr or "").strip()
                _crawl_state["message"] = err[-1000:] if err else "Crawl failed."
    except subprocess.TimeoutExpired:
        with _state_lock:
            _crawl_state["status"] = "error"
            _crawl_state["message"] = "Crawl timed out after 5 minutes."
    except Exception as exc:  # noqa: BLE001
        with _state_lock:
            _crawl_state["status"] = "error"
            _crawl_state["message"] = str(exc)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _load_results() -> list:
    if not os.path.exists(RESULTS_FILE):
        return []
    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Homepage with URL input form."""
    with _state_lock:
        state = _crawl_state.copy()
    return templates.TemplateResponse("index.html", {"request": request, "crawl_state": state})


@app.post("/crawl")
async def start_crawl(url: str = Form(...)):
    """Start crawling a given URL in the background."""
    with _state_lock:
        if _crawl_state["status"] == "running":
            return RedirectResponse("/results", status_code=303)
        _crawl_state["status"] = "running"
        _crawl_state["url"] = url.strip()
        _crawl_state["message"] = f"Crawling {url.strip()} …"

    thread = threading.Thread(target=_run_crawl, args=(url.strip(),), daemon=True)
    thread.start()
    return RedirectResponse("/results", status_code=303)


@app.get("/crawl-status")
async def crawl_status():
    """Return current crawl state as JSON (used by the loading indicator)."""
    with _state_lock:
        return JSONResponse(_crawl_state.copy())


@app.get("/results", response_class=HTMLResponse)
async def results(request: Request, page: int = 1, search: str = ""):
    """List of crawled pages with pagination and keyword search."""
    with _state_lock:
        state = _crawl_state.copy()

    pages_data = _load_results()

    # Keyword filter
    if search:
        keyword = search.lower()
        pages_data = [
            p for p in pages_data
            if keyword in p.get("title", "").lower()
            or keyword in p.get("page_url", "").lower()
            or keyword in p.get("text_content", "").lower()
        ]

    # Pagination
    page_size = 10
    total = len(pages_data)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    paginated = pages_data[start: start + page_size]

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "pages": paginated,
            "total": total,
            "current_page": page,
            "total_pages": total_pages,
            "search": search,
            "crawl_state": state,
        },
    )


@app.get("/page", response_class=HTMLResponse)
async def page_detail(request: Request, url: str = ""):
    """Detail view for a single crawled page."""
    page = None
    if url:
        pages_data = _load_results()
        page = next((p for p in pages_data if p.get("page_url") == url), None)

    return templates.TemplateResponse(
        "page.html",
        {"request": request, "page": page, "url": url},
    )
