"""FastAPI app: password-gated GUI plus the poller running in-process."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, notify, poller
from .models import WEEKDAYS, Watch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# httpx logs a line per request at INFO. At 12 requests every 30 seconds
# that is ~35k lines and ~11 MB a day -- roughly 4 GB a year of full URLs,
# which is a real problem on a 20 GB instance and drowns out the messages
# that matter. Failures still surface: the poller logs those itself.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Paths reachable without a session.
PUBLIC_PATHS = {"/login", "/healthz"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run_forever(stop))
    app.state.poller_stop = stop
    app.state.poller_task = task
    try:
        yield
    finally:
        # Ask the poller to finish its cycle, and only force it if it hangs.
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.TimeoutError:
            task.cancel()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Poller exited with an error")


app = FastAPI(title="Tee Time Watcher", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)
    redirect = auth.require_login(request)
    if redirect is not None:
        return redirect
    return await call_next(request)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if auth.is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form("")):
    if not auth.check_password(password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect password."},
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    auth.issue_cookie(response)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_cookie(response)
    return response


@app.get("/healthz")
async def healthz():
    status = db.get_poll_status()
    return JSONResponse(
        {
            "ok": True,
            "last_poll_at": status["last_poll_at"] if status else None,
            "last_error": status["last_error"] if status else None,
        }
    )


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    watches = db.list_watches()
    today = poller.local_now().date()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "watches": watches,
            "status": db.get_poll_status(),
            "recent": db.recent_slots(limit=25),
            "courses": config.COURSES,
            "poll_interval": config.POLL_INTERVAL_SECONDS,
            "ntfy_topic": config.NTFY_TOPIC,
            "today": today,
            "storage": db.storage_stats(),
            "retention_days": config.RETENTION_DAYS,
        },
    )


# --------------------------------------------------------------------------
# watches
# --------------------------------------------------------------------------


def _clean_time(value: str) -> str | None:
    value = (value or "").strip()
    return value or None


def _watch_from_form(
    watch_id: int | None,
    label: str,
    courses: list[str],
    days: list[str],
    horizon_days: int,
    specific_date: str,
    time_start: str,
    time_end: str,
    min_players: int,
    holes: str,
    active: bool,
) -> Watch:
    return Watch(
        id=watch_id,
        label=label.strip(),
        courses=[c for c in courses if c in config.COURSES],
        days=[d for d in days if d in WEEKDAYS],
        horizon_days=max(0, min(horizon_days, 90)),
        specific_date=_clean_time(specific_date),
        time_start=_clean_time(time_start),
        time_end=_clean_time(time_end),
        min_players=max(0, min(min_players, 4)),
        holes=holes if holes in ("9", "18", "any") else "any",
        active=active,
    )


@app.get("/watches/new", response_class=HTMLResponse)
async def new_watch_form(request: Request):
    return templates.TemplateResponse(
        request,
        "watch_form.html",
        {
            "watch": Watch(),
            "courses": config.COURSES,
            "weekdays": WEEKDAYS,
            "error": None,
        },
    )


@app.get("/watches/{watch_id}/edit", response_class=HTMLResponse)
async def edit_watch_form(request: Request, watch_id: int):
    watch = db.get_watch(watch_id)
    if watch is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "watch_form.html",
        {
            "watch": watch,
            "courses": config.COURSES,
            "weekdays": WEEKDAYS,
            "error": None,
        },
    )


@app.post("/watches/save")
async def save_watch(
    request: Request,
    watch_id: str = Form(""),
    label: str = Form(""),
    courses: list[str] = Form(default=[]),
    days: list[str] = Form(default=[]),
    horizon_days: int = Form(14),
    specific_date: str = Form(""),
    time_start: str = Form(""),
    time_end: str = Form(""),
    min_players: int = Form(0),
    holes: str = Form("any"),
    # An unchecked checkbox is simply absent from the POST, so the default
    # here has to mean "off" -- otherwise unchecking it never takes effect.
    active: str = Form(""),
):
    watch = _watch_from_form(
        int(watch_id) if watch_id.strip() else None,
        label,
        courses,
        days,
        horizon_days,
        specific_date,
        time_start,
        time_end,
        min_players,
        holes,
        active == "on",
    )

    error = _validate(watch)
    if error:
        return templates.TemplateResponse(
            request,
            "watch_form.html",
            {
                "watch": watch,
                "courses": config.COURSES,
                "weekdays": WEEKDAYS,
                "error": error,
            },
            status_code=400,
        )

    new_id = db.save_watch(watch)
    return RedirectResponse(f"/watches/{new_id}", status_code=303)


def _validate(watch: Watch) -> str | None:
    if not watch.courses:
        return "Pick at least one course."
    if not watch.days and not watch.specific_date:
        return "Pick at least one day of the week, or a specific date."
    if watch.specific_date:
        try:
            dt.date.fromisoformat(watch.specific_date)
        except ValueError:
            return "Specific date must look like YYYY-MM-DD."
    for value in (watch.time_start, watch.time_end):
        if value:
            try:
                dt.datetime.strptime(value, "%H:%M")
            except ValueError:
                return "Times must look like HH:MM (24-hour)."
    if watch.time_start and watch.time_end and watch.time_start > watch.time_end:
        return "The earliest time must come before the latest time."
    return None


@app.get("/watches/{watch_id}", response_class=HTMLResponse)
async def watch_detail(request: Request, watch_id: int):
    watch = db.get_watch(watch_id)
    if watch is None:
        return RedirectResponse("/", status_code=303)
    today = poller.local_now().date()
    return templates.TemplateResponse(
        request,
        "watch_detail.html",
        {
            "watch": watch,
            "courses": config.COURSES,
            "slots": db.recent_slots(watch_id=watch_id, limit=100),
            "dates": watch.candidate_dates(today),
        },
    )


@app.post("/watches/{watch_id}/toggle")
async def toggle_watch(watch_id: int):
    watch = db.get_watch(watch_id)
    if watch is not None:
        db.set_watch_active(watch_id, not watch.active)
    return RedirectResponse("/", status_code=303)


@app.post("/watches/{watch_id}/delete")
async def remove_watch(watch_id: int):
    db.delete_watch(watch_id)
    return RedirectResponse("/", status_code=303)


@app.post("/test-notification")
async def test_notification():
    async with httpx.AsyncClient() as client:
        await notify.send_test_alert(client)
    return RedirectResponse("/", status_code=303)
