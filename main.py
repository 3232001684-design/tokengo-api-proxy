"""
TokenGo - AI API 中转服务
一个密钥，畅用多个 AI 模型
FastAPI + SQLite 实现，OpenAI / Anthropic 兼容协议。
"""
from fastapi import FastAPI, HTTPException, Header, Request, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from pydantic import BaseModel
from typing import Optional, Dict, List, Any
import httpx
import sqlite3
import time
import os
import json
import secrets
import hashlib
import uuid
from datetime import datetime, timedelta

PUBLIC_BASE_URL = "https://tokengo-d0cb.onrender.com"
PUBLIC_URL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_url.txt")

_public_url_cache = {"value": None, "mtime": 0.0}

def get_public_url() -> str:
    try:
        mtime = os.path.getmtime(PUBLIC_URL_FILE)
        if _public_url_cache["value"] and mtime == _public_url_cache["mtime"]:
            return _public_url_cache["value"]
        with open(PUBLIC_URL_FILE, "r", encoding="utf-8-sig") as f:
            url = f.read().strip().lstrip('\ufeff').strip()
        if url.startswith("http"):
            _public_url_cache["value"] = url
            _public_url_cache["mtime"] = mtime
            return url
    except Exception:
        pass
    return PUBLIC_BASE_URL


class PublicURLMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        ctype = response.headers.get("content-type", "")
        if "text/html" in ctype:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            current_url = get_public_url()
            try:
                text = body.decode("utf-8").replace("__PUBLIC_BASE_URL__", current_url)
            except Exception:
                text = body.decode("utf-8", errors="replace").replace("__PUBLIC_BASE_URL__", current_url)
            new_headers = dict(response.headers)
            new_headers["content-length"] = str(len(text.encode("utf-8")))
            return Response(content=text.encode("utf-8"), media_type="text/html",
                            status_code=response.status_code, headers=new_headers)
        return response


app = FastAPI(title="TokenGo - AI API 中转服务", version="3.0.0")

app.add_middleware(PublicURLMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_proxy.db"))

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"))
PAYMENT_PROOF_DIR = os.path.join(UPLOAD_DIR, "proofs")
PAYMENT_QR_DIR = os.path.join(UPLOAD_DIR, "qr_codes")
os.makedirs(PAYMENT_PROOF_DIR, exist_ok=True)
os.makedirs(PAYMENT_QR_DIR, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")), name="static")

UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "sk-7218e8e5a3e71bbf72f2979f3662d4e20bf355b21f3dea5843d3e1cd197dd469")
UPSTREAM_BASE_URL = os.environ.get("UPSTREAM_BASE_URL", "https://wbtssfvj.kdns.fr/v1")

CHANNELS_CONFIG = [
    {
        "id": "openai-gpt",
        "name": "OpenAI GPT",
        "base_url": UPSTREAM_BASE_URL,
        "api_key": UPSTREAM_API_KEY,
        "models": ["gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
                   "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark", "codex-auto-review", "gpt-5.2"],
        "platform": "OpenAI",
        "weight": 1,
    },
    {
        "id": "openai-image",
        "name": "OpenAI Image",
        "base_url": UPSTREAM_BASE_URL,
        "api_key": UPSTREAM_API_KEY,
        "models": ["gpt-image-1", "gpt-image-1.5", "gpt-image-2"],
        "platform": "OpenAI",
        "weight": 1,
    },
    {
        "id": "claude-proxy",
        "name": "Claude Proxy",
        "base_url": UPSTREAM_BASE_URL,
        "api_key": UPSTREAM_API_KEY,
        "models": ["claude-fable-5", "claude-sonnet-4.6", "claude-haiku-4.5"],
        "platform": "Claude",
        "weight": 1,
    },
]

ALL_MODELS: List[str] = []
for _ch in CHANNELS_CONFIG:
    ALL_MODELS.extend(_ch["models"])

MODEL_PLATFORM: Dict[str, str] = {}
for _ch in CHANNELS_CONFIG:
    for _m in _ch["models"]:
        MODEL_PLATFORM[_m] = _ch["platform"]

DEFAULT_PRICES = {
    "gpt-5.6": {"input": 0.005, "output": 0.015, "platform": "OpenAI"},
    "gpt-5.6-sol": {"input": 0.005, "output": 0.015, "platform": "OpenAI"},
    "gpt-5.6-terra": {"input": 0.005, "output": 0.015, "platform": "OpenAI"},
    "gpt-5.6-luna": {"input": 0.005, "output": 0.015, "platform": "OpenAI"},
    "gpt-5.5": {"input": 0.004, "output": 0.012, "platform": "OpenAI"},
    "gpt-5.4": {"input": 0.003, "output": 0.010, "platform": "OpenAI"},
    "gpt-5.4-mini": {"input": 0.001, "output": 0.004, "platform": "OpenAI"},
    "gpt-5.3-codex-spark": {"input": 0.002, "output": 0.008, "platform": "OpenAI"},
    "codex-auto-review": {"input": 0.002, "output": 0.008, "platform": "OpenAI"},
    "gpt-5.2": {"input": 0.002, "output": 0.006, "platform": "OpenAI"},
    "gpt-image-1": {"input": 0.020, "output": 0.020, "platform": "OpenAI"},
    "gpt-image-1.5": {"input": 0.025, "output": 0.025, "platform": "OpenAI"},
    "gpt-image-2": {"input": 0.030, "output": 0.030, "platform": "OpenAI"},
    "claude-fable-5": {"input": 0.005, "output": 0.015, "platform": "Claude"},
    "claude-sonnet-4.6": {"input": 0.004, "output": 0.012, "platform": "Claude"},
    "claude-haiku-4.5": {"input": 0.001, "output": 0.004, "platform": "Claude"},
}


def get_db():
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    turso_token = os.environ.get("TURSO_AUTH_TOKEN")
    if turso_url and turso_token:
        try:
            import libsql
            conn = libsql.connect(turso_url, auth_token=turso_token)
            conn.row_factory = sqlite3.Row
            return conn
        except ImportError:
            pass
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column(c, table, col, defn):
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
    except sqlite3.OperationalError:
        pass


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tokens
                 (id TEXT PRIMARY KEY, name TEXT, quota REAL, used REAL,
                  expire_at INTEGER, allowed_models TEXT, created_at INTEGER,
                  status TEXT DEFAULT 'active')''')
    c.execute('''CREATE TABLE IF NOT EXISTS usage
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT, model TEXT,
                  prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
                  cost REAL, created_at INTEGER, platform TEXT, channel_id TEXT,
                  user_id TEXT, response_ms INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels
                 (id TEXT PRIMARY KEY, name TEXT, base_url TEXT, api_key TEXT,
                  models TEXT, price_per_1k_tokens REAL, status TEXT DEFAULT 'active',
                  platform TEXT, weight INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS prices
                 (model_name TEXT PRIMARY KEY, price_per_1k_tokens REAL,
                  input_price REAL, output_price REAL, platform TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders
                 (id TEXT PRIMARY KEY, type TEXT, amount REAL, status TEXT,
                  created_at INTEGER, detail TEXT,
                  user_id TEXT, user_email TEXT, payment_method TEXT, usd_quota REAL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tickets
                 (id TEXT PRIMARY KEY, title TEXT, content TEXT, status TEXT,
                  created_at INTEGER, user_id TEXT, user_email TEXT, reply TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS redemptions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, token_id TEXT,
                  amount REAL, created_at INTEGER, status TEXT DEFAULT 'used',
                  user_email TEXT, user_id TEXT, source TEXT DEFAULT 'code')''')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
                  username TEXT, balance REAL DEFAULT 0, created_at REAL,
                  token_id TEXT, session_token TEXT, role TEXT DEFAULT 'user',
                  invite_code TEXT UNIQUE, invited_by TEXT, total_rebate REAL DEFAULT 0,
                  available_rebate REAL DEFAULT 0)''')
    _add_column(c, "users", "role", "TEXT DEFAULT 'user'")
    _add_column(c, "users", "invite_code", "TEXT")
    _add_column(c, "users", "invited_by", "TEXT")
    _add_column(c, "users", "total_rebate", "REAL DEFAULT 0")
    _add_column(c, "users", "available_rebate", "REAL DEFAULT 0")
    _add_column(c, "orders", "user_id", "TEXT")
    _add_column(c, "orders", "user_email", "TEXT")
    _add_column(c, "orders", "payment_method", "TEXT")
    _add_column(c, "orders", "usd_quota", "REAL DEFAULT 0")
    _add_column(c, "tickets", "user_id", "TEXT")
    _add_column(c, "tickets", "user_email", "TEXT")
    _add_column(c, "tickets", "reply", "TEXT")
    _add_column(c, "redemptions", "user_email", "TEXT")
    _add_column(c, "redemptions", "user_id", "TEXT")
    _add_column(c, "redemptions", "source", "TEXT DEFAULT 'code'")
    _add_column(c, "usage", "user_id", "TEXT")
    _add_column(c, "usage", "response_ms", "INTEGER DEFAULT 0")
    c.execute('''CREATE TABLE IF NOT EXISTS invites
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  inviter_id TEXT NOT NULL, invitee_id TEXT NOT NULL, invitee_email TEXT,
                  created_at REAL, status TEXT DEFAULT 'active')''')
    c.execute('''CREATE TABLE IF NOT EXISTS rebates
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  inviter_id TEXT NOT NULL, invitee_id TEXT, invitee_email TEXT,
                  order_id TEXT, order_amount REAL, rebate_amount REAL,
                  created_at REAL, status TEXT DEFAULT 'available',
                  note TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS response_times
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  token_id TEXT, model TEXT, response_ms INTEGER,
                  created_at INTEGER, user_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS code_usage
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  code TEXT NOT NULL, user_id TEXT, user_email TEXT,
                  used_at INTEGER, UNIQUE(code, user_email))''')
    c.execute('''CREATE TABLE IF NOT EXISTS failed_logins
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT, ip TEXT, failed_at INTEGER,
                  lock_until INTEGER DEFAULT 0)''')
    _add_column(c, "users", "status", "TEXT DEFAULT 'active'")
    _add_column(c, "users", "locked_until", "INTEGER DEFAULT 0")
    _add_column(c, "users", "failed_count", "INTEGER DEFAULT 0")
    _add_column(c, "orders", "pay_amount_actual", "REAL DEFAULT 0")
    _add_column(c, "orders", "pay_screenshot", "TEXT")
    _add_column(c, "orders", "pay_txid", "TEXT")
    _add_column(c, "orders", "pay_note", "TEXT")
    _add_column(c, "orders", "confirmed_by", "TEXT")
    _add_column(c, "orders", "confirmed_at", "INTEGER DEFAULT 0")
    _add_column(c, "orders", "unique_suffix", "TEXT")
    c.execute('''CREATE TABLE IF NOT EXISTS payment_config
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  method TEXT NOT NULL UNIQUE,
                  name TEXT NOT NULL,
                  qr_code TEXT,
                  account TEXT,
                  instructions TEXT,
                  enabled INTEGER DEFAULT 1,
                  updated_at REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS payment_proofs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  order_id TEXT NOT NULL,
                  user_id TEXT,
                  user_email TEXT,
                  screenshot_path TEXT,
                  txid TEXT,
                  note TEXT,
                  uploaded_at REAL,
                  status TEXT DEFAULT 'pending')''')
    c.execute('''CREATE TABLE IF NOT EXISTS cards
                 (id TEXT PRIMARY KEY, face_value REAL NOT NULL, price REAL NOT NULL,
                  status TEXT DEFAULT 'unused', used_by TEXT, used_at REAL,
                  created_at REAL, batch_id TEXT)''')

    admin_email = "Commecy2014@gmail.com".lower()
    admin_pw = hash_password("admin123")
    c.execute("SELECT id FROM users WHERE email=?", (admin_email,))
    if not c.fetchone():
        admin_id = "admin-" + secrets.token_hex(8)
        admin_invite = "TGADMIN" + secrets.token_hex(2).upper()
        c.execute('''INSERT INTO users(id,email,password,username,balance,created_at,role,invite_code)
                     VALUES(?,?,?,?,?,?,?,?)''',
                  (admin_id, admin_email, admin_pw, "管理员", 9999.0, time.time(), "admin", admin_invite))
    else:
        c.execute("UPDATE users SET role='admin', password=?, username='管理员', balance=9999.0, failed_count=0, locked_until=0 WHERE email=?",
                  (admin_pw, admin_email))

    c.execute("SELECT id FROM users WHERE invite_code IS NULL OR invite_code=''")
    for row in c.fetchall():
        uid = row["id"]
        ic = "TG" + secrets.token_hex(4).upper()
        c.execute("UPDATE users SET invite_code=? WHERE id=?", (ic, uid))

    default_payments = [
        ("alipay", "支付宝", "", "", "请使用支付宝扫描二维码付款，付款后点击「我已支付」上传截图", 1),
        ("wechat", "微信支付", "", "", "请使用微信扫描二维码付款，付款后点击「我已支付」上传截图", 1),
        ("mastercard", "MasterCard 信用卡", "", "", "请使用信用卡完成支付，支持Visa、MasterCard等国际信用卡，支付后点击「我已支付」", 1),
        ("usdt_trc20", "USDT (TRC20)", "", "", "请向以下 USDT TRC20 地址转账，转账后点击「我已支付」上传截图", 1),
        ("usdt_erc20", "USDT (ERC20)", "", "", "请向以下 USDT ERC20 地址转账，转账后点击「我已支付」上传截图", 0),
    ]
    for method, name, qr, acct, instr, en in default_payments:
        c.execute("SELECT id FROM payment_config WHERE method=?", (method,))
        if not c.fetchone():
            c.execute('''INSERT INTO payment_config(method,name,qr_code,account,instructions,enabled,updated_at)
                         VALUES(?,?,?,?,?,?,?)''',
                      (method, name, qr, acct, instr, en, time.time()))

    for ch in CHANNELS_CONFIG:
        c.execute("SELECT id FROM channels WHERE id=?", (ch["id"],))
        if not c.fetchone():
            c.execute('''INSERT INTO channels(id,name,base_url,api_key,models,
                         price_per_1k_tokens,status,platform,weight)
                         VALUES(?,?,?,?,?,?,?,?,?)''',
                      (ch["id"], ch["name"], ch["base_url"], ch["api_key"],
                       json.dumps(ch["models"]), 0.0, "active", ch["platform"], ch["weight"]))
        else:
            c.execute('''UPDATE channels SET base_url=?, api_key=?, models=?, platform=?, status='active'
                         WHERE id=?''',
                      (ch["base_url"], ch["api_key"], json.dumps(ch["models"]),
                       ch["platform"], ch["id"]))

    for m, p in DEFAULT_PRICES.items():
        c.execute("INSERT OR REPLACE INTO prices(model_name,price_per_1k_tokens,input_price,output_price,platform)"
                  " VALUES(?,?,?,?,?)",
                  (m, p["input"], p["input"], p["output"], p["platform"]))

    c.execute("SELECT id FROM tokens WHERE id='demo-master'")
    if not c.fetchone():
        c.execute('''INSERT INTO tokens(id,name,quota,used,expire_at,allowed_models,created_at,status)
                     VALUES(?,?,?,?,?,?,?,?)''',
                  ("demo-master", "主密钥", 100.0, 12.34, 0, "all", int(time.time()), "active"))

    conn.commit()
    conn.close()


def find_channel_for_model(model: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM channels WHERE status='active'")
    for row in c.fetchall():
        models = json.loads(row["models"]) if row["models"] else []
        if model in models:
            conn.close()
            return dict(row)
    conn.close()
    return None


def verify_token(authorization: Optional[str]) -> Dict[str, Any]:
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")
    key = authorization.replace("Bearer ", "").strip()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM tokens WHERE id=? AND status='active'", (key,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    t = dict(row)
    if t["expire_at"] and t["expire_at"] < int(time.time()):
        raise HTTPException(status_code=401, detail="API Key 已过期")
    if t["quota"] > 0 and t["used"] >= t["quota"]:
        raise HTTPException(status_code=402, detail="额度已用尽")
    return t


def hash_password(password: str) -> str:
    salt = "tokengo!@#2026"
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed


def get_user_by_session(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    if not authorization:
        return None
    token = authorization.replace("Bearer ", "").strip()
    if not token or token.startswith("sk-tg-") or token == "demo-master":
        return None
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE session_token=?", (token,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def require_user(authorization: Optional[str]) -> Dict[str, Any]:
    user = get_user_by_session(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def create_token_internal(name: str, quota: float, expire_days: int = 0,
                          allowed_models: str = "all") -> str:
    tid = "sk-tg-" + secrets.token_hex(16)
    expire_at = int((datetime.now() + timedelta(days=expire_days)).timestamp()) if expire_days else 0
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO tokens(id,name,quota,used,expire_at,allowed_models,created_at,status)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (tid, name, quota, 0.0, expire_at, allowed_models, int(time.time()), "active"))
    if user := get_user_by_session(None):
        if not user.get("token_id"):
            c.execute("UPDATE users SET token_id=? WHERE id=?", (tid, user["id"]))
    conn.commit()
    conn.close()
    return tid


def generate_card_id() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    def seg():
        return "".join(secrets.choice(alphabet) for _ in range(4))
    return f"TG-{seg()}-{seg()}-{seg()}"


def is_admin(user: Optional[Dict[str, Any]]) -> bool:
    return bool(user and (user.get("role") == "admin" or user.get("token_id") == "demo-master"))


def require_admin(authorization: Optional[str]) -> Dict[str, Any]:
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")
    token = authorization.replace("Bearer ", "").strip()
    if token == "demo-master":
        return {"id": "demo-master", "email": "admin", "username": "管理员",
                "token_id": "demo-master", "is_master_key": True}
    user = require_user(authorization)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def record_usage(token_id: str, model: str, prompt_tokens: int, completion_tokens: int,
                 platform: str, channel_id: str, cost: float = 0.0,
                 response_ms: int = 0, user_id: Optional[str] = None):
    total = (prompt_tokens or 0) + (completion_tokens or 0)
    now = int(time.time())
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO usage(token_id,model,prompt_tokens,completion_tokens,total_tokens,
                 cost,created_at,platform,channel_id,user_id,response_ms)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
              (token_id, model, prompt_tokens, completion_tokens, total, cost,
               now, platform, channel_id, user_id, response_ms))
    c.execute("UPDATE tokens SET used = used + ? WHERE id=?", (cost, token_id))
    if user_id:
        c.execute("UPDATE users SET balance = MAX(0, balance - ?) WHERE id=?", (cost, user_id))
    elif token_id:
        c.execute("SELECT id FROM users WHERE token_id=?", (token_id,))
        u = c.fetchone()
        if u:
            c.execute("UPDATE users SET balance = MAX(0, balance - ?) WHERE id=?", (cost, u["id"]))
            c.execute("UPDATE usage SET user_id=? WHERE token_id=? AND created_at=?", (u["id"], token_id, now))
    c.execute('''INSERT INTO response_times(token_id,model,response_ms,created_at,user_id)
                 VALUES(?,?,?,?,?)''',
              (token_id, model, response_ms or 0, now, user_id))
    conn.commit()
    conn.close()


def compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p = DEFAULT_PRICES.get(model, {"input": 0.002, "output": 0.006})
    return (prompt_tokens / 1000.0) * p["input"] + (completion_tokens / 1000.0) * p["output"]


def issue_rebate(inviter_id: str, invitee_id: str, invitee_email: str,
                 order_id: str, order_amount_rmb: float, conn) -> float:
    rebate = round(order_amount_rmb * 0.15, 4)
    if rebate <= 0:
        return 0.0
    c = conn.cursor()
    c.execute('''INSERT INTO rebates(inviter_id,invitee_id,invitee_email,order_id,
                 order_amount,rebate_amount,created_at,status,note)
                 VALUES(?,?,?,?,?,?,?,?,?)''',
              (inviter_id, invitee_id, invitee_email, order_id,
               order_amount_rmb, rebate, time.time(), "available", f"好友 {invitee_email} 充值返利"))
    c.execute("UPDATE users SET total_rebate = total_rebate + ?, available_rebate = available_rebate + ? WHERE id=?",
              (rebate, rebate, inviter_id))
    return rebate


def generate_invite_code() -> str:
    return "TG" + secrets.token_hex(4).upper()


LOGIN_RATE_LIMIT = 10
LOGIN_FAIL_LOCK_THRESHOLD = 5
LOGIN_LOCK_DURATION = 900
IP_BLOCK_THRESHOLD = 20
IP_BLOCK_DURATION = 3600

_CAPTCHA_STORE: Dict[str, Dict[str, Any]] = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_login_rate_limit(ip: str) -> None:
    now = int(time.time())
    window_start = now - 60
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM failed_logins WHERE ip=? AND failed_at>=?",
              (ip, window_start))
    count = c.fetchone()["n"]
    c.execute("SELECT lock_until FROM failed_logins WHERE ip=? ORDER BY lock_until DESC LIMIT 1", (ip,))
    row = c.fetchone()
    conn.close()
    if count >= LOGIN_RATE_LIMIT:
        raise HTTPException(status_code=429, detail=f"请求过于频繁，请稍后再试（每分钟限 {LOGIN_RATE_LIMIT} 次）")
    if row and row["lock_until"] and row["lock_until"] > now:
        remain = row["lock_until"] - now
        raise HTTPException(status_code=429, detail=f"该 IP 已被临时封禁，请 {remain} 秒后再试")


def check_account_lockout(email: str) -> None:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT failed_count, locked_until, status FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    if not row:
        return
    if row["status"] == "locked":
        raise HTTPException(status_code=403, detail="账户已被管理员锁定，请联系客服")
    if row["locked_until"] and row["locked_until"] > int(time.time()):
        remain = row["locked_until"] - int(time.time())
        raise HTTPException(status_code=403, detail=f"账户因多次登录失败已被锁定，请 {remain} 秒后再试")


def record_failed_login(email: str, ip: str) -> int:
    now = int(time.time())
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO failed_logins(email,ip,failed_at,lock_until) VALUES(?,?,?,0)",
              (email, ip, now))
    c.execute("UPDATE users SET failed_count = failed_count + 1 WHERE email=?", (email,))
    c.execute("SELECT failed_count FROM users WHERE email=?", (email,))
    row = c.fetchone()
    fail_count = row["failed_count"] if row else 0
    if fail_count >= LOGIN_FAIL_LOCK_THRESHOLD:
        lock_until = now + LOGIN_LOCK_DURATION
        c.execute("UPDATE users SET locked_until=? WHERE email=?", (lock_until, email))
        c.execute("UPDATE users SET failed_count=0 WHERE email=?", (email,))
    c.execute("SELECT COUNT(*) AS n FROM failed_logins WHERE ip=? AND failed_at>=?",
              (ip, now - 3600))
    ip_fails = c.fetchone()["n"]
    if ip_fails >= IP_BLOCK_THRESHOLD:
        c.execute("UPDATE failed_logins SET lock_until=? WHERE ip=? AND lock_until=0",
                  (now + IP_BLOCK_DURATION, ip))
    conn.commit()
    conn.close()
    return fail_count


def reset_failed_logins(email: str) -> None:
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET failed_count=0, locked_until=0 WHERE email=?", (email,))
    c.execute("DELETE FROM failed_logins WHERE email=?", (email,))
    conn.commit()
    conn.close()


def generate_captcha() -> Dict[str, str]:
    a = secrets.randbelow(9) + 1
    b = secrets.randbelow(9) + 1
    op = secrets.choice(["+", "-", "×"])
    if op == "+":
        answer = str(a + b)
    elif op == "-":
        if a < b:
            a, b = b, a
        answer = str(a - b)
    else:
        answer = str(a * b)
    token = secrets.token_urlsafe(16)
    _CAPTCHA_STORE[token] = {"answer": answer, "expires": time.time() + 300}
    expired = [k for k, v in _CAPTCHA_STORE.items() if v["expires"] < time.time()]
    for k in expired:
        _CAPTCHA_STORE.pop(k, None)
    return {"token": token, "question": f"{a} {op} {b} = ?"}


def verify_captcha(token: str, answer: str) -> bool:
    if not token or not answer:
        return False
    item = _CAPTCHA_STORE.pop(token, None)
    if not item:
        return False
    if item["expires"] < time.time():
        return False
    return item["answer"].strip() == answer.strip()


def require_captcha_for_login(email: str) -> bool:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT failed_count FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    return bool(row and row["failed_count"] >= 2)


class TokenCreate(BaseModel):
    name: str = "新密钥"
    quota: float = 10.0
    expire_days: int = 0
    allowed_models: str = "all"


class OrderCreate(BaseModel):
    type: str = "topup"
    amount: float
    detail: str = ""
    payment_method: str = "alipay"


class TicketCreate(BaseModel):
    title: str
    content: str = ""


class RedeemCode(BaseModel):
    code: str
    token_id: str = "demo-master"


class ProfileUpdate(BaseModel):
    username: str = ""
    email: str = ""
    password: str = ""


class UserRegister(BaseModel):
    email: str
    password: str
    username: str = ""
    invite_code: str = ""
    captcha_token: str = ""
    captcha_answer: str = ""


class UserLogin(BaseModel):
    email: str
    password: str
    captcha_token: str = ""
    captcha_answer: str = ""


class CardGenerate(BaseModel):
    count: int = 1
    face_value: float = 12.0
    price: float = 1.0


class CardRedeem(BaseModel):
    card_id: str
    email: str


class RebateWithdraw(BaseModel):
    amount: float = 0.0


class PaymentProofSubmit(BaseModel):
    txid: str = ""
    note: str = ""


class PaymentConfigUpdate(BaseModel):
    method: str
    name: str = ""
    qr_code: str = ""
    account: str = ""
    instructions: str = ""
    enabled: int = 1


def load_template(name: str) -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", name + ".html")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return f"<html><body><h1>{name} page not found</h1></body></html>"


@app.get("/", response_class=HTMLResponse)
async def index():
    return load_template("home")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return load_template("dashboard")


@app.get("/help", response_class=HTMLResponse)
async def help_page():
    return load_template("help")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return load_template("login")


@app.get("/redeem", response_class=HTMLResponse)
async def redeem_page():
    return load_template("redeem")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return load_template("admin")


@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    data = []
    for m in ALL_MODELS:
        data.append({
            "id": m,
            "object": "model",
            "created": now,
            "owned_by": MODEL_PLATFORM.get(m, "openai"),
        })
    return {"object": "list", "data": data}


async def _forward_chat(body: dict, authorization: Optional[str]):
    token = verify_token(authorization)
    model = body.get("model", "")
    channel = find_channel_for_model(model)
    if not channel:
        raise HTTPException(status_code=400, detail=f"不支持的模型: {model}")

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE token_id=?", (token["id"],))
    u_row = c.fetchone()
    conn.close()
    user_id = u_row["id"] if u_row else None

    is_stream = bool(body.get("stream", False))
    headers = {
        "Authorization": f"Bearer {channel['api_key']}",
        "Content-Type": "application/json",
    }
    url = channel["base_url"].rstrip("/") + "/chat/completions"

    platform = channel["platform"]
    channel_id = channel["id"]
    t0 = time.time()

    if is_stream:
        async def gen():
            prompt_t = 0
            completion_t = 0
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
                    async with client.stream("POST", url, headers=headers, json=body) as resp:
                        if resp.status_code >= 400:
                            err = await resp.aread()
                            yield f"data: {err.decode(errors='ignore')}\n\n"
                            return
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            if line.startswith("data:"):
                                payload = line[5:].strip()
                                if payload == "[DONE]":
                                    yield "data: [DONE]\n\n"
                                    break
                                try:
                                    obj = json.loads(payload)
                                    u = obj.get("usage")
                                    if u:
                                        prompt_t = u.get("prompt_tokens", prompt_t)
                                        completion_t = u.get("completion_tokens", completion_t)
                                except Exception:
                                    pass
                            yield line + "\n"
                            if "" not in line:
                                yield "\n"
            except httpx.HTTPError as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            finally:
                if prompt_t or completion_t:
                    cost = compute_cost(model, prompt_t, completion_t)
                    elapsed_ms = int((time.time() - t0) * 1000)
                    record_usage(token["id"], model, prompt_t, completion_t, platform, channel_id,
                                 cost, response_ms=elapsed_ms, user_id=user_id)

        return StreamingResponse(gen(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        resp = await client.post(url, headers=headers, json=body)
    elapsed_ms = int((time.time() - t0) * 1000)
    if resp.status_code >= 400:
        return JSONResponse(status_code=resp.status_code, content=resp.json() if resp.text else {"error": "upstream error", "status": resp.status_code})

    try:
        data = resp.json()
    except Exception:
        return JSONResponse(status_code=502, content={"error": "上游返回无效响应", "detail": (resp.text[:500] if resp.text else "empty response")})
    usage = data.get("usage", {}) or {}
    pt = usage.get("prompt_tokens", 0)
    ct = usage.get("completion_tokens", 0)
    cost = compute_cost(model, pt, ct)
    record_usage(token["id"], model, pt, ct, platform, channel_id, cost,
                 response_ms=elapsed_ms, user_id=user_id)
    return data


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: Optional[str] = Header(None)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")
    return await _forward_chat(body, authorization)


@app.post("/v1/completions")
async def completions(request: Request, authorization: Optional[str] = Header(None)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")
    return await _forward_chat(body, authorization)


@app.post("/v1/messages")
async def anthropic_messages(request: Request, authorization: Optional[str] = Header(None),
                             x_api_key: Optional[str] = Header(None, alias="x-api-key")):
    auth = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    token = verify_token(auth)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")
    model = body.get("model", "")
    channel = find_channel_for_model(model) or find_channel_for_model("claude-sonnet-4.6")
    if not channel:
        raise HTTPException(status_code=400, detail=f"不支持的模型: {model}")

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE token_id=?", (token["id"],))
    u_row = c.fetchone()
    conn.close()
    user_id = u_row["id"] if u_row else None

    headers = {
        "x-api-key": channel["api_key"],
        "anthropic-version": body.get("anthropic_version", "2023-06-01"),
        "Content-Type": "application/json",
    }
    url = channel["base_url"].rstrip("/") + "/messages"
    is_stream = bool(body.get("stream", False))
    t0 = time.time()

    if is_stream:
        async def gen():
            pt = ct = 0
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
                    async with client.stream("POST", url, headers=headers, json=body) as resp:
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            if "input_tokens" in line:
                                try:
                                    pt = json.loads(line.split(":", 1)[1]).get("message", {}).get("input_tokens", pt)
                                except Exception:
                                    pass
                            if "output_tokens" in line:
                                try:
                                    obj = json.loads(line.split(":", 1)[1])
                                    ct = obj.get("usage", {}).get("output_tokens", obj.get("output_tokens", ct))
                                except Exception:
                                    pass
                            yield line + "\n"
            except httpx.HTTPError as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
            finally:
                if pt or ct:
                    cost = compute_cost(model, pt, ct)
                    elapsed_ms = int((time.time() - t0) * 1000)
                    record_usage(token["id"], model, pt, ct, channel["platform"], channel["id"], cost,
                                 response_ms=elapsed_ms, user_id=user_id)

        return StreamingResponse(gen(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        resp = await client.post(url, headers=headers, json=body)
    elapsed_ms = int((time.time() - t0) * 1000)
    data = resp.json() if resp.text else {}
    usage = data.get("usage", {}) or {}
    pt = usage.get("input_tokens", 0)
    ct = usage.get("output_tokens", 0)
    cost = compute_cost(model, pt, ct)
    record_usage(token["id"], model, pt, ct, channel["platform"], channel["id"], cost,
                 response_ms=elapsed_ms, user_id=user_id)
    return JSONResponse(status_code=resp.status_code, content=data)


@app.get("/api/stats")
def api_stats(authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    conn = get_db()
    c = conn.cursor()
    today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    if user and user.get("token_id"):
        scope_token = user["token_id"]
        c.execute("SELECT SUM(quota-used) AS bal FROM tokens WHERE id=?", (scope_token,))
        bal = c.fetchone()["bal"] or 0.0
        c.execute("SELECT COUNT(*) AS n FROM tokens WHERE id=?", (scope_token,))
        key_count = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM usage WHERE token_id=? AND created_at>=?", (scope_token, today_start))
        today_req = c.fetchone()["n"]
        c.execute("SELECT SUM(cost) AS s FROM usage WHERE token_id=? AND created_at>=?", (scope_token, today_start))
        today_cost = c.fetchone()["s"] or 0.0
        c.execute("SELECT SUM(total_tokens) AS s FROM usage WHERE token_id=? AND created_at>=?", (scope_token, today_start))
        today_tokens = c.fetchone()["s"] or 0
        c.execute("SELECT SUM(total_tokens) AS s FROM usage WHERE token_id=?", (scope_token,))
        total_tokens = c.fetchone()["s"] or 0
        c.execute("SELECT platform, COUNT(*) AS n, SUM(cost) AS cost, SUM(total_tokens) AS tok FROM usage WHERE token_id=? GROUP BY platform", (scope_token,))
        platform_rows = c.fetchall()
        c.execute("SELECT model, COUNT(*) AS n, SUM(total_tokens) AS tok, SUM(cost) AS cost FROM usage WHERE token_id=? GROUP BY model ORDER BY n DESC", (scope_token,))
        model_rows = c.fetchall()
        trend_q = "SELECT COALESCE(SUM(total_tokens),0) AS s FROM usage WHERE token_id=? AND created_at>=? AND created_at<?"
        trend_params = lambda ds, de: (scope_token, ds, de)
        c.execute("SELECT * FROM usage WHERE token_id=? ORDER BY created_at DESC LIMIT 10", (scope_token,))
        recent_rows = c.fetchall()
    else:
        c.execute("SELECT SUM(quota-used) AS bal FROM tokens WHERE status='active'")
        bal = c.fetchone()["bal"] or 0.0
        c.execute("SELECT COUNT(*) AS n FROM tokens")
        key_count = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) AS n FROM usage WHERE created_at>=?", (today_start,))
        today_req = c.fetchone()["n"]
        c.execute("SELECT SUM(cost) AS s FROM usage WHERE created_at>=?", (today_start,))
        today_cost = c.fetchone()["s"] or 0.0
        c.execute("SELECT SUM(total_tokens) AS s FROM usage WHERE created_at>=?", (today_start,))
        today_tokens = c.fetchone()["s"] or 0
        c.execute("SELECT SUM(total_tokens) AS s FROM usage")
        total_tokens = c.fetchone()["s"] or 0
        c.execute("SELECT platform, COUNT(*) AS n, SUM(cost) AS cost, SUM(total_tokens) AS tok FROM usage GROUP BY platform")
        platform_rows = c.fetchall()
        c.execute("SELECT model, COUNT(*) AS n, SUM(total_tokens) AS tok, SUM(cost) AS cost FROM usage GROUP BY model ORDER BY n DESC")
        model_rows = c.fetchall()
        trend_q = "SELECT COALESCE(SUM(total_tokens),0) AS s FROM usage WHERE created_at>=? AND created_at<?"
        trend_params = lambda ds, de: (ds, de)
        c.execute("SELECT * FROM usage ORDER BY created_at DESC LIMIT 10")
        recent_rows = c.fetchall()

    platform_split = [{"platform": r["platform"] or "未知", "requests": r["n"],
                       "cost": round(r["cost"] or 0, 4), "tokens": r["tok"] or 0} for r in platform_rows]

    model_dist = [{"model": r["model"], "requests": r["n"], "tokens": r["tok"] or 0,
                   "cost": round(r["cost"] or 0, 4)} for r in model_rows]

    trend = []
    for i in range(13, -1, -1):
        day = (datetime.now() - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        ds = int(day.timestamp())
        de = ds + 86400
        c.execute(trend_q, trend_params(ds, de))
        trend.append({"date": day.strftime("%m-%d"), "tokens": c.fetchone()["s"]})

    recent = [dict(r) for r in recent_rows]

    if user and user.get("token_id"):
        c.execute("SELECT AVG(response_ms) AS a FROM response_times WHERE token_id=? AND response_ms>0 ORDER BY created_at DESC LIMIT 100",
                  (user["token_id"],))
    else:
        c.execute("SELECT AVG(response_ms) AS a FROM response_times WHERE response_ms>0 ORDER BY created_at DESC LIMIT 100")
    avg_ms = c.fetchone()["a"] or 0
    conn.close()

    return {
        "balance": round(bal, 4),
        "key_count": key_count,
        "today_requests": today_req,
        "today_cost": round(today_cost, 4),
        "today_tokens": today_tokens,
        "total_tokens": total_tokens,
        "rpm": today_req // max(1, (int(time.time()) - today_start) // 60),
        "tpm": today_tokens // max(1, (int(time.time()) - today_start) // 60),
        "avg_response_ms": int(avg_ms),
        "platform_split": platform_split,
        "model_distribution": model_dist,
        "trend": trend,
        "recent": recent,
    }


@app.get("/api/tokens")
def list_tokens(authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    conn = get_db()
    c = conn.cursor()
    if user and user.get("token_id"):
        c.execute("SELECT * FROM tokens WHERE id=? ORDER BY created_at DESC", (user["token_id"],))
    else:
        c.execute("SELECT * FROM tokens ORDER BY created_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["remain"] = round(r["quota"] - r["used"], 4)
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
        r["expire_at_str"] = (datetime.fromtimestamp(r["expire_at"]).strftime("%Y-%m-%d")
                              if r["expire_at"] else "永久")
    return rows


@app.post("/api/tokens")
def create_token(body: TokenCreate, authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    tid = "sk-tg-" + secrets.token_hex(16)
    expire_at = int((datetime.now() + timedelta(days=body.expire_days)).timestamp()) if body.expire_days else 0
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO tokens(id,name,quota,used,expire_at,allowed_models,created_at,status)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (tid, body.name, body.quota, 0.0, expire_at, body.allowed_models, int(time.time()), "active"))
    if user and not user.get("token_id"):
        c.execute("UPDATE users SET token_id=? WHERE id=?", (tid, user["id"]))
    conn.commit()
    conn.close()
    return {"id": tid, "name": body.name, "quota": body.quota, "status": "active"}


@app.delete("/api/tokens/{tid}")
def delete_token(tid: str, authorization: Optional[str] = Header(None)):
    if tid == "demo-master":
        raise HTTPException(status_code=400, detail="主密钥不可删除")
    user = get_user_by_session(authorization)
    conn = get_db()
    c = conn.cursor()
    if not (user and is_admin(user)):
        if not user or user.get("token_id") != tid:
            conn.close()
            raise HTTPException(status_code=403, detail="无权删除此密钥")
    c.execute("DELETE FROM tokens WHERE id=?", (tid,))
    if user and user.get("token_id") == tid:
        c.execute("UPDATE users SET token_id=NULL WHERE id=?", (user["id"],))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/channels")
def list_channels():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM channels ORDER BY platform")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    out = []
    for r in rows:
        r["models"] = json.loads(r["models"]) if r["models"] else []
        out.append(r)
    return out


@app.get("/api/prices")
def list_prices():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM prices ORDER BY platform, model_name")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


@app.get("/api/usage")
def api_usage(days: int = Query(7), limit: int = Query(100), authorization: Optional[str] = Header(None)):
    since = int((datetime.now() - timedelta(days=days)).timestamp())
    conn = get_db()
    c = conn.cursor()
    user = get_user_by_session(authorization)
    if user and is_admin(user):
        c.execute("SELECT * FROM usage WHERE created_at>=? ORDER BY created_at DESC LIMIT ?", (since, limit))
    elif user and user.get("token_id"):
        c.execute("SELECT * FROM usage WHERE token_id=? AND created_at>=? ORDER BY created_at DESC LIMIT ?",
                  (user["token_id"], since, limit))
    else:
        c.execute("SELECT * FROM usage WHERE created_at>=? ORDER BY created_at DESC LIMIT ?", (since, limit))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S")
    return rows


@app.get("/api/orders")
def list_orders(authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    conn = get_db()
    c = conn.cursor()
    if user and is_admin(user):
        c.execute("SELECT * FROM orders ORDER BY created_at DESC")
    elif user:
        c.execute("SELECT * FROM orders WHERE user_id=? OR user_email=? ORDER BY created_at DESC",
                  (user["id"], user["email"]))
    else:
        c.execute("SELECT * FROM orders ORDER BY created_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
        r["paid_at_str"] = (datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
                            if r["status"] == "paid" else "")
    return rows


@app.post("/api/orders")
def create_order(body: OrderCreate, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="金额必须大于 0")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM payment_config WHERE method=? AND enabled=1", (body.payment_method,))
    pay_cfg = c.fetchone()
    if not pay_cfg:
        conn.close()
        raise HTTPException(status_code=400, detail=f"支付方式 {body.payment_method} 不可用")
    unique_suffix = f".{secrets.randbelow(99) + 1:02d}"
    pay_amount_actual = round(body.amount + float(unique_suffix), 2)
    oid = "TG" + secrets.token_hex(8).upper()
    usd_quota = body.amount * 12
    c.execute('''INSERT INTO orders(id,type,amount,status,created_at,detail,user_id,user_email,
                 payment_method,usd_quota,pay_amount_actual,unique_suffix)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
              (oid, body.type, body.amount, "pending", int(time.time()), body.detail,
               user["id"], user["email"], body.payment_method, usd_quota,
               pay_amount_actual, unique_suffix))
    conn.commit()
    conn.close()
    return {
        "id": oid, "status": "pending", "usd_quota": usd_quota,
        "pay_amount_actual": pay_amount_actual,
        "unique_suffix": unique_suffix,
        "payment_method": body.payment_method,
        "payment_name": pay_cfg["name"],
        "qr_code": pay_cfg["qr_code"] or "",
        "account": pay_cfg["account"] or "",
        "instructions": pay_cfg["instructions"] or "",
    }


@app.post("/api/orders/{oid}/submit-proof")
async def submit_payment_proof(oid: str, authorization: Optional[str] = Header(None),
                               txid: str = Form(""), note: str = Form(""),
                               screenshot: UploadFile = File(None)):
    user = require_user(authorization)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE id=?", (oid,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="订单不存在")
    if row["user_id"] != user["id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="无权操作此订单")
    if row["status"] not in ("pending", "rejected"):
        conn.close()
        raise HTTPException(status_code=400, detail=f"订单状态为 {row['status']}，无法提交凭证")
    screenshot_path = ""
    if screenshot and screenshot.filename:
        ext = os.path.splitext(screenshot.filename)[1] or ".png"
        filename = f"proof_{oid}_{uuid.uuid4().hex[:8]}{ext}"
        filepath = os.path.join(PAYMENT_PROOF_DIR, filename)
        content = await screenshot.read()
        with open(filepath, "wb") as f:
            f.write(content)
        screenshot_path = f"/uploads/proofs/{filename}"
    c.execute('''UPDATE orders SET status='verifying', pay_txid=?, pay_note=?,
                 pay_screenshot=?, confirmed_at=0 WHERE id=?''',
              (txid, note, screenshot_path, oid))
    c.execute('''INSERT INTO payment_proofs(order_id,user_id,user_email,screenshot_path,txid,note,uploaded_at,status)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (oid, user["id"], user["email"], screenshot_path, txid, note, time.time(), "pending"))
    conn.commit()
    conn.close()
    return {"ok": True, "status": "verifying", "message": "凭证已提交，等待管理员确认到账"}


@app.post("/api/admin/orders/{oid}/confirm")
def admin_confirm_order(oid: str, authorization: Optional[str] = Header(None)):
    admin = require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE id=?", (oid,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="订单不存在")
    if row["status"] == "paid":
        conn.close()
        raise HTTPException(status_code=400, detail="订单已确认")
    if row["status"] not in ("pending", "verifying"):
        conn.close()
        raise HTTPException(status_code=400, detail=f"订单状态 {row['status']}，无法确认")
    amount = row["amount"]
    usd_quota = row["usd_quota"] or amount * 12
    c.execute("SELECT * FROM users WHERE id=?", (row["user_id"],))
    u_row = c.fetchone()
    if not u_row:
        conn.close()
        raise HTTPException(status_code=400, detail="用户不存在")
    token_id = u_row["token_id"]
    if not token_id:
        token_id = create_token_internal(name=u_row["email"], quota=0.0, expire_days=0, allowed_models="all")
        c.execute("UPDATE users SET token_id=? WHERE id=?", (token_id, u_row["id"]))
    c.execute("UPDATE tokens SET quota = quota + ? WHERE id=?", (usd_quota, token_id))
    c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (usd_quota, u_row["id"]))
    c.execute('''UPDATE orders SET status='paid', confirmed_by=?, confirmed_at=? WHERE id=?''',
              (admin.get("id", "admin"), int(time.time()), oid))
    c.execute("UPDATE payment_proofs SET status='confirmed' WHERE order_id=?", (oid,))
    rebate_amount = 0.0
    if row["type"] in ("topup", "subscription") and u_row["invited_by"]:
        c.execute("SELECT id FROM users WHERE invite_code=?", (u_row["invited_by"],))
        inviter = c.fetchone()
        if inviter:
            rebate_amount = issue_rebate(inviter["id"], u_row["id"], u_row["email"],
                                         oid, amount, conn)
    conn.commit()
    conn.close()
    return {"ok": True, "added_quota": usd_quota, "rebate_amount": rebate_amount,
            "token_id": token_id, "confirmed_by": admin.get("id", "admin")}


@app.post("/api/admin/orders/{oid}/reject")
def admin_reject_order(oid: str, authorization: Optional[str] = Header(None),
                       reason: str = ""):
    admin = require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE id=?", (oid,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="订单不存在")
    if row["status"] == "paid":
        conn.close()
        raise HTTPException(status_code=400, detail="已确认订单不可拒绝")
    c.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
    c.execute("UPDATE payment_proofs SET status='rejected' WHERE order_id=?", (oid,))
    conn.commit()
    conn.close()
    return {"ok": True, "status": "rejected", "reason": reason}


@app.get("/api/admin/orders/pending")
def admin_pending_orders(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT o.*, u.username, p.screenshot_path, p.txid AS proof_txid, p.note AS proof_note
                 FROM orders o
                 LEFT JOIN users u ON o.user_id=u.id
                 LEFT JOIN payment_proofs p ON o.id=p.order_id AND p.status='pending'
                 WHERE o.status IN ('verifying','pending')
                 ORDER BY o.created_at DESC''')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
    return rows


@app.get("/api/admin/orders")
def admin_all_orders(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT o.*, u.username
                 FROM orders o
                 LEFT JOIN users u ON o.user_id=u.id
                 ORDER BY o.created_at DESC''')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
    return rows


@app.get("/api/payment/config")
def get_payment_config(authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT method,name,qr_code,account,instructions FROM payment_config WHERE enabled=1")
    methods = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"methods": methods}


@app.get("/api/admin/payment/config/all")
def admin_get_all_payment_config(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM payment_config")
    methods = [dict(r) for r in c.fetchall()]
    conn.close()
    return methods


@app.post("/api/admin/payment/config")
def admin_update_payment_config(body: PaymentConfigUpdate, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM payment_config WHERE method=?", (body.method,))
    if c.fetchone():
        c.execute('''UPDATE payment_config SET name=?,qr_code=?,account=?,instructions=?,enabled=?,updated_at=?
                     WHERE method=?''',
                  (body.name, body.qr_code, body.account, body.instructions, body.enabled, time.time(), body.method))
    else:
        c.execute('''INSERT INTO payment_config(method,name,qr_code,account,instructions,enabled,updated_at)
                     VALUES(?,?,?,?,?,?,?)''',
                  (body.method, body.name, body.qr_code, body.account, body.instructions, body.enabled, time.time()))
    conn.commit()
    conn.close()
    return {"ok": True, "method": body.method}


@app.post("/api/admin/payment/{method}/upload-qr")
async def admin_upload_qr(method: str, authorization: Optional[str] = Header(None),
                          file: UploadFile = File(...)):
    require_admin(authorization)
    ext = os.path.splitext(file.filename)[1] or ".png"
    filename = f"qr_{method}_{uuid.uuid4().hex[:8]}{ext}"
    filepath = os.path.join(PAYMENT_QR_DIR, filename)
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)
    qr_url = f"/uploads/qr_codes/{filename}"
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE payment_config SET qr_code=?, updated_at=? WHERE method=?",
              (qr_url, time.time(), method))
    conn.commit()
    conn.close()
    return {"ok": True, "qr_code": qr_url}


@app.post("/api/payment/notify")
async def payment_notify(request: Request):
    body = await request.body()
    return JSONResponse(content={"code": "SUCCESS", "message": "received"})


@app.get("/api/tickets")
def list_tickets(authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    conn = get_db()
    c = conn.cursor()
    if user and is_admin(user):
        c.execute("SELECT * FROM tickets ORDER BY created_at DESC")
    elif user:
        c.execute("SELECT * FROM tickets WHERE user_id=? OR user_email=? ORDER BY created_at DESC",
                  (user["id"], user["email"]))
    else:
        c.execute("SELECT * FROM tickets ORDER BY created_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
    return rows


@app.post("/api/tickets")
def create_ticket(body: TicketCreate, authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    tid = "T" + secrets.token_hex(6).upper()
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO tickets(id,title,content,status,created_at,user_id,user_email,reply) VALUES(?,?,?,?,?,?,?,?)",
              (tid, body.title, body.content, "open", int(time.time()),
               user["id"], user["email"], ""))
    conn.commit()
    conn.close()
    return {"id": tid, "status": "open"}


@app.post("/api/redeem")
def redeem_code(body: RedeemCode, authorization: Optional[str] = Header(None)):
    code = body.code.strip().upper()
    reward_map = {"TOKENGO100": 100.0, "NEWUSER10": 10.0, "WELCOME5": 5.0}
    amount = reward_map.get(code)
    if not amount:
        raise HTTPException(status_code=400, detail="无效的兑换码")
    user = get_user_by_session(authorization)
    user_email = user["email"] if user else ""
    user_id = user["id"] if user else ""
    token_id = body.token_id
    if user and not token_id:
        token_id = user.get("token_id") or "demo-master"
    conn = get_db()
    c = conn.cursor()
    if user_email:
        c.execute("SELECT id FROM code_usage WHERE code=? AND user_email=?", (code, user_email))
        if c.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="您已使用过此兑换码")
    c.execute("UPDATE tokens SET quota = quota + ? WHERE id=?", (amount, token_id))
    if user:
        c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user["id"]))
    c.execute('''INSERT INTO redemptions(code,token_id,amount,created_at,status,user_email,user_id,source)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (code, token_id, amount, int(time.time()), "used", user_email, user_id, "code"))
    if user_email:
        c.execute("INSERT OR IGNORE INTO code_usage(code,user_id,user_email,used_at) VALUES(?,?,?,?)",
                  (code, user_id, user_email, int(time.time())))
    conn.commit()
    conn.close()
    return {"ok": True, "added": amount}


@app.post("/api/auth/login")
def api_login(body: UserLogin, request: Request):
    email = body.email.lower().strip()
    password = body.password
    ip = _client_ip(request)

    check_login_rate_limit(ip)
    check_account_lockout(email)

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()

    if not row or not verify_password(password, row["password"]):
        fail_count = record_failed_login(email, ip)
        raise HTTPException(status_code=401, detail=f"邮箱或密码错误（已失败 {fail_count + 1} 次）")

    reset_failed_logins(email)

    session_token = secrets.token_hex(32)
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET session_token=? WHERE id=?", (session_token, row["id"]))
    conn.commit()
    conn.close()

    row_dict = dict(row)
    return {
        "session_token": session_token,
        "email": row_dict["email"],
        "username": row_dict.get("username") or row_dict["email"],
        "role": row_dict.get("role") or "user",
        "token_id": row_dict.get("token_id") or "",
        "balance": row_dict.get("balance") or 0.0,
    }


@app.post("/api/auth/register")
def api_register(body: UserRegister, request: Request):
    email = body.email.lower().strip()
    password = body.password
    username = body.username.strip() or email.split("@")[0]

    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码至少需要8位")

    if body.captcha_token and body.captcha_answer:
        if not verify_captcha(body.captcha_token, body.captcha_answer):
            raise HTTPException(status_code=400, detail="验证码错误")

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email=?", (email,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="该邮箱已被注册")

    uid = "user-" + secrets.token_hex(8)
    invite_code = "TG" + secrets.token_hex(4).upper()
    hashed_pw = hash_password(password)

    c.execute('''INSERT INTO users(id,email,password,username,balance,created_at,role,invite_code)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (uid, email, hashed_pw, username, 1.0, time.time(), "user", invite_code))

    token_id = "sk-tg-" + secrets.token_hex(16)
    c.execute('''INSERT INTO tokens(id,name,quota,used,expire_at,allowed_models,created_at,status)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (token_id, f"{username} 的密钥", 1.0, 0.0, 0, "all", int(time.time()), "active"))
    c.execute("UPDATE users SET token_id=? WHERE id=?", (token_id, uid))

    if body.invite_code:
        c.execute("SELECT id, username FROM users WHERE invite_code=?", (body.invite_code.strip().upper(),))
        inviter = c.fetchone()
        if inviter and inviter["id"] != uid:
            c.execute('''INSERT INTO invites(inviter_id,invitee_id,invitee_email,created_at,status)
                         VALUES(?,?,?,?,?)''',
                      (inviter["id"], uid, email, time.time(), "active"))
            c.execute("UPDATE users SET invited_by=? WHERE id=?", (body.invite_code.strip().upper(), uid))

    session_token = secrets.token_hex(32)
    c.execute("UPDATE users SET session_token=? WHERE id=?", (session_token, uid))
    conn.commit()
    conn.close()

    return {
        "session_token": session_token,
        "email": email,
        "username": username,
        "role": "user",
        "token_id": token_id,
        "balance": 1.0,
        "message": "注册成功！已获得 $1 试用额度",
    }


@app.get("/api/auth/captcha")
def api_captcha():
    return generate_captcha()


@app.get("/api/auth/me")
def api_me(authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user.get("username") or user["email"],
        "role": user.get("role") or "user",
        "token_id": user.get("token_id") or "",
        "balance": user.get("balance") or 0.0,
        "invite_code": user.get("invite_code") or "",
        "total_rebate": user.get("total_rebate") or 0.0,
        "available_rebate": user.get("available_rebate") or 0.0,
    }


@app.post("/api/auth/logout")
def api_logout(authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    if user:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET session_token=NULL WHERE id=?", (user["id"],))
        conn.commit()
        conn.close()
    return {"ok": True, "message": "已登出"}


init_db()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)