import json
import os
import hashlib
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load environment variables early so they are available immediately
load_dotenv()

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from snaptrade_client import SnapTrade

DEFAULT_USER_ID = "puto_pablo"
DEFAULT_USER_SECRET = "0bb9efc3-a75a-482c-b577-fa2e0fac47d6"

DATA_DIR_DEFAULT = "/var/data"
DATA_FILENAME = "snaptrade_users.json"

users_lock = threading.Lock()
pending_user_id: Optional[str] = None
auth_lock = threading.Lock()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def resolve_data_path() -> str:
    data_dir = os.getenv("DATA_DIR", DATA_DIR_DEFAULT)
    if os.path.isdir(data_dir):
        return os.path.join(data_dir, DATA_FILENAME)
    return os.path.join(os.getcwd(), DATA_FILENAME)

def load_users() -> Dict[str, Dict[str, Any]]:
    path = resolve_data_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        print(f"[load_users] Could not read {path}. Using an empty dictionary.")
        return {}

def save_users(users: Dict[str, Dict[str, Any]]) -> None:
    path = resolve_data_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)

def ensure_default_user() -> None:
    with users_lock:
        users = load_users()
        if DEFAULT_USER_ID not in users:
            users[DEFAULT_USER_ID] = {
                "user_secret": DEFAULT_USER_SECRET,
                "created_at": now_iso(),
                "connections": [],
                "last_connected": None,
            }
            save_users(users)
            print(
                "[bootstrap] Added DEFAULT user to persistence file "
                f"({DEFAULT_USER_ID})."
            )

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup actions
    ensure_default_user()
    print(f"[startup] Persistence at: {resolve_data_path()}")
    yield
    # Shutdown actions
    pass

app = FastAPI(lifespan=lifespan)


def get_env_credentials() -> Tuple[str, str]:
    client_id = os.getenv("SNAPTRADE_CLIENT_ID", "").strip()
    consumer_key = os.getenv("SNAPTRADE_CONSUMER_KEY", "").strip()
    if not client_id or not consumer_key:
        raise RuntimeError(
            "Missing environment variables: SNAPTRADE_CLIENT_ID and/or SNAPTRADE_CONSUMER_KEY."
        )
    return client_id, consumer_key


def create_snaptrade() -> SnapTrade:
    client_id, consumer_key = get_env_credentials()
    try:
        return SnapTrade(client_id=client_id, consumer_key=consumer_key)
    except TypeError:
        return SnapTrade(client_id, consumer_key)


def sdk_body(resp: Any) -> Any:
    return getattr(resp, "body", resp)


def safe_str(val: Any) -> str:
    try:
        return str(val)
    except Exception:
        return "<no-string>"


def extract_redirect_uri(login_response: Any) -> Optional[str]:
    body = sdk_body(login_response)
    if isinstance(body, dict):
        return body.get("redirectURI") or body.get("redirect_uri")
    return (
        getattr(body, "redirect_uri", None)
        or getattr(body, "redirectURI", None)
        or getattr(login_response, "redirect_uri", None)
        or getattr(login_response, "redirectURI", None)
    )


def mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"


CLIENT_SESSION_COOKIE = "st_session"
CLIENT_LOGIN_PARAM = "token"


def sha256_hex(val: str) -> str:
    return hashlib.sha256((val or "").encode("utf-8")).hexdigest()


def token_expired(expires_at_iso: Optional[str]) -> bool:
    if not expires_at_iso:
        return True
    try:
        dt = datetime.fromisoformat(expires_at_iso)
    except Exception:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc)


def issue_client_login_token(user_id: str, *, ttl_days: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    created = now_iso()
    expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()

    with users_lock:
        users = load_users()
        user = users.get(user_id)
        if not user:
            raise RuntimeError("User not found.")
        auth = user.get("client_auth") if isinstance(user, dict) else None
        if not isinstance(auth, dict):
            auth = {}
        auth.update(
            {
                "login_token_hash": sha256_hex(token),
                "login_token_created_at": created,
                "login_token_expires_at": expires,
            }
        )
        user["client_auth"] = auth
        users[user_id] = user
        save_users(users)

    return token


def find_user_id_by_login_token(token: str) -> Optional[str]:
    token_hash = sha256_hex(token)
    with users_lock:
        users = load_users()
        for uid, user in users.items():
            auth = user.get("client_auth") if isinstance(user, dict) else None
            if not isinstance(auth, dict):
                continue
            if auth.get("login_token_hash") != token_hash:
                continue
            if token_expired(auth.get("login_token_expires_at")):
                continue
            return uid
    return None


def invalidate_login_token(user_id: str) -> None:
    with users_lock:
        users = load_users()
        user = users.get(user_id)
        if not user:
            return
        auth = user.get("client_auth") if isinstance(user, dict) else None
        if not isinstance(auth, dict):
            return
        auth["login_token_hash"] = None
        auth["login_token_expires_at"] = datetime.now(timezone.utc).isoformat()
        user["client_auth"] = auth
        users[user_id] = user
        save_users(users)


def issue_session_for_user(user_id: str, *, ttl_hours: int = 12) -> str:
    session_token = secrets.token_urlsafe(32)
    created = now_iso()
    expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()

    with users_lock:
        users = load_users()
        user = users.get(user_id)
        if not user:
            raise RuntimeError("User not found.")
        auth = user.get("client_auth") if isinstance(user, dict) else None
        if not isinstance(auth, dict):
            auth = {}
        auth.update(
            {
                "session_token_hash": sha256_hex(session_token),
                "session_created_at": created,
                "session_expires_at": expires,
            }
        )
        user["client_auth"] = auth
        users[user_id] = user
        save_users(users)

    return session_token


def find_user_id_by_session_token(session_token: str) -> Optional[str]:
    token_hash = sha256_hex(session_token)
    with users_lock:
        users = load_users()
        for uid, user in users.items():
            auth = user.get("client_auth") if isinstance(user, dict) else None
            if not isinstance(auth, dict):
                continue
            if auth.get("session_token_hash") != token_hash:
                continue
            if token_expired(auth.get("session_expires_at")):
                continue
            return uid
    return None


def redact_for_logs(payload: Any) -> str:
    raw = safe_str(payload)
    return raw.replace("Bearer ", "Bearer [REDACTED] ").replace("token=", "token=[REDACTED]")


def html_page(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en" class="antialiased text-slate-300 bg-slate-900">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{{title}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
      body {{ font-family: 'Inter', sans-serif; }}
      .glass-card {{
          background: rgba(30, 41, 59, 0.7);
          backdrop-filter: blur(12px);
          -webkit-backdrop-filter: blur(12px);
          border: 1px solid rgba(255, 255, 255, 0.08);
          box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
      }}
    </style>
  </head>
  <body class="min-h-screen bg-[radial-gradient(ellipse_at_top_right,_var(--tw-gradient-stops))] from-slate-800 via-slate-900 to-black text-slate-200">
    <div class="max-w-5xl mx-auto py-12 px-4 sm:px-6 lg:px-8">
      {{content}}
    </div>
  </body>
</html>""".replace("{{title}}", title).replace("{{content}}", content)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def render_home(message: Optional[Tuple[str, str]] = None) -> str:
    with users_lock:
        users = load_users()

    rows = []
    for uid, u in sorted(users.items(), key=lambda kv: kv[0].lower()):
        created_at = u.get("created_at") or "-"
        connections = u.get("connections") or []
        rows.append(
            f"<tr class='border-b border-slate-700/50 hover:bg-slate-800/30 transition-colors'>"
            f"<td class='py-3 px-4'><a href='/dashboard/{uid}' class='text-indigo-400 hover:text-indigo-300 font-medium'>{uid}</a></td>"
            f"<td class='py-3 px-4'><code class='text-xs text-slate-400 bg-slate-800 px-2 py-1 rounded'>{created_at}</code></td>"
            f"<td class='py-3 px-4 text-slate-300'>{len(connections)}</td>"
            f"</tr>"
        )

    msg_html = ""
    if message:
        kind, text = message
        cls = "border-emerald-500/30 bg-emerald-500/10 text-emerald-300" if kind == "ok" else "border-rose-500/30 bg-rose-500/10 text-rose-300"
        msg_html = f"<div class='mb-6 p-4 rounded-lg border {cls} text-sm'>{text}</div>"

    users_table = (
        "<div class='overflow-x-auto'><table class='w-full text-left text-sm'>"
        "<thead><tr class='text-slate-400 border-b border-slate-700 uppercase tracking-wider text-xs'>"
        "<th class='py-3 px-4 font-semibold'>User ID</th>"
        "<th class='py-3 px-4 font-semibold'>Created At</th>"
        "<th class='py-3 px-4 font-semibold'>Connections</th>"
        "</tr></thead><tbody>"
        + ("".join(rows) if rows else "<tr><td colspan='3' class='py-6 text-center text-slate-500'>No users found.</td></tr>")
        + "</tbody></table></div>"
    )

    content = f"""
    <div class="flex flex-col md:flex-row md:items-end justify-between gap-4 mb-8">
      <div>
        <h1 class="text-3xl font-bold text-white tracking-tight">SnapTrade + Coinbase Connector</h1>
        <p class="mt-2 text-sm text-slate-400">Persistence file: <code class="bg-slate-800 px-1.5 py-0.5 rounded">{resolve_data_path()}</code></p>
      </div>
      <div class="text-xs text-amber-200/80 bg-amber-500/10 px-3 py-2 rounded-lg border border-amber-500/20 max-w-sm">
        <strong>Note:</strong> On SnapTrade's free tier, you can typically only register 1 user via Personal Keys.
      </div>
    </div>
    {msg_html}
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-8">
      <div class="lg:col-span-7 glass-card rounded-xl p-6">
        <h2 class="text-lg font-semibold text-white mb-4">Saved Users</h2>
        {users_table}
      </div>
      <div class="lg:col-span-5 space-y-6">
        <div class="glass-card rounded-xl p-6">
          <h2 class="text-lg font-semibold text-white mb-4">Register SnapTrade User</h2>
          <form method="post" action="/register-user" class="space-y-4">
            <div>
              <label for="user_id_reg" class="block text-sm font-medium text-slate-400 mb-1">User ID</label>
              <input id="user_id_reg" name="user_id" placeholder="e.g. demo_user_001" required 
                     class="w-full bg-slate-800/50 border border-slate-600 rounded-lg px-4 py-2.5 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all" />
            </div>
            <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-medium py-2.5 px-4 rounded-lg transition-colors">
              Register User
            </button>
          </form>
          <p class="mt-3 text-xs text-slate-500">
            Uses <code>register_snap_trade_user(body={{"userId": user_id}})</code>.
          </p>
        </div>
        
        <div class="glass-card rounded-xl p-6">
          <h2 class="text-lg font-semibold text-white mb-4">Connect Coinbase</h2>
          <form method="post" action="/connect-coinbase" class="space-y-4">
            <div>
              <label for="user_id_conn" class="block text-sm font-medium text-slate-400 mb-1">Select User ID</label>
              <select id="user_id_conn" name="user_id" required 
                      class="w-full bg-slate-800/50 border border-slate-600 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent transition-all appearance-none">
                {''.join([f"<option value='{uid}'>{uid}</option>" for uid in users.keys()])}
              </select>
            </div>
            <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-2.5 px-4 rounded-lg transition-colors">
              Open Connection Portal
            </button>
          </form>
          <p class="mt-3 text-xs text-slate-500">
            The callback does not include the user ID, so this app uses a temporary global variable <code>pending_user_id</code>.
          </p>
        </div>
      </div>
    </div>
    """
    return html_page("SnapTrade + Coinbase (Admin)", content)


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return HTMLResponse(render_home())


@app.post("/register-user", response_class=HTMLResponse)
def register_user(user_id: str = Form(...)) -> HTMLResponse:
    user_id = (user_id or "").strip()
    if not user_id:
        return HTMLResponse(render_home(("err", "user_id is required.")), status_code=400)

    print(f"[register-user] Attempting to register user_id={user_id}")

    try:
        snaptrade = create_snaptrade()
        resp = snaptrade.authentication.register_snap_trade_user(body={"userId": user_id})
        body = sdk_body(resp)

        if isinstance(body, dict):
            user_secret = body.get("userSecret") or body.get("user_secret")
        else:
            user_secret = getattr(body, "user_secret", None) or getattr(body, "userSecret", None)

        if not user_secret:
            return HTMLResponse(
                render_home(("err", f"userSecret not found in response: {safe_str(body)}")),
                status_code=502,
            )

        with users_lock:
            users = load_users()
            users[user_id] = {
                "user_secret": user_secret,
                "created_at": now_iso(),
                "connections": users.get(user_id, {}).get("connections", []),
                "last_connected": users.get(user_id, {}).get("last_connected"),
            }
            save_users(users)

        print(f"[register-user] Registered and persisted user_id={user_id}")
        return HTMLResponse(render_home(("ok", f"User registered: {user_id}")))

    except Exception as e:
        msg = safe_str(e)
        print(f"[register-user] Error: {msg}")

        if "Personal keys can only register one user" in msg:
            friendly = (
                "Could not register user because your SnapTrade account (free tier) "
                "only allows 1 user registration. Use the existing user or upgrade your plan."
            )
            return HTMLResponse(render_home(("err", friendly)), status_code=400)

        return HTMLResponse(render_home(("err", f"Error registering user: {msg}")), status_code=500)


@app.post("/connect-coinbase")
def connect_coinbase(user_id: str = Form(...)) -> RedirectResponse:
    global pending_user_id

    user_id = (user_id or "").strip()
    if not user_id:
        return RedirectResponse(url="/", status_code=303)

    with users_lock:
        users = load_users()
        user = users.get(user_id)

    if not user:
        html = render_home(("err", f"User {user_id} does not exist in JSON file."))
        return HTMLResponse(html, status_code=404)

    user_secret = (user.get("user_secret") or "").strip()
    if not user_secret:
        html = render_home(("err", f"User {user_id} does not have a saved user_secret."))
        return HTMLResponse(html, status_code=500)

    pending_user_id = user_id
    print(f"[connect-coinbase] Initiating connection for user_id={user_id}")

    try:
        snaptrade = create_snaptrade()
        try:
            login_response = snaptrade.authentication.login_snap_trade_user(
                user_id=user_id,
                user_secret=user_secret,
                broker="COINBASE",
                connection_type="trade",
            )
        except TypeError:
            login_response = snaptrade.authentication.login_snap_trade_user(
                body={
                    "userId": user_id,
                    "userSecret": user_secret,
                    "broker": "COINBASE",
                    "connectionType": "trade",
                }
            )

        redirect_uri = extract_redirect_uri(login_response)
        if not redirect_uri:
            body = sdk_body(login_response)
            raise RuntimeError(f"Could not obtain redirectURI. Response: {safe_str(body)}")

        print(f"[connect-coinbase] Redirecting to Connection Portal (user_id={user_id})")
        return RedirectResponse(url=redirect_uri, status_code=302)

    except Exception as e:
        msg = safe_str(e)
        print(f"[connect-coinbase] Error: {msg}")
        html = render_home(("err", f"Error initiating connection: {msg}"))
        return HTMLResponse(html, status_code=500)


@app.get("/callback", response_class=HTMLResponse)
def callback(
    request: Request, status: Optional[str] = None, connection_id: Optional[str] = None
) -> HTMLResponse:
    global pending_user_id

    status = (status or "").strip()
    connection_id = (connection_id or "").strip()
    if not connection_id:
        connection_id = (request.query_params.get("connectionId") or "").strip()

    user_id = pending_user_id
    print(f"[callback] status={status} connection_id={connection_id} pending_user_id={user_id}")

    if not user_id:
        content = """
        <div class="glass-card rounded-xl p-6 text-center max-w-md mx-auto mt-12">
          <h2 class="text-xl font-semibold text-rose-400 mb-2">Error</h2>
          <p class="text-slate-400 mb-6">No pending user found (pending_user_id is empty). Please restart the connection from the home page.</p>
          <p><a href="/" class="text-indigo-400 hover:text-indigo-300 font-medium">Return Home</a></p>
        </div>
        """
        return HTMLResponse(html_page("Callback Error", content), status_code=400)

    if status != "SUCCESS":
        pending_user_id = None
        content = f"""
        <div class="glass-card rounded-xl p-6 text-center max-w-md mx-auto mt-12">
          <h2 class="text-xl font-semibold text-amber-400 mb-2">Connection Incomplete</h2>
          <p class="text-slate-400 mb-2">Status: <code class="bg-slate-800 px-2 py-1 rounded text-amber-200">{status or "N/A"}</code></p>
          <p class="text-slate-400 mb-6">User: <code class="bg-slate-800 px-2 py-1 rounded">{user_id}</code></p>
          <p><a href="/" class="text-indigo-400 hover:text-indigo-300 font-medium">Return Home</a></p>
        </div>
        """
        return HTMLResponse(html_page("Callback Failed", content), status_code=400)

    if not connection_id:
        pending_user_id = None
        content = f"""
        <div class="glass-card rounded-xl p-6 text-center max-w-md mx-auto mt-12">
          <h2 class="text-xl font-semibold text-rose-400 mb-2">Error</h2>
          <p class="text-slate-400 mb-2">status=SUCCESS but connection_id is missing.</p>
          <p class="text-slate-400 mb-6">User: <code class="bg-slate-800 px-2 py-1 rounded">{user_id}</code></p>
          <p><a href="/" class="text-indigo-400 hover:text-indigo-300 font-medium">Return Home</a></p>
        </div>
        """
        return HTMLResponse(html_page("Callback Error", content), status_code=400)

    with users_lock:
        users = load_users()
        user = users.get(user_id)
        if not user:
            pending_user_id = None
            content = f"""
            <div class="glass-card rounded-xl p-6 text-center max-w-md mx-auto mt-12">
              <h2 class="text-xl font-semibold text-rose-400 mb-2">Error</h2>
              <p class="text-slate-400 mb-6">The pending user <code class="bg-slate-800 px-2 py-1 rounded">{user_id}</code> does not exist in persistence.</p>
              <p><a href="/" class="text-indigo-400 hover:text-indigo-300 font-medium">Return Home</a></p>
            </div>
            """
            return HTMLResponse(html_page("Callback Error", content), status_code=404)

        connections = user.get("connections") or []
        if connection_id not in connections:
            connections.append(connection_id)
        user["connections"] = connections
        user["last_connected"] = now_iso()
        users[user_id] = user
        save_users(users)

    pending_user_id = None

    content = f"""
    <div class="glass-card rounded-xl p-6 text-center max-w-md mx-auto mt-12 border-emerald-500/30">
      <h2 class="text-2xl font-bold text-emerald-400 mb-4">Connection Saved!</h2>
      <p class="text-slate-300 mb-2">User: <code class="bg-slate-800 px-2 py-1 rounded">{user_id}</code></p>
      <p class="text-slate-300 mb-8">Connection ID: <code class="bg-slate-800 px-2 py-1 rounded">{connection_id}</code></p>
      <div class="flex flex-col gap-3">
        <a href="/dashboard/{user_id}" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-medium py-2.5 px-4 rounded-lg transition-colors">Go to Dashboard</a>
        <a href="/" class="w-full bg-slate-700 hover:bg-slate-600 text-white font-medium py-2.5 px-4 rounded-lg transition-colors">Return Home</a>
      </div>
    </div>
    """
    return HTMLResponse(html_page("Callback Success", content))


@app.post("/generate-client-link", response_class=HTMLResponse)
def generate_client_link(request: Request, user_id: str = Form(...)) -> HTMLResponse:
    user_id = (user_id or "").strip()
    if not user_id:
        return HTMLResponse(render_home(("err", "user_id is required.")), status_code=400)

    try:
        token = issue_client_login_token(user_id)
    except Exception as e:
        msg = safe_str(e)
        print(f"[client-link] Error user_id={user_id}: {redact_for_logs(msg)}")
        return HTMLResponse(render_home(("err", f"Could not generate link: {msg}")), status_code=500)

    base = str(request.base_url).rstrip("/")
    link = f"{base}/client/login?{CLIENT_LOGIN_PARAM}={token}"
    print(f"[client-link] Generated for user_id={user_id}")
    content = f"""
    <div class="flex items-center justify-between mb-8">
      <div>
        <h1 class="text-3xl font-bold text-white tracking-tight">Client Access Link</h1>
      </div>
      <a href="/dashboard/{user_id}" class="text-indigo-400 hover:text-indigo-300 font-medium text-sm transition-colors">
        &larr; Back to Dashboard
      </a>
    </div>
    
    <div class="glass-card rounded-xl p-8 max-w-2xl mx-auto">
      <h2 class="text-xl font-semibold text-white mb-3">Share this link</h2>
      <p class="text-slate-400 text-sm mb-6">This one-time link allows the client to securely log in. Treat it like a password.</p>
      
      <div class="bg-slate-900/80 border border-slate-700 rounded-lg p-4 mb-6 break-all">
        <code class="text-emerald-400 text-sm">{link}</code>
      </div>
      
      <div class="text-xs text-amber-200/80 bg-amber-500/10 px-4 py-3 rounded-lg border border-amber-500/20">
        <strong>Security Note:</strong> The token is stored server-side as a SHA-256 hash (never in plain text) and will invalidate upon first use.
      </div>
    </div>
    """
    return HTMLResponse(html_page("Access Link", content))


@app.get("/client/login", response_class=HTMLResponse)
def client_login(request: Request) -> HTMLResponse:
    token = (request.query_params.get(CLIENT_LOGIN_PARAM) or "").strip()
    if not token:
        content = """
        <div class="glass-card rounded-xl p-6 text-center max-w-md mx-auto mt-12 border-rose-500/30">
          <h2 class="text-xl font-semibold text-rose-400 mb-2">Invalid Access</h2>
          <p class="text-slate-400 mb-6">Access token is missing. Please request a valid link from your administrator.</p>
        </div>
        """
        return HTMLResponse(html_page("Login", content), status_code=400)

    user_id = find_user_id_by_login_token(token)
    if not user_id:
        content = """
        <div class="glass-card rounded-xl p-6 text-center max-w-md mx-auto mt-12 border-rose-500/30">
          <h2 class="text-xl font-semibold text-rose-400 mb-2">Invalid Access</h2>
          <p class="text-slate-400 mb-6">The token is invalid or has expired. Please request a new link.</p>
        </div>
        """
        return HTMLResponse(html_page("Login", content), status_code=401)

    invalidate_login_token(user_id)
    session_token = issue_session_for_user(user_id)
    resp = RedirectResponse(url="/client/dashboard", status_code=302)
    resp.set_cookie(
        key=CLIENT_SESSION_COOKIE,
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme in ["https", "wss"]),
        max_age=12 * 60 * 60,
    )
    print(f"[client-login] Session created for user_id={user_id}")
    return resp


@app.get("/client/dashboard", response_class=HTMLResponse)
def client_dashboard(request: Request) -> HTMLResponse:
    session_token = (request.cookies.get(CLIENT_SESSION_COOKIE) or "").strip()
    if not session_token:
        content = """
        <div class="glass-card rounded-xl p-6 text-center max-w-md mx-auto mt-12 border-rose-500/30">
          <h2 class="text-xl font-semibold text-rose-400 mb-2">Session Required</h2>
          <p class="text-slate-400 mb-6">Your session does not exist or has expired. Please open your access link again.</p>
        </div>
        """
        return HTMLResponse(html_page("Client Portal", content), status_code=401)

    user_id = find_user_id_by_session_token(session_token)
    if not user_id:
        content = """
        <div class="glass-card rounded-xl p-6 text-center max-w-md mx-auto mt-12 border-rose-500/30">
          <h2 class="text-xl font-semibold text-rose-400 mb-2">Invalid Session</h2>
          <p class="text-slate-400 mb-6">Your session has expired. Please request a new link or log in again.</p>
        </div>
        """
        return HTMLResponse(html_page("Client Portal", content), status_code=401)

    with users_lock:
        users = load_users()
        user = users.get(user_id) or {}

    last_connected = user.get("last_connected") or "-"
    connections = user.get("connections") or []
    conn_items = (
        "".join([f"<li class='py-2 px-3 bg-slate-800/50 border border-slate-700 rounded-lg text-slate-300 font-mono text-sm'>{c}</li>" for c in connections])
        if connections
        else "<li class='py-3 text-center text-slate-500 italic'>No connections yet.</li>"
    )

    content = f"""
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
      <div>
        <h1 class="text-3xl font-bold text-white tracking-tight">Client Portal</h1>
        <p class="mt-1 text-sm text-slate-400">Logged in as: <code class="bg-slate-800 px-1.5 py-0.5 rounded text-indigo-300">{user_id}</code></p>
      </div>
      <form method="post" action="/client/logout">
        <button type="submit" class="bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-600 font-medium py-2 px-4 rounded-lg transition-colors text-sm">
          Sign Out
        </button>
      </form>
    </div>

    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
      <div class="glass-card rounded-xl p-6 flex flex-col justify-between">
        <div>
          <h2 class="text-lg font-semibold text-white mb-2">Connect Coinbase</h2>
          <p class="text-sm text-slate-400 mb-6">
            Securely link your Coinbase account via SnapTrade. You will be redirected to an authorization portal.
          </p>
        </div>
        <form method="post" action="/connect-coinbase">
          <input type="hidden" name="user_id" value="{user_id}" />
          <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-3 px-4 rounded-lg transition-colors shadow-lg shadow-emerald-500/20">
            Open Connection Portal
          </button>
        </form>
      </div>

      <div class="glass-card rounded-xl p-6">
        <h2 class="text-lg font-semibold text-white mb-4">Status Overview</h2>
        <div class="space-y-4">
          <div class="flex justify-between items-center border-b border-slate-700/50 pb-3">
            <span class="text-sm text-slate-400">Last Connected</span>
            <code class="text-xs text-slate-300 bg-slate-800 px-2 py-1 rounded">{last_connected}</code>
          </div>
          <div class="flex justify-between items-center border-b border-slate-700/50 pb-3">
            <span class="text-sm text-slate-400">Total Connections</span>
            <span class="text-sm font-semibold text-white bg-indigo-500/20 text-indigo-300 px-2.5 py-0.5 rounded-full">{len(connections)}</span>
          </div>
        </div>
      </div>
    </div>

    <div class="glass-card rounded-xl p-6 mt-6">
      <h2 class="text-lg font-semibold text-white mb-4">Your Connection IDs</h2>
      <ul class="space-y-2 max-h-64 overflow-y-auto pr-2 custom-scrollbar">
        {conn_items}
      </ul>
    </div>
    <style>
      .custom-scrollbar::-webkit-scrollbar {{ width: 6px; }}
      .custom-scrollbar::-webkit-scrollbar-track {{ background: rgba(30, 41, 59, 0.5); border-radius: 4px; }}
      .custom-scrollbar::-webkit-scrollbar-thumb {{ background: rgba(71, 85, 105, 0.8); border-radius: 4px; }}
      .custom-scrollbar::-webkit-scrollbar-thumb:hover {{ background: rgba(100, 116, 139, 1); }}
    </style>
    """
    return HTMLResponse(html_page("Client Portal", content))


@app.post("/client/logout")
def client_logout(request: Request) -> RedirectResponse:
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(key=CLIENT_SESSION_COOKIE)
    return resp


@app.get("/dashboard/{user_id}", response_class=HTMLResponse)
def dashboard(user_id: str) -> HTMLResponse:
    user_id = (user_id or "").strip()
    with users_lock:
        users = load_users()
        user = users.get(user_id)

    if not user:
        html = render_home(("err", f"User not found: {user_id}"))
        return HTMLResponse(html, status_code=404)

    secret = (user.get("user_secret") or "").strip()
    created_at = user.get("created_at") or "-"
    last_connected = user.get("last_connected") or "-"
    connections = user.get("connections") or []
    auth = user.get("client_auth") if isinstance(user, dict) else None
    if not isinstance(auth, dict):
        auth = {}
    login_expires = auth.get("login_token_expires_at") or "-"
    session_expires = auth.get("session_expires_at") or "-"

    conn_items = (
        "".join([f"<li class='py-2 px-3 bg-slate-800/50 border border-slate-700 rounded-lg text-slate-300 font-mono text-sm mb-2'>{c}</li>" for c in connections])
        if connections
        else "<li class='py-3 text-slate-500 italic'>No connections yet.</li>"
    )

    content = f"""
    <div class="flex items-center justify-between mb-8">
      <div>
        <h1 class="text-3xl font-bold text-white tracking-tight">Dashboard: {user_id}</h1>
      </div>
      <a href="/" class="text-indigo-400 hover:text-indigo-300 font-medium text-sm transition-colors">
        &larr; Back to Home
      </a>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div class="glass-card rounded-xl p-6">
        <h2 class="text-lg font-semibold text-white mb-4">User Details</h2>
        <div class="space-y-4">
          <div class="flex flex-col sm:flex-row sm:justify-between sm:items-center border-b border-slate-700/50 pb-3 gap-1">
            <span class="text-sm text-slate-400">User ID</span>
            <code class="text-xs text-indigo-300 bg-indigo-500/10 px-2 py-1 rounded border border-indigo-500/20">{user_id}</code>
          </div>
          <div class="flex flex-col sm:flex-row sm:justify-between sm:items-center border-b border-slate-700/50 pb-3 gap-1">
            <span class="text-sm text-slate-400">User Secret</span>
            <code class="text-xs text-slate-300 bg-slate-800 px-2 py-1 rounded">{mask_secret(secret)}</code>
          </div>
          <div class="flex flex-col sm:flex-row sm:justify-between sm:items-center border-b border-slate-700/50 pb-3 gap-1">
            <span class="text-sm text-slate-400">Created At</span>
            <code class="text-xs text-slate-300 bg-slate-800 px-2 py-1 rounded">{created_at}</code>
          </div>
          <div class="flex flex-col sm:flex-row sm:justify-between sm:items-center border-b border-slate-700/50 pb-3 gap-1">
            <span class="text-sm text-slate-400">Last Connected</span>
            <code class="text-xs text-slate-300 bg-slate-800 px-2 py-1 rounded">{last_connected}</code>
          </div>
        </div>
      </div>

      <div class="glass-card rounded-xl p-6">
        <h2 class="text-lg font-semibold text-white mb-4">Client Access Control</h2>
        <div class="space-y-4 mb-6">
          <div class="flex flex-col sm:flex-row sm:justify-between sm:items-center border-b border-slate-700/50 pb-3 gap-1">
            <span class="text-sm text-slate-400">Login Token Expires</span>
            <code class="text-xs text-slate-300 bg-slate-800 px-2 py-1 rounded">{login_expires}</code>
          </div>
          <div class="flex flex-col sm:flex-row sm:justify-between sm:items-center border-b border-slate-700/50 pb-3 gap-1">
            <span class="text-sm text-slate-400">Session Expires</span>
            <code class="text-xs text-slate-300 bg-slate-800 px-2 py-1 rounded">{session_expires}</code>
          </div>
        </div>
        
        <form method="post" action="/generate-client-link">
          <input type="hidden" name="user_id" value="{user_id}" />
          <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-medium py-2.5 px-4 rounded-lg transition-colors">
            Generate Access Link
          </button>
        </form>
        <p class="mt-3 text-xs text-slate-500">
          Access tokens are stored as SHA-256 hashes. Sessions use HttpOnly cookies.
        </p>
      </div>

      <div class="glass-card rounded-xl p-6 lg:col-span-2">
        <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center mb-4 gap-4">
          <h2 class="text-lg font-semibold text-white">Connections</h2>
          <form method="post" action="/connect-coinbase">
            <input type="hidden" name="user_id" value="{user_id}" />
            <button type="submit" class="bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-2 px-4 rounded-lg transition-colors text-sm">
              Connect Coinbase (Again)
            </button>
          </form>
        </div>
        <ul class="max-h-64 overflow-y-auto pr-2 custom-scrollbar">
          {conn_items}
        </ul>
      </div>
    </div>
    <style>
      .custom-scrollbar::-webkit-scrollbar {{ width: 6px; }}
      .custom-scrollbar::-webkit-scrollbar-track {{ background: rgba(30, 41, 59, 0.5); border-radius: 4px; }}
      .custom-scrollbar::-webkit-scrollbar-thumb {{ background: rgba(71, 85, 105, 0.8); border-radius: 4px; }}
      .custom-scrollbar::-webkit-scrollbar-thumb:hover {{ background: rgba(100, 116, 139, 1); }}
    </style>
    """
    return HTMLResponse(html_page(f"Dashboard: {user_id}", content))


@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    msg = safe_str(exc)
    print(f"[unhandled] path={request.url.path} error={redact_for_logs(msg)}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal error. Check server logs."},
    )
