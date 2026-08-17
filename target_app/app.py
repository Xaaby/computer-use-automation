"""
target_app/app.py
Flask mock bank / credit-union admin UI.

Auth / timeout decisions (Phase 1 plan):
- Signed-cookie Flask sessions (itsdangerous). flask-session is a project
  dependency but is NOT used here — filesystem/SQLite session backends can
  enable WAL mode, which RULES.md forbids.
- Idle timeout is 120s from last authenticated request. Expiry redirects to
  /login?expired=true with visible text "session has expired" (Phase 3
  SESSION_EXPIRED recognizer).
- GET /members/search is PUBLIC so a Playwright goto() snapshot can see
  aria-label="Member ID" without a prior login. POST search and all
  detail / transfer / frame routes require a live session.
- Member IDs whose string form starts with "9" → HTTP 403 (PERMISSION_DENIED)
  before the unknown-ID check. Unknown IDs that do not start with 9 stay on
  /members/search with visible text "No member found" (MEMBER_NOT_FOUND).
  Direct GET of an unknown ID uses not_found.html (404).
- /simulate/error and /simulate/slow are public test hooks (policy blocks
  the agent from navigating them). They do not require a session.
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# Repo-root .env (APP_USERNAME / APP_PASSWORD / TARGET_APP_PORT).
_APP_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _APP_DIR.parent
load_dotenv(_REPO_ROOT / ".env")

IDLE_TIMEOUT_SECONDS = 120
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin")

# flask-session is intentionally unused — signed cookies only, never SQLite.
app = Flask(
    __name__,
    template_folder=str(_APP_DIR / "templates"),
    root_path=str(_APP_DIR),
)
app.config["DEBUG"] = False
app.config["TESTING"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_NAME"] = "mock_bank_session"
# Local mock only — override with FLASK_SECRET_KEY in real deployments.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "mock-bank-dev-only-not-a-secret")

_MEMBERS_PATH = _APP_DIR / "data" / "members.json"


def _load_members() -> dict[str, dict]:
    raw = json.loads(_MEMBERS_PATH.read_text(encoding="utf-8"))
    return {member["id"]: member for member in raw["members"]}


# In-memory copy so transfers can mutate balances without writing JSON.
MEMBERS: dict[str, dict] = _load_members()

_PUBLIC_GET_PATHS = {
    "/",
    "/login",
    "/logout",
    "/members/search",
    "/simulate/error",
    "/simulate/slow",
}


def _idle_expired() -> bool:
    last = session.get("last_activity")
    if last is None:
        return True
    return (time.time() - float(last)) > IDLE_TIMEOUT_SECONDS


def _is_public() -> bool:
    if request.method == "GET" and request.path in _PUBLIC_GET_PATHS:
        return True
    if request.method == "POST" and request.path == "/login":
        return True
    return False


def _safe_next_url(candidate: str | None) -> str:
    """Reject open redirects; only same-origin relative paths."""
    if not candidate:
        return url_for("search")
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return url_for("search")
    if not candidate.startswith("/") or candidate.startswith("//"):
        return url_for("search")
    return candidate


def _touch_session() -> None:
    session["last_activity"] = time.time()


def _member_forbidden(member_id: str) -> bool:
    return member_id.startswith("9")


@app.before_request
def enforce_session_and_idle_timeout():
    if _is_public():
        # Drop a stale cookie but still render public pages (GET search must
        # keep showing the Member ID field for discovery snapshots).
        if session.get("user") and _idle_expired():
            session.clear()
        return None

    if not session.get("user"):
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    if _idle_expired():
        session.clear()
        return redirect(
            url_for("login", expired="true", next=request.full_path.rstrip("?"))
        )

    _touch_session()
    return None


@app.errorhandler(403)
def handle_forbidden(_error):
    # Do not reuse error.html — that page's "Application Error" text is the
    # APP_ERROR recognizer, not PERMISSION_DENIED (HTTP 403).
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Forbidden</title></head><body>"
        "<h1>Forbidden</h1>"
        "<p>You do not have permission to view this member.</p>"
        "</body></html>",
        403,
    )


@app.route("/")
def index():
    return redirect(url_for("search"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("user") and not _idle_expired():
            return redirect(_safe_next_url(request.args.get("next")))
        expired = request.args.get("expired") == "true"
        return render_template(
            "login.html",
            expired=expired,
            next_url=request.args.get("next", ""),
            error=None,
        )

    username = request.form.get("username", "")
    password = request.form.get("password", "")
    next_url = _safe_next_url(request.form.get("next") or request.args.get("next"))
    if username == APP_USERNAME and password == APP_PASSWORD:
        session.clear()
        session["user"] = username
        _touch_session()
        return redirect(next_url)

    return render_template(
        "login.html",
        expired=False,
        next_url=request.form.get("next", ""),
        error="Invalid username or password",
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/members/search", methods=["GET", "POST"])
def search():
    if request.method == "GET":
        return render_template("search.html", error=None)

    # POST is session-gated by before_request.
    member_id = (request.form.get("member_id") or "").strip()
    if _member_forbidden(member_id):
        abort(403)
    member = MEMBERS.get(member_id)
    if member is None:
        # Stay on /members/search so MEMBER_NOT_FOUND (text + route) matches.
        return render_template("search.html", error="No member found")
    return redirect(url_for("member_detail", member_id=member_id))


@app.route("/members/<member_id>")
def member_detail(member_id: str):
    if _member_forbidden(member_id):
        abort(403)
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html"), 404
    return render_template("member_detail.html", member=member)


@app.route("/members/<member_id>/transfer", methods=["GET", "POST"])
def transfer(member_id: str):
    if _member_forbidden(member_id):
        abort(403)
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html"), 404

    if request.method == "GET":
        return render_template("transfer_form.html", member=member, error=None)

    from_account = (request.form.get("from_account") or "").strip()
    to_account = (request.form.get("to_account") or "").strip()
    amount_raw = (request.form.get("amount") or "").strip()

    source = next((a for a in member["accounts"] if a["number"] == from_account), None)
    if source is None:
        return render_template(
            "transfer_form.html",
            member=member,
            error="Select a valid from account",
        )
    if not to_account:
        return render_template(
            "transfer_form.html",
            member=member,
            error="To account is required",
        )
    try:
        amount = Decimal(amount_raw)
    except InvalidOperation:
        return render_template(
            "transfer_form.html",
            member=member,
            error="Enter a valid amount",
        )
    if amount <= 0:
        return render_template(
            "transfer_form.html",
            member=member,
            error="Amount must be greater than zero",
        )
    if amount > Decimal(source["balance"]):
        return render_template(
            "transfer_form.html",
            member=member,
            error="Amount exceeds available balance",
        )

    session["pending_transfer"] = {
        "member_id": member_id,
        "from_account": from_account,
        "to_account": to_account,
        "amount": str(amount),
    }
    return redirect(url_for("transfers_confirm"))


@app.route("/transfers/confirm", methods=["GET", "POST"])
def transfers_confirm():
    pending = session.get("pending_transfer")
    if not pending:
        return redirect(url_for("search"))

    member = MEMBERS.get(pending["member_id"])
    if member is None:
        session.pop("pending_transfer", None)
        return render_template("not_found.html"), 404

    if request.method == "GET":
        return render_template(
            "transfer_confirm.html",
            member=member,
            pending=pending,
        )

    # POST is the irreversible commit (Confirm Transfer).
    amount = Decimal(pending["amount"])
    source = next(
        (a for a in member["accounts"] if a["number"] == pending["from_account"]),
        None,
    )
    if source is None:
        session.pop("pending_transfer", None)
        return render_template(
            "transfer_form.html",
            member=member,
            error="From account is no longer available",
        )

    source["balance"] = f"{Decimal(source['balance']) - amount:.2f}"
    dest = None
    for other in MEMBERS.values():
        dest = next(
            (a for a in other["accounts"] if a["number"] == pending["to_account"]),
            None,
        )
        if dest is not None:
            dest["balance"] = f"{Decimal(dest['balance']) + amount:.2f}"
            break

    session.pop("pending_transfer", None)
    return render_template(
        "transfer_confirm.html",
        member=member,
        pending=pending,
        committed=True,
        destination_found=dest is not None,
    )


@app.route("/frames/accounts/<member_id>")
def accounts_frame(member_id: str):
    if _member_forbidden(member_id):
        abort(403)
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template("not_found.html"), 404
    return render_template("accounts_frame.html", member=member)


@app.route("/simulate/error")
def simulate_error():
    return render_template("error.html"), 500


@app.route("/simulate/slow")
def simulate_slow():
    raw = request.args.get("delay", "5")
    try:
        delay = int(raw)
    except (TypeError, ValueError):
        delay = 5
    # Cap so a bad query cannot hang the process indefinitely.
    delay = max(0, min(delay, 60))
    time.sleep(delay)
    return "OK", 200


if __name__ == "__main__":
    # RULES.md: Flask ALWAYS runs with use_reloader=False, debug=False.
    port = int(os.environ.get("TARGET_APP_PORT", "5000"))
    app.run(
        host="127.0.0.1",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
