import json
import os
import re
import subprocess
import sys
import threading

from google import genai
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_FILE = os.path.join(BASE_DIR, "results.json")
SPIDER_FILE = os.path.join(BASE_DIR, "crawler", "spider.py")
FACTCHECK_FILE = os.path.join(BASE_DIR, "factcheck_results.json")
GRAPH_FILE = os.path.join(BASE_DIR, "site_graph.json")

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


def _build_site_graph() -> dict:
    """Build a graph of nodes (pages) and edges (internal links) from results."""
    pages = _load_results()
    node_ids: set = set()
    edges: list = []

    for page in pages:
        source = page.get("page_url", "")
        if not source:
            continue
        node_ids.add(source)
        for target in page.get("internal_links", []):
            if target and target != source:
                node_ids.add(target)
                edges.append({"source": source, "target": target})

    graph = {
        "nodes": [{"id": url} for url in sorted(node_ids)],
        "edges": edges,
    }

    # Persist to site_graph.json
    try:
        with open(GRAPH_FILE, "w", encoding="utf-8") as fh:
            json.dump(graph, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass

    return graph


# ---------------------------------------------------------------------------
# Fact-check helpers
# ---------------------------------------------------------------------------

class FactCheckRequest(BaseModel):
    url: str


def _load_factcheck_cache() -> dict:
    if not os.path.exists(FACTCHECK_FILE):
        return {}
    try:
        with open(FACTCHECK_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_factcheck_cache(cache: dict) -> None:
    try:
        with open(FACTCHECK_FILE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _extract_claims(text: str) -> list:
    """Extract candidate factual sentences from crawled text content."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    claims = []
    for s in sentences:
        s = s.strip()
        # Keep sentences that look like factual statements (not questions)
        if len(s) >= 30 and not s.endswith("?") and not s.startswith("#"):
            claims.append(s)
    # Limit to 5 claims to keep API usage reasonable
    return claims[:5]


def _factcheck_claim(claim: str, api_key: str) -> dict:
    """Send a single claim to the Gemini API for fact-checking."""
    client = genai.Client(api_key=api_key)
    prompt = (
        "You are a fact checking assistant.\n"
        "Compare the extracted claim with real world knowledge.\n\n"
        f'Claim: "{claim}"\n\n'
        "Return JSON format only, no additional text or markdown:\n"
        "{\n"
        '  "claim": "...",\n'
        '  "verification": "true / false / uncertain",\n'
        '  "correct_information": "...",\n'
        '  "confidence_score": "0.0 to 1.0",\n'
        '  "explanation": "..."\n'
        "}"
    )
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
    )
    raw = response.text.strip()
    # Strip markdown code fences if the model wraps its response
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


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


# ---------------------------------------------------------------------------
# Site-graph / Site-map routes
# ---------------------------------------------------------------------------

@app.get("/site-graph")
async def site_graph():
    """Return nodes and edges for the site structure graph as JSON."""
    graph = _build_site_graph()
    return JSONResponse(graph)


@app.get("/site-map", response_class=HTMLResponse)
async def site_map(request: Request):
    """Interactive site structure visualization page."""
    return templates.TemplateResponse("sitemap.html", {"request": request})


# ---------------------------------------------------------------------------
# Fact-check routes
# ---------------------------------------------------------------------------

@app.get("/fact-check", response_class=HTMLResponse)
async def factcheck_page(request: Request):
    """AI Fact-Check dashboard page."""
    pages_data = _load_results()
    urls = [p.get("page_url", "") for p in pages_data if p.get("page_url")]
    return templates.TemplateResponse(
        "factcheck.html", {"request": request, "crawled_urls": urls}
    )


@app.post("/factcheck")
async def factcheck(body: FactCheckRequest):
    """Fact-check claims extracted from a crawled page using Gemini AI."""
    url = body.url.strip()

    api_key = os.environ.get("GOOGLE_AI_STUDIO_API_KEY", "")
    if not api_key:
        return JSONResponse(
            {"error": "GOOGLE_AI_STUDIO_API_KEY environment variable is not set."},
            status_code=500,
        )

    # Return cached result if available
    cache = _load_factcheck_cache()
    if url in cache:
        return JSONResponse(cache[url])

    # Locate the page in crawl results
    pages_data = _load_results()
    page = next((p for p in pages_data if p.get("page_url") == url), None)
    if not page:
        return JSONResponse(
            {"error": "Page not found in crawl results. Please crawl it first."},
            status_code=404,
        )

    text = page.get("text_content", "")
    claims = _extract_claims(text)
    if not claims:
        return JSONResponse(
            {"error": "No factual claims could be extracted from the page content."},
            status_code=400,
        )

    results = []
    for claim in claims:
        try:
            result = _factcheck_claim(claim, api_key)
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning("Fact-check claim failed: %s", exc)
            results.append(
                {
                    "claim": claim,
                    "verification": "uncertain",
                    "correct_information": "Could not verify.",
                    "confidence_score": "0.0",
                    "explanation": "An error occurred while contacting the AI service.",
                }
            )

    total = len(results)
    true_claims = sum(
        1 for r in results if str(r.get("verification", "")).lower().startswith("true")
    )
    false_claims = sum(
        1 for r in results if str(r.get("verification", "")).lower().startswith("false")
    )
    uncertain_claims = total - true_claims - false_claims

    try:
        scores = [float(r.get("confidence_score", 0)) for r in results]
        avg_confidence = sum(scores) / total if total else 0.0
    except (ValueError, TypeError):
        avg_confidence = 0.0

    reliability_score = round((true_claims / total) * avg_confidence, 2) if total else 0.0

    response_data = {
        "url": url,
        "results": results,
        "summary": {
            "number_of_claims": total,
            "true_claims": true_claims,
            "false_claims": false_claims,
            "uncertain_claims": uncertain_claims,
            "overall_reliability_score": reliability_score,
        },
    }

    cache[url] = response_data
    _save_factcheck_cache(cache)

    return JSONResponse(response_data)
