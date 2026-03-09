import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone

from google import genai
from fastapi import FastAPI, Form, Request, Security, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_FILE = os.path.join(BASE_DIR, "results.json")
SPIDER_FILE = os.path.join(BASE_DIR, "crawler", "spider.py")
FACTCHECK_FILE = os.path.join(BASE_DIR, "factcheck_results.json")
GRAPH_FILE = os.path.join(BASE_DIR, "site_graph.json")
KNOWLEDGE_INDEX_FILE = os.path.join(BASE_DIR, "knowledge_index.json")
QA_CACHE_FILE = os.path.join(BASE_DIR, "qa_cache.json")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
API_KEYS_FILE = os.path.join(BASE_DIR, "api_keys.json")
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
_log = logging.getLogger(__name__)

# Rate limiter (per IP; API key routes use this too)
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Scrapy Web Crawler API",
    description=(
        "Universal platform-aware web crawler with automatic platform detection, "
        "structured content extraction, AI fact-checking, and a public developer API."
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ---------------------------------------------------------------------------
# Crawl state (shared between threads)
# ---------------------------------------------------------------------------
_crawl_state: dict = {
    "status": "idle",   # "idle" | "running" | "complete" | "error"
    "url": None,
    "message": "",
    "platform": None,
    "strategy": None,
}
_state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# API Key management
# ---------------------------------------------------------------------------

def _load_api_keys() -> dict:
    if not os.path.exists(API_KEYS_FILE):
        # Bootstrap a default key on first run
        default_key = "sk_" + secrets.token_hex(16)
        keys = {
            default_key: {
                "key": default_key,
                "name": "Default Key",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "requests_count": 0,
            }
        }
        _save_api_keys(keys)
        _log.info(
            "🔑 New default API key generated: %s — copy it from /developer or api_keys.json",
            default_key,
        )
        return keys
    try:
        with open(API_KEYS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_api_keys(keys: dict) -> None:
    try:
        with open(API_KEYS_FILE, "w", encoding="utf-8") as fh:
            json.dump(keys, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        _log.warning("Failed to save api_keys.json: %s", exc)


def _validate_api_key(key: str | None) -> bool:
    if not key:
        return False
    keys = _load_api_keys()
    if key not in keys:
        return False
    # Increment usage counter (best-effort)
    try:
        keys[key]["requests_count"] = keys[key].get("requests_count", 0) + 1
        _save_api_keys(keys)
    except Exception:
        pass
    return True


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_api_key(key: str = Security(_api_key_header)) -> str:
    if not _validate_api_key(key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key. Pass X-API-Key header.")
    return key


# ---------------------------------------------------------------------------
# Job management (for async crawl API)
# ---------------------------------------------------------------------------

_jobs: dict = {}   # job_id → metadata
_jobs_lock = threading.Lock()


def _job_results_file(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _create_job(url: str, platform_info: dict | None = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "url": url,
        "status": "started",
        "pages_crawled": 0,
        "platform": platform_info.get("platform") if platform_info else None,
        "strategy": platform_info.get("strategy") if platform_info else None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    # Persist
    try:
        with open(os.path.join(JOBS_DIR, f"{job_id}_meta.json"), "w") as fh:
            json.dump(job, fh, indent=2)
    except OSError:
        pass
    return job_id


def _update_job(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)
    # Persist updated metadata
    try:
        meta_path = os.path.join(JOBS_DIR, f"{job_id}_meta.json")
        with _jobs_lock:
            data = dict(_jobs.get(job_id, {}))
        with open(meta_path, "w") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        if job_id in _jobs:
            return dict(_jobs[job_id])
    # Try loading from disk (for persistence across restarts)
    meta_path = os.path.join(JOBS_DIR, f"{job_id}_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass
    return None

# ---------------------------------------------------------------------------
# Background crawl worker
# ---------------------------------------------------------------------------

def _run_crawl(url: str, job_id: str | None = None) -> None:
    """Run Scrapy in a subprocess and update crawl_state when finished."""
    result_file = _job_results_file(job_id) if job_id else RESULTS_FILE

    # Remove stale results file so we always get a fresh list
    if os.path.exists(result_file):
        try:
            os.remove(result_file)
        except OSError:
            pass

    cmd = [
        sys.executable, "-m", "scrapy", "runspider", SPIDER_FILE,
        "-a", f"start_url={url}",
        "-o", result_file,
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
        if job_id:
            if result.returncode == 0:
                pages = _load_job_results(job_id)
                _update_job(
                    job_id,
                    status="complete",
                    pages_crawled=len(pages),
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                err = (result.stderr or "").strip()
                _update_job(
                    job_id,
                    status="error",
                    error=err[-500:] if err else "Crawl failed.",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
        else:
            with _state_lock:
                if result.returncode == 0:
                    _crawl_state["status"] = "complete"
                    _crawl_state["message"] = "Crawl completed successfully."
                else:
                    _crawl_state["status"] = "error"
                    err = (result.stderr or "").strip()
                    _crawl_state["message"] = err[-1000:] if err else "Crawl failed."
        if result.returncode == 0:
            api_key = os.environ.get("GOOGLE_AI_STUDIO_API_KEY", "")
            _build_knowledge_index(api_key)
    except subprocess.TimeoutExpired:
        if job_id:
            _update_job(job_id, status="error", error="Crawl timed out after 5 minutes.",
                        finished_at=datetime.now(timezone.utc).isoformat())
        else:
            with _state_lock:
                _crawl_state["status"] = "error"
                _crawl_state["message"] = "Crawl timed out after 5 minutes."
    except Exception as exc:  # noqa: BLE001
        if job_id:
            _update_job(job_id, status="error", error=str(exc),
                        finished_at=datetime.now(timezone.utc).isoformat())
        else:
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


def _load_job_results(job_id: str) -> list:
    path = _job_results_file(job_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
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
# Knowledge-index helpers
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str


def _load_knowledge_index() -> list:
    if not os.path.exists(KNOWLEDGE_INDEX_FILE):
        return []
    try:
        with open(KNOWLEDGE_INDEX_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _load_qa_cache() -> dict:
    if not os.path.exists(QA_CACHE_FILE):
        return {}
    try:
        with open(QA_CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_qa_cache(cache: dict) -> None:
    try:
        with open(QA_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _summarize_page(page: dict, api_key: str) -> str:
    """Generate a 2-3 sentence summary for a crawled page using Gemini."""
    client = genai.Client(api_key=api_key)
    title = page.get("title", "Untitled")
    text = page.get("text_content", "")[:3000]
    prompt = (
        f"Summarize the following web page content in 2-3 concise sentences.\n"
        f"Page title: {title}\n\n"
        f"Content:\n{text}\n\n"
        "Return only the summary text, no additional formatting."
    )
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
    )
    return response.text.strip()


def _build_knowledge_index(api_key: str) -> None:
    """Build knowledge_index.json with one entry per crawled page."""
    import logging
    pages = _load_results()
    knowledge_index = []
    for page in pages:
        url = page.get("page_url", "")
        title = page.get("title", "Untitled")
        content = page.get("text_content", "")
        summary = ""
        if api_key and content:
            try:
                summary = _summarize_page(page, api_key)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("Summary failed for %s: %s", url, exc)
                summary = content[:300]
        else:
            summary = content[:300]
        knowledge_index.append({
            "url": url,
            "title": title,
            "summary": summary,
            "content": content,
        })
    try:
        with open(KNOWLEDGE_INDEX_FILE, "w", encoding="utf-8") as fh:
            json.dump(knowledge_index, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


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
    pages = _load_results()
    fc_cache = _load_factcheck_cache()
    stats = {
        "total_pages": len(pages),
        "factcheck_count": len(fc_cache),
    }
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "crawl_state": state, "stats": stats},
    )


@app.post("/crawl")
async def start_crawl(url: str = Form(...)):
    """Start crawling a given URL in the background."""
    with _state_lock:
        if _crawl_state["status"] == "running":
            return RedirectResponse("/results", status_code=303)
        _crawl_state["status"] = "running"
        _crawl_state["url"] = url.strip()
        _crawl_state["message"] = f"Crawling {url.strip()} …"
        _crawl_state["platform"] = None
        _crawl_state["strategy"] = None

    def _run_with_detection(target_url: str) -> None:
        from crawler.platform_detector import detect_platform
        from crawler.strategies import run_strategy
        detection = detect_platform(target_url)
        with _state_lock:
            _crawl_state["platform"] = detection.get("platform")
            _crawl_state["strategy"] = detection.get("strategy")
        _log.info(
            "%s detected – using %s",
            detection.get("platform", "Generic"),
            detection.get("strategy", "Scrapy HTML Crawl"),
        )
        # Try API/RSS/Sitemap extraction first
        pages, used_strategy = run_strategy(detection)
        if pages:
            # Save results directly
            try:
                with open(RESULTS_FILE, "w", encoding="utf-8") as fh:
                    json.dump(pages, fh, indent=2, ensure_ascii=False)
            except OSError:
                pass
            with _state_lock:
                _crawl_state["status"] = "complete"
                _crawl_state["strategy"] = used_strategy
                _crawl_state["message"] = (
                    f"{detection.get('platform')} crawl complete via {used_strategy}."
                )
            api_key = os.environ.get("GOOGLE_AI_STUDIO_API_KEY", "")
            _build_knowledge_index(api_key)
        else:
            # Fall back to Scrapy spider
            _run_crawl(target_url)
            with _state_lock:
                _crawl_state["strategy"] = "Scrapy HTML Crawl"

    thread = threading.Thread(
        target=_run_with_detection, args=(url.strip(),), daemon=True
    )
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


# ---------------------------------------------------------------------------
# AI Knowledge Query routes
# ---------------------------------------------------------------------------

@app.get("/ask", response_class=HTMLResponse)
async def ask_page(request: Request):
    """AI Knowledge Query chat interface."""
    return templates.TemplateResponse("ask.html", {"request": request})


@app.post("/ask")
async def ask(body: AskRequest):
    """Answer a question about crawled website content using Gemini AI."""
    question = body.question.strip()
    if not question:
        return JSONResponse({"error": "Question cannot be empty."}, status_code=400)

    api_key = os.environ.get("GOOGLE_AI_STUDIO_API_KEY", "")
    if not api_key:
        return JSONResponse(
            {"error": "GOOGLE_AI_STUDIO_API_KEY environment variable is not set."},
            status_code=500,
        )

    # Return cached answer if available
    cache = _load_qa_cache()
    if question in cache:
        return JSONResponse(cache[question])

    # Load the knowledge index built after the last crawl
    knowledge = _load_knowledge_index()
    if not knowledge:
        return JSONResponse(
            {"error": "No crawled content found. Please crawl a website first."},
            status_code=404,
        )

    # Build context: include summary + first 800 chars of content per page (up to 50 pages)
    context_parts = []
    for entry in knowledge[:50]:
        context_parts.append(
            f"URL: {entry.get('url', '')}\n"
            f"Title: {entry.get('title', 'Untitled')}\n"
            f"Summary: {entry.get('summary', '')}\n"
            f"Content snippet: {entry.get('content', '')[:800]}"
        )
    context = "\n\n---\n\n".join(context_parts)

    prompt = (
        "You are analyzing content from a crawled website.\n"
        "Answer the user's question using only the provided website content.\n"
        "Also list the exact URLs of the pages most relevant to your answer.\n\n"
        f"Website content:\n{context}\n\n"
        f"User question: {question}\n\n"
        "Return JSON format only, no markdown or additional text:\n"
        "{\n"
        '  "answer": "...",\n'
        '  "sources": ["url1", "url2"]\n'
        "}"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
        )
        raw = response.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"error": f"AI query failed: {exc}"},
            status_code=500,
        )

    # Persist to cache
    cache[question] = result
    _save_qa_cache(cache)

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Account Manager helpers
# ---------------------------------------------------------------------------

class AccountCreateRequest(BaseModel):
    signup_url: str
    login_url: str
    password: str


def _load_accounts() -> list:
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_accounts(accounts: list) -> None:
    import logging as _logging
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(accounts, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        _logging.getLogger(__name__).warning("Failed to save accounts file: %s", exc)


def _run_account_creation(
    signup_url: str,
    login_url: str,
    password: str,
    account_index: int,
) -> None:
    """Background worker that runs the full email-verification signup flow."""
    import datetime
    from crawler.auth import create_account_with_verification

    session_file = os.path.join(BASE_DIR, f"session_{account_index}.json")
    result = create_account_with_verification(
        signup_url=signup_url,
        login_url=login_url,
        password=password,
        session_file=session_file,
    )
    result["signup_url"] = signup_url
    result["login_url"] = login_url
    result["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result["index"] = account_index

    accounts = _load_accounts()
    # Replace placeholder entry added before the thread started
    for i, acc in enumerate(accounts):
        if acc.get("index") == account_index:
            accounts[i] = result
            break
    else:
        accounts.append(result)
    _save_accounts(accounts)


# ---------------------------------------------------------------------------
# Account Manager routes
# ---------------------------------------------------------------------------

@app.get("/account-manager", response_class=HTMLResponse)
async def account_manager(request: Request):
    """Account Manager dashboard – shows created accounts and their status."""
    accounts = _load_accounts()
    return templates.TemplateResponse(
        "account_manager.html",
        {"request": request, "accounts": accounts},
    )


@app.post("/account-manager/create")
async def account_manager_create(body: AccountCreateRequest):
    """Trigger account creation with email verification in the background."""
    accounts = _load_accounts()
    account_index = len(accounts)

    # Add a placeholder so the UI can show "pending" immediately
    import datetime
    placeholder = {
        "index": account_index,
        "email": None,
        "username": None,
        "password": body.password,
        "signup_url": body.signup_url,
        "login_url": body.login_url,
        "verification_status": "pending",
        "login_status": "pending",
        "verification_link": None,
        "session_file": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    accounts.append(placeholder)
    _save_accounts(accounts)

    thread = threading.Thread(
        target=_run_account_creation,
        args=(body.signup_url, body.login_url, body.password, account_index),
        daemon=True,
    )
    thread.start()

    return JSONResponse({"status": "started", "account_index": account_index})


@app.get("/account-manager/status/{account_index}")
async def account_manager_status(account_index: int):
    """Return the current status of an account by index."""
    accounts = _load_accounts()
    if account_index < 0 or account_index >= len(accounts):
        return JSONResponse({"error": "Account not found."}, status_code=404)
    return JSONResponse(accounts[account_index])


# ===========================================================================
# Platform Detection page
# ===========================================================================

@app.get("/platform-detection", response_class=HTMLResponse)
async def platform_detection_page(request: Request):
    """Platform detection dashboard page."""
    with _state_lock:
        state = _crawl_state.copy()
    return templates.TemplateResponse(
        "platform_detection.html",
        {"request": request, "crawl_state": state},
    )


# ===========================================================================
# Developer API page
# ===========================================================================

@app.get("/developer", response_class=HTMLResponse)
async def developer_page(request: Request):
    """Developer API documentation and key management page."""
    keys = _load_api_keys()
    return templates.TemplateResponse(
        "developer.html",
        {"request": request, "api_keys": list(keys.values())},
    )


@app.post("/developer/generate-key")
async def generate_api_key(request: Request):
    """Generate a new API key."""
    body = await request.json()
    name = (body.get("name") or "Unnamed Key").strip()[:80]
    new_key = "sk_" + secrets.token_hex(16)
    keys = _load_api_keys()
    keys[new_key] = {
        "key": new_key,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "requests_count": 0,
    }
    _save_api_keys(keys)
    return JSONResponse({"key": new_key, "name": name})


# ===========================================================================
# Public Developer API  –  /api/v1/*
# ===========================================================================

# Pydantic models for API requests
class ApiCrawlRequest(BaseModel):
    url: str


class ApiDetectRequest(BaseModel):
    url: str


class ApiFactCheckRequest(BaseModel):
    text: str


# ── GET /api/v1/stats (no auth – public health check) ──────────────────────

@app.get("/api/v1/stats", tags=["API"])
async def api_stats():
    """Public stats endpoint – no API key required."""
    pages = _load_results()
    fc_cache = _load_factcheck_cache()
    with _state_lock:
        state = _crawl_state.copy()
    return JSONResponse({
        "total_pages": len(pages),
        "factcheck_count": len(fc_cache),
        "crawl_status": state.get("status", "idle"),
        "crawl_url": state.get("url"),
        "platform": state.get("platform"),
        "strategy": state.get("strategy"),
    })


# ── POST /api/v1/crawl ───────────────────────────────────────────────────────

@app.post("/api/v1/crawl", tags=["API"])
@limiter.limit("100/hour")  # Conservative limit: each crawl spawns a background subprocess
async def api_start_crawl(
    request: Request,
    body: ApiCrawlRequest,
    _key: str = Depends(_require_api_key),
):
    """
    Start a new crawl job.  Returns a ``job_id`` to poll for status.

    Requires header: ``X-API-Key: <your-key>``
    """
    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    def _bg(target_url: str, jid: str) -> None:
        from crawler.platform_detector import detect_platform
        from crawler.strategies import run_strategy
        detection = detect_platform(target_url)
        _update_job(
            jid,
            platform=detection.get("platform"),
            strategy=detection.get("strategy"),
            status="running",
        )
        _log.info(
            "API job %s: %s detected – strategy: %s",
            jid,
            detection.get("platform"),
            detection.get("strategy"),
        )
        pages, used_strategy = run_strategy(detection)
        if pages:
            try:
                with open(_job_results_file(jid), "w", encoding="utf-8") as fh:
                    json.dump(pages, fh, indent=2, ensure_ascii=False)
            except OSError:
                pass
            _update_job(
                jid,
                status="complete",
                pages_crawled=len(pages),
                strategy=used_strategy,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        else:
            _run_crawl(target_url, job_id=jid)

    job_id = _create_job(url)
    threading.Thread(target=_bg, args=(url, job_id), daemon=True).start()
    return JSONResponse({"job_id": job_id, "status": "started"}, status_code=202)


# ── GET /api/v1/status/{job_id} ─────────────────────────────────────────────

@app.get("/api/v1/status/{job_id}", tags=["API"])
@limiter.limit("200/hour")
async def api_job_status(
    request: Request,
    job_id: str,
    _key: str = Depends(_require_api_key),
):
    """Return the current status of a crawl job."""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse({
        "job_id": job_id,
        "status": job.get("status"),
        "pages_crawled": job.get("pages_crawled", 0),
        "platform": job.get("platform"),
        "strategy": job.get("strategy"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
    })


# ── GET /api/v1/results/{job_id} ────────────────────────────────────────────

@app.get("/api/v1/results/{job_id}", tags=["API"])
@limiter.limit("100/hour")
async def api_job_results(
    request: Request,
    job_id: str,
    _key: str = Depends(_require_api_key),
):
    """Return the crawled pages for a completed job."""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") not in ("complete",):
        return JSONResponse(
            {"job_id": job_id, "status": job.get("status"), "pages": []},
            status_code=202,
        )
    pages = _load_job_results(job_id)
    return JSONResponse({"job_id": job_id, "pages": pages})


# ── POST /api/v1/detect-platform ────────────────────────────────────────────

@app.post("/api/v1/detect-platform", tags=["API"])
@limiter.limit("100/hour")
async def api_detect_platform(
    request: Request,
    body: ApiDetectRequest,
    _key: str = Depends(_require_api_key),
):
    """
    Detect the platform of a given URL and return the optimal extraction strategy.
    """
    from crawler.platform_detector import detect_platform
    if not body.url.strip():
        raise HTTPException(status_code=400, detail="url is required")
    result = detect_platform(body.url.strip())
    return JSONResponse({
        "url": result["url"],
        "platform": result["platform"],
        "strategy": result["strategy"],
        "api_endpoint": result.get("api_endpoint"),
        "rss_url": result.get("rss_url"),
        "sitemap_url": result.get("sitemap_url"),
        "js_heavy": result.get("js_heavy", False),
        "signals": result.get("signals", []),
    })


# ── POST /api/v1/factcheck ───────────────────────────────────────────────────

@app.post("/api/v1/factcheck", tags=["API"])
@limiter.limit("50/hour")
async def api_factcheck_text(
    request: Request,
    body: ApiFactCheckRequest,
    _key: str = Depends(_require_api_key),
):
    """
    Fact-check claims extracted from free text using Gemini AI.
    """
    api_key = os.environ.get("GOOGLE_AI_STUDIO_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_AI_STUDIO_API_KEY environment variable is not set.",
        )
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    claims = _extract_claims(text)
    if not claims:
        return JSONResponse({"claims": []})

    results = []
    for claim in claims:
        try:
            r = _factcheck_claim(claim, api_key)
            results.append({
                "claim": r.get("claim", claim),
                "verdict": r.get("verification", "uncertain"),
                "confidence": r.get("confidence_score", "0.0"),
                "explanation": r.get("explanation", ""),
                "correct_information": r.get("correct_information", ""),
            })
        except Exception as exc:  # noqa: BLE001
            results.append({
                "claim": claim,
                "verdict": "uncertain",
                "confidence": "0.0",
                "explanation": f"Error: {exc}",
                "correct_information": "",
            })
    return JSONResponse({"claims": results})
