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

# 公网访问地址（所有前端代码引用此变量；由 start 脚本自动更新，勿手动硬编码）
PUBLIC_BASE_URL = "https://tokengo.serveo.net"
PUBLIC_URL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_url.txt")

_public_url_cache = {"value": None, "mtime": 0.0}

def get_public_url() -> str:
    """读取当前公网地址（从 public_url.txt，带缓存）。隧道换地址时只需更新该文件，无需重启服务。"""
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
    """自动把 HTML 响应中的 __PUBLIC_BASE_URL__ 占位符替换为当前公网地址。
    这样所有前端页面只需写占位符，隧道换地址时无需改代码。"""
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

# 创建上传目录（付款截图 / 收款码）—— 云端用 /tmp，本地用项目目录
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"))
PAYMENT_PROOF_DIR = os.path.join(UPLOAD_DIR, "proofs")
PAYMENT_QR_DIR = os.path.join(UPLOAD_DIR, "qr_codes")
os.makedirs(PAYMENT_PROOF_DIR, exist_ok=True)
os.makedirs(PAYMENT_QR_DIR, exist_ok=True)

# 挂载静态文件服务（访问 /uploads/xxx.png）
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ============================================================================
# 上游配置（已验证可用）
# ============================================================================
UPSTREAM_API_KEY = "sk-7218e8e5a3e71bbf72f2979f3662d4e20bf355b21f3dea5843d3e1cd197dd469"
UPSTREAM_BASE_URL = "https://wbtssfvj.kdns.fr/v1"

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

# 默认价格（每 1K token，USD）：input / output
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

# ============================================================================
# 数据库
# ============================================================================

def get_db():
    """获取数据库连接。优先用 Turso 云数据库（环境变量配置），否则用本地 SQLite。
    这样本地开发用 SQLite，Render 部署用 Turso，代码完全兼容。"""
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
    """安全地给已有表添加列（已存在则忽略）"""
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
    # 兼容旧数据库：补齐可能缺失的列
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
    # 邀请关系表
    c.execute('''CREATE TABLE IF NOT EXISTS invites
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  inviter_id TEXT NOT NULL, invitee_id TEXT NOT NULL, invitee_email TEXT,
                  created_at REAL, status TEXT DEFAULT 'active')''')
    # 返利流水表
    c.execute('''CREATE TABLE IF NOT EXISTS rebates
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  inviter_id TEXT NOT NULL, invitee_id TEXT, invitee_email TEXT,
                  order_id TEXT, order_amount REAL, rebate_amount REAL,
                  created_at REAL, status TEXT DEFAULT 'available',
                  note TEXT)''')
    # 响应时间记录表（用于真实统计 avg_response_ms）
    c.execute('''CREATE TABLE IF NOT EXISTS response_times
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  token_id TEXT, model TEXT, response_ms INTEGER,
                  created_at INTEGER, user_id TEXT)''')
    # 兑换码使用次数表（防止同一兑换码被同一用户重复使用）
    c.execute('''CREATE TABLE IF NOT EXISTS code_usage
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  code TEXT NOT NULL, user_id TEXT, user_email TEXT,
                  used_at INTEGER, UNIQUE(code, user_email))''')
    # 失败登录记录表（防爆破：账户锁定 + IP 限流）
    c.execute('''CREATE TABLE IF NOT EXISTS failed_logins
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  email TEXT, ip TEXT, failed_at INTEGER,
                  lock_until INTEGER DEFAULT 0)''')
    # 用户状态锁定字段
    _add_column(c, "users", "status", "TEXT DEFAULT 'active'")
    _add_column(c, "users", "locked_until", "INTEGER DEFAULT 0")
    _add_column(c, "users", "failed_count", "INTEGER DEFAULT 0")
    # 订单支付凭证字段（真实支付流程）
    _add_column(c, "orders", "pay_amount_actual", "REAL DEFAULT 0")  # 用户实际支付的唯一金额
    _add_column(c, "orders", "pay_screenshot", "TEXT")  # 付款截图路径
    _add_column(c, "orders", "pay_txid", "TEXT")  # 交易流水号
    _add_column(c, "orders", "pay_note", "TEXT")  # 用户备注
    _add_column(c, "orders", "confirmed_by", "TEXT")  # 确认人
    _add_column(c, "orders", "confirmed_at", "INTEGER DEFAULT 0")  # 确认时间
    _add_column(c, "orders", "unique_suffix", "TEXT")  # 唯一金额尾数（如 .37）
    # 支付方式配置表（收款码等）
    c.execute('''CREATE TABLE IF NOT EXISTS payment_config
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  method TEXT NOT NULL UNIQUE,
                  name TEXT NOT NULL,
                  qr_code TEXT,
                  account TEXT,
                  instructions TEXT,
                  enabled INTEGER DEFAULT 1,
                  updated_at REAL)''')
    # 付款截图上传记录
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
    # 创建管理员账户
    admin_email = "Commecy2014@gmail.com".lower()
    admin_pw = hash_password("admin123456")
    c.execute("SELECT id FROM users WHERE email=?", (admin_email,))
    if not c.fetchone():
        admin_id = "admin-" + secrets.token_hex(8)
        admin_invite = "TGADMIN" + secrets.token_hex(2).upper()
        c.execute('''INSERT INTO users(id,email,password,username,balance,created_at,role,invite_code)
                     VALUES(?,?,?,?,?,?,?,?)''',
                  (admin_id, admin_email, admin_pw, "管理员", 9999.0, time.time(), "admin", admin_invite))
    else:
        c.execute("UPDATE users SET role='admin', password=?, username='管理员', balance=9999.0 WHERE email=?",
                  (admin_pw, admin_email))
    # 为已存在用户补齐 invite_code
    c.execute("SELECT id FROM users WHERE invite_code IS NULL OR invite_code=''")
    for row in c.fetchall():
        uid = row["id"]
        ic = "TG" + secrets.token_hex(4).upper()
        c.execute("UPDATE users SET invite_code=? WHERE id=?", (ic, uid))
    c.execute('''CREATE TABLE IF NOT EXISTS cards
                 (id TEXT PRIMARY KEY, face_value REAL NOT NULL, price REAL NOT NULL,
                  status TEXT DEFAULT 'unused', used_by TEXT, used_at REAL,
                  created_at REAL, batch_id TEXT)''')
    # 种子：默认支付方式（收款码稍后由管理员上传）
    default_payments = [
        ("alipay", "支付宝", "", "", "请使用支付宝扫描二维码付款，付款后点击「我已支付」上传截图", 1),
        ("wechat", "微信支付", "", "", "请使用微信扫描二维码付款，付款后点击「我已支付」上传截图", 1),
        ("usdt_trc20", "USDT (TRC20)", "", "", "请向以下 USDT TRC20 地址转账，转账后点击「我已支付」上传截图", 1),
        ("usdt_erc20", "USDT (ERC20)", "", "", "请向以下 USDT ERC20 地址转账，转账后点击「我已支付」上传截图", 0),
    ]
    for method, name, qr, acct, instr, en in default_payments:
        c.execute("SELECT id FROM payment_config WHERE method=?", (method,))
        if not c.fetchone():
            c.execute('''INSERT INTO payment_config(method,name,qr_code,account,instructions,enabled,updated_at)
                         VALUES(?,?,?,?,?,?,?)''',
                      (method, name, qr, acct, instr, en, time.time()))

    # 种子：渠道
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

    # 种子：价格
    for m, p in DEFAULT_PRICES.items():
        c.execute("INSERT OR REPLACE INTO prices(model_name,price_per_1k_tokens,input_price,output_price,platform)"
                  " VALUES(?,?,?,?,?)",
                  (m, p["input"], p["input"], p["output"], p["platform"]))

    # 种子：演示用 API 密钥
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
    """校验调用方 API Key（sk-tg-... 或上游 demo key）。返回 token 记录。"""
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
    """sha256 哈希密码（加盐：固定站点盐 + 用户密码）"""
    salt = "tokengo!@#2026"
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed


def get_user_by_session(authorization: Optional[str]) -> Optional[Dict[str, Any]]:
    """根据 Authorization: Bearer <session_token> 获取用户。无则返回 None。"""
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
    """要求登录用户，否则抛 401"""
    user = get_user_by_session(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def create_token_internal(name: str, quota: float, expire_days: int = 0,
                          allowed_models: str = "all") -> str:
    """内部函数：直接创建 API 密钥，返回密钥 ID"""
    tid = "sk-tg-" + secrets.token_hex(16)
    expire_at = int((datetime.now() + timedelta(days=expire_days)).timestamp()) if expire_days else 0
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO tokens(id,name,quota,used,expire_at,allowed_models,created_at,status)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (tid, name, quota, 0.0, expire_at, allowed_models, int(time.time()), "active"))
    conn.commit()
    conn.close()
    return tid


def generate_card_id() -> str:
    """生成卡密 ID：TG-XXXX-XXXX-XXXX（X 为大小写字母数字）"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去除易混淆字符 0/O/1/I/L
    def seg():
        return "".join(secrets.choice(alphabet) for _ in range(4))
    return f"TG-{seg()}-{seg()}-{seg()}"


def is_admin(user: Optional[Dict[str, Any]]) -> bool:
    """判断用户是否为管理员（role=admin 或持有 demo-master 主密钥）"""
    return bool(user and (user.get("role") == "admin" or user.get("token_id") == "demo-master"))


def require_admin(authorization: Optional[str]) -> Dict[str, Any]:
    """要求管理员权限：支持 demo-master API Key 或管理员用户会话"""
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")
    token = authorization.replace("Bearer ", "").strip()
    # 1. 直接使用 demo-master 主密钥
    if token == "demo-master":
        return {"id": "demo-master", "email": "admin", "username": "管理员",
                "token_id": "demo-master", "is_master_key": True}
    # 2. 管理员用户会话
    user = require_user(authorization)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def record_usage(token_id: str, model: str, prompt_tokens: int, completion_tokens: int,
                 platform: str, channel_id: str, cost: float = 0.0,
                 response_ms: int = 0, user_id: Optional[str] = None):
    """记录一次 API 调用：写入 usage 表，扣减 token.used 与 users.balance，
    同时写入 response_times 表用于真实统计平均响应时间。"""
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
    # 同步扣减用户余额
    if user_id:
        c.execute("UPDATE users SET balance = MAX(0, balance - ?) WHERE id=?", (cost, user_id))
    elif token_id:
        # 通过 token_id 反查 user_id（兼容旧调用）
        c.execute("SELECT id FROM users WHERE token_id=?", (token_id,))
        u = c.fetchone()
        if u:
            c.execute("UPDATE users SET balance = MAX(0, balance - ?) WHERE id=?", (cost, u["id"]))
            # 回填 usage.user_id
            c.execute("UPDATE usage SET user_id=? WHERE token_id=? AND created_at=?", (u["id"], token_id, now))
    # 写入响应时间表
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
    """发放邀请返利：返利 = 充值金额 × 15%，同时增加邀请人 available_rebate 与 total_rebate。
    返回返利金额。"""
    rebate = round(order_amount_rmb * 0.15, 4)
    if rebate <= 0:
        return 0.0
    c = conn.cursor()
    # 写入返利流水
    c.execute('''INSERT INTO rebates(inviter_id,invitee_id,invitee_email,order_id,
                 order_amount,rebate_amount,created_at,status,note)
                 VALUES(?,?,?,?,?,?,?,?,?)''',
              (inviter_id, invitee_id, invitee_email, order_id,
               order_amount_rmb, rebate, time.time(), "available", f"好友 {invitee_email} 充值返利"))
    # 累计到邀请人账户
    c.execute("UPDATE users SET total_rebate = total_rebate + ?, available_rebate = available_rebate + ? WHERE id=?",
              (rebate, rebate, inviter_id))
    return rebate


def generate_invite_code() -> str:
    """生成唯一邀请码：TG + 8 位大写字母数字"""
    return "TG" + secrets.token_hex(4).upper()


# ============================================================================
# 安全防护：登录限流 / 账户锁定 / CAPTCHA 验证码
# ============================================================================
LOGIN_RATE_LIMIT = 10        # 单 IP 每分钟最多 10 次登录尝试
LOGIN_FAIL_LOCK_THRESHOLD = 5   # 失败 5 次锁定账户
LOGIN_LOCK_DURATION = 900        # 锁定 15 分钟（秒）
IP_BLOCK_THRESHOLD = 20          # 单 IP 累计失败 20 次封禁 1 小时
IP_BLOCK_DURATION = 3600

# CAPTCHA 内存存储：{token: {"answer": "10", "expires": ts}}
_CAPTCHA_STORE: Dict[str, Dict[str, Any]] = {}


def _client_ip(request: Request) -> str:
    """获取客户端真实 IP（支持反向代理）"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_login_rate_limit(ip: str) -> None:
    """检查单 IP 登录频率：每分钟最多 LOGIN_RATE_LIMIT 次"""
    now = int(time.time())
    window_start = now - 60
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM failed_logins WHERE ip=? AND failed_at>=?",
              (ip, window_start))
    count = c.fetchone()["n"]
    # IP 累计失败封禁检查
    c.execute("SELECT lock_until FROM failed_logins WHERE ip=? ORDER BY lock_until DESC LIMIT 1", (ip,))
    row = c.fetchone()
    conn.close()
    if count >= LOGIN_RATE_LIMIT:
        raise HTTPException(status_code=429, detail=f"请求过于频繁，请稍后再试（每分钟限 {LOGIN_RATE_LIMIT} 次）")
    if row and row["lock_until"] and row["lock_until"] > now:
        remain = row["lock_until"] - now
        raise HTTPException(status_code=429, detail=f"该 IP 已被临时封禁，请 {remain} 秒后再试")


def check_account_lockout(email: str) -> None:
    """检查账户是否被锁定"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT failed_count, locked_until, status FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    if not row:
        return  # 账户不存在时不暴露信息，让后续密码校验失败
    if row["status"] == "locked":
        raise HTTPException(status_code=403, detail="账户已被管理员锁定，请联系客服")
    if row["locked_until"] and row["locked_until"] > int(time.time()):
        remain = row["locked_until"] - int(time.time())
        raise HTTPException(status_code=403, detail=f"账户因多次登录失败已被锁定，请 {remain} 秒后再试")


def record_failed_login(email: str, ip: str) -> int:
    """记录一次失败登录：返回当前累计失败次数"""
    now = int(time.time())
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO failed_logins(email,ip,failed_at,lock_until) VALUES(?,?,?,0)",
              (email, ip, now))
    # 累计用户失败次数
    c.execute("UPDATE users SET failed_count = failed_count + 1 WHERE email=?", (email,))
    c.execute("SELECT failed_count FROM users WHERE email=?", (email,))
    row = c.fetchone()
    fail_count = row["failed_count"] if row else 0
    # 达到阈值则锁定账户
    if fail_count >= LOGIN_FAIL_LOCK_THRESHOLD:
        lock_until = now + LOGIN_LOCK_DURATION
        c.execute("UPDATE users SET locked_until=? WHERE email=?", (lock_until, email))
        # 重置计数，下次锁定需再累计 5 次
        c.execute("UPDATE users SET failed_count=0 WHERE email=?", (email,))
    # IP 累计失败封禁
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
    """登录成功后重置失败计数"""
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET failed_count=0, locked_until=0 WHERE email=?", (email,))
    c.execute("DELETE FROM failed_logins WHERE email=?", (email,))
    conn.commit()
    conn.close()


def generate_captcha() -> Dict[str, str]:
    """生成数学题 CAPTCHA：返回 {token, question}"""
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
    # 清理过期 CAPTCHA
    expired = [k for k, v in _CAPTCHA_STORE.items() if v["expires"] < time.time()]
    for k in expired:
        _CAPTCHA_STORE.pop(k, None)
    return {"token": token, "question": f"{a} {op} {b} = ?"}


def verify_captcha(token: str, answer: str) -> bool:
    """验证 CAPTCHA 答案"""
    if not token or not answer:
        return False
    item = _CAPTCHA_STORE.pop(token, None)
    if not item:
        return False
    if item["expires"] < time.time():
        return False
    return item["answer"].strip() == answer.strip()


def require_captcha_for_login(email: str) -> bool:
    """判断该账户是否需要 CAPTCHA（失败 2 次以上需要）"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT failed_count FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    return bool(row and row["failed_count"] >= 2)


# ============================================================================
# Pydantic 模型
# ============================================================================

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
    amount: float = 0.0  # 0 表示全部提现到余额


class PaymentProofSubmit(BaseModel):
    """用户提交付款凭证"""
    txid: str = ""       # 交易流水号（可选）
    note: str = ""       # 备注


class PaymentConfigUpdate(BaseModel):
    """管理员更新支付配置"""
    method: str
    name: str = ""
    qr_code: str = ""
    account: str = ""
    instructions: str = ""
    enabled: int = 1


# ============================================================================
# 页面路由
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return HOME_HTML


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.get("/help", response_class=HTMLResponse)
async def help_page():
    return HELP_HTML


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_HTML


@app.get("/redeem", response_class=HTMLResponse)
async def redeem_page():
    return REDEEM_HTML


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return ADMIN_HTML


# ============================================================================
# OpenAI / Anthropic 兼容 API
# ============================================================================

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

    # 通过 token_id 反查 user_id，便于扣减余额
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

    # 非流式
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
    """Anthropic 协议：转发到上游 /v1/messages"""
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

    # 通过 token_id 反查 user_id
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


# ============================================================================
# 管理 API
# ============================================================================

@app.get("/api/stats")
def api_stats(authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    conn = get_db()
    c = conn.cursor()
    today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    # 已登录用户只看自己的密钥数据；未登录（demo-master/管理员）看全量
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
        recent_q = "SELECT * FROM usage WHERE token_id=? ORDER BY created_at DESC LIMIT 10"
        recent_rows = c.execute(recent_q, (scope_token,)).fetchall()
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

    # 平台拆分
    platform_split = [{"platform": r["platform"] or "未知", "requests": r["n"],
                       "cost": round(r["cost"] or 0, 4), "tokens": r["tok"] or 0} for r in platform_rows]

    # 模型分布
    model_dist = [{"model": r["model"], "requests": r["n"], "tokens": r["tok"] or 0,
                   "cost": round(r["cost"] or 0, 4)} for r in model_rows]

    # 趋势（最近 14 天）
    trend = []
    for i in range(13, -1, -1):
        day = (datetime.now() - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        ds = int(day.timestamp())
        de = ds + 86400
        c.execute(trend_q, trend_params(ds, de))
        trend.append({"date": day.strftime("%m-%d"), "tokens": c.fetchone()["s"]})

    recent = [dict(r) for r in recent_rows]

    # 真实统计平均响应时间（最近 100 次调用）
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
    """创建 API 密钥：登录用户会绑定到其账户；未登录则创建匿名密钥（向后兼容）"""
    user = get_user_by_session(authorization)
    tid = "sk-tg-" + secrets.token_hex(16)
    expire_at = int((datetime.now() + timedelta(days=body.expire_days)).timestamp()) if body.expire_days else 0
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO tokens(id,name,quota,used,expire_at,allowed_models,created_at,status)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (tid, body.name, body.quota, 0.0, expire_at, body.allowed_models, int(time.time()), "active"))
    # 如果用户登录且当前没有 API 密钥，则绑定该密钥；否则只创建额外密钥
    if user and not user.get("token_id"):
        c.execute("UPDATE users SET token_id=? WHERE id=?", (tid, user["id"]))
    conn.commit()
    conn.close()
    return {"id": tid, "name": body.name, "quota": body.quota, "status": "active"}


@app.delete("/api/tokens/{tid}")
def delete_token(tid: str, authorization: Optional[str] = Header(None)):
    """删除密钥：管理员或密钥所有者可删；demo-master 主密钥禁止删除"""
    if tid == "demo-master":
        raise HTTPException(status_code=400, detail="主密钥不可删除")
    user = get_user_by_session(authorization)
    conn = get_db()
    c = conn.cursor()
    # 管理员可删任意密钥；普通用户只能删自己的密钥
    if not (user and is_admin(user)):
        if not user or user.get("token_id") != tid:
            conn.close()
            raise HTTPException(status_code=403, detail="无权删除此密钥")
    c.execute("DELETE FROM tokens WHERE id=?", (tid,))
    # 解绑用户主密钥
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
    # 管理员看全部；普通用户只看自己的使用记录
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
    """订单列表：管理员看全部；普通用户只看自己的订单"""
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
    """创建充值订单：生成唯一金额尾数用于支付匹配，返回收款码信息。
    注意：不再立即到账，需用户付款后提交凭证，管理员确认。"""
    user = require_user(authorization)
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="金额必须大于 0")
    # 验证支付方式是否启用
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM payment_config WHERE method=? AND enabled=1", (body.payment_method,))
    pay_cfg = c.fetchone()
    if not pay_cfg:
        conn.close()
        raise HTTPException(status_code=400, detail=f"支付方式 {body.payment_method} 不可用")
    # 生成唯一金额尾数（0.01-0.99），用于管理员核对付款
    unique_suffix = f".{secrets.randbelow(99) + 1:02d}"
    pay_amount_actual = round(body.amount + float(unique_suffix), 2)
    oid = "TG" + secrets.token_hex(8).upper()
    usd_quota = body.amount * 12  # 1 RMB = 12 USD 额度
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
        "pay_amount_actual": pay_amount_actual,  # 用户需支付的真实金额（含唯一尾数）
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
    """用户提交付款凭证：上传截图 + 交易流水号 + 备注。
    订单状态从 pending → verifying，等待管理员确认。"""
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
        # 保存截图
        ext = os.path.splitext(screenshot.filename)[1] or ".png"
        filename = f"proof_{oid}_{uuid.uuid4().hex[:8]}{ext}"
        filepath = os.path.join(PAYMENT_PROOF_DIR, filename)
        content = await screenshot.read()
        with open(filepath, "wb") as f:
            f.write(content)
        screenshot_path = f"/uploads/proofs/{filename}"
    # 更新订单
    c.execute('''UPDATE orders SET status='verifying', pay_txid=?, pay_note=?,
                 pay_screenshot=?, confirmed_at=0 WHERE id=?''',
              (txid, note, screenshot_path, oid))
    # 写入凭证记录
    c.execute('''INSERT INTO payment_proofs(order_id,user_id,user_email,screenshot_path,txid,note,uploaded_at,status)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (oid, user["id"], user["email"], screenshot_path, txid, note, time.time(), "pending"))
    conn.commit()
    conn.close()
    return {"ok": True, "status": "verifying", "message": "凭证已提交，等待管理员确认到账"}


@app.post("/api/admin/orders/{oid}/confirm")
def admin_confirm_order(oid: str, authorization: Optional[str] = Header(None)):
    """管理员确认订单已收款：加额度 + 触发返利。
    这是真实支付流程的核心——管理员核对收款记录后手动确认。"""
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
    # 查找用户及其 API 密钥
    c.execute("SELECT * FROM users WHERE id=?", (row["user_id"],))
    u_row = c.fetchone()
    if not u_row:
        conn.close()
        raise HTTPException(status_code=400, detail="用户不存在")
    token_id = u_row["token_id"]
    if not token_id:
        token_id = create_token_internal(name=u_row["email"], quota=0.0, expire_days=0, allowed_models="all")
        c.execute("UPDATE users SET token_id=? WHERE id=?", (token_id, u_row["id"]))
    # 加额度
    c.execute("UPDATE tokens SET quota = quota + ? WHERE id=?", (usd_quota, token_id))
    c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (usd_quota, u_row["id"]))
    c.execute('''UPDATE orders SET status='paid', confirmed_by=?, confirmed_at=? WHERE id=?''',
              (admin.get("id", "admin"), int(time.time()), oid))
    # 标记凭证为已确认
    c.execute("UPDATE payment_proofs SET status='confirmed' WHERE order_id=?", (oid,))
    # 触发邀请返利
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
    """管理员拒绝订单（未收到款项）"""
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
    """管理员查看待确认订单列表（用户已提交凭证，等待确认）"""
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


@app.get("/api/payment/config")
def get_payment_config(authorization: Optional[str] = Header(None)):
    """获取已启用的支付方式及收款码（登录用户可见）"""
    user = get_user_by_session(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT method,name,qr_code,account,instructions FROM payment_config WHERE enabled=1")
    methods = [dict(r) for r in c.fetchall()]
    conn.close()
    return {"methods": methods}


@app.post("/api/admin/payment/config")
def admin_update_payment_config(body: PaymentConfigUpdate, authorization: Optional[str] = Header(None)):
    """管理员更新支付方式配置（上传收款码、账户地址等）"""
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
    """管理员上传收款码图片"""
    require_admin(authorization)
    ext = os.path.splitext(file.filename)[1] or ".png"
    filename = f"qr_{method}_{uuid.uuid4().hex[:8]}{ext}"
    filepath = os.path.join(PAYMENT_QR_DIR, filename)
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)
    qr_url = f"/uploads/qr_codes/{filename}"
    # 更新数据库
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE payment_config SET qr_code=?, updated_at=? WHERE method=?",
              (qr_url, time.time(), method))
    conn.commit()
    conn.close()
    return {"ok": True, "qr_code": qr_url}


@app.post("/api/payment/notify")
async def payment_notify(request: Request):
    """支付回调接口（预留）：当未来接入商户 API 时，
    微信/支付宝会异步通知此端点。当前返回成功但不处理。
    接入正式商户后，在此验证签名并自动确认订单。"""
    body = await request.body()
    # TODO: 当获得商户 API 后，在此处：
    # 1. 验证签名
    # 2. 解析订单号和金额
    # 3. 自动调用 admin_confirm_order 确认到账
    return JSONResponse(content={"code": "SUCCESS", "message": "received"})


@app.get("/api/tickets")
def list_tickets(authorization: Optional[str] = Header(None)):
    """工单列表：管理员看全部；普通用户只看自己的工单"""
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
    """创建工单：必须登录；记录 user_id / user_email"""
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
    """兑换码兑换：登录用户则记录到账户；未登录则加到指定 token_id（向后兼容）。
    同一兑换码对同一用户邮箱只能使用一次（防重放）。"""
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
    # 防重放：同一码 + 同一邮箱只能用一次
    if user_email:
        c.execute("SELECT id FROM code_usage WHERE code=? AND user_email=?", (code, user_email))
        if c.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="您已使用过此兑换码")
    # 加额度
    c.execute("UPDATE tokens SET quota = quota + ? WHERE id=?", (amount, token_id))
    if user:
        c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, user["id"]))
    # 写入兑换流水
    c.execute('''INSERT INTO redemptions(code,token_id,amount,created_at,status,user_email,user_id,source)
                 VALUES(?,?,?,?,?,?,?,?)''',
              (code, token_id, amount, int(time.time()), "used", user_email, user_id, "code"))
    # 记录兑换码使用
    if user_email:
        c.execute("INSERT OR IGNORE INTO code_usage(code,user_id,user_email,used_at) VALUES(?,?,?,?)",
                  (code, user_id, user_email, int(time.time())))
    conn.commit()
    conn.close()
    return {"ok": True, "added": amount}


@app.get("/api/redemptions")
def list_redemptions(authorization: Optional[str] = Header(None)):
    """兑换记录：管理员看全部；普通用户只看自己的记录"""
    user = get_user_by_session(authorization)
    conn = get_db()
    c = conn.cursor()
    if user and is_admin(user):
        c.execute("SELECT * FROM redemptions ORDER BY created_at DESC LIMIT 50")
    elif user:
        c.execute("SELECT * FROM redemptions WHERE user_email=? OR user_id=? ORDER BY created_at DESC LIMIT 50",
                  (user["email"], user["id"]))
    else:
        c.execute("SELECT * FROM redemptions ORDER BY created_at DESC LIMIT 50")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
    return rows


# 订阅套餐配置（管理员可调）
SUBSCRIPTION_PLANS = [
    {"name": "日卡", "price": 5, "duration": "1天", "quota": 60, "features": ["全模型访问", "1天有效"]},
    {"name": "周卡", "price": 25, "duration": "7天", "quota": 300, "features": ["全模型访问", "7天有效", "优先客服"]},
    {"name": "月卡", "price": 88, "duration": "30天", "quota": 1200, "features": ["全模型访问", "30天有效", "优先客服", "9折优惠"]},
]


@app.get("/api/subscription")
def subscription(authorization: Optional[str] = Header(None)):
    """订阅信息：从用户最近的订阅订单中读取真实数据"""
    user = get_user_by_session(authorization)
    plan_name = "免费版"
    expire_at = "永久"
    status = "active"
    quota_remain = 0.0
    if user:
        # 查找用户最近一笔订阅订单
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM orders WHERE user_id=? AND type='subscription' AND status='paid' ORDER BY created_at DESC LIMIT 1",
                  (user["id"],))
        sub = c.fetchone()
        if sub:
            plan_name = sub["detail"] or "订阅会员"
            # 根据套餐计算到期时间
            plan_def = next((p for p in SUBSCRIPTION_PLANS if p["name"] == plan_name), None)
            if plan_def:
                days = int(plan_def["duration"].replace("天", "").replace("个月", "30"))
                expire_ts = sub["created_at"] + days * 86400
                if expire_ts < time.time():
                    status = "expired"
                    expire_at = "已过期"
                else:
                    expire_at = datetime.fromtimestamp(expire_ts).strftime("%Y-%m-%d")
            else:
                expire_at = "永久"
        # 当前剩余额度 = token.quota - token.used
        if user.get("token_id"):
            c.execute("SELECT quota, used FROM tokens WHERE id=?", (user["token_id"],))
            t = c.fetchone()
            if t:
                quota_remain = round(t["quota"] - t["used"], 2)
        conn.close()
    return {
        "plan": plan_name,
        "expire_at": expire_at,
        "status": status,
        "quota_remain": quota_remain,
        "plans": SUBSCRIPTION_PLANS,
    }


# ============================================================================
# 卡密系统 API
# ============================================================================

@app.post("/api/admin/generate-cards")
def admin_generate_cards(body: CardGenerate, authorization: Optional[str] = Header(None)):
    """管理员生成卡密：参数 count/face_value/price"""
    require_admin(authorization)
    if body.count < 1 or body.count > 1000:
        raise HTTPException(status_code=400, detail="数量须在 1-1000 之间")
    if body.face_value <= 0:
        raise HTTPException(status_code=400, detail="面值必须大于 0")
    if body.price < 0:
        raise HTTPException(status_code=400, detail="售价不能为负")
    batch_id = "B" + secrets.token_hex(6).upper()
    now = time.time()
    cards = []
    conn = get_db()
    c = conn.cursor()
    for _ in range(body.count):
        # 避免极小概率碰撞
        for _attempt in range(5):
            cid = generate_card_id()
            c.execute("SELECT id FROM cards WHERE id=?", (cid,))
            if not c.fetchone():
                break
        c.execute('''INSERT INTO cards(id,face_value,price,status,used_by,used_at,created_at,batch_id)
                     VALUES(?,?,?,?,?,?,?,?)''',
                  (cid, body.face_value, body.price, "unused", None, None, now, batch_id))
        cards.append({
            "id": cid, "face_value": body.face_value, "price": body.price,
            "status": "unused", "batch_id": batch_id,
        })
    conn.commit()
    conn.close()
    return {"batch_id": batch_id, "count": len(cards), "cards": cards}


@app.get("/api/admin/cards")
def admin_list_cards(authorization: Optional[str] = Header(None),
                     status: Optional[str] = Query(None)):
    """管理员查询所有卡密（支持 status 筛选）"""
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    if status and status in ("unused", "used", "expired"):
        c.execute("SELECT * FROM cards WHERE status=? ORDER BY created_at DESC", (status,))
    else:
        c.execute("SELECT * FROM cards ORDER BY created_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
        r["used_at_str"] = datetime.fromtimestamp(r["used_at"]).strftime("%Y-%m-%d %H:%M") if r["used_at"] else ""
    return rows


@app.get("/api/admin/cards/stats")
def admin_cards_stats(authorization: Optional[str] = Header(None)):
    """卡密统计：各状态数量与总面值"""
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    stats = {}
    for s in ("unused", "used", "expired"):
        c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(face_value),0) AS v FROM cards WHERE status=?", (s,))
        row = c.fetchone()
        stats[s] = {"count": row["n"], "face_value": round(row["v"], 2)}
    c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(face_value),0) AS v FROM cards")
    row = c.fetchone()
    stats["total"] = {"count": row["n"], "face_value": round(row["v"], 2)}
    conn.close()
    return stats


# ============================================================================
# 管理员仪表盘 API：用户管理 / 兑换历史 / 平台统计
# ============================================================================

def _mask_email(email: str) -> str:
    """邮箱脱敏：a***@example.com（数据泄露防护）"""
    if not email or "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return name[0] + "***@" + domain
    return name[:2] + "***@" + domain


@app.get("/api/admin/stats")
def admin_platform_stats(authorization: Optional[str] = Header(None)):
    """平台总览统计：用户数 / 兑换数 / 充值额 / AI 调用数 / 卡密库存"""
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    now = int(time.time())
    today_start = int(time.time()) - (int(time.time()) % 86400)
    # 用户统计
    c.execute("SELECT COUNT(*) AS n FROM users")
    total_users = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) AS n FROM users WHERE created_at>=?", (today_start,))
    today_users = c.fetchone()["n"]
    # 兑换统计（兑换码 + 卡密）
    c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS v FROM redemptions")
    r1 = c.fetchone()
    c.execute("SELECT COUNT(*) AS n FROM cards WHERE status='used'")
    used_cards = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(face_value),0) AS v FROM cards WHERE status='unused'")
    r2 = c.fetchone()
    # 充值统计
    c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS v FROM orders WHERE status='paid'")
    r3 = c.fetchone()
    c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS v FROM orders WHERE status='paid' AND created_at>=?",
              (today_start,))
    r4 = c.fetchone()
    # AI 调用统计
    c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(total_tokens),0) AS t, COALESCE(SUM(cost),0) AS c FROM usage")
    r5 = c.fetchone()
    c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(total_tokens),0) AS t FROM usage WHERE created_at>=?",
              (today_start,))
    r6 = c.fetchone()
    # 工单统计
    c.execute("SELECT COUNT(*) AS n FROM tickets WHERE status='open'")
    open_tickets = c.fetchone()["n"]
    # 平均响应时间
    c.execute("SELECT AVG(response_ms) AS a FROM response_times WHERE response_ms>0")
    avg_ms = c.fetchone()["a"] or 0
    conn.close()
    return {
        "users": {"total": total_users, "today_new": today_users},
        "redemptions": {
            "code_count": r1["n"], "code_amount": round(r1["v"], 2),
            "card_used": used_cards,
            "card_unused_count": r2["n"], "card_unused_value": round(r2["v"], 2),
        },
        "revenue": {
            "paid_orders": r3["n"], "total_revenue_rmb": round(r3["v"], 2),
            "today_orders": r4["n"], "today_revenue_rmb": round(r4["v"], 2),
        },
        "ai": {
            "total_calls": r5["n"], "total_tokens": r5["t"], "total_cost_usd": round(r5["c"], 4),
            "today_calls": r6["n"], "today_tokens": r6["t"],
        },
        "tickets": {"open": open_tickets},
        "avg_response_ms": int(avg_ms),
    }


@app.get("/api/admin/users")
def admin_list_users(authorization: Optional[str] = Header(None),
                     search: str = Query("", description="按邮箱/用户名搜索"),
                     only_redeemers: bool = Query(False, description="只看有兑换记录的用户"),
                     page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=200)):
    """管理员查询所有用户（含兑换统计）。支持搜索与分页。
    敏感字段（邮箱）做脱敏处理，防止批量数据泄露。"""
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    where = []
    params = []
    if search:
        where.append("(email LIKE ? OR username LIKE ? OR id LIKE ?)")
        kw = f"%{search}%"
        params.extend([kw, kw, kw])
    if only_redeemers:
        where.append("EXISTS (SELECT 1 FROM redemptions r WHERE r.user_id=users.id OR r.user_email=users.email)")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    # 总数
    c.execute(f"SELECT COUNT(*) AS n FROM users {where_sql}", params)
    total = c.fetchone()["n"]
    # 分页查询
    offset = (page - 1) * page_size
    c.execute(f'''SELECT id, email, username, balance, created_at, role, status,
                  token_id, invite_code, invited_by, total_rebate, available_rebate,
                  failed_count, locked_until
                  FROM users {where_sql}
                  ORDER BY created_at DESC LIMIT ? OFFSET ?''',
              params + [page_size, offset])
    users = []
    for r in c.fetchall():
        # 该用户的兑换统计
        c.execute('''SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS v
                     FROM redemptions WHERE user_id=? OR user_email=?''',
                  (r["id"], r["email"]))
        rd = c.fetchone()
        # 卡密兑换次数
        c.execute("SELECT COUNT(*) AS n FROM cards WHERE used_by=?", (r["email"],))
        card_count = c.fetchone()["n"]
        # 充值总额
        c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS v FROM orders WHERE user_id=? AND status='paid'",
                  (r["id"],))
        od = c.fetchone()
        # AI 调用数
        c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(cost),0) AS v FROM usage WHERE user_id=?", (r["id"],))
        ug = c.fetchone()
        users.append({
            "id": r["id"],
            "email": _mask_email(r["email"]),
            "email_raw": r["email"],  # 仅管理员可见
            "username": r["username"],
            "balance": round(r["balance"], 4),
            "created_at": r["created_at"],
            "created_at_str": datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M") if r["created_at"] else "",
            "role": r["role"],
            "status": r["status"],
            "token_id": r["token_id"],
            "invite_code": r["invite_code"],
            "invited_by": r["invited_by"],
            "total_rebate": round(r["total_rebate"], 4),
            "available_rebate": round(r["available_rebate"], 4),
            "failed_count": r["failed_count"],
            "is_locked": bool(r["locked_until"] and r["locked_until"] > time.time()),
            "redemption_count": rd["n"],
            "redemption_amount": round(rd["v"], 2),
            "card_redeem_count": card_count,
            "paid_orders": od["n"],
            "total_recharge_rmb": round(od["v"], 2),
            "ai_calls": ug["n"],
            "ai_cost_usd": round(ug["v"], 4),
        })
    conn.close()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "users": users,
    }


@app.get("/api/admin/users/{uid}")
def admin_user_detail(uid: str, authorization: Optional[str] = Header(None)):
    """管理员查看单个用户详情：含完整兑换历史、订单、工单、AI调用记录"""
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=?", (uid,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="用户不存在")
    user = dict(row)
    # 兑换码历史
    c.execute('''SELECT * FROM redemptions WHERE user_id=? OR user_email=?
                 ORDER BY created_at DESC''', (uid, user["email"]))
    redemptions = [dict(r) for r in c.fetchall()]
    for r in redemptions:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S") if r["created_at"] else ""
    # 卡密兑换历史
    c.execute("SELECT * FROM cards WHERE used_by=? ORDER BY used_at DESC", (user["email"],))
    cards = [dict(r) for r in c.fetchall()]
    for r in cards:
        r["used_at_str"] = datetime.fromtimestamp(r["used_at"]).strftime("%Y-%m-%d %H:%M:%S") if r["used_at"] else ""
    # 订单历史
    c.execute("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC", (uid,))
    orders = [dict(r) for r in c.fetchall()]
    for r in orders:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
    # 工单
    c.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY created_at DESC", (uid,))
    tickets = [dict(r) for r in c.fetchall()]
    for r in tickets:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
    # AI 调用记录（最近 50 条）
    c.execute("SELECT * FROM usage WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (uid,))
    usages = [dict(r) for r in c.fetchall()]
    for r in usages:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S") if r["created_at"] else ""
    # 邀请的人
    c.execute('''SELECT u.email, u.username, u.created_at FROM invites i
                 JOIN users u ON i.invitee_id=u.id WHERE i.inviter_id=?''', (uid,))
    invitees = [dict(r) for r in c.fetchall()]
    for r in invitees:
        r["created_at_str"] = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""
        r["email"] = _mask_email(r["email"])
    conn.close()
    user["created_at_str"] = datetime.fromtimestamp(user["created_at"]).strftime("%Y-%m-%d %H:%M:%S") if user["created_at"] else ""
    user["is_locked"] = bool(user.get("locked_until") and user["locked_until"] > time.time())
    return {
        "user": user,
        "redemptions": redemptions,
        "cards": cards,
        "orders": orders,
        "tickets": tickets,
        "usages": usages,
        "invitees": invitees,
    }


@app.post("/api/admin/users/{uid}/lock")
def admin_lock_user(uid: str, authorization: Optional[str] = Header(None)):
    """管理员锁定/解锁用户账户（数据安全：阻断可疑账户）"""
    admin = require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id=?", (uid,))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["role"] == "admin":
        conn.close()
        raise HTTPException(status_code=400, detail="不能锁定管理员账户")
    new_status = "active" if row["status"] == "locked" else "locked"
    c.execute("UPDATE users SET status=?, locked_until=0, failed_count=0 WHERE id=?",
              (new_status, uid))
    conn.commit()
    conn.close()
    return {"ok": True, "new_status": new_status, "uid": uid}


@app.get("/api/admin/redemptions")
def admin_list_redemptions(authorization: Optional[str] = Header(None),
                           page: int = Query(1, ge=1),
                           page_size: int = Query(50, ge=1, le=200),
                           source: str = Query("", description="筛选来源：code/card")):
    """管理员查询全平台兑换记录（兑换码 + 卡密），含用户信息"""
    require_admin(authorization)
    conn = get_db()
    c = conn.cursor()
    # 兑换码记录
    code_rows = []
    if not source or source == "code":
        c.execute('''SELECT r.*, u.username, u.email AS user_email_addr
                     FROM redemptions r LEFT JOIN users u ON r.user_id=u.id
                     ORDER BY r.created_at DESC''')
        for r in c.fetchall():
            code_rows.append({
                "type": "code",
                "id": r["id"],
                "code": r["code"],
                "user_email": _mask_email(r["user_email"] or r["user_email_addr"] or ""),
                "user_email_raw": r["user_email"] or r["user_email_addr"] or "",
                "username": r["username"],
                "amount": r["amount"],
                "status": r["status"],
                "source": r["source"],
                "created_at": r["created_at"],
                "created_at_str": datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S") if r["created_at"] else "",
            })
    # 卡密记录
    card_rows = []
    if not source or source == "card":
        c.execute('''SELECT cd.*, u.username
                     FROM cards cd LEFT JOIN users u ON cd.used_by=u.email
                     WHERE cd.status='used' ORDER BY cd.used_at DESC''')
        for r in c.fetchall():
            card_rows.append({
                "type": "card",
                "id": r["id"],
                "code": r["id"],
                "user_email": _mask_email(r["used_by"] or ""),
                "user_email_raw": r["used_by"] or "",
                "username": r["username"],
                "amount": r["face_value"],
                "status": "used",
                "source": "card",
                "created_at": r["used_at"],
                "created_at_str": datetime.fromtimestamp(r["used_at"]).strftime("%Y-%m-%d %H:%M:%S") if r["used_at"] else "",
            })
    conn.close()
    all_rows = code_rows + card_rows
    all_rows.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    total = len(all_rows)
    offset = (page - 1) * page_size
    paged = all_rows[offset:offset + page_size]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "redemptions": paged,
    }


@app.post("/api/card/redeem")
def card_redeem(body: CardRedeem):
    """卡密兑换：无需登录，凭卡密+邮箱自动注册或加额度"""
    card_id = body.card_id.strip().upper()
    email = body.email.strip().lower()
    if not card_id or not email:
        raise HTTPException(status_code=400, detail="卡密和邮箱不能为空")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    conn = get_db()
    try:
        c = conn.cursor()
        # 1. 验证卡密
        c.execute("SELECT * FROM cards WHERE id=?", (card_id,))
        card = c.fetchone()
        if not card:
            raise HTTPException(status_code=404, detail="卡密不存在")
        card = dict(card)
        if card["status"] == "used":
            raise HTTPException(status_code=400, detail="卡密已被使用")
        if card["status"] == "expired":
            raise HTTPException(status_code=400, detail="卡密已过期")
        face_value = card["face_value"]

        # 2. 查找或创建用户
        c.execute("SELECT * FROM users WHERE email=?", (email,))
        user_row = c.fetchone()
        is_new_user = False
        if user_row:
            user = dict(user_row)
            # 已有用户：直接给其 API 密钥加额度
            c.execute("UPDATE tokens SET quota = quota + ? WHERE id=?", (face_value, user["token_id"]))
            c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (face_value, user["id"]))
            api_key = user["token_id"]
            session_token = user["session_token"]
            username = user["username"]
        else:
            # 新用户：自动注册账户并创建 API 密钥
            is_new_user = True
            uid = "u-" + secrets.token_hex(8)
            username = email.split("@")[0]
            session_token = secrets.token_urlsafe(32)
            # 自动发卡：创建面值等于卡密面值的 API 密钥
            api_key = create_token_internal(name=email, quota=face_value, expire_days=0, allowed_models="all")
            c.execute('''INSERT INTO users(id,email,password,username,balance,created_at,token_id,session_token)
                         VALUES(?,?,?,?,?,?,?,?)''',
                      (uid, email, hash_password(secrets.token_urlsafe(16)), username,
                       face_value, time.time(), api_key, session_token))

        # 3. 标记卡密为已使用
        c.execute("UPDATE cards SET status='used', used_by=?, used_at=? WHERE id=?",
                  (email, time.time(), card_id))

        # 4. 记录兑换流水（复用 redemptions 表）
        c.execute("INSERT INTO redemptions(code,token_id,amount,created_at,status) VALUES(?,?,?,?,?)",
                  (card_id, api_key, face_value, int(time.time()), "used"))

        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "is_new_user": is_new_user,
        "card_id": card_id,
        "face_value": face_value,
        "price": card["price"],
        "email": email,
        "username": username,
        "api_key": api_key,
        "session_token": session_token,
        "balance": face_value,
        "api_base": PUBLIC_BASE_URL.rstrip("/") + "/v1",
        "help_url": PUBLIC_BASE_URL.rstrip("/") + "/help",
    }


# ============================================================================
# 用户认证 API（注册 / 登录 / 当前用户）
# ============================================================================

TRIAL_QUOTA = 1.0  # 新用户赠送 $1 试用额度


@app.post("/api/auth/register")
def auth_register(body: UserRegister, request: Request):
    """用户注册：可选邀请码；强制 CAPTCHA 防机器注册；IP 限流"""
    email = body.email.strip().lower()
    if not email or not body.password:
        raise HTTPException(status_code=400, detail="邮箱和密码不能为空")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    # IP 限流：每分钟最多 3 次注册
    ip = _client_ip(request)
    check_login_rate_limit(ip)
    # CAPTCHA 校验（强制）
    if not verify_captcha(body.captcha_token, body.captcha_answer):
        cap = generate_captcha()
        raise HTTPException(status_code=400,
                            detail=f"请完成验证码：{cap['question']}",
                            headers={"X-Captcha-Token": cap["token"],
                                     "X-Captcha-Question": cap["question"],
                                     "X-Need-Captcha": "1"})
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email=?", (email,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="该邮箱已注册")
    # 处理邀请码
    invite_code = (body.invite_code or "").strip().upper()
    invited_by = ""
    inviter_id = None
    if invite_code:
        c.execute("SELECT id FROM users WHERE invite_code=?", (invite_code,))
        inviter = c.fetchone()
        if not inviter:
            conn.close()
            raise HTTPException(status_code=400, detail="邀请码无效")
        inviter_id = inviter["id"]
        invited_by = invite_code
    uid = "u-" + secrets.token_hex(8)
    username = body.username.strip() or email.split("@")[0]
    session_token = secrets.token_urlsafe(32)
    new_invite_code = generate_invite_code()
    # 自动发卡：创建一个 $1 试用 API 密钥
    api_key = create_token_internal(name=email, quota=TRIAL_QUOTA, expire_days=0, allowed_models="all")
    c.execute('''INSERT INTO users(id,email,password,username,balance,created_at,token_id,session_token,
                 invite_code,invited_by,total_rebate,available_rebate)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
              (uid, email, hash_password(body.password), username, TRIAL_QUOTA,
               time.time(), api_key, session_token, new_invite_code, invited_by, 0.0, 0.0))
    # 写入邀请关系表
    if inviter_id:
        c.execute('''INSERT INTO invites(inviter_id,invitee_id,invitee_email,created_at,status)
                     VALUES(?,?,?,?,?)''',
                  (inviter_id, uid, email, time.time(), "active"))
    conn.commit()
    conn.close()
    return {
        "id": uid,
        "email": email,
        "username": username,
        "balance": TRIAL_QUOTA,
        "api_key": api_key,
        "session_token": session_token,
        "invite_code": new_invite_code,
        "invited_by": invited_by,
    }


@app.post("/api/auth/login")
def auth_login(body: UserLogin, request: Request):
    email = body.email.strip().lower()
    if not email or not body.password:
        raise HTTPException(status_code=400, detail="邮箱和密码不能为空")
    ip = _client_ip(request)
    # 1. IP 限流
    check_login_rate_limit(ip)
    # 2. 账户锁定检查
    check_account_lockout(email)
    # 3. CAPTCHA 校验（失败 2 次后强制要求）
    need_captcha = require_captcha_for_login(email)
    if need_captcha:
        if not verify_captcha(body.captcha_token, body.captcha_answer):
            cap = generate_captcha()
            raise HTTPException(status_code=400,
                                detail=f"请完成验证码：{cap['question']}",
                                headers={"X-Captcha-Token": cap["token"],
                                         "X-Captcha-Question": cap["question"],
                                         "X-Need-Captcha": "1"})
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email=?", (email,))
    row = c.fetchone()
    if not row or not verify_password(body.password, row["password"]):
        conn.close()
        fail_count = record_failed_login(email, ip)
        # 失败 2 次起返回 CAPTCHA
        if fail_count >= 2:
            cap = generate_captcha()
            raise HTTPException(status_code=401,
                                detail=f"邮箱或密码错误（已失败 {fail_count} 次，5 次将锁定账户 15 分钟）。请完成验证码：{cap['question']}",
                                headers={"X-Captcha-Token": cap["token"],
                                         "X-Captcha-Question": cap["question"],
                                         "X-Need-Captcha": "1"})
        raise HTTPException(status_code=401,
                            detail=f"邮箱或密码错误（已失败 {fail_count} 次，5 次将锁定账户 15 分钟）")
    # 登录成功：重置失败计数
    reset_failed_logins(email)
    session_token = secrets.token_urlsafe(32)
    c.execute("UPDATE users SET session_token=? WHERE id=?", (session_token, row["id"]))
    conn.commit()
    conn.close()
    return {
        "id": row["id"],
        "email": row["email"],
        "username": row["username"],
        "balance": row["balance"],
        "api_key": row["token_id"],
        "session_token": session_token,
        "role": row["role"] if "role" in row.keys() else "user",
        "is_admin": is_admin(dict(row)),
        "invite_code": row["invite_code"] if "invite_code" in row.keys() else "",
    }


@app.get("/api/captcha")
def api_captcha():
    """获取 CAPTCHA 验证码（数学题）"""
    return generate_captcha()


@app.post("/api/captcha/verify")
def api_captcha_verify(token: str = "", answer: str = ""):
    """验证 CAPTCHA（用于前端预校验）"""
    ok = verify_captcha(token, answer)
    return {"ok": ok}


@app.get("/api/auth/me")
def auth_me(authorization: Optional[str] = Header(None)):
    user = require_user(authorization)
    # 统计邀请人数
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) AS n FROM invites WHERE inviter_id=?", (user["id"],))
    invitee_count = c.fetchone()["n"]
    c.execute("SELECT COALESCE(SUM(rebate_amount),0) AS s FROM rebates WHERE inviter_id=?", (user["id"],))
    total_rebate = c.fetchone()["s"]
    c.execute("SELECT COALESCE(SUM(rebate_amount),0) AS s FROM rebates WHERE inviter_id=? AND status='available'",
              (user["id"],))
    available_rebate = c.fetchone()["s"]
    conn.close()
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user["username"],
        "balance": user["balance"],
        "api_key": user["token_id"],
        "created_at": user["created_at"],
        "role": user.get("role", "user"),
        "is_admin": is_admin(user),
        "invite_code": user.get("invite_code", ""),
        "invited_by": user.get("invited_by", ""),
        "invitee_count": invitee_count,
        "total_rebate": round(total_rebate, 4),
        "available_rebate": round(available_rebate, 4),
    }


@app.put("/api/auth/profile")
def update_profile(body: ProfileUpdate, authorization: Optional[str] = Header(None)):
    """更新个人资料：用户名 / 邮箱 / 密码"""
    user = require_user(authorization)
    conn = get_db()
    c = conn.cursor()
    updates = []
    params = []
    if body.username.strip():
        updates.append("username=?")
        params.append(body.username.strip())
    if body.email.strip() and body.email.strip().lower() != user["email"]:
        new_email = body.email.strip().lower()
        c.execute("SELECT id FROM users WHERE email=?", (new_email,))
        if c.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="该邮箱已被使用")
        updates.append("email=?")
        params.append(new_email)
    if body.password:
        updates.append("password=?")
        params.append(hash_password(body.password))
    if updates:
        params.append(user["id"])
        c.execute(f"UPDATE users SET {','.join(updates)} WHERE id=?", params)
        conn.commit()
    conn.close()
    return {"ok": True, "updated_fields": len(updates)}


@app.get("/api/referral")
def get_referral(authorization: Optional[str] = Header(None)):
    """邀请返利信息：邀请链接、累计返利、可提现返利、邀请人数、返利流水"""
    user = require_user(authorization)
    conn = get_db()
    c = conn.cursor()
    # 邀请人数
    c.execute("SELECT COUNT(*) AS n FROM invites WHERE inviter_id=?", (user["id"],))
    invitee_count = c.fetchone()["n"]
    # 累计返利 / 可提现返利
    c.execute("SELECT COALESCE(SUM(rebate_amount),0) AS s FROM rebates WHERE inviter_id=?", (user["id"],))
    total_rebate = c.fetchone()["s"]
    c.execute("SELECT COALESCE(SUM(rebate_amount),0) AS s FROM rebates WHERE inviter_id=? AND status='available'",
              (user["id"],))
    available_rebate = c.fetchone()["s"]
    # 返利流水（最近 50 条）
    c.execute('''SELECT r.*, u.email AS invitee_email_addr
                 FROM rebates r LEFT JOIN users u ON r.invitee_id = u.id
                 WHERE r.inviter_id=? ORDER BY r.created_at DESC LIMIT 50''', (user["id"],))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    rebate_list = []
    for r in rows:
        rebate_list.append({
            "invitee": r["invitee_email"] or r.get("invitee_email_addr") or "用户",
            "order_amount": r["order_amount"],
            "rebate_amount": r["rebate_amount"],
            "status": r["status"],
            "created_at_str": datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M"),
        })
    invite_link = f"{PUBLIC_BASE_URL.rstrip('/')}/login?ref={user.get('invite_code','')}"
    return {
        "invite_code": user.get("invite_code", ""),
        "invite_link": invite_link,
        "invitee_count": invitee_count,
        "total_rebate": round(total_rebate, 4),
        "available_rebate": round(available_rebate, 4),
        "rebate_rate": 0.15,
        "rebate_list": rebate_list,
    }


@app.post("/api/referral/withdraw")
def withdraw_rebate(body: RebateWithdraw, authorization: Optional[str] = Header(None)):
    """返利提现到余额：amount=0 表示全部提现"""
    user = require_user(authorization)
    conn = get_db()
    c = conn.cursor()
    # 取所有可提现返利记录（按时间顺序）
    c.execute('''SELECT id, rebate_amount FROM rebates
                 WHERE inviter_id=? AND status='available'
                 ORDER BY created_at ASC''', (user["id"],))
    rows = c.fetchall()
    available = round(sum(r["rebate_amount"] for r in rows), 4)
    target = body.amount if body.amount > 0 else available
    if available <= 0:
        conn.close()
        raise HTTPException(status_code=400, detail="可提现返利为 0")
    if target > available:
        conn.close()
        raise HTTPException(status_code=400, detail="提现金额超过可提现返利")
    # 按顺序标记记录为 withdrawn，累计达到目标金额
    withdrawn_ids = []
    cumulative = 0.0
    for r in rows:
        if cumulative >= target:
            break
        withdrawn_ids.append(r["id"])
        cumulative += r["rebate_amount"]
    actual_withdrawn = round(cumulative, 4)
    if withdrawn_ids:
        placeholders = ",".join("?" * len(withdrawn_ids))
        c.execute(f"UPDATE rebates SET status='withdrawn' WHERE id IN ({placeholders})",
                  withdrawn_ids)
    # 加到用户余额和 API 密钥额度
    if user.get("token_id"):
        c.execute("UPDATE tokens SET quota = quota + ? WHERE id=?", (actual_withdrawn, user["token_id"]))
    c.execute("UPDATE users SET balance = balance + ? WHERE id=?", (actual_withdrawn, user["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "withdrawn": actual_withdrawn}


@app.post("/api/auth/logout")
def auth_logout(authorization: Optional[str] = Header(None)):
    user = get_user_by_session(authorization)
    if user:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET session_token=NULL WHERE id=?", (user["id"],))
        conn.commit()
        conn.close()
    return {"ok": True}


# ============================================================================
# HTML：首页
# ============================================================================

HOME_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TokenGo - 一个密钥，畅用多个 AI 模型</title>
<style>
:root{
  --primary:#14b8a6; --primary-dark:#0d9488; --primary-light:#5eead4; --primary-50:#f0fdfa;
  --accent:#f59e0b; --accent-dark:#d97706;
  --bg:#ffffff; --bg-soft:#f8fafc; --card:#ffffff; --text:#0f172a; --text-soft:#64748b;
  --border:#e2e8f0; --shadow:0 10px 40px rgba(0,0,0,.08); --shadow-sm:0 1px 3px rgba(0,0,0,.04),0 1px 2px rgba(0,0,0,.06);
  --shadow-card:0 1px 3px rgba(0,0,0,.04),0 1px 2px rgba(0,0,0,.06);
  --alipay:#00aeef; --wxpay:#2bb741; --stripe:#635bff; --usdt:#26a17b;
  --danger:#ef4444; --danger-dark:#dc2626; --success:#10b981; --success-dark:#059669;
}
[data-theme="dark"]{
  --primary:#2dd4bf; --primary-dark:#14b8a6; --primary-light:#5eead4; --primary-50:rgba(20,184,166,.1);
  --accent:#fbbf24; --accent-dark:#f59e0b;
  --bg:#0f172a; --bg-soft:#1e293b; --card:#1e293b; --text:#e2e8f0; --text-soft:#94a3b8;
  --border:#334155; --shadow:0 10px 40px rgba(0,0,0,.4); --shadow-sm:0 1px 3px rgba(0,0,0,.2);
  --shadow-card:0 1px 3px rgba(0,0,0,.2);
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--text);line-height:1.6;transition:background .3s,color .3s}
a{color:inherit;text-decoration:none}
.container{max-width:1200px;margin:0 auto;padding:0 24px}
.announce{background:linear-gradient(90deg,var(--accent),#fb923c);color:#fff;text-align:center;
  padding:8px 16px;font-size:14px;font-weight:600;letter-spacing:.5px}
.nav{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.85);
  backdrop-filter:blur(12px);border-bottom:1px solid var(--border)}
[data-theme="dark"] .nav{background:rgba(15,23,42,.85)}
.nav-inner{display:flex;align-items:center;justify-content:space-between;height:64px}
.logo{font-size:22px;font-weight:800;color:var(--primary);display:flex;align-items:center;gap:8px}
.nav-links{display:flex;align-items:center;gap:20px}
.nav-links a{font-size:14px;color:var(--text-soft);transition:color .2s}
.nav-links a:hover{color:var(--primary)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;padding:.625rem 1rem;
  border-radius:.75rem;font-size:.875rem;font-weight:500;cursor:pointer;border:none;
  transition:all .2s cubic-bezier(.4,0,.2,1);line-height:1.25rem}
.btn-primary{background:linear-gradient(to right,#14b8a6,#0d9488);color:#fff;
  box-shadow:0 4px 6px -1px rgba(0,0,0,.1),0 2px 4px -2px rgba(0,0,0,.1),0 0 20px rgba(20,184,166,.25)}
.btn-primary:hover{background:linear-gradient(to right,#0d9488,#0f766e);transform:translateY(-1px);
  box-shadow:0 6px 12px -2px rgba(20,184,166,.35)}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-outline:hover{border-color:var(--primary);color:var(--primary);background:var(--primary-50)}
.btn-accent{background:linear-gradient(to right,#f59e0b,#d97706);color:#fff;
  box-shadow:0 4px 6px -1px rgba(245,158,11,.2)}
.btn-accent:hover{background:linear-gradient(to right,#d97706,#b45309);transform:translateY(-1px)}
.btn-success{background:linear-gradient(to right,#10b981,#059669);color:#fff;
  box-shadow:0 4px 6px -1px rgba(16,185,129,.25)}
.btn-success:hover{transform:translateY(-1px)}
.btn-alipay{background:var(--alipay);color:#fff;box-shadow:0 4px 6px -1px rgba(0,174,239,.25)}
.btn-wxpay{background:var(--wxpay);color:#fff;box-shadow:0 4px 6px -1px rgba(43,183,65,.25)}
.btn-stripe{background:var(--stripe);color:#fff;box-shadow:0 4px 6px -1px rgba(99,91,255,.25)}
.btn-danger{background:linear-gradient(to right,#ef4444,#dc2626);color:#fff;
  box-shadow:0 4px 6px -1px rgba(239,68,68,.25)}
.theme-toggle{background:transparent;border:1px solid var(--border);width:38px;height:38px;border-radius:10px;
  cursor:pointer;font-size:16px;color:var(--text)}
.hero{padding:80px 0 60px;text-align:center;position:relative;overflow:hidden}
.hero::before{content:"";position:absolute;inset:0;background:radial-gradient(circle at 30% 20%,rgba(20,184,166,.15),transparent 50%),
  radial-gradient(circle at 70% 80%,rgba(245,158,11,.12),transparent 50%);z-index:-1}
.hero h1{font-size:48px;font-weight:800;letter-spacing:-1px;margin-bottom:18px;line-height:1.2}
.gradient-text{background:linear-gradient(90deg,#14b8a6,#0d9488,#f59e0b);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{font-size:18px;color:var(--text-soft);max-width:640px;margin:0 auto 32px}
.hero-actions{display:flex;gap:14px;justify-content:center;flex-wrap:wrap}
.btn-lg{padding:.75rem 1.75rem;font-size:16px}
.hero-badges{display:flex;gap:12px;justify-content:center;margin-top:24px;flex-wrap:wrap}
.hero-badge{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--text-soft);
  background:var(--card);padding:6px 14px;border-radius:999px;border:1px solid var(--border);
  box-shadow:var(--shadow-sm)}
.hero-badge .dot{width:6px;height:6px;border-radius:50%;background:var(--success)}
.payments{display:flex;gap:14px;justify-content:center;margin-top:40px;flex-wrap:wrap;align-items:center}
.payments span{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text-soft);
  background:var(--card);padding:8px 14px;border-radius:.75rem;border:1px solid var(--border);
  box-shadow:var(--shadow-sm);transition:all .2s}
.payments span:hover{transform:translateY(-2px);border-color:var(--primary)}
.section{padding:70px 0}
.section-title{text-align:center;font-size:32px;font-weight:800;margin-bottom:12px}
.section-sub{text-align:center;color:var(--text-soft);margin-bottom:40px}
.model-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:20px}
.model-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px;
  transition:all .3s;box-shadow:var(--shadow-card)}
.model-card:hover{transform:translateY(-4px);border-color:var(--primary);box-shadow:0 10px 40px rgba(20,184,166,.15)}
.model-icon{width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;
  font-size:24px;margin-bottom:14px}
.model-card h3{font-size:18px;margin-bottom:6px}
.model-card p{font-size:13px;color:var(--text-soft)}
.model-badge{display:inline-block;font-size:11px;padding:3px 8px;border-radius:6px;
  background:rgba(20,184,166,.12);color:var(--primary);margin-top:10px;font-weight:600}
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:28px}
.feature{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:32px;text-align:center;
  box-shadow:var(--shadow-card);transition:all .3s}
.feature:hover{transform:translateY(-4px);box-shadow:0 10px 40px rgba(0,0,0,.08)}
.feature-icon{width:64px;height:64px;border-radius:16px;margin:0 auto 18px;display:flex;align-items:center;
  justify-content:center;font-size:30px;background:linear-gradient(135deg,#14b8a6,#0d9488);color:#fff;
  box-shadow:0 4px 12px rgba(20,184,166,.3)}
.feature h3{font-size:20px;margin-bottom:10px}
.feature p{color:var(--text-soft);font-size:14px}
footer{background:var(--bg-soft);border-top:1px solid var(--border);padding:32px 0;text-align:center;
  color:var(--text-soft);font-size:14px}
@media(max-width:768px){.hero h1{font-size:32px}.hero p{font-size:15px}.nav-links a:not(.btn){display:none}}
</style>
</head>
<body>
<div class="announce">🔥 限时优惠 ¥1 RMB = $12 USD 美金额度</div>
<nav class="nav">
  <div class="container nav-inner">
    <a href="/" class="logo">🚀 TokenGo</a>
    <div class="nav-links">
      <a href="/help">使用教程</a>
      <a href="/redeem" class="btn btn-accent">🎫 卡密兑换</a>
      <span id="navUserArea">
        <a href="/login" class="btn btn-outline">登录</a>
        <a href="/login?mode=register" class="btn btn-primary">注册</a>
      </span>
      <a href="/dashboard" class="btn btn-outline">控制台</a>
      <a href="#" class="btn btn-accent" onclick="contactSupport(event)">联系客服</a>
    </div>
  </div>
</nav>

<section class="hero">
  <div class="container">
    <h1>一个密钥，畅用 <span class="gradient-text">多个 AI 模型</span></h1>
    <p>无需管理多个订阅账号，一站式接入 Claude、GPT、Gemini 等主流 AI 服务</p>
    <div class="hero-actions">
      <a href="/login?mode=register" class="btn btn-primary btn-lg">🚀 免费注册</a>
      <a href="/dashboard" class="btn btn-outline btn-lg">📖 进入控制台</a>
    </div>
    <div class="hero-badges">
      <span class="hero-badge"><span class="dot"></span>🎁 注册即送 $1 试用额度，无需信用卡</span>
      <span class="hero-badge"><span class="dot"></span>订阅 API</span>
      <span class="hero-badge"><span class="dot"></span>会话持久化</span>
      <span class="hero-badge"><span class="dot"></span>按量付费</span>
    </div>
    <div class="payments">
      <span>💚 支付宝</span>
      <span>💙 微信支付</span>
      <span>💳 Stripe</span>
      <span>₮ USDT (TRC20 / ERC20)</span>
      <span>🪙 USDC (ERC20)</span>
      <span>🅿️ PayPal</span>
    </div>
  </div>
</section>

<section class="section">
  <div class="container">
    <h2 class="section-title">支持的主流 AI 模型</h2>
    <p class="section-sub">一个 API Key 即可调用以下全部模型，开箱即用</p>
    <div class="model-grid" id="modelGrid"></div>
  </div>
</section>

<section class="section" style="background:var(--bg-soft)">
  <div class="container">
    <h2 class="section-title">为什么选择 TokenGo</h2>
    <p class="section-sub">专业、稳定、高性价比的 AI API 中转服务</p>
    <div class="features">
      <div class="feature">
        <div class="feature-icon">🔑</div>
        <h3>一键接入</h3>
        <p>一个 API Key 即可调用所有已接入的 AI 模型，无需单独申请多个账号，开箱即用。</p>
      </div>
      <div class="feature">
        <div class="feature-icon">🛡️</div>
        <h3>稳定可靠</h3>
        <p>多上游账号智能路由 + 自动故障转移，告别报错与超时，让您的应用始终在线。</p>
      </div>
      <div class="feature">
        <div class="feature-icon">💎</div>
        <h3>按量付费</h3>
        <p>基于用量的计费与额度上限，团队消耗全透明。¥1 RMB = $12 USD 额度，远低于官方价格。</p>
      </div>
    </div>
  </div>
</section>

<section class="section">
  <div class="container">
    <h2 class="section-title">🎫 卡密兑换 · 即买即用</h2>
    <p class="section-sub">购买卡密后无需注册，输入卡密和邮箱即可领取 API 密钥与额度</p>
    <div class="redeem-banner" style="background:linear-gradient(135deg,#14b8a6,#0d9488);border-radius:20px;
         padding:40px;color:#fff;display:grid;grid-template-columns:1fr 1fr;gap:32px;align-items:center;
         box-shadow:0 20px 40px rgba(20,184,166,.25)">
      <div>
        <h3 style="font-size:26px;margin-bottom:12px">输入卡密，立即获取 API 密钥</h3>
        <p style="font-size:15px;opacity:.92;margin-bottom:20px">
          卡密格式：<b>TG-XXXX-XXXX-XXXX</b><br>
          面值 $12 / $60 / $120 任选，售价低至 ¥1/$12 额度
        </p>
        <a href="/redeem" class="btn btn-accent btn-lg" style="background:#f59e0b;color:#fff">
          🚀 前往卡密兑换
        </a>
      </div>
      <div style="background:rgba(255,255,255,.12);border-radius:14px;padding:24px;backdrop-filter:blur(8px)">
        <div style="font-size:13px;opacity:.85;margin-bottom:8px">卡密示例</div>
        <div style="font-family:Consolas,monospace;font-size:20px;font-weight:700;letter-spacing:1px;
             background:rgba(0,0,0,.2);padding:14px 18px;border-radius:10px;border:1px dashed rgba(255,255,255,.4)">
          TG-A1B2-C3D4-E5F6
        </div>
        <div style="margin-top:14px;font-size:13px;opacity:.9">
          ✅ 无需注册，邮箱即可领取<br>
          ✅ 自动开通 API 密钥<br>
          ✅ 即时到账，立即可用
        </div>
      </div>
    </div>
  </div>
</section>

<footer>
  <div class="container">© 2026 TokenGo. 保留所有权利。</div>
</footer>

<div id="supportModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);
  z-index:100;align-items:center;justify-content:center">
  <div style="background:var(--card);border-radius:16px;padding:32px;max-width:380px;width:90%;text-align:center">
    <h3 style="margin-bottom:16px">联系客服</h3>
    <p style="color:var(--text-soft);margin-bottom:8px">邮箱：Commecy2014@gmail.com</p>
    <button class="btn btn-primary" onclick="document.getElementById('supportModal').style.display='none'">关闭</button>
  </div>
</div>

<script>
const MODELS=[
  {n:'GPT-5.6',d:'最强推理能力',i:'🧠',c:'#10a37f',b:'推荐'},
  {n:'GPT-5.5',d:'均衡高性能',i:'⚡',c:'#10a37f',b:''},
  {n:'GPT-5.4',d:'快速响应',i:'🚀',c:'#10a37f',b:''},
  {n:'GPT-5.4-mini',d:'轻量低价',i:'💨',c:'#10a37f',b:'低价'},
  {n:'GPT-5.2',d:'稳定可靠',i:'✨',c:'#10a37f',b:''},
  {n:'Codex',d:'代码专精',i:'💻',c:'#6e56cf',b:'代码'},
  {n:'GPT-Image-1',d:'AI 图像生成',i:'🎨',c:'#ff6b6b',b:'图像'},
  {n:'GPT-Image-2',d:'高清图像',i:'🖼️',c:'#ff6b6b',b:'新'},
  {n:'Claude Sonnet 4.6',d:'长文创作之王',i:'🎭',c:'#d97757',b:'Claude'},
];
const grid=document.getElementById('modelGrid');
MODELS.forEach(m=>{
  const el=document.createElement('div');
  el.className='model-card';
  el.innerHTML=`<div class="model-icon" style="background:${m.c}22;color:${m.c}">${m.i}</div>
    <h3>${m.n}</h3><p>${m.d}</p>${m.b?`<span class="model-badge">${m.b}</span>`:''}`;
  grid.appendChild(el);
});
function contactSupport(e){e.preventDefault();document.getElementById('supportModal').style.display='flex';}
// 动态渲染登录态
async function renderNavUser(){
  const area=document.getElementById('navUserArea');
  if(!area)return;
  const token=localStorage.getItem('tg_session');
  if(!token){area.innerHTML='<a href="/login" class="btn btn-outline">登录</a><a href="/login?mode=register" class="btn btn-primary">注册</a>';return;}
  try{
    const r=await fetch('/api/auth/me',{headers:{'Authorization':'Bearer '+token}});
    if(!r.ok){localStorage.removeItem('tg_session');area.innerHTML='<a href="/login" class="btn btn-outline">登录</a><a href="/login?mode=register" class="btn btn-primary">注册</a>';return;}
    const u=await r.json();
    area.innerHTML=`<span style="font-size:14px;color:var(--text-soft)">👋 ${u.username||u.email}</span><a href="/dashboard" class="btn btn-primary">控制台</a><button class="btn btn-outline" onclick="logout()">退出</button>`;
  }catch(e){area.innerHTML='<a href="/login" class="btn btn-outline">登录</a><a href="/login?mode=register" class="btn btn-primary">注册</a>';}
}
async function logout(){
  const token=localStorage.getItem('tg_session');
  if(token){try{await fetch('/api/auth/logout',{method:'POST',headers:{'Authorization':'Bearer '+token}});}catch(e){}}
  localStorage.removeItem('tg_session');
  location.href='/';
}
renderNavUser();
</script>
</body>
</html>"""


# ============================================================================
# HTML：登录/注册页面
# ============================================================================

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 / 注册 - TokenGo</title>
<style>
:root{
  --primary:#14b8a6; --primary-dark:#0d9488; --accent:#f59e0b; --accent-dark:#d97706;
  --bg:#ffffff; --bg-soft:#f8fafc; --card:#ffffff; --text:#0f172a; --text-soft:#64748b;
  --border:#e2e8f0; --shadow:0 10px 30px rgba(2,6,23,.08); --danger:#ef4444; --success:#10b981;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:linear-gradient(135deg,#f0f9ff 0%,#faf5ff 100%);color:var(--text);min-height:100vh;
  display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px}
a{color:var(--primary);text-decoration:none}
a:hover{text-decoration:underline}
.brand{font-size:32px;font-weight:800;color:var(--primary);display:flex;align-items:center;gap:10px;margin-bottom:8px;text-decoration:none}
.brand:hover{text-decoration:none}
.subtitle{color:var(--text-soft);text-align:center;margin-bottom:28px;font-size:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:36px 32px;
  width:100%;max-width:420px;box-shadow:var(--shadow)}
.tabs{display:flex;background:var(--bg-soft);border-radius:.75rem;padding:4px;margin-bottom:24px}
.tab{flex:1;text-align:center;padding:10px;border-radius:.5rem;cursor:pointer;font-size:14px;font-weight:600;
  color:var(--text-soft);transition:all .2s;border:none;background:transparent}
.tab.active{background:var(--card);color:var(--primary);box-shadow:0 2px 6px rgba(2,6,23,.08)}
.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;color:var(--text-soft)}
.form-group input{width:100%;padding:.625rem 1rem;border:1px solid var(--border);border-radius:.75rem;
  background:var(--card);color:var(--text);font-size:.875rem;font-family:inherit;transition:border-color .2s,box-shadow .2s}
.form-group input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(20,184,166,.15)}
.btn{width:100%;padding:.75rem 1rem;border-radius:.75rem;font-size:.9375rem;font-weight:600;cursor:pointer;
  border:none;transition:all .2s;background:linear-gradient(to right,#14b8a6,#0d9488);color:#fff;margin-top:4px;
  box-shadow:0 4px 6px -1px rgba(20,184,166,.25)}
.btn:hover{background:linear-gradient(to right,#0d9488,#0f766e);transform:translateY(-1px);
  box-shadow:0 6px 12px -2px rgba(20,184,166,.35)}
.btn:disabled{opacity:.6;cursor:not-allowed;transform:none}
.hint{font-size:12px;color:var(--text-soft);margin-top:6px}
.error-msg{background:rgba(239,68,68,.1);color:var(--danger);padding:10px 12px;border-radius:8px;
  font-size:13px;margin-bottom:14px;display:none}
.error-msg.show{display:block}
.success-msg{background:rgba(16,185,129,.1);color:var(--success);padding:10px 12px;border-radius:8px;
  font-size:13px;margin-bottom:14px;display:none}
.success-msg.show{display:block}
.back-home{text-align:center;margin-top:18px;font-size:13px;color:var(--text-soft)}
.trial-badge{display:inline-block;background:linear-gradient(90deg,var(--accent),#fb923c);
  color:#fff;padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;margin-top:4px}
#registerForm{display:none}
.footer{text-align:center;margin-top:24px;font-size:12px;color:var(--text-soft)}
</style>
</head>
<body>
<a href="/" class="brand">🚀 TokenGo</a>
<div class="subtitle">一个密钥，畅用多个 AI 模型</div>
<div class="card">
  <div class="tabs">
    <button class="tab active" id="tabLogin" onclick="switchTab('login')">登录</button>
    <button class="tab" id="tabRegister" onclick="switchTab('register')">注册</button>
  </div>

  <div class="error-msg" id="errorMsg"></div>
  <div class="success-msg" id="successMsg"></div>

  <!-- 登录表单 -->
  <form id="loginForm" onsubmit="doLogin(event)">
    <div class="form-group">
      <label>邮箱</label>
      <input type="email" id="loginEmail" placeholder="you@example.com" required>
    </div>
    <div class="form-group">
      <label>密码</label>
      <input type="password" id="loginPassword" placeholder="请输入密码" required>
    </div>
    <button type="submit" class="btn" id="loginBtn">登录</button>
    <div class="hint">还没有账户？<a href="#" onclick="switchTab('register');return false;">立即注册</a>，新用户赠送 <span class="trial-badge">$1 试用额度</span></div>
  </form>

  <!-- 注册表单 -->
  <form id="registerForm" onsubmit="doRegister(event)">
    <div class="form-group">
      <label>邮箱</label>
      <input type="email" id="regEmail" placeholder="you@example.com" required>
    </div>
    <div class="form-group">
      <label>用户名（可选）</label>
      <input type="text" id="regUsername" placeholder="留空则用邮箱前缀">
    </div>
    <div class="form-group">
      <label>密码</label>
      <input type="password" id="regPassword" placeholder="至少 6 位" required minlength="6">
    </div>
    <div class="form-group">
      <label>确认密码</label>
      <input type="password" id="regPassword2" placeholder="再次输入密码" required>
    </div>
    <div class="form-group">
      <label>邀请码（可选）</label>
      <input type="text" id="regInvite" placeholder="好友邀请码，填了双方都得返利">
    </div>
    <button type="submit" class="btn" id="registerBtn">注册并领取 $1 试用额度</button>
    <div class="hint">已有账户？<a href="#" onclick="switchTab('login');return false;">前往登录</a></div>
  </form>
</div>
<div class="back-home"><a href="/">← 返回首页</a></div>
<div class="footer">© 2026 TokenGo. 保留所有权利。</div>

<script>
const PUB='""" + PUBLIC_BASE_URL + """';
function switchTab(t){
  document.getElementById('tabLogin').classList.toggle('active',t==='login');
  document.getElementById('tabRegister').classList.toggle('active',t==='register');
  document.getElementById('loginForm').style.display=t==='login'?'block':'none';
  document.getElementById('registerForm').style.display=t==='register'?'block':'none';
  hideMsg();
}
function showError(msg){const e=document.getElementById('errorMsg');e.textContent=msg;e.classList.add('show');}
function showSuccess(msg){const e=document.getElementById('successMsg');e.textContent=msg;e.classList.add('show');}
function hideMsg(){document.getElementById('errorMsg').classList.remove('show');document.getElementById('successMsg').classList.remove('show');}

// 从 URL 切换到注册模式
if(new URLSearchParams(location.search).get('mode')==='register'){switchTab('register');}

async function doLogin(e){
  e.preventDefault();
  hideMsg();
  const email=document.getElementById('loginEmail').value.trim();
  const password=document.getElementById('loginPassword').value;
  if(!email||!password){showError('请填写邮箱和密码');return;}
  const btn=document.getElementById('loginBtn');btn.disabled=true;btn.textContent='登录中...';
  try{
    const r=await fetch('/api/auth/login',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:email,password:password})
    });
    const data=await r.json();
    if(!r.ok){showError(data.detail||'登录失败');btn.disabled=false;btn.textContent='登录';return;}
    localStorage.setItem('tg_session',data.session_token);
    showSuccess('登录成功！正在跳转到控制台...');
    setTimeout(()=>{location.href='/dashboard';},600);
  }catch(err){showError('网络错误：'+err.message);btn.disabled=false;btn.textContent='登录';}
}

async function doRegister(e){
  e.preventDefault();
  hideMsg();
  const email=document.getElementById('regEmail').value.trim();
  const username=document.getElementById('regUsername').value.trim();
  const password=document.getElementById('regPassword').value;
  const password2=document.getElementById('regPassword2').value;
  // 优先从输入框读取，否则从 URL ?ref=XXX 读取
  let inviteCode=document.getElementById('regInvite').value.trim();
  if(!inviteCode){
    const urlRef=new URLSearchParams(location.search).get('ref');
    if(urlRef){inviteCode=urlRef;document.getElementById('regInvite').value=urlRef;}
  }
  if(!email||!password){showError('请填写邮箱和密码');return;}
  if(password.length<6){showError('密码至少 6 位');return;}
  if(password!==password2){showError('两次密码不一致');return;}
  const btn=document.getElementById('registerBtn');btn.disabled=true;btn.textContent='注册中...';
  try{
    const r=await fetch('/api/auth/register',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:email,username:username,password:password,invite_code:inviteCode})
    });
    const data=await r.json();
    if(!r.ok){showError(data.detail||'注册失败');btn.disabled=false;btn.textContent='注册并领取 $1 试用额度';return;}
    localStorage.setItem('tg_session',data.session_token);
    showSuccess('注册成功！已自动发放 $1 试用 API 密钥，正在跳转...');
    setTimeout(()=>{location.href='/dashboard';},900);
  }catch(err){showError('网络错误：'+err.message);btn.disabled=false;btn.textContent='注册并领取 $1 试用额度';}
}

// 已登录直接跳控制台
(async()=>{
  const token=localStorage.getItem('tg_session');
  if(!token)return;
  try{
    const r=await fetch('/api/auth/me',{headers:{'Authorization':'Bearer '+token}});
    if(r.ok){location.href='/dashboard';}
  }catch(e){}
})();
</script>
</body>
</html>"""


# ============================================================================
# HTML：卡密兑换页面
# ============================================================================

REDEEM_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>卡密兑换 - TokenGo</title>
<style>
:root{
  --primary:#14b8a6; --primary-dark:#0d9488; --accent:#f59e0b; --accent-dark:#d97706;
  --bg:#ffffff; --bg-soft:#f8fafc; --card:#ffffff; --text:#0f172a; --text-soft:#64748b;
  --border:#e2e8f0; --shadow:0 10px 30px rgba(2,6,23,.08); --danger:#ef4444; --success:#10b981;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg-soft);color:var(--text);min-height:100vh;transition:background .3s,color .3s}
a{color:var(--primary);text-decoration:none}
a:hover{text-decoration:underline}
.container{max-width:1200px;margin:0 auto;padding:0 24px}
.announce{background:linear-gradient(90deg,var(--accent),#fb923c);color:#fff;text-align:center;
  padding:8px 16px;font-size:14px;font-weight:600;letter-spacing:.5px}
.nav{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.85);
  backdrop-filter:blur(12px);border-bottom:1px solid var(--border)}
.nav-inner{display:flex;align-items:center;justify-content:space-between;height:64px}
.logo{font-size:22px;font-weight:800;color:var(--primary);display:flex;align-items:center;gap:8px}
.nav-links{display:flex;align-items:center;gap:18px}
.nav-links a{font-size:14px;color:var(--text-soft);transition:color .2s}
.nav-links a:hover{color:var(--primary)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;padding:.625rem 1rem;
  border-radius:.75rem;font-size:.875rem;font-weight:500;cursor:pointer;border:none;
  transition:all .2s cubic-bezier(.4,0,.2,1);text-decoration:none;line-height:1.25rem}
.btn:hover{text-decoration:none}
.btn-primary{background:linear-gradient(to right,#14b8a6,#0d9488);color:#fff;
  box-shadow:0 4px 6px -1px rgba(20,184,166,.25)}
.btn-primary:hover{background:linear-gradient(to right,#0d9488,#0f766e);transform:translateY(-1px)}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-outline:hover{border-color:var(--primary);color:var(--primary);background:var(--primary-50)}
.btn-accent{background:linear-gradient(to right,#f59e0b,#d97706);color:#fff;
  box-shadow:0 4px 6px -1px rgba(245,158,11,.2)}
.btn-accent:hover{transform:translateY(-1px)}
.hero{padding:60px 0 30px;text-align:center;position:relative;overflow:hidden}
.hero::before{content:"";position:absolute;inset:0;background:radial-gradient(circle at 30% 20%,rgba(20,184,166,.15),transparent 50%),
  radial-gradient(circle at 70% 80%,rgba(245,158,11,.12),transparent 50%);z-index:-1}
.hero h1{font-size:42px;font-weight:800;letter-spacing:-1px;margin-bottom:14px;line-height:1.2}
.gradient-text{background:linear-gradient(90deg,var(--primary),var(--accent));
  -webkit-background-clip:text;background-clip:text;color:transparent}
.hero p{font-size:17px;color:var(--text-soft);max-width:640px;margin:0 auto}
.redeem-wrap{max-width:540px;margin:0 auto 60px;padding:0 24px}
.redeem-card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:36px 32px;
  box-shadow:var(--shadow)}
.redeem-card h2{font-size:22px;margin-bottom:6px;text-align:center}
.redeem-card .sub{text-align:center;color:var(--text-soft);font-size:14px;margin-bottom:24px}
.form-group{margin-bottom:18px}
.form-group label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;color:var(--text-soft)}
.form-group input{width:100%;padding:.75rem 1rem;border:1.5px solid var(--border);border-radius:.75rem;
  background:var(--card);color:var(--text);font-size:15px;font-family:inherit;transition:border-color .2s,box-shadow .2s}
.form-group input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(20,184,166,.15)}
.form-group input.mono{font-family:"SFMono-Regular",Consolas,monospace;letter-spacing:1px;font-weight:600}
.btn-block{width:100%;padding:.875rem 1rem;border-radius:.75rem;font-size:16px;font-weight:600;cursor:pointer;
  border:none;transition:all .2s;background:linear-gradient(to right,#14b8a6,#0d9488);color:#fff;margin-top:6px;
  box-shadow:0 4px 6px -1px rgba(20,184,166,.25)}
.btn-block:hover{background:linear-gradient(to right,#0d9488,#0f766e);transform:translateY(-1px)}
.btn-block:disabled{opacity:.6;cursor:not-allowed;transform:none}
.error-msg{background:rgba(239,68,68,.1);color:var(--danger);padding:11px 14px;border-radius:.75rem;
  font-size:13px;margin-bottom:14px;display:none;border:1px solid rgba(239,68,68,.2)}
.error-msg.show{display:block}
.tip{font-size:12px;color:var(--text-soft);margin-top:6px;line-height:1.6}
.tip code{background:var(--bg-soft);padding:2px 6px;border-radius:4px;font-family:Consolas,monospace;
  color:var(--accent);font-size:12px}
.result-card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:36px 32px;
  box-shadow:var(--shadow);display:none}
.result-card.show{display:block;animation:fadeIn .35s}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.result-card .success-icon{text-align:center;font-size:54px;margin-bottom:12px}
.result-card h2{text-align:center;font-size:24px;margin-bottom:6px;color:var(--success)}
.result-card .sub{text-align:center;color:var(--text-soft);font-size:14px;margin-bottom:24px}
.result-row{display:flex;align-items:center;justify-content:space-between;gap:12px;
  background:var(--bg-soft);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.result-row .label{font-size:13px;color:var(--text-soft);font-weight:600;flex-shrink:0}
.result-row .value{font-family:"SFMono-Regular",Consolas,monospace;font-size:14px;font-weight:600;
  color:var(--text);word-break:break-all;text-align:right;flex:1}
.result-row .value.balance{color:var(--success);font-size:20px}
.result-row .value.accent{color:var(--primary)}
.copy-btn{background:transparent;border:1px solid var(--border);color:var(--text-soft);
  padding:5px 10px;border-radius:7px;font-size:12px;cursor:pointer;margin-left:8px;flex-shrink:0;transition:all .2s;font-family:inherit}
.copy-btn:hover{border-color:var(--primary);color:var(--primary)}
.result-actions{display:flex;gap:10px;justify-content:center;margin-top:20px;flex-wrap:wrap}
.callout{background:rgba(20,184,166,.08);border-left:4px solid var(--primary);padding:14px 18px;
  border-radius:8px;margin-top:18px;font-size:13px;color:var(--text-soft);line-height:1.7}
.callout b{color:var(--text)}
.feature-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:18px;
  margin-top:40px;max-width:900px;margin-left:auto;margin-right:auto}
.feature-item{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;text-align:center}
.feature-item .icon{font-size:34px;margin-bottom:10px}
.feature-item h4{font-size:15px;margin-bottom:6px}
.feature-item p{font-size:13px;color:var(--text-soft);line-height:1.5}
footer{background:var(--bg);border-top:1px solid var(--border);padding:32px 0;text-align:center;
  color:var(--text-soft);font-size:14px}
@media(max-width:768px){
  .hero h1{font-size:30px}
  .hero p{font-size:15px}
  .nav-links a:not(.btn){display:none}
  .redeem-card,.result-card{padding:28px 20px}
}
</style>
</head>
<body>
<div class="announce">🔥 限时优惠 ¥1 RMB = $12 USD 美金额度 · 卡密兑换即时到账</div>
<nav class="nav">
  <div class="container nav-inner">
    <a href="/" class="logo">🚀 TokenGo</a>
    <div class="nav-links">
      <a href="/help">使用教程</a>
      <a href="/redeem" class="btn btn-accent">🎫 卡密兑换</a>
      <a href="/login" class="btn btn-outline">登录</a>
      <a href="/login?mode=register" class="btn btn-primary">注册</a>
      <a href="/dashboard" class="btn btn-outline">控制台</a>
    </div>
  </div>
</nav>

<section class="hero">
  <div class="container">
    <h1>🎫 卡密 <span class="gradient-text">兑换中心</span></h1>
    <p>输入您的卡密和邮箱，立即获取 API 密钥与额度，无需注册</p>
  </div>
</section>

<div class="redeem-wrap">
  <div class="error-msg" id="errorMsg"></div>
  <div class="redeem-card" id="redeemForm">
    <h2>卡密兑换</h2>
    <div class="sub">凭卡密领取 API 密钥与额度</div>
    <form onsubmit="doRedeem(event)">
      <div class="form-group">
        <label>卡密（Card ID）</label>
        <input type="text" id="cardId" class="mono" placeholder="TG-XXXX-XXXX-XXXX"
               required autocomplete="off" style="text-transform:uppercase">
        <div class="tip">卡密格式：<code>TG-XXXX-XXXX-XXXX</code>（不区分大小写）</div>
      </div>
      <div class="form-group">
        <label>邮箱（Email）</label>
        <input type="email" id="email" placeholder="you@example.com" required>
        <div class="tip">邮箱用于关联账户：已注册则自动加额度，未注册将自动创建账户</div>
      </div>
      <button type="submit" class="btn-block" id="redeemBtn">🚀 立即兑换</button>
    </form>
    <div class="callout">
      <b>💡 兑换说明：</b><br>
      1. 卡密面值将自动充值到您的账户余额<br>
      2. 新用户将自动获得 API 密钥（格式 <code>sk-tg-xxx</code>）<br>
      3. 兑换后请妥善保存 API 密钥，密钥仅显示一次
    </div>
  </div>

  <div class="result-card" id="resultCard">
    <div class="success-icon">🎉</div>
    <h2>兑换成功！</h2>
    <div class="sub" id="resultSub">您的 API 密钥与额度已发放</div>

    <div class="result-row">
      <span class="label">账户余额</span>
      <span class="value balance" id="rBalance">$0.00</span>
    </div>
    <div class="result-row">
      <span class="label">API 密钥</span>
      <span class="value accent" id="rApiKey">sk-tg-xxx</span>
      <button class="copy-btn" onclick="copyVal('rApiKey')">复制</button>
    </div>
    <div class="result-row">
      <span class="label">API 地址</span>
      <span class="value" id="rApiBase">https://...</span>
      <button class="copy-btn" onclick="copyVal('rApiBase')">复制</button>
    </div>
    <div class="result-row">
      <span class="label">卡密面值</span>
      <span class="value" id="rFaceValue">$0.00</span>
    </div>
    <div class="result-row">
      <span class="label">关联邮箱</span>
      <span class="value" id="rEmail">-</span>
    </div>

    <div class="result-actions">
      <a href="/help" class="btn btn-outline">📖 使用教程</a>
      <a href="/dashboard" class="btn btn-primary">🚀 进入控制台</a>
      <button class="btn btn-accent" onclick="resetForm()">兑换另一张</button>
    </div>

    <div class="callout">
      <b>⚠️ 重要提示：</b>请立即复制并保存您的 API 密钥！密钥仅显示一次。<br>
      <b>登录控制台：</b>首次兑换将自动注册账户，初始密码已发送至您的邮箱（如未收到请使用「忘记密码」重置）。<br>
      <b>使用方式：</b>在 Claude Code、Cursor、VS Code 等客户端填入上方 API 地址与密钥即可。
    </div>
  </div>

  <div class="feature-list">
    <div class="feature-item">
      <div class="icon">⚡</div>
      <h4>即时到账</h4>
      <p>兑换后立即获得 API 密钥与额度，无需等待</p>
    </div>
    <div class="feature-item">
      <div class="icon">🔐</div>
      <h4>安全可靠</h4>
      <p>支持 OpenAI / Anthropic 协议，密钥独立隔离</p>
    </div>
    <div class="feature-item">
      <div class="icon">💰</div>
      <h4>超高性价比</h4>
      <p>¥1 RMB = $12 USD 额度，远低于官方价格</p>
    </div>
    <div class="feature-item">
      <div class="icon">🌐</div>
      <h4>多端兼容</h4>
      <p>Claude Code / Cursor / VS Code 等全部支持</p>
    </div>
  </div>
</div>

<footer>
  <div class="container">© 2026 TokenGo. 保留所有权利。</div>
</footer>

<script>
function showError(msg){const e=document.getElementById('errorMsg');e.textContent=msg;e.classList.add('show');}
function hideError(){document.getElementById('errorMsg').classList.remove('show');}

async function doRedeem(e){
  e.preventDefault();
  hideError();
  const cardId=document.getElementById('cardId').value.trim().toUpperCase();
  const email=document.getElementById('email').value.trim().toLowerCase();
  if(!cardId){showError('请输入卡密');return;}
  if(!email){showError('请输入邮箱');return;}
  if(!/^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(email)){showError('邮箱格式不正确');return;}
  const btn=document.getElementById('redeemBtn');
  btn.disabled=true;btn.textContent='兑换中...';
  try{
    const r=await fetch('/api/card/redeem',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({card_id:cardId,email:email})
    });
    const data=await r.json();
    if(!r.ok){showError(data.detail||'兑换失败');btn.disabled=false;btn.textContent='🚀 立即兑换';return;}
    showResult(data);
  }catch(err){showError('网络错误：'+err.message);btn.disabled=false;btn.textContent='🚀 立即兑换';}
}

function showResult(data){
  document.getElementById('redeemForm').style.display='none';
  const rc=document.getElementById('resultCard');
  rc.classList.add('show');
  document.getElementById('resultSub').textContent=data.is_new_user?
    '已为您自动注册账户并发放 API 密钥':'已为您的账户增加额度';
  document.getElementById('rBalance').textContent='$'+Number(data.face_value).toFixed(2)+' USD';
  document.getElementById('rApiKey').textContent=data.api_key;
  document.getElementById('rApiBase').textContent=data.api_base;
  document.getElementById('rFaceValue').textContent='$'+Number(data.face_value).toFixed(2)+' (售价 ¥'+Number(data.price).toFixed(2)+')';
  document.getElementById('rEmail').textContent=data.email;
  rc.scrollIntoView({behavior:'smooth',block:'center'});
}

function resetForm(){
  document.getElementById('resultCard').classList.remove('show');
  document.getElementById('redeemForm').style.display='block';
  document.getElementById('cardId').value='';
  document.getElementById('email').value='';
  document.getElementById('redeemBtn').disabled=false;
  document.getElementById('redeemBtn').textContent='🚀 立即兑换';
  hideError();
  window.scrollTo({top:0,behavior:'smooth'});
}

function copyVal(id){
  const text=document.getElementById(id).textContent;
  const ta=document.createElement('textarea');ta.value=text;
  ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{document.execCommand('copy');}catch(e){}
  document.body.removeChild(ta);
  const btn=event.target;const orig=btn.textContent;
  btn.textContent='已复制';setTimeout(()=>{btn.textContent=orig;},1500);
}
</script>
</body>
</html>"""


# ============================================================================
# HTML：控制台（SPA，11 个子页面）
# ============================================================================


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TokenGo 控制台</title>
<style>
:root{
  --primary:#14b8a6; --primary-dark:#0d9488; --primary-light:#5eead4; --primary-50:#f0fdfa;
  --accent:#f59e0b; --accent-dark:#d97706;
  --bg:#f1f5f9; --card:#ffffff; --text:#0f172a; --text-soft:#64748b;
  --border:#e2e8f0; --sidebar:#0f172a; --sidebar-text:#cbd5e1; --sidebar-active:#14b8a6;
  --shadow:0 1px 3px rgba(0,0,0,.04),0 1px 2px rgba(0,0,0,.06); --shadow-lg:0 10px 40px rgba(0,0,0,.08);
  --danger:#ef4444; --danger-dark:#dc2626; --success:#10b981; --success-dark:#059669;
  --alipay:#00aeef; --wxpay:#2bb741; --stripe:#635bff; --usdt:#26a17b;
}
[data-theme="dark"]{
  --primary:#2dd4bf; --primary-dark:#14b8a6; --primary-light:#5eead4; --primary-50:rgba(20,184,166,.1);
  --accent:#fbbf24; --accent-dark:#f59e0b;
  --bg:#020617; --card:#1e293b; --text:#e2e8f0; --text-soft:#94a3b8;
  --border:#334155; --sidebar:#000000; --sidebar-text:#94a3b8; --sidebar-active:#14b8a6;
  --shadow:0 1px 3px rgba(0,0,0,.2); --shadow-lg:0 10px 40px rgba(0,0,0,.4);
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--text);transition:background .3s,color .3s}
.app{display:flex;min-height:100vh}
.sidebar{width:240px;background:var(--sidebar);color:var(--sidebar-text);
  position:fixed;top:0;left:0;bottom:0;display:flex;flex-direction:column;z-index:40;
  transition:transform .3s}
.sidebar-brand{padding:20px 24px;font-size:20px;font-weight:800;color:#fff;display:flex;align-items:center;gap:8px}
.sidebar-nav{flex:1;padding:12px;overflow-y:auto}
.nav-item{display:flex;align-items:center;gap:12px;padding:11px 14px;border-radius:10px;
  cursor:pointer;font-size:14px;color:var(--sidebar-text);transition:all .2s;margin-bottom:2px}
.nav-item:hover{background:rgba(255,255,255,.06);color:#fff}
.nav-item.active{background:var(--sidebar-active);color:#fff;font-weight:600}
.main{flex:1;margin-left:240px;display:flex;flex-direction:column;min-height:100vh}
.topbar{background:var(--card);border-bottom:1px solid var(--border);height:60px;
  display:flex;align-items:center;justify-content:space-between;padding:0 24px;position:sticky;top:0;z-index:30}
.topbar h1{font-size:18px;font-weight:700}
.topbar-actions{display:flex;align-items:center;gap:12px}
.content{padding:24px;flex:1}
.page{display:none;animation:fadeIn .25s}
.page.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;padding:.625rem 1rem;
  border-radius:.75rem;font-size:.875rem;font-weight:500;cursor:pointer;border:none;
  transition:all .2s cubic-bezier(.4,0,.2,1);line-height:1.25rem}
.btn-primary{background:linear-gradient(to right,#14b8a6,#0d9488);color:#fff;
  box-shadow:0 4px 6px -1px rgba(0,0,0,.1),0 2px 4px -2px rgba(0,0,0,.1),0 0 20px rgba(20,184,166,.25)}
.btn-primary:hover{background:linear-gradient(to right,#0d9488,#0f766e);transform:translateY(-1px)}
.btn-accent{background:linear-gradient(to right,#f59e0b,#d97706);color:#fff;
  box-shadow:0 4px 6px -1px rgba(245,158,11,.2)}
.btn-accent:hover{transform:translateY(-1px)}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-outline:hover{border-color:var(--primary);color:var(--primary);background:var(--primary-50)}
.btn-danger{background:linear-gradient(to right,#ef4444,#dc2626);color:#fff;
  box-shadow:0 4px 6px -1px rgba(239,68,68,.25)}
.btn-success{background:linear-gradient(to right,#10b981,#059669);color:#fff;
  box-shadow:0 4px 6px -1px rgba(16,185,129,.25)}
.btn-alipay{background:var(--alipay);color:#fff;box-shadow:0 4px 6px -1px rgba(0,174,239,.25)}
.btn-wxpay{background:var(--wxpay);color:#fff;box-shadow:0 4px 6px -1px rgba(43,183,65,.25)}
.btn-stripe{background:var(--stripe);color:#fff;box-shadow:0 4px 6px -1px rgba(99,91,255,.25)}
.btn-sm{padding:.375rem .75rem;font-size:12px;border-radius:.5rem}
.icon-btn{background:transparent;border:1px solid var(--border);width:36px;height:36px;border-radius:.75rem;cursor:pointer;font-size:15px;
  transition:all .2s}
.icon-btn:hover{border-color:var(--primary);color:var(--primary)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px;margin-bottom:24px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:20px;
  box-shadow:var(--shadow);transition:all .3s}
.stat-card:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg)}
.stat-card .label{font-size:13px;color:var(--text-soft);margin-bottom:8px}
.stat-card .value{font-size:26px;font-weight:800;background:linear-gradient(90deg,#14b8a6,#0d9488);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.stat-card .delta{font-size:12px;margin-top:6px}
.delta.up{color:var(--success)} .delta.down{color:var(--danger)}
.panel{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:20px;
  margin-bottom:20px;box-shadow:var(--shadow)}
.panel-title{font-size:16px;font-weight:700;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--border)}
th{font-size:12px;color:var(--text-soft);font-weight:600;text-transform:uppercase;letter-spacing:.5px}
tr:hover{background:var(--bg)}
.tag{display:inline-block;font-size:11px;padding:3px 8px;border-radius:6px;font-weight:600}
.tag-active{background:rgba(16,185,129,.15);color:var(--success)}
.tag-pending{background:rgba(245,158,11,.15);color:var(--accent)}
.tag-closed{background:rgba(100,116,139,.15);color:var(--text-soft)}
.input,.select,textarea{width:100%;padding:.625rem 1rem;border:1px solid var(--border);border-radius:.75rem;
  background:var(--card);color:var(--text);font-size:.875rem;font-family:inherit;transition:all .2s}
.input:focus,.select:focus,textarea:focus{outline:none;border-color:var(--primary);
  box-shadow:0 0 0 3px rgba(20,184,166,.15)}
label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;color:var(--text-soft)}
.form-row{margin-bottom:16px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;
  align-items:center;justify-content:center;backdrop-filter:blur(4px)}
.modal-bg.show{display:flex}
.modal{background:var(--card);border-radius:20px;padding:28px;max-width:460px;width:90%;
  box-shadow:0 20px 60px rgba(0,0,0,.3)}
.modal h3{margin-bottom:18px;font-size:18px}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:20px}
.pill-group{display:flex;gap:8px;flex-wrap:wrap}
.pill{padding:.5rem 1rem;border-radius:.75rem;border:1px solid var(--border);cursor:pointer;
  font-size:.875rem;transition:all .2s}
.pill:hover{border-color:var(--primary)}
.pill.active{background:linear-gradient(to right,#14b8a6,#0d9488);color:#fff;border-color:transparent;
  box-shadow:0 4px 6px -1px rgba(20,184,166,.25)}
.toast{position:fixed;bottom:24px;right:24px;background:var(--text);color:var(--bg);
  padding:12px 20px;border-radius:10px;z-index:200;box-shadow:var(--shadow);font-size:14px;
  transform:translateY(100px);opacity:0;transition:all .3s}
.toast.show{transform:none;opacity:1}
canvas{max-width:100%}
.mono{font-family:"SFMono-Regular",Consolas,monospace;font-size:13px}
.mobile-toggle{display:none}
@media(max-width:900px){
  .sidebar{transform:translateX(-100%)}
  .sidebar.open{transform:none}
  .main{margin-left:0}
  .mobile-toggle{display:inline-block}
  .cards{grid-template-columns:repeat(2,1fr)}
  .grid-2,.grid-3{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-brand">🚀 TokenGo</div>
    <nav class="sidebar-nav" id="nav">
      <div class="nav-item active" data-page="overview">📊 仪表盘</div>
      <div class="nav-item" data-page="keys">🔑 API密钥</div>
      <div class="nav-item" data-page="usage">📝 使用记录</div>
      <div class="nav-item" data-page="channels">📡 可用渠道</div>
      <div class="nav-item" data-page="subscription">💎 我的订阅</div>
      <div class="nav-item" data-page="topup">💰 充值/订阅</div>
      <div class="nav-item" data-page="orders">📦 我的订单</div>
      <div class="nav-item" data-page="redeem">🎫 兑换</div>
      <div class="nav-item" data-page="referral">🎁 邀请返利</div>
      <div class="nav-item" data-page="tickets">🎫 客服工单</div>
      <div class="nav-item" data-page="profile">👤 个人资料</div>
      <div class="nav-item" data-page="cards" id="navCards" style="display:none">🎴 卡密管理</div>
    </nav>
  </aside>

  <div class="main">
    <header class="topbar">
      <div style="display:flex;align-items:center;gap:12px">
        <button class="icon-btn mobile-toggle" onclick="document.getElementById('sidebar').classList.toggle('open')">☰</button>
        <h1 id="pageTitle">仪表盘</h1>
      </div>
      <div class="topbar-actions">
        <span id="topbarUser" style="font-size:13px;color:var(--text-soft)">加载中...</span>
        <a href="/" class="btn btn-outline btn-sm">🏠 首页</a>
        <button class="btn btn-danger btn-sm" onclick="dashboardLogout()">退出登录</button>
        <button class="btn btn-primary btn-sm" onclick="contactSupport()">客服</button>
      </div>
    </header>

    <main class="content">
      <!-- 仪表盘 -->
      <section class="page active" id="page-overview">
        <div class="cards" id="statCards"></div>
        <div class="grid-2">
          <div class="panel">
            <div class="panel-title">性能指标</div>
            <div id="perfMetrics"></div>
          </div>
          <div class="panel">
            <div class="panel-title">按平台拆分</div>
            <div id="platformSplit"></div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">Token 使用趋势（近 14 天）</div>
          <canvas id="trendChart" width="1000" height="260"></canvas>
        </div>
        <div class="panel">
          <div class="panel-title">模型分布</div>
          <div id="modelDist"></div>
        </div>
        <div class="panel">
          <div class="panel-title">最近使用记录
            <a href="#" class="btn btn-outline btn-sm" onclick="navTo('usage');return false">查看全部</a>
          </div>
          <div id="recentUsage"></div>
        </div>
        <div class="panel">
          <div class="panel-title">快捷操作</div>
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <button class="btn btn-primary" onclick="navTo('keys');setTimeout(openCreateKey,50)">➕ 创建密钥</button>
            <button class="btn btn-accent" onclick="navTo('topup')">💰 立即充值</button>
            <button class="btn btn-outline" onclick="navTo('redeem')">🎫 兑换码</button>
            <button class="btn btn-outline" onclick="navTo('channels')">📡 查看渠道</button>
          </div>
        </div>
      </section>

      <!-- API 密钥 -->
      <section class="page" id="page-keys">
        <div class="panel">
          <div class="panel-title">API 密钥管理
            <button class="btn btn-primary" onclick="openCreateKey()">➕ 创建API密钥</button>
          </div>
          <div id="keysTable"></div>
        </div>
      </section>

      <!-- 使用记录 -->
      <section class="page" id="page-usage">
        <div class="panel">
          <div class="panel-title">使用记录</div>
          <div class="grid-3" style="margin-bottom:16px">
            <div><label>时间范围</label>
              <select class="select" id="usageDays">
                <option value="1">今天</option>
                <option value="7" selected>最近7天</option>
                <option value="30">最近30天</option>
              </select>
            </div>
            <div style="display:flex;align-items:flex-end"><button class="btn btn-primary" onclick="loadUsage()">查询</button></div>
          </div>
          <div id="usageTable"></div>
          <div style="display:flex;justify-content:center;gap:8px;margin-top:16px" id="pager"></div>
        </div>
      </section>

      <!-- 可用渠道 -->
      <section class="page" id="page-channels">
        <div class="panel">
          <div class="panel-title">可用渠道与模型</div>
          <div id="channelsTable"></div>
        </div>
        <div class="panel">
          <div class="panel-title">模型价格（每 1K Token，USD）</div>
          <div id="pricesTable"></div>
        </div>
      </section>

      <!-- 我的订阅 -->
      <section class="page" id="page-subscription">
        <div class="panel">
          <div class="panel-title">当前订阅</div>
          <div id="subCurrent"></div>
        </div>
        <div class="panel">
          <div class="panel-title">订阅套餐</div>
          <div class="grid-3" id="plansList"></div>
        </div>
      </section>

      <!-- 充值/订阅 -->
      <section class="page" id="page-topup">
        <div class="panel">
          <div class="panel-title">账户充值</div>
          <p style="color:var(--text-soft);margin-bottom:16px">💡 充值说明：¥1 RMB = $12 USD 美金额度</p>
          <div class="form-row">
            <label>选择充值金额</label>
            <div class="pill-group" id="amountGroup">
              <div class="pill" data-v="10">¥10</div>
              <div class="pill active" data-v="50">¥50</div>
              <div class="pill" data-v="100">¥100</div>
              <div class="pill" data-v="500">¥500</div>
            </div>
          </div>
          <div class="form-row">
            <label>支付方式</label>
            <div class="pill-group" id="payGroup">
              <div class="pill active" data-v="alipay">💚 支付宝</div>
              <div class="pill" data-v="wechat">💙 微信支付</div>
              <div class="pill" data-v="usdt">₮ USDT</div>
            </div>
          </div>
          <div class="form-row">
            <label>预计获得额度</label>
            <div class="input" id="willGet" style="font-weight:700;color:var(--primary)">$600.00 USD</div>
          </div>
          <button class="btn btn-accent" onclick="doTopup()">立即充值</button>
        </div>
      </section>

      <!-- 我的订单 -->
      <section class="page" id="page-orders">
        <div class="panel">
          <div class="panel-title">我的订单</div>
          <div id="ordersTable"></div>
        </div>
      </section>

      <!-- 兑换 -->
      <section class="page" id="page-redeem">
        <div class="panel">
          <div class="panel-title">兑换码</div>
          <div class="grid-2">
            <div><label>输入兑换码</label><input class="input" id="redeemCode" placeholder="例如 TOKENGO100"></div>
            <div style="display:flex;align-items:flex-end"><button class="btn btn-primary" onclick="doRedeem()">兑换</button></div>
          </div>
          <p style="color:var(--text-soft);font-size:13px;margin-top:12px">可用兑换码示例：TOKENGO100 / NEWUSER10 / WELCOME5</p>
        </div>
        <div class="panel">
          <div class="panel-title">兑换记录</div>
          <div id="redeemList"></div>
        </div>
      </section>

      <!-- 邀请返利 -->
      <section class="page" id="page-referral">
        <div class="panel">
          <div class="panel-title">邀请返利</div>
          <div class="form-row">
            <label>我的邀请链接</label>
            <div style="display:flex;gap:8px">
              <input class="input" id="inviteLink" readonly value="">
              <button class="btn btn-primary" onclick="copyInvite()">复制</button>
            </div>
          </div>
          <div class="form-row">
            <label>返利规则</label>
            <p style="font-size:14px;color:var(--text-soft)">好友通过您的链接注册并充值，您将获得其充值金额的 <b style="color:var(--accent)">15%</b> 作为返利，可提现或用于消费。</p>
          </div>
        </div>
        <div class="cards" id="referralSummary" style="margin-bottom:16px"></div>
        <div class="panel">
          <div class="panel-title">返利记录</div>
          <div id="rebateList"></div>
        </div>
      </section>

      <!-- 客服工单 -->
      <section class="page" id="page-tickets">
        <div class="panel">
          <div class="panel-title">客服工单
            <button class="btn btn-primary" onclick="openCreateTicket()">➕ 提交工单</button>
          </div>
          <div id="ticketsTable"></div>
        </div>
      </section>

      <!-- 个人资料 -->
      <section class="page" id="page-profile">
        <div class="panel">
          <div class="panel-title">个人资料</div>
          <div style="display:flex;align-items:center;gap:20px;margin-bottom:24px">
            <div style="width:80px;height:80px;border-radius:50%;background:linear-gradient(135deg,var(--primary),var(--accent));
              display:flex;align-items:center;justify-content:center;font-size:36px;color:#fff">TG</div>
            <div>
              <div style="font-size:20px;font-weight:700" id="pfUser">TokenGo 用户</div>
              <div style="color:var(--text-soft)" id="pfEmail">Commecy2014@gmail.com</div>
            </div>
          </div>
          <div class="grid-2">
            <div class="form-row"><label>用户名</label><input class="input" id="pfUsername" value="TokenGo 用户"></div>
            <div class="form-row"><label>邮箱</label><input class="input" id="pfEmailInput" value="Commecy2014@gmail.com"></div>
            <div class="form-row"><label>新密码</label><input class="input" id="pfPassword" type="password" placeholder="留空则不修改"></div>
            <div class="form-row"><label>默认 API 端点</label><input class="input" value="__PUBLIC_BASE_URL__/v1" readonly></div>
          </div>
          <button class="btn btn-primary" onclick="saveProfile()">保存修改</button>
        </div>
        <div class="panel">
          <div class="panel-title">API 接入信息</div>
          <p style="font-size:14px;color:var(--text-soft);margin-bottom:8px">Base URL:</p>
          <div class="input mono" style="margin-bottom:12px">__PUBLIC_BASE_URL__/v1</div>
          <p style="font-size:14px;color:var(--text-soft);margin-bottom:8px">您的密钥（主密钥）:</p>
          <div class="input mono" id="profileKey">demo-master</div>
        </div>
        <div class="panel" id="adminEntryPanel">
          <div class="panel-title">🎴 管理员入口</div>
          <p style="font-size:13px;color:var(--text-soft);margin-bottom:12px">
            输入管理员主密钥（demo-master）以解锁卡密管理功能，可生成卡密用于闲鱼/淘宝售卖。
          </p>
          <div class="form-row"><label>管理员密钥</label>
            <input class="input" id="adminKeyInput" type="password" placeholder="例如 demo-master">
          </div>
          <button class="btn btn-primary" onclick="unlockAdmin()">🔓 解锁卡密管理</button>
          <button class="btn btn-outline" id="adminLockBtn" style="display:none;margin-left:8px" onclick="lockAdmin()">🔒 退出管理员模式</button>
          <div id="adminStatus" style="margin-top:10px;font-size:13px"></div>
        </div>
      </section>

      <!-- 卡密管理（仅管理员可见） -->
      <section class="page" id="page-cards">
        <div class="panel">
          <div class="panel-title">🎴 卡密管理
            <button class="btn btn-primary" onclick="openGenCardModal()">➕ 生成卡密</button>
          </div>
          <p style="color:var(--text-soft);font-size:13px;margin-bottom:16px">
            管理员功能：生成卡密用于闲鱼/淘宝售卖，用户可通过 <a href="/redeem" target="_blank" style="color:var(--primary)">/redeem</a> 页面兑换
          </p>
          <div class="cards" id="cardStats"></div>
        </div>
        <div class="panel">
          <div class="panel-title">卡密列表
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              <button class="btn btn-outline btn-sm" onclick="loadCards('')">全部</button>
              <button class="btn btn-outline btn-sm" onclick="loadCards('unused')">未使用</button>
              <button class="btn btn-outline btn-sm" onclick="loadCards('used')">已使用</button>
              <button class="btn btn-outline btn-sm" onclick="loadCards('expired')">已过期</button>
              <button class="btn btn-accent btn-sm" onclick="exportCards()">📋 导出TXT</button>
            </div>
          </div>
          <div id="cardsTable"></div>
        </div>
      </section>
    </main>
  </div>
</div>

<!-- 生成卡密模态框 -->
<div class="modal-bg" id="cardModal">
  <div class="modal">
    <h3>生成卡密</h3>
    <div class="form-row"><label>生成数量（1-1000）</label><input class="input" id="cardCount" type="number" value="10" min="1" max="1000"></div>
    <div class="form-row"><label>面值（USD 美元）</label><input class="input" id="cardFace" type="number" value="12" step="0.1"></div>
    <div class="form-row"><label>售价（RMB 人民币）</label><input class="input" id="cardPrice" type="number" value="1" step="0.1"></div>
    <div style="background:var(--bg-soft);padding:12px;border-radius:8px;font-size:13px;color:var(--text-soft);margin-bottom:8px">
      💡 常见组合：面值 $12 售价 ¥1 / 面值 $60 售价 ¥5 / 面值 $120 售价 ¥10
    </div>
    <div class="modal-actions">
      <button class="btn btn-outline" onclick="closeModal('cardModal')">取消</button>
      <button class="btn btn-primary" onclick="genCards()">生成</button>
    </div>
  </div>
</div>

<!-- 卡密生成结果模态框 -->
<div class="modal-bg" id="cardResultModal">
  <div class="modal" style="max-width:560px">
    <h3>🎉 卡密生成成功</h3>
    <p style="color:var(--text-soft);font-size:13px;margin-bottom:14px" id="cardResultInfo"></p>
    <textarea class="input mono" id="cardResultText" rows="12" style="font-size:12px;letter-spacing:.5px" readonly></textarea>
    <div class="modal-actions">
      <button class="btn btn-outline" onclick="copyCardResult()">复制全部</button>
      <button class="btn btn-primary" onclick="closeModal('cardResultModal');loadCards('')">关闭</button>
    </div>
  </div>
</div>

<!-- 创建密钥模态框 -->
<div class="modal-bg" id="keyModal">
  <div class="modal">
    <h3>创建 API 密钥</h3>
    <div class="form-row"><label>名称</label><input class="input" id="keyName" placeholder="例如：生产环境"></div>
    <div class="form-row"><label>额度（USD）</label><input class="input" id="keyQuota" type="number" value="10" step="0.1"></div>
    <div class="form-row"><label>有效期（天，0=永久）</label><input class="input" id="keyExpire" type="number" value="0"></div>
    <div class="form-row"><label>允许的模型（all=全部）</label><input class="input" id="keyModels" value="all"></div>
    <div class="modal-actions">
      <button class="btn btn-outline" onclick="closeModal('keyModal')">取消</button>
      <button class="btn btn-primary" onclick="saveKey()">创建</button>
    </div>
  </div>
</div>

<!-- 创建工单模态框 -->
<div class="modal-bg" id="ticketModal">
  <div class="modal">
    <h3>提交工单</h3>
    <div class="form-row"><label>标题</label><input class="input" id="ticketTitle" placeholder="请简要描述问题"></div>
    <div class="form-row"><label>内容</label><textarea class="input" id="ticketContent" rows="4" placeholder="详细描述您遇到的问题..."></textarea></div>
    <div class="modal-actions">
      <button class="btn btn-outline" onclick="closeModal('ticketModal')">取消</button>
      <button class="btn btn-primary" onclick="saveTicket()">提交</button>
    </div>
  </div>
</div>

<!-- 联系客服模态框 -->
<div class="modal-bg" id="supportModal">
  <div class="modal" style="text-align:center">
    <h3>联系客服</h3>
    <p style="color:var(--text-soft);margin:8px 0 20px">邮箱：Commecy2014@gmail.com</p>
    <button class="btn btn-primary" onclick="closeModal('supportModal')">关闭</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const PAGE_TITLES={overview:'仪表盘',keys:'API密钥',usage:'使用记录',channels:'可用渠道',
  subscription:'我的订阅',topup:'充值/订阅',orders:'我的订单',redeem:'兑换',
  referral:'邀请返利',tickets:'客服工单',profile:'个人资料',cards:'卡密管理'};
let currentPage='overview';
let usageData=[];
let usagePage=1;
const PAGE_SIZE=10;

function $(id){return document.getElementById(id);}
function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200);}
function openModal(id){$(id).classList.add('show');}
function closeModal(id){$(id).classList.remove('show');}
function contactSupport(){openModal('supportModal');}
function navTo(page){document.querySelector('.nav-item[data-page="'+page+'"]').click();}

// 侧边栏切换
document.querySelectorAll('.nav-item').forEach(item=>{
  item.addEventListener('click',()=>{
    const page=item.dataset.page;
    document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
    item.classList.add('active');
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    $('page-'+page).classList.add('active');
    $('pageTitle').textContent=PAGE_TITLES[page];
    currentPage=page;
    document.getElementById('sidebar').classList.remove('open');
    loadPageData(page);
  });
});

function loadPageData(page){
  if(page==='overview')loadStats();
  else if(page==='keys')loadKeys();
  else if(page==='usage')loadUsage();
  else if(page==='channels')loadChannels();
  else if(page==='subscription')loadSubscription();
  else if(page==='orders')loadOrders();
  else if(page==='redeem')loadRedemptions();
  else if(page==='referral')loadReferral();
  else if(page==='tickets')loadTickets();
  else if(page==='topup')updateWillGet();
  else if(page==='cards')loadCardsPage();
}

function toggleTheme(){}

// 公网地址（前端引用）
const PUB_URL='""" + PUBLIC_BASE_URL + """';
// 会话 token 工具
function getSession(){return localStorage.getItem('tg_session');}
function authHeaders(){const t=getSession();return t?{'Authorization':'Bearer '+t}:{};}

async function api(path,opts={}){
  // 自动注入 Authorization 头
  opts.headers=opts.headers||{};
  const t=getSession();
  if(t){opts.headers['Authorization']='Bearer '+t;}
  const res=await fetch(path,opts);
  if(res.status===401){
    localStorage.removeItem('tg_session');
    toast('会话已过期，请重新登录');
    setTimeout(()=>{location.href='/login';},800);
    throw new Error('未登录');
  }
  if(!res.ok){const e=await res.json().catch(()=>({detail:'请求失败'}));throw new Error(e.detail||'请求失败');}
  return res.json();
}

// 退出登录
async function dashboardLogout(){
  const t=getSession();
  if(t){try{await fetch('/api/auth/logout',{method:'POST',headers:{'Authorization':'Bearer '+t}});}catch(e){}}
  localStorage.removeItem('tg_session');
  location.href='/login';
}

// 页面加载：校验登录状态，渲染顶部用户信息
async function checkLoginAndRenderUser(){
  const t=getSession();
  if(!t){location.href='/login';return;}
  try{
    const u=await api('/api/auth/me');
    window._currentUser=u;
    $('topbarUser').textContent='👋 '+(u.username||u.email);
    // 同步到个人资料页
    if($('pfUser')){$('pfUser').textContent=u.username||u.email;}
    if($('pfEmail')){$('pfEmail').textContent=u.email;}
    if($('pfUsername')){$('pfUsername').value=u.username||'';}
    if($('pfEmailInput')){$('pfEmailInput').value=u.email;}
    if($('profileKey')){$('profileKey').textContent=u.api_key||'(无)';}
    // 管理员显示卡密管理菜单
    refreshAdminNav();
  }catch(e){
    // api() 内部已处理跳转
  }
}

// 管理员模式：判断是否解锁
function isAdminUnlocked(){
  const u=window._currentUser||{};
  return u.is_admin===true || u.api_key==='demo-master' || u.role==='admin' || !!localStorage.getItem('tg_admin_key');
}
function refreshAdminNav(){
  if($('navCards')){
    $('navCards').style.display=isAdminUnlocked()?'flex':'none';
  }
  // 同步管理员入口面板状态
  if($('adminStatus')){
    if(localStorage.getItem('tg_admin_key')){
      $('adminStatus').innerHTML='<span style="color:var(--success)">✓ 已解锁管理员模式</span>';
      $('adminLockBtn').style.display='inline-flex';
    }else if(window._currentUser&&(window._currentUser.is_admin||window._currentUser.api_key==='demo-master'||window._currentUser.role==='admin')){
      $('adminStatus').innerHTML='<span style="color:var(--success)">✓ 管理员账户已登录</span>';
      $('adminLockBtn').style.display='none';
    }else{
      $('adminStatus').innerHTML='';
      $('adminLockBtn').style.display='none';
    }
  }
}
async function unlockAdmin(){
  const key=$('adminKeyInput').value.trim();
  if(!key){toast('请输入管理员密钥');return;}
  // 用 demo-master 主密钥调用统计接口验证
  try{
    const r=await fetch('/api/admin/cards/stats',{headers:{'Authorization':'Bearer '+key}});
    if(!r.ok){const e=await r.json().catch(()=>({detail:'验证失败'}));toast(e.detail||'管理员密钥无效');return;}
    localStorage.setItem('tg_admin_key',key);
    $('adminKeyInput').value='';
    toast('已解锁管理员模式');
    refreshAdminNav();
    // 跳转到卡密管理
    if($('navCards')){$('navCards').click();}
  }catch(e){toast('网络错误：'+e.message);}
}
function lockAdmin(){
  localStorage.removeItem('tg_admin_key');
  toast('已退出管理员模式');
  refreshAdminNav();
  // 如果当前在卡密页，跳回仪表盘
  if(currentPage==='cards'){navTo('overview');}
}

// ===== 仪表盘 =====
async function loadStats(){
  try{
    const s=await api('/api/stats');
    const cards=[
      {l:'账户余额',v:'$'+s.balance.toFixed(2),d:'可用额度',up:true},
      {l:'API密钥数',v:s.key_count,d:'已创建',up:true},
      {l:'今日请求',v:s.today_requests,d:'今日累计',up:true},
      {l:'今日消费',v:'$'+s.today_cost.toFixed(4),d:'今日累计',up:false},
      {l:'今日Token',v:(s.today_tokens/1000).toFixed(1)+'K',d:'今日累计',up:true},
      {l:'累计Token',v:(s.total_tokens/1000).toFixed(1)+'K',d:'历史总量',up:true},
    ];
    $('statCards').innerHTML=cards.map(c=>`<div class="stat-card">
      <div class="label">${c.l}</div><div class="value">${c.v}</div>
      <div class="delta ${c.up?'up':'down'}">${c.up?'▲':'▼'} ${c.d}</div></div>`).join('');
    $('perfMetrics').innerHTML=`
      <div class="stat-card" style="margin-bottom:10px"><div class="label">RPM（每分钟请求）</div><div class="value">${s.rpm}</div></div>
      <div class="stat-card" style="margin-bottom:10px"><div class="label">TPM（每分钟Token）</div><div class="value">${s.tpm}</div></div>
      <div class="stat-card"><div class="label">平均响应时间</div><div class="value">${s.avg_response_ms} ms</div></div>`;
    $('platformSplit').innerHTML=s.platform_split.length?
      `<table><thead><tr><th>平台</th><th>请求数</th><th>Token</th><th>消费</th></tr></thead><tbody>`+
      s.platform_split.map(p=>`<tr><td>${p.platform}</td><td>${p.requests}</td><td>${p.tokens}</td><td>$${p.cost.toFixed(4)}</td></tr>`).join('')+
      `</tbody></table>`:'<p style="color:var(--text-soft)">暂无数据</p>';
    $('modelDist').innerHTML=s.model_distribution.length?
      `<table><thead><tr><th>模型</th><th>请求数</th><th>Token</th><th>消费</th></tr></thead><tbody>`+
      s.model_distribution.map(m=>`<tr><td class="mono">${m.model}</td><td>${m.requests}</td><td>${m.tokens}</td><td>$${m.cost.toFixed(4)}</td></tr>`).join('')+
      `</tbody></table>`:'<p style="color:var(--text-soft)">暂无数据</p>';
    $('recentUsage').innerHTML=s.recent.length?
      `<table><thead><tr><th>时间</th><th>模型</th><th>Token</th><th>费用</th></tr></thead><tbody>`+
      s.recent.map(r=>`<tr><td>${new Date(r.created_at*1000).toLocaleString('zh-CN')}</td><td class="mono">${r.model}</td><td>${r.total_tokens}</td><td>$${(r.cost||0).toFixed(4)}</td></tr>`).join('')+
      `</tbody></table>`:'<p style="color:var(--text-soft)">暂无记录</p>';
    drawTrend(s.trend);
  }catch(e){toast('加载统计失败：'+e.message);}
}

function drawTrend(trend){
  const cv=$('trendChart');if(!cv||!trend||!trend.length)return;
  const ctx=cv.getContext('2d');
  const W=cv.width,H=cv.height,pad=40;
  ctx.clearRect(0,0,W,H);
  const max=Math.max(...trend.map(t=>t.tokens),1);
  const isDark=document.documentElement.getAttribute('data-theme')==='dark';
  const gridColor=isDark?'#334155':'#e2e8f0';
  const textColor=isDark?'#94a3b8':'#64748b';
  const lineColor=isDark?'#2dd4bf':'#14b8a6';
  // 网格
  ctx.strokeStyle=gridColor;ctx.fillStyle=textColor;ctx.font='11px sans-serif';
  for(let i=0;i<=4;i++){
    const y=pad+(H-pad*2)*i/4;
    ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(W-pad,y);ctx.stroke();
    ctx.fillText(Math.round(max*(1-i/4)),4,y+4);
  }
  // 折线
  const step=(W-pad*2)/(trend.length-1);
  ctx.strokeStyle=lineColor;ctx.lineWidth=2;
  ctx.beginPath();
  trend.forEach((t,i)=>{
    const x=pad+i*step;
    const y=pad+(H-pad*2)*(1-t.tokens/max);
    if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
  });
  ctx.stroke();
  // 填充
  ctx.lineTo(pad+(trend.length-1)*step,H-pad);
  ctx.lineTo(pad,H-pad);
  ctx.closePath();
  ctx.fillStyle=isDark?'rgba(45,212,191,.15)':'rgba(20,184,166,.15)';
  ctx.fill();
  // X轴标签
  ctx.fillStyle=textColor;
  trend.forEach((t,i)=>{
    if(i%2===0){const x=pad+i*step;ctx.fillText(t.date,x-12,H-pad+16);}
  });
}

// ===== API 密钥 =====
function openCreateKey(){openModal('keyModal');}
async function loadKeys(){
  try{
    const keys=await api('/api/tokens');
    $('keysTable').innerHTML=keys.length?`<table><thead><tr><th>名称</th><th>密钥</th><th>额度</th><th>已用</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>`+
      keys.map(k=>`<tr><td>${k.name}</td><td class="mono">${k.id}</td><td>$${k.quota.toFixed(2)}</td><td>$${k.used.toFixed(4)}</td>
        <td><span class="tag ${k.status==='active'?'tag-active':'tag-closed'}">${k.status==='active'?'启用':'停用'}</span></td>
        <td>${k.created_at_str}</td><td><button class="btn btn-danger btn-sm" onclick="delKey('${k.id}')">删除</button>
        <button class="btn btn-outline btn-sm" onclick="copyText('${k.id}')">复制</button></td></tr>`).join('')+
      `</tbody></table>`:'<p style="color:var(--text-soft)">暂无密钥，点击右上角创建</p>';
  }catch(e){toast('加载密钥失败：'+e.message);}
}
async function saveKey(){
  try{
    const body={name:$('keyName').value||'新密钥',quota:parseFloat($('keyQuota').value)||10,
      expire_days:parseInt($('keyExpire').value)||0,allowed_models:$('keyModels').value||'all'};
    await api('/api/tokens',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    closeModal('keyModal');toast('密钥创建成功');loadKeys();
    $('keyName').value='';$('keyQuota').value='10';$('keyExpire').value='0';$('keyModels').value='all';
  }catch(e){toast('创建失败：'+e.message);}
}
async function delKey(id){
  if(!confirm('确认删除此密钥？'))return;
  try{await api('/api/tokens/'+id,{method:'DELETE'});toast('已删除');loadKeys();}catch(e){toast(e.message);}
}

// ===== 使用记录 =====
async function loadUsage(){
  try{
    const days=$('usageDays').value;
    usageData=await api('/api/usage?days='+days);
    usagePage=1;renderUsage();
  }catch(e){toast('加载失败：'+e.message);}
}
function renderUsage(){
  const total=usageData.length;
  const pages=Math.max(1,Math.ceil(total/PAGE_SIZE));
  if(usagePage>pages)usagePage=pages;
  const start=(usagePage-1)*PAGE_SIZE;
  const slice=usageData.slice(start,start+PAGE_SIZE);
  $('usageTable').innerHTML=slice.length?`<table><thead><tr><th>时间</th><th>模型</th><th>平台</th><th>输入Token</th><th>输出Token</th><th>总Token</th><th>费用</th></tr></thead><tbody>`+
    slice.map(r=>`<tr><td>${r.created_at_str}</td><td class="mono">${r.model}</td><td>${r.platform||'-'}</td><td>${r.prompt_tokens}</td><td>${r.completion_tokens}</td><td>${r.total_tokens}</td><td>$${(r.cost||0).toFixed(4)}</td></tr>`).join('')+
    `</tbody></table>`:'<p style="color:var(--text-soft)">暂无记录</p>';
  let pg='';
  for(let i=1;i<=pages;i++)pg+=`<button class="btn ${i===usagePage?'btn-primary':'btn-outline'} btn-sm" onclick="usagePage=${i};renderUsage()">${i}</button>`;
  $('pager').innerHTML=pg;
}

// ===== 渠道 =====
async function loadChannels(){
  try{
    const [chs,prs]=await Promise.all([api('/api/channels'),api('/api/prices')]);
    $('channelsTable').innerHTML=`<table><thead><tr><th>模型</th><th>平台</th><th>渠道</th><th>状态</th><th>输入价</th><th>输出价</th></tr></thead><tbody>`+
      chs.flatMap(ch=>ch.models.map(m=>`<tr><td class="mono">${m}</td><td>${ch.platform}</td><td>${ch.name}</td><td><span class="tag tag-active">在线</span></td>
        <td>$${(DEFAULT_P(m)||0).toFixed(4)}</td><td>$${(DEFAULT_P(m,1)||0).toFixed(4)}</td></tr>`)).join('')+
      `</tbody></table>`;
    $('pricesTable').innerHTML=`<table><thead><tr><th>模型</th><th>平台</th><th>输入价/1K</th><th>输出价/1K</th></tr></thead><tbody>`+
      prs.map(p=>`<tr><td class="mono">${p.model_name}</td><td>${p.platform}</td><td>$${p.input_price.toFixed(4)}</td><td>$${p.output_price.toFixed(4)}</td></tr>`).join('')+
      `</tbody></table>`;
  }catch(e){toast('加载失败：'+e.message);}
}
function DEFAULT_P(m,out){const P={};PRICELIST.forEach(p=>P[p[0]]=[p[1],p[2]]);return P[m]?P[m][out?1:0]:0.002;}
const PRICELIST=[
  ['gpt-5.6',0.005,0.015],['gpt-5.6-sol',0.005,0.015],['gpt-5.6-terra',0.005,0.015],['gpt-5.6-luna',0.005,0.015],
  ['gpt-5.5',0.004,0.012],['gpt-5.4',0.003,0.010],['gpt-5.4-mini',0.001,0.004],
  ['gpt-5.3-codex-spark',0.002,0.008],['codex-auto-review',0.002,0.008],['gpt-5.2',0.002,0.006],
  ['gpt-image-1',0.020,0.020],['gpt-image-1.5',0.025,0.025],['gpt-image-2',0.030,0.030],
  ['claude-fable-5',0.005,0.015],['claude-sonnet-4.6',0.004,0.012],['claude-haiku-4.5',0.001,0.004]];

// ===== 订阅 =====
async function loadSubscription(){
  try{
    const s=await api('/api/subscription');
    $('subCurrent').innerHTML=`<div class="stat-card"><div class="label">当前套餐</div>
      <div class="value">${s.plan}</div><div class="delta up">到期：${s.expire_at} · 剩余 $${s.quota_remain}</div></div>`;
    $('plansList').innerHTML=s.plans.map(p=>`<div class="stat-card">
      <div class="label">${p.name} · ${p.duration}</div><div class="value">¥${p.price}</div>
      <div class="delta">额度 $${p.quota}</div>
      <ul style="margin-top:10px;font-size:13px;color:var(--text-soft)">${p.features.map(f=>`<li>✓ ${f}</li>`).join('')}</ul>
      <button class="btn btn-primary btn-sm" style="margin-top:12px" onclick="buyPlan('${p.name}',${p.price})">购买</button></div>`).join('');
  }catch(e){toast('加载失败：'+e.message);}
}
async function buyPlan(name,price){
  // 订阅也走真实支付流程
  topupAmount=price;
  topupPay='alipay';
  await doTopup('subscription', name);
}

// ===== 充值（真实支付流程：创建订单→显示收款码→上传凭证→等待确认）=====
let topupAmount=50,topupPay='alipay';
document.querySelectorAll('#amountGroup .pill').forEach(p=>p.onclick=()=>{document.querySelectorAll('#amountGroup .pill').forEach(x=>x.classList.remove('active'));p.classList.add('active');topupAmount=parseInt(p.dataset.v);updateWillGet();});
document.querySelectorAll('#payGroup .pill').forEach(p=>p.onclick=()=>{document.querySelectorAll('#payGroup .pill').forEach(x=>x.classList.remove('active'));p.classList.add('active');topupPay=p.dataset.v;});
function updateWillGet(){const v=topupAmount*12;$('willGet').textContent='$'+v.toFixed(2)+' USD';}

async function doTopup(orderType, detail){
  orderType=orderType||'topup';
  detail=detail||topupPay;
  try{
    // 1. 创建订单（返回收款码+唯一金额）
    const o=await api('/api/orders',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type:orderType,amount:topupAmount,detail:detail,payment_method:topupPay})});
    // 2. 弹出支付窗口，显示收款码和需付金额
    showPaymentModal(o);
  }catch(e){toast(e.message);}
}

function showPaymentModal(order){
  // 构建支付弹窗
  const modal=document.createElement('div');
  modal.className='modal-overlay show';
  modal.id='paymentModal';
  modal.style.cssText='position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5);z-index:3000;display:flex;align-items:center;justify-content:center;padding:20px';
  let qrHtml='';
  if(order.qr_code){
    qrHtml=`<div style="text-align:center;margin:16px 0">
      <img src="${order.qr_code}" style="max-width:240px;width:100%;border-radius:12px;border:1px solid #e2e8f0" alt="收款码">
    </div>`;
  }else if(order.account){
    qrHtml=`<div style="text-align:center;margin:16px 0;padding:20px;background:#f8fafc;border-radius:8px">
      <p style="font-size:14px;color:#64748b;margin-bottom:8px">${order.payment_name} 地址</p>
      <p style="font-size:13px;font-family:monospace;word-break:break-all;color:#1e293b">${order.account}</p>
      <button class="btn btn-soft" style="margin-top:8px" onclick="navigator.clipboard.writeText('${order.account}');toast('地址已复制')">复制地址</button>
    </div>`;
  }else{
    qrHtml=`<div style="text-align:center;margin:16px 0;padding:20px;background:#fef3c7;border-radius:8px">
      <p style="font-size:14px;color:#92400e">管理员尚未上传 ${order.payment_name} 收款码，请联系客服获取收款方式。</p>
    </div>`;
  }
  modal.innerHTML=`
    <div style="background:#fff;border-radius:12px;max-width:480px;width:100%;padding:24px;max-height:90vh;overflow-y:auto">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h3 style="font-size:18px">${order.payment_name} 付款</h3>
        <button onclick="document.getElementById('paymentModal').remove()" style="background:none;border:none;font-size:24px;cursor:pointer;color:#64748b">&times;</button>
      </div>
      <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:12px;margin-bottom:16px">
        <p style="font-size:13px;color:#1e40af;margin:0"><b>请支付精确金额（含尾数）：</b></p>
        <p style="font-size:28px;font-weight:700;color:#1e40af;margin:4px 0">¥${order.pay_amount_actual}</p>
        <p style="font-size:12px;color:#3b82f6;margin:0">尾数 ${order.unique_suffix} 用于核对，请勿修改金额</p>
      </div>
      ${qrHtml}
      <p style="font-size:13px;color:#64748b;margin:8px 0">${order.instructions||'请扫码付款后，点击下方按钮上传付款截图'}</p>
      <div style="background:#f1f5f9;border-radius:8px;padding:12px;margin:12px 0">
        <p style="font-size:12px;color:#64748b;margin:0 0 4px">订单号</p>
        <p style="font-size:13px;font-family:monospace;margin:0">${order.id}</p>
        <p style="font-size:12px;color:#64748b;margin:8px 0 4px">支付后获得</p>
        <p style="font-size:14px;font-weight:600;color:#059669;margin:0">$${order.usd_quota} USD 额度</p>
      </div>
      <form id="proofForm" onsubmit="submitProof(event,'${order.id}')" style="margin-top:16px">
        <div style="margin-bottom:12px">
          <label style="font-size:13px;color:#64748b;display:block;margin-bottom:4px">付款截图（必填）</label>
          <input type="file" id="proofScreenshot" accept="image/*" required
            style="width:100%;padding:8px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px">
        </div>
        <div style="margin-bottom:12px">
          <label style="font-size:13px;color:#64748b;display:block;margin-bottom:4px">交易流水号（选填）</label>
          <input type="text" id="proofTxid" placeholder="微信/支付宝交易号"
            style="width:100%;padding:8px 12px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px">
        </div>
        <div style="margin-bottom:16px">
          <label style="font-size:13px;color:#64748b;display:block;margin-bottom:4px">备注（选填）</label>
          <input type="text" id="proofNote" placeholder="如有备注请填写"
            style="width:100%;padding:8px 12px;border:1px solid #e2e8f0;border-radius:6px;font-size:13px">
        </div>
        <button type="submit" class="btn btn-primary" style="width:100%;padding:12px;font-size:15px">
          我已支付，提交凭证
        </button>
      </form>
      <p style="font-size:12px;color:#94a3b8;text-align:center;margin-top:12px">
        提交后等待管理员确认到账，确认后额度自动到账
      </p>
    </div>`;
  document.body.appendChild(modal);
}

async function submitProof(e, orderId){
  e.preventDefault();
  const screenshot=document.getElementById('proofScreenshot').files[0];
  const txid=document.getElementById('proofTxid').value.trim();
  const note=document.getElementById('proofNote').value.trim();
  if(!screenshot){toast('请上传付款截图');return;}
  const btn=e.target.querySelector('button[type=submit]');
  btn.disabled=true;btn.textContent='提交中...';
  try{
    const formData=new FormData();
    formData.append('screenshot',screenshot);
    formData.append('txid',txid);
    formData.append('note',note);
    const token=localStorage.getItem('tg_session');
    const r=await fetch('/api/orders/'+orderId+'/submit-proof',{
      method:'POST',
      headers:{'Authorization':'Bearer '+token},
      body:formData
    });
    const data=await r.json();
    if(!r.ok){toast(data.detail||'提交失败');btn.disabled=false;btn.textContent='我已支付，提交凭证';return;}
    document.getElementById('paymentModal').remove();
    toast('凭证已提交，等待管理员确认到账');
    loadOrders();
  }catch(e){toast('网络错误：'+e.message);btn.disabled=false;btn.textContent='我已支付，提交凭证';}
}

// ===== 订单 =====
async function loadOrders(){
  try{
    const orders=await api('/api/orders');
    $('ordersTable').innerHTML=orders.length?`<table><thead><tr><th>订单号</th><th>类型</th><th>金额</th><th>状态</th><th>时间</th></tr></thead><tbody>`+
      orders.map(o=>`<tr><td class="mono">${o.id}</td><td>${o.type==='topup'?'充值':'订阅'}</td><td>¥${o.amount}</td>
        <td><span class="tag ${o.status==='paid'?'tag-active':'tag-pending'}">${o.status==='paid'?'已支付':'待支付'}</span></td><td>${o.created_at_str}</td></tr>`).join('')+
      `</tbody></table>`:'<p style="color:var(--text-soft)">暂无订单</p>';
  }catch(e){toast('加载失败：'+e.message);}
}

// ===== 兑换 =====
async function doRedeem(){
  try{
    const code=$('redeemCode').value.trim();
    if(!code){toast('请输入兑换码');return;}
    await api('/api/redeem',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code})});
    toast('兑换成功！');$('redeemCode').value='';loadRedemptions();
  }catch(e){toast(e.message);}
}
async function loadRedemptions(){
  try{
    const list=await api('/api/redemptions');
    $('redeemList').innerHTML=list.length?`<table><thead><tr><th>兑换码</th><th>金额</th><th>时间</th></tr></thead><tbody>`+
      list.map(r=>`<tr><td class="mono">${r.code}</td><td>$${r.amount}</td><td>${r.created_at_str}</td></tr>`).join('')+
      `</tbody></table>`:'<p style="color:var(--text-soft)">暂无兑换记录</p>';
  }catch(e){toast(e.message);}
}

// ===== 卡密管理（管理员） =====
let currentCardFilter='';
let lastCardsList=[];
function openGenCardModal(){openModal('cardModal');}
// 管理员 API 调用：优先使用 localStorage 中的管理员密钥
function adminHeaders(){
  const ak=localStorage.getItem('tg_admin_key');
  return ak?{'Authorization':'Bearer '+ak}:{};
}
async function adminApi(path,opts={}){
  opts.headers=opts.headers||{};
  const ak=localStorage.getItem('tg_admin_key');
  if(ak){opts.headers['Authorization']='Bearer '+ak;}
  else{const t=getSession();if(t){opts.headers['Authorization']='Bearer '+t;}}
  const res=await fetch(path,opts);
  if(res.status===401){
    toast('管理员密钥无效，请重新解锁');localStorage.removeItem('tg_admin_key');
    setTimeout(()=>{navTo('profile');},800);
    throw new Error('未授权');
  }
  if(!res.ok){const e=await res.json().catch(()=>({detail:'请求失败'}));throw new Error(e.detail||'请求失败');}
  return res.json();
}
async function loadCardsPage(){
  await Promise.all([loadCardStats(),loadCards(currentCardFilter)]);
}
async function loadCardStats(){
  try{
    const s=await adminApi('/api/admin/cards/stats');
    const cards=[
      {l:'未使用',v:s.unused.count,d:'面值 $'+s.unused.face_value.toFixed(2),c:'var(--success)'},
      {l:'已使用',v:s.used.count,d:'面值 $'+s.used.face_value.toFixed(2),c:'var(--accent)'},
      {l:'已过期',v:s.expired.count,d:'面值 $'+s.expired.face_value.toFixed(2),c:'var(--text-soft)'},
      {l:'总卡密',v:s.total.count,d:'总面值 $'+s.total.face_value.toFixed(2),c:'var(--primary)'},
    ];
    $('cardStats').innerHTML=cards.map(c=>`<div class="stat-card">
      <div class="label">${c.l}</div><div class="value" style="color:${c.c}">${c.v}</div>
      <div class="delta">${c.d}</div></div>`).join('');
  }catch(e){toast('加载统计失败：'+e.message);}
}
async function loadCards(filter){
  currentCardFilter=filter||'';
  try{
    const url='/api/admin/cards'+(currentCardFilter?('?status='+currentCardFilter):'');
    const list=await adminApi(url);
    lastCardsList=list;
    $('cardsTable').innerHTML=list.length?`<table><thead><tr><th>卡密</th><th>面值</th><th>售价</th><th>状态</th><th>使用者</th><th>使用时间</th><th>批次</th><th>操作</th></tr></thead><tbody>`+
      list.map(c=>`<tr>
        <td class="mono" style="font-weight:700;letter-spacing:.5px">${c.id}</td>
        <td>$${Number(c.face_value).toFixed(2)}</td>
        <td>¥${Number(c.price).toFixed(2)}</td>
        <td><span class="tag ${c.status==='unused'?'tag-active':(c.status==='used'?'tag-pending':'tag-closed')}">${c.status==='unused'?'未使用':(c.status==='used'?'已使用':'已过期')}</span></td>
        <td style="font-size:12px">${c.used_by||'-'}</td>
        <td style="font-size:12px">${c.used_at_str||'-'}</td>
        <td class="mono" style="font-size:11px">${c.batch_id||'-'}</td>
        <td><button class="btn btn-outline btn-sm" onclick="copyCardId('${c.id}')">复制</button></td>
      </tr>`).join('')+
      `</tbody></table>`:'<p style="color:var(--text-soft)">暂无卡密，点击右上角生成</p>';
  }catch(e){toast('加载卡密失败：'+e.message);}
}
async function genCards(){
  try{
    const body={
      count:parseInt($('cardCount').value)||10,
      face_value:parseFloat($('cardFace').value)||12,
      price:parseFloat($('cardPrice').value)||1,
    };
    if(body.count<1||body.count>1000){toast('数量须在 1-1000 之间');return;}
    const r=await adminApi('/api/admin/generate-cards',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    closeModal('cardModal');
    // 展示生成结果
    const text=r.cards.map(c=>c.id).join('\\n');
    $('cardResultText').value=text;
    $('cardResultInfo').textContent=`批次 ${r.batch_id} · 共 ${r.count} 张 · 面值 $${body.face_value} · 售价 ¥${body.price}`;
    openModal('cardResultModal');
    toast('已生成 '+r.count+' 张卡密');
  }catch(e){toast('生成失败：'+e.message);}
}
function copyCardId(id){
  const ta=document.createElement('textarea');ta.value=id;
  ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{document.execCommand('copy');}catch(e){}
  document.body.removeChild(ta);
  toast('已复制：'+id);
}
function copyCardResult(){
  const t=$('cardResultText');t.select();
  try{document.execCommand('copy');toast('已复制全部卡密');}catch(e){toast('复制失败');}
}
function exportCards(){
  if(!lastCardsList.length){toast('暂无卡密可导出');return;}
  const text=lastCardsList.map(c=>`${c.id}\\t$${Number(c.face_value).toFixed(2)}\\t¥${Number(c.price).toFixed(2)}\\t${c.status}`).join('\\n');
  const blob=new Blob(['卡密\\t面值\\t售价\\t状态\\n'+text],{type:'text/plain;charset=utf-8'});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');a.href=url;a.download='tokengo-cards-'+Date.now()+'.txt';
  document.body.appendChild(a);a.click();document.body.removeChild(a);
  URL.revokeObjectURL(url);
  toast('已导出 '+lastCardsList.length+' 张卡密');
}

// ===== 邀请返利 =====
async function loadReferral(){
  try{
    const r=await api('/api/referral');
    // 更新邀请链接
    if($('inviteLink')){$('inviteLink').value=r.invite_link;}
    // 渲染统计卡片
    const summary=document.getElementById('referralSummary');
    if(summary){
      summary.innerHTML=`
        <div class="stat-card"><div class="label">已邀请人数</div><div class="value">${r.invitee_count}</div></div>
        <div class="stat-card"><div class="label">累计返利</div><div class="value" style="color:var(--success)">$${Number(r.total_rebate).toFixed(2)}</div></div>
        <div class="stat-card"><div class="label">可提现返利</div><div class="value" style="color:var(--accent)">$${Number(r.available_rebate).toFixed(2)}
          ${r.available_rebate>0?`<button class="btn btn-accent btn-sm" style="margin-left:8px" onclick="withdrawRebate(0)">全部提现到余额</button>`:''}
        </div></div>
        <div class="stat-card"><div class="label">返利比例</div><div class="value">${(r.rebate_rate*100).toFixed(0)}%</div></div>`;
    }
    // 渲染返利流水
    const list=r.rebate_list||[];
    $('rebateList').innerHTML=list.length?`<table><thead><tr><th>好友</th><th>充值金额</th><th>返利</th><th>状态</th><th>时间</th></tr></thead><tbody>`+
      list.map(x=>`<tr><td>${x.invitee}</td><td>¥${x.order_amount}</td>
        <td style="color:var(--success)">+$${Number(x.rebate_amount).toFixed(2)}</td>
        <td><span class="tag ${x.status==='available'?'tag-active':'tag-closed'}">${x.status==='available'?'可提现':x.status==='withdrawn'?'已提现':'已发放'}</span></td>
        <td>${x.created_at_str}</td></tr>`).join('')+
      `</tbody></table>`:'<p style="color:var(--text-soft)">暂无返利记录，快去邀请好友吧！</p>';
  }catch(e){toast('加载返利失败：'+e.message);}
}
async function withdrawRebate(amount){
  try{
    const r=await api('/api/referral/withdraw',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({amount:amount})});
    toast(`已提现 $${Number(r.withdrawn).toFixed(2)} 到余额`);
    loadReferral();
  }catch(e){toast(e.message);}
}
function copyInvite(){
  const inp=$('inviteLink');inp.select();document.execCommand('copy');toast('邀请链接已复制');
}

// ===== 工单 =====
function openCreateTicket(){openModal('ticketModal');}
async function loadTickets(){
  try{
    const list=await api('/api/tickets');
    $('ticketsTable').innerHTML=list.length?`<table><thead><tr><th>工单号</th><th>标题</th><th>状态</th><th>时间</th></tr></thead><tbody>`+
      list.map(t=>`<tr><td class="mono">${t.id}</td><td>${t.title}</td>
        <td><span class="tag ${t.status==='open'?'tag-pending':'tag-closed'}">${t.status==='open'?'处理中':'已关闭'}</span></td><td>${t.created_at_str}</td></tr>`).join('')+
      `</tbody></table>`:'<p style="color:var(--text-soft)">暂无工单</p>';
  }catch(e){toast(e.message);}
}
async function saveTicket(){
  try{
    const body={title:$('ticketTitle').value,content:$('ticketContent').value};
    if(!body.title){toast('请填写标题');return;}
    await api('/api/tickets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    closeModal('ticketModal');toast('工单已提交');loadTickets();
    $('ticketTitle').value='';$('ticketContent').value='';
  }catch(e){toast(e.message);}
}

// ===== 个人资料 =====
async function saveProfile(){
  try{
    const body={
      username:$('pfUsername').value,
      email:$('pfEmailInput').value,
      password:$('pfPassword').value||''
    };
    await api('/api/auth/profile',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    // 更新顶部显示
    $('pfUser').textContent=body.username||$('pfEmail').textContent;
    $('pfEmail').textContent=body.email||$('pfEmail').textContent;
    $('pfPassword').value='';
    toast('资料已保存');
    // 刷新顶部用户区
    if(typeof checkLoginAndRenderUser==='function'){checkLoginAndRenderUser();}
  }catch(e){toast(e.message);}
}

// 通用复制
function copyText(t){const i=document.createElement('input');i.value=t;document.body.appendChild(i);i.select();document.execCommand('copy');document.body.removeChild(i);toast('已复制');}

// 初始化：先校验登录，再加载首页数据
(async()=>{
  await checkLoginAndRenderUser();
  if(getSession()){loadStats();}
})();
</script>
</body>
</html>"""


# ============================================================================
# HTML：帮助页面
# ============================================================================

HELP_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>使用教程 - TokenGo</title>
<style>
:root{
  --primary:#14b8a6; --primary-dark:#0d9488; --accent:#f59e0b;
  --bg:#ffffff; --bg-soft:#f8fafc; --card:#ffffff; --text:#0f172a; --text-soft:#64748b;
  --border:#e2e8f0; --code-bg:#1e293b; --code-header:#334155;
}
[data-theme="dark"]{
  --primary:#2dd4bf; --primary-dark:#14b8a6; --accent:#fbbf24;
  --bg:#0f172a; --bg-soft:#1e293b; --card:#1e293b; --text:#e2e8f0; --text-soft:#94a3b8;
  --border:#334155; --code-bg:#020617; --code-header:#1e293b;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--text);line-height:1.7;transition:background .3s,color .3s}
a{color:var(--primary);text-decoration:none}
a:hover{text-decoration:underline}
.nav{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--border)}
[data-theme="dark"] .nav{background:rgba(15,23,42,.92)}
.nav-inner{max-width:1400px;margin:0 auto;padding:0 20px;height:64px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.logo{font-size:20px;font-weight:800;color:var(--primary);white-space:nowrap}
.nav-right{display:flex;gap:10px;align-items:center;flex:1;justify-content:flex-end}
.search-wrap{position:relative;flex:1;max-width:380px}
.search-input{width:100%;padding:.5625rem .875rem .5625rem 2.25rem;border-radius:.75rem;border:1px solid var(--border);background:var(--bg-soft);color:var(--text);font-size:.875rem;outline:none;transition:border-color .2s,box-shadow .2s}
.search-input:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(20,184,166,.15)}
.search-icon{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--text-soft);font-size:13px;pointer-events:none}
.btn{padding:.5rem .875rem;border-radius:.75rem;font-size:.875rem;font-weight:500;cursor:pointer;border:none;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:.5rem;white-space:nowrap;transition:all .2s}
.btn:hover{text-decoration:none;transform:translateY(-1px)}
.btn-primary{background:linear-gradient(to right,#14b8a6,#0d9488);color:#fff;box-shadow:0 4px 6px -1px rgba(20,184,166,.25)}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-outline:hover{border-color:var(--primary);color:var(--primary);background:rgba(20,184,166,.08)}
.icon-btn{background:transparent;border:1px solid var(--border);width:36px;height:36px;border-radius:.75rem;cursor:pointer;font-size:15px;display:inline-flex;align-items:center;justify-content:center;transition:all .2s}
.icon-btn:hover{border-color:var(--primary);color:var(--primary)}
.menu-toggle{display:none}
.layout{display:flex;max-width:1400px;margin:0 auto;align-items:flex-start}
.sidebar{width:260px;flex-shrink:0;position:sticky;top:64px;height:calc(100vh - 64px);overflow-y:auto;padding:24px 12px 24px 20px;border-right:1px solid var(--border)}
.sidebar-title{font-size:12px;color:var(--text-soft);text-transform:uppercase;letter-spacing:1.5px;margin:0 0 12px 4px;font-weight:700}
.toc{list-style:none;margin:0;padding:0}
.toc li{margin:0}
.toc a{display:block;padding:8px 12px;border-radius:6px;color:var(--text-soft);font-size:13.5px;text-decoration:none!important;transition:all .2s;border-left:2px solid transparent;line-height:1.4}
.toc a:hover{background:var(--bg-soft);color:var(--text)}
.toc a.active{background:rgba(20,184,166,.1);color:var(--primary);border-left-color:var(--primary);font-weight:600}
.content{flex:1;min-width:0;padding:40px 48px 60px}
h1{font-size:34px;margin-bottom:8px}
.subtitle{color:var(--text-soft);margin-bottom:32px;font-size:16px}
.section{scroll-margin-top:80px;margin-bottom:8px}
.section.hidden{display:none}
h2{font-size:24px;margin:40px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--primary);scroll-margin-top:80px}
h3{font-size:18px;margin:22px 0 10px;scroll-margin-top:80px}
h4{font-size:15px;margin:16px 0 8px;color:var(--text)}
p{margin-bottom:12px}
code{background:var(--bg-soft);padding:2px 6px;border-radius:4px;font-family:Consolas,monospace;font-size:13px;color:var(--accent);word-break:break-all}
pre{background:var(--code-bg);color:#e2e8f0;padding:16px;border-radius:10px;overflow-x:auto;margin:12px 0;font-size:13px}
pre code{background:transparent;color:inherit;padding:0;word-break:normal}
.code-block{position:relative;margin:14px 0}
.code-block pre{margin:0;padding-top:44px}
.code-header{position:absolute;top:0;left:0;right:0;height:32px;background:var(--code-header);padding:6px 12px;border-radius:10px 10px 0 0;display:flex;align-items:center;justify-content:space-between;font-size:12px;color:#94a3b8;font-family:Consolas,monospace}
.copy-btn{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);color:#e2e8f0;padding:4px 10px;border-radius:6px;font-size:12px;cursor:pointer;transition:all .2s;font-family:inherit}
.copy-btn:hover{background:rgba(255,255,255,.18)}
.copy-btn.copied{background:var(--primary);border-color:var(--primary);color:#fff}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin:16px 0}
.callout{background:rgba(20,184,166,.08);border-left:4px solid var(--primary);padding:14px 18px;border-radius:8px;margin:16px 0}
.warn{background:rgba(245,158,11,.1);border-left:4px solid var(--accent);padding:14px 18px;border-radius:8px;margin:16px 0}
ul,ol{margin:8px 0 12px 24px}
li{margin-bottom:6px}
table{width:100%;border-collapse:collapse;margin:14px 0;font-size:13.5px;display:block;overflow-x:auto}
th,td{padding:10px 12px;border:1px solid var(--border);text-align:left;white-space:nowrap}
th{background:var(--bg-soft);font-weight:600;color:var(--text)}
tbody tr:nth-child(even){background:var(--bg-soft)}
.no-results{text-align:center;padding:60px 20px;color:var(--text-soft);display:none}
.no-results.show{display:block}
footer{text-align:center;padding:32px;color:var(--text-soft);border-top:1px solid var(--border);margin-top:40px}
@media (max-width:900px){
  .nav-inner{padding:0 14px}
  .logo{font-size:18px}
  .search-wrap{max-width:none}
  .btn-outline{display:none}
  .menu-toggle{display:inline-flex}
  .sidebar{position:fixed;left:-280px;top:64px;height:calc(100vh - 64px);background:var(--bg);z-index:40;transition:left .3s;box-shadow:2px 0 16px rgba(0,0,0,.15);width:260px}
  .sidebar.open{left:0}
  .content{padding:24px 18px 40px}
  .nav-right{gap:8px}
}
</style>
</head>
<body>
<nav class="nav">
  <div class="nav-inner">
    <a href="/" class="logo">🚀 TokenGo</a>
    <div class="nav-right">
      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input type="text" id="searchInput" class="search-input" placeholder="搜索章节…" autocomplete="off">
      </div>
      <a href="/" class="btn btn-outline">首页</a>
      <a href="/dashboard" class="btn btn-outline">控制台</a>
      <button class="icon-btn menu-toggle" onclick="toggleMenu()" id="menuBtn" title="目录">☰</button>
    </div>
  </div>
</nav>

<div class="layout">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-title">导航目录</div>
    <ul class="toc">
      <li><a href="#quick-start">1. 快速开始</a></li>
      <li><a href="#preparation">2. 准备工作</a></li>
      <li><a href="#install-claude-code">3. 安装 Claude Code（终端版）</a></li>
      <li><a href="#config-claude-code">4. 配置 Claude Code 使用中转</a></li>
      <li><a href="#install-vscode">5. 安装 VS Code + 插件</a></li>
      <li><a href="#config-vscode">6. 配置 VS Code 插件</a></li>
      <li><a href="#openclaw">7. 安装配置 OpenClaw</a></li>
      <li><a href="#opencode">8. Open Code</a></li>
      <li><a href="#hermes">9. Hermes Agent</a></li>
      <li><a href="#cursor">10. Cursor</a></li>
      <li><a href="#billing">11. 计费说明</a></li>
      <li><a href="#codex">12. CodeX 使用教程</a></li>
      <li><a href="#image-gen">13. 图像生成（gpt-image-2）</a></li>
      <li><a href="#video-gen">14. 视频生成（Seedance）</a></li>
      <li><a href="#faq">15. 常见问题</a></li>
    </ul>
  </aside>

  <main class="content">
    <h1>使用教程</h1>
    <p class="subtitle">从零开始，全面接入 TokenGo AI API 中转服务，支持 Claude Code / VS Code / OpenClaw / Cursor / CodeX 等多种客户端。</p>

    <div class="callout">
      <b>Base URL：</b><code>__PUBLIC_BASE_URL__/v1</code><br>
      <b>协议兼容：</b>OpenAI / Anthropic 完全兼容<br>
      <b>汇率：</b>¥1 RMB = $12 USD 美金额度
    </div>

    <section class="section" id="quick-start">
      <h2>🚀 1. 快速开始</h2>
      <p>TokenGo 是一个 AI API 中转服务，兼容 OpenAI / Anthropic 协议，让你以更低成本使用 GPT、Claude 等顶级模型。下面 5 步即可完成接入。</p>

      <h3>步骤 1：访问网站并注册</h3>
      <ol>
        <li>打开 <a href="__PUBLIC_BASE_URL__">__PUBLIC_BASE_URL__</a> 进入首页</li>
        <li>点击右上角「注册」，使用邮箱或手机号注册账户</li>
        <li>登录后进入<a href="__PUBLIC_BASE_URL__/dashboard">控制台</a></li>
      </ol>

      <h3>步骤 2：充值额度</h3>
      <p>在控制台「充值/订阅」页面选择金额并支付。</p>
      <div class="callout">
        <b>汇率：</b>¥1 RMB = $12 USD 美金额度，远低于官方价格。<br>
        <b>支付方式：</b>支付宝、微信、USDT (TRC20/ERC20)、兑换码。
      </div>

      <h3>步骤 3：创建 API 密钥</h3>
      <ol>
        <li>进入「API 密钥」页面，点击「创建 API 密钥」</li>
        <li>设置名称、额度和有效期</li>
        <li>点击「创建」后复制生成的密钥（格式：<code>sk-tg-xxxxxxxx</code>）</li>
      </ol>
      <div class="warn"><b>⚠️ 注意：</b>密钥仅显示一次，请妥善保存，泄露后请立即在控制台重置。</div>

      <h3>步骤 4：配置客户端</h3>
      <p>根据你使用的客户端，参考下方对应章节进行配置。最常用的两种：</p>
      <ul>
        <li><b>Claude Code（终端）：</b>设置环境变量 <code>ANTHROPIC_BASE_URL=__PUBLIC_BASE_URL__</code> 和 <code>ANTHROPIC_AUTH_TOKEN=sk-tg-你的密钥</code></li>
        <li><b>OpenAI SDK：</b>设置 <code>base_url="__PUBLIC_BASE_URL__/v1"</code> 和 <code>api_key="sk-tg-你的密钥"</code></li>
      </ul>

      <h3>步骤 5：测试调用</h3>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>curl __PUBLIC_BASE_URL__/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer sk-tg-你的密钥" \\
  -d '{
    "model": "gpt-5.6",
    "messages": [{"role": "user", "content": "你好"}]
  }'</code></pre>
      </div>
      <p>如果返回正常 JSON 响应，说明接入成功！🎉</p>
    </section>

    <section class="section" id="preparation">
      <h2>🛠️ 2. 准备工作</h2>
      <p>使用 Claude Code 及相关 AI 客户端前，需要先安装 Node.js 运行环境（建议 v18 或以上）。</p>

      <h3>macOS</h3>
      <p>推荐使用 Homebrew 安装：</p>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code># 安装 Homebrew（如已安装可跳过）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 安装 Node.js
brew install node

# 验证
node -v
npm -v</code></pre>
      </div>

      <h3>Linux</h3>
      <p>使用 NodeSource 官方源安装：</p>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code># Ubuntu / Debian
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# 验证
node -v
npm -v</code></pre>
      </div>

      <h3>Windows</h3>
      <p>方法一：使用官方安装包（推荐）</p>
      <ol>
        <li>访问 <a href="https://nodejs.org">https://nodejs.org</a> 下载 LTS 安装包</li>
        <li>双击运行安装程序，全部默认下一步即可</li>
        <li>打开 PowerShell 验证：<code>node -v</code> 和 <code>npm -v</code></li>
      </ol>
      <p>方法二：使用 winget 安装：</p>
      <div class="code-block">
        <div class="code-header"><span>powershell</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>winget install OpenJS.NodeJS.LTS</code></pre>
      </div>

      <div class="callout"><b>💡 提示：</b>建议安装 Node.js 18 及以上版本，旧版本可能导致 Claude Code 无法正常运行。</div>
    </section>

    <section class="section" id="install-claude-code">
      <h2>📦 3. 安装 Claude Code（终端版）</h2>
      <p>Claude Code 是 Anthropic 官方的命令行 AI 编程助手，通过 TokenGo 中转后可使用折扣价格调用 Claude 模型。</p>

      <h3>macOS / Linux / WSL2</h3>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>npm install -g @anthropic-ai/claude-code

# 验证安装
claude --version</code></pre>
      </div>

      <h3>Windows（原生）</h3>
      <div class="code-block">
        <div class="code-header"><span>powershell</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>npm install -g @anthropic-ai/claude-code

# 验证安装
claude --version</code></pre>
      </div>

      <div class="warn">
        <b>⚠️ Windows 用户注意：</b>
        <ul>
          <li>Windows 原生环境支持有限，部分功能可能异常，推荐使用 <a href="https://learn.microsoft.com/windows/wsl/">WSL2</a></li>
          <li>如在 PowerShell 中遇到执行策略错误，运行：<code>Set-ExecutionPolicy -Scope CurrentUser RemoteSigned</code></li>
          <li>首次启动：<code>claude</code> 命令进入交互式界面</li>
        </ul>
      </div>
    </section>

    <section class="section" id="config-claude-code">
      <h2>⚙️ 4. 配置 Claude Code 使用中转服务</h2>
      <p>安装完成后，需要将 Claude Code 指向 TokenGo 中转地址。提供三种配置方法，任选其一即可。</p>

      <h3>方法 1：环境变量配置（推荐）</h3>
      <p>macOS / Linux：</p>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code># 添加到 ~/.zshrc 或 ~/.bashrc
export ANTHROPIC_BASE_URL="__PUBLIC_BASE_URL__"
export ANTHROPIC_AUTH_TOKEN="sk-tg-你的密钥"

# 立即生效
source ~/.zshrc

# 启动 Claude Code
claude</code></pre>
      </div>
      <p>Windows PowerShell：</p>
      <div class="code-block">
        <div class="code-header"><span>powershell</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code># 当前用户永久设置
[Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", "__PUBLIC_BASE_URL__", "User")
[Environment]::SetEnvironmentVariable("ANTHROPIC_AUTH_TOKEN", "sk-tg-你的密钥", "User")

# 重启 PowerShell 后生效
claude</code></pre>
      </div>

      <h3>方法 2：settings.json 配置</h3>
      <p>编辑 Claude Code 配置文件（<code>~/.claude/settings.json</code>）：</p>
      <div class="code-block">
        <div class="code-header"><span>json</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>{
  "env": {
    "ANTHROPIC_BASE_URL": "__PUBLIC_BASE_URL__",
    "ANTHROPIC_AUTH_TOKEN": "sk-tg-你的密钥"
  },
  "model": "claude-sonnet-4-5",
  "theme": "dark"
}</code></pre>
      </div>

      <h3>方法 3：使用 CC Switch 工具</h3>
      <p>CC Switch 是一个图形化配置切换工具，可在多个 Claude Code 配置间快速切换。</p>
      <ol>
        <li>从 <a href="https://github.com/farion1231/cc-switch">GitHub 下载 CC Switch</a></li>
        <li>启动后点击「添加配置」</li>
        <li>名称填 <code>TokenGo</code></li>
        <li>Base URL 填 <code>__PUBLIC_BASE_URL__</code></li>
        <li>API Key 填 <code>sk-tg-你的密钥</code></li>
        <li>点击保存并切换到该配置</li>
      </ol>

      <div class="callout"><b>✅ 验证：</b>运行 <code>claude</code> 后输入任意问题，能正常回复即说明配置成功。</div>
    </section>

    <section class="section" id="install-vscode">
      <h2>💻 5. 安装 VS Code + Claude Code 插件</h2>

      <h3>安装 VS Code</h3>
      <ol>
        <li>访问 <a href="https://code.visualstudio.com">https://code.visualstudio.com</a></li>
        <li>下载对应操作系统的安装包并安装</li>
        <li>启动 VS Code</li>
      </ol>

      <h3>安装 Claude Code 插件</h3>
      <ol>
        <li>打开 VS Code</li>
        <li>点击左侧扩展图标（或 <code>Ctrl+Shift+X</code> / <code>Cmd+Shift+X</code>）</li>
        <li>搜索 <code>Claude Code</code></li>
        <li>点击「Install」安装 Anthropic 官方插件</li>
        <li>安装完成后，左侧出现 Claude Code 图标</li>
      </ol>
      <div class="callout"><b>💡 提示：</b>如搜索不到，可访问 <a href="https://marketplace.visualstudio.com/items?itemName=anthropic.claude-code">VS Code Marketplace</a> 直接安装。</div>
    </section>

    <section class="section" id="config-vscode">
      <h2>🔧 6. 配置 VS Code 中的 Claude Code 插件</h2>
      <p>插件安装完成后，需要在 VS Code 的 settings.json 中配置中转地址。</p>

      <h3>打开 settings.json</h3>
      <ol>
        <li>按 <code>Ctrl+Shift+P</code>（macOS 为 <code>Cmd+Shift+P</code>）打开命令面板</li>
        <li>输入 <code>Preferences: Open User Settings (JSON)</code></li>
        <li>选择「Open User Settings (JSON)」</li>
      </ol>

      <h3>添加配置</h3>
      <div class="code-block">
        <div class="code-header"><span>json</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>{
  "claude-code.baseUrl": "__PUBLIC_BASE_URL__",
  "claude-code.apiKey": "sk-tg-你的密钥",
  "claude-code.model": "claude-sonnet-4-5"
}</code></pre>
      </div>

      <div class="warn"><b>⚠️ 注意：</b>如果 settings.json 已有内容，请将上述字段合并到现有 JSON 对象中，不要直接覆盖整个文件。</div>

      <h3>验证配置</h3>
      <ol>
        <li>保存 settings.json</li>
        <li>点击左侧 Claude Code 图标打开侧边栏</li>
        <li>输入任意问题测试是否能正常对话</li>
      </ol>
    </section>

    <section class="section" id="openclaw">
      <h2>🦅 7. 安装配置 OpenClaw</h2>
      <p>OpenClaw 是开源的 Claude Code 替代客户端，提供更灵活的自定义能力。</p>

      <h3>安装 OpenClaw</h3>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>npm install -g openclaw

# 验证
openclaw --version</code></pre>
      </div>

      <h3>方法 1：配置向导（推荐新手）</h3>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>openclaw init

# 按提示输入：
# ? Select provider: Custom / Anthropic-compatible
# ? Base URL: __PUBLIC_BASE_URL__
# ? API Key: sk-tg-你的密钥
# ? Default model: claude-sonnet-4-5</code></pre>
      </div>

      <h3>方法 2：手动配置</h3>
      <p>编辑 <code>~/.openclaw/config.json</code>：</p>
      <div class="code-block">
        <div class="code-header"><span>json</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>{
  "provider": "anthropic",
  "baseUrl": "__PUBLIC_BASE_URL__",
  "apiKey": "sk-tg-你的密钥",
  "model": "claude-sonnet-4-5",
  "maxTokens": 8192
}</code></pre>
      </div>

      <h3>方法 3：环境变量</h3>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>export OPENCLAW_BASE_URL="__PUBLIC_BASE_URL__"
export OPENCLAW_API_KEY="sk-tg-你的密钥"

openclaw chat</code></pre>
      </div>
    </section>

    <section class="section" id="opencode">
      <h2>🐙 8. Open Code</h2>
      <p>Open Code 是另一款开源 AI 编程助手，通过 <code>opencode.json</code> 进行配置。</p>

      <p>在项目根目录或 <code>~/.config/opencode/opencode.json</code> 创建配置文件：</p>
      <div class="code-block">
        <div class="code-header"><span>json</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>{
  "provider": "openai-compatible",
  "baseUrl": "__PUBLIC_BASE_URL__/v1",
  "apiKey": "sk-tg-你的密钥",
  "model": "claude-sonnet-4-5",
  "models": {
    "claude-sonnet-4-5": {
      "name": "Claude Sonnet 4.5",
      "contextLength": 200000
    },
    "gpt-5.6": {
      "name": "GPT-5.6",
      "contextLength": 128000
    }
  }
}</code></pre>
      </div>

      <div class="callout"><b>💡 提示：</b>使用 Claude 模型时 baseUrl 用 <code>__PUBLIC_BASE_URL__/v1</code>（OpenAI 兼容协议），TokenGo 会自动转换。</div>
    </section>

    <section class="section" id="hermes">
      <h2>⚡ 9. Hermes Agent</h2>
      <p>Hermes Agent 是面向团队的 AI Agent 框架，支持三种方式接入 TokenGo。</p>

      <h3>方法 1：配置文件方式</h3>
      <p>编辑 <code>~/.hermes/config.yaml</code>：</p>
      <div class="code-block">
        <div class="code-header"><span>yaml</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>provider:
  type: openai
  base_url: __PUBLIC_BASE_URL__/v1
  api_key: sk-tg-你的密钥
  model: claude-sonnet-4-5

agent:
  max_tokens: 8192
  temperature: 0.7</code></pre>
      </div>

      <h3>方法 2：命令行参数</h3>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>hermes run \\
  --base-url __PUBLIC_BASE_URL__/v1 \\
  --api-key sk-tg-你的密钥 \\
  --model claude-sonnet-4-5 \\
  --task "实现一个登录页面"</code></pre>
      </div>

      <h3>方法 3：SDK 调用</h3>
      <div class="code-block">
        <div class="code-header"><span>python</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>from hermes import Hermes

client = Hermes(
    base_url="__PUBLIC_BASE_URL__/v1",
    api_key="sk-tg-你的密钥",
    model="claude-sonnet-4-5"
)

result = client.run(task="重构这段代码", context="./src")
print(result.output)</code></pre>
      </div>
    </section>

    <section class="section" id="cursor">
      <h2>🖱️ 10. Cursor</h2>
      <p>Cursor 可通过自定义 OpenAI Base URL 接入 TokenGo。在「Settings → Models」中配置：</p>
      <ul>
        <li>OpenAI Base URL：<code>__PUBLIC_BASE_URL__/v1</code></li>
        <li>API Key：<code>sk-tg-你的密钥</code></li>
      </ul>

      <h3>自定义模型名称对照表</h3>
      <p>在 Cursor 的「Add Model」中按下表添加自定义模型，名称必须完全一致：</p>
      <div class="card">
        <table>
          <thead>
            <tr><th>显示名称</th><th>模型 ID（实际请求）</th><th>用途</th></tr>
          </thead>
          <tbody>
            <tr><td>Claude Sonnet 4.5</td><td><code>claude-sonnet-4-5</code></td><td>主力编程</td></tr>
            <tr><td>Claude Fable 5</td><td><code>claude-fable-5</code></td><td>深度推理</td></tr>
            <tr><td>Claude Haiku 4.5</td><td><code>claude-haiku-4-5</code></td><td>快速响应</td></tr>
            <tr><td>GPT-5.6</td><td><code>gpt-5.6</code></td><td>通用对话</td></tr>
            <tr><td>GPT-5.6-Sol</td><td><code>gpt-5.6-sol</code></td><td>复杂任务</td></tr>
            <tr><td>GPT-5.4-Mini</td><td><code>gpt-5.4-mini</code></td><td>低成本快速</td></tr>
            <tr><td>Codex Auto Review</td><td><code>codex-auto-review</code></td><td>代码审查</td></tr>
          </tbody>
        </table>
      </div>
      <div class="warn"><b>⚠️ 重要：</b>开启「Override OpenAI Base URL」开关，否则 Cursor 会忽略自定义地址。</div>
    </section>

    <section class="section" id="billing">
      <h2>💰 11. 计费说明</h2>

      <h3>计费方式</h3>
      <ul>
        <li><b>按量计费：</b>根据实际 Token 用量计费，1 Token ≈ 0.75 个英文字符</li>
        <li><b>汇率：</b>¥1 RMB = $12 USD 美金额度</li>
        <li><b>价格倍率：</b>约为官方价格的 1/12</li>
        <li><b>明细查询：</b>控制台「使用记录」实时查看每次调用扣费</li>
      </ul>

      <h3>Claude 模型价格表</h3>
      <div class="card">
        <table>
          <thead>
            <tr><th>模型</th><th>输入 ($/1M)</th><th>输出 ($/1M)</th><th>本站输入 (¥/1M)</th><th>本站输出 (¥/1M)</th></tr>
          </thead>
          <tbody>
            <tr><td>claude-sonnet-4-5</td><td>$3.00</td><td>$15.00</td><td>¥0.25</td><td>¥1.25</td></tr>
            <tr><td>claude-fable-5</td><td>$5.00</td><td>$25.00</td><td>¥0.42</td><td>¥2.08</td></tr>
            <tr><td>claude-haiku-4-5</td><td>$1.00</td><td>$5.00</td><td>¥0.083</td><td>¥0.42</td></tr>
            <tr><td>claude-opus-4-1</td><td>$15.00</td><td>$75.00</td><td>¥1.25</td><td>¥6.25</td></tr>
          </tbody>
        </table>
      </div>
      <p><small>* 价格仅供参考，以控制台实时价格为准。本站价格按 ¥1 = $12 折算。</small></p>

      <h3>日均费用参考</h3>
      <div class="card">
        <table>
          <thead>
            <tr><th>使用场景</th><th>日均 Token 量</th><th>官方日均</th><th>本站日均</th></tr>
          </thead>
          <tbody>
            <tr><td>轻度使用（问答）</td><td>10 万</td><td>$0.50</td><td>¥0.10</td></tr>
            <tr><td>日常编程</td><td>50 万</td><td>$2.50</td><td>¥0.50</td></tr>
            <tr><td>重度编程</td><td>200 万</td><td>$10.00</td><td>¥2.00</td></tr>
            <tr><td>团队协作</td><td>1000 万</td><td>$50.00</td><td>¥10.00</td></tr>
          </tbody>
        </table>
      </div>

      <h3>省钱技巧</h3>
      <div class="callout">
        <ul>
          <li><b>模型选择：</b>日常对话用 <code>claude-haiku-4-5</code>，编程用 <code>claude-sonnet-4-5</code>，仅在复杂推理时用 <code>claude-fable-5</code></li>
          <li><b>开启缓存：</b>Claude Code 自动支持 Prompt Cache，重复上下文按 1/10 价格计费</li>
          <li><b>充值返利：</b>大额充值享额外赠送，邀请好友返利 15%</li>
          <li><b>使用兑换码：</b>关注活动可获取兑换码抵扣额度</li>
          <li><b>控制上下文：</b>及时清理长会话，避免无效 Token 消耗</li>
        </ul>
      </div>
    </section>

    <section class="section" id="codex">
      <h2>📝 12. CodeX 使用教程</h2>
      <p>CodeX 是 OpenAI 的代码生成客户端，通过 TokenGo 中转可使用 GPT 系列。</p>

      <h3>config.toml 配置</h3>
      <p>编辑 <code>~/.codex/config.toml</code>：</p>
      <div class="code-block">
        <div class="code-header"><span>toml</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>[model]
name = "gpt-5.6"
base_url = "__PUBLIC_BASE_URL__/v1"
provider = "openai"

[request]
max_tokens = 8192
temperature = 0.5</code></pre>
      </div>

      <h3>auth.json 配置</h3>
      <p>编辑 <code>~/.codex/auth.json</code>：</p>
      <div class="code-block">
        <div class="code-header"><span>json</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>{
  "api_key": "sk-tg-你的密钥",
  "base_url": "__PUBLIC_BASE_URL__/v1"
}</code></pre>
      </div>

      <div class="callout"><b>✅ 验证：</b>运行 <code>codex "写一个冒泡排序"</code>，正常输出代码即配置成功。</div>
    </section>

    <section class="section" id="image-gen">
      <h2>🎨 13. 图像生成（gpt-image-2）</h2>
      <p>TokenGo 提供 <code>gpt-image-2</code> 图像生成接口，支持文生图和图改图。</p>

      <h3>注意事项</h3>
      <div class="warn">
        <ul>
          <li>图像生成耗时较长（10-60 秒），请合理设置超时</li>
          <li>单次调用费用较高，建议先小图测试再批量生成</li>
          <li>支持 PNG / JPG / WebP 输出格式</li>
          <li>建议使用异步方式调用（见下文示例）</li>
        </ul>
      </div>

      <h3>超时设置</h3>
      <p>由于图像生成较慢，HTTP 客户端需设置较长超时：</p>
      <div class="code-block">
        <div class="code-header"><span>python</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>import requests
resp = requests.post(
    "__PUBLIC_BASE_URL__/v1/images/generations",
    headers={"Authorization": "Bearer sk-tg-你的密钥"},
    json={"model": "gpt-image-2", "prompt": "一只可爱的猫咪"},
    timeout=120  # 秒
)</code></pre>
      </div>

      <h3>curl 文生图</h3>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>curl __PUBLIC_BASE_URL__/v1/images/generations \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer sk-tg-你的密钥" \\
  -d '{
    "model": "gpt-image-2",
    "prompt": "赛博朋克风格的城市夜景，霓虹灯闪烁",
    "n": 1,
    "size": "1024x1024"
  }'</code></pre>
      </div>

      <h3>curl 图改图</h3>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>curl __PUBLIC_BASE_URL__/v1/images/edits \\
  -H "Authorization: Bearer sk-tg-你的密钥" \\
  -F model="gpt-image-2" \\
  -F prompt="把背景换成日落" \\
  -F image="@input.png"</code></pre>
      </div>

      <h3>Python SDK</h3>
      <div class="code-block">
        <div class="code-header"><span>python</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>from openai import OpenAI

client = OpenAI(
    api_key="sk-tg-你的密钥",
    base_url="__PUBLIC_BASE_URL__/v1"
)

# 文生图
result = client.images.generate(
    model="gpt-image-2",
    prompt="油画风格的山水风景",
    size="1024x1024",
    n=1
)
print(result.data[0].url)

# 图改图
edit = client.images.edit(
    model="gpt-image-2",
    image=open("input.png", "rb"),
    prompt="把天空换成星空"
)
print(edit.data[0].url)</code></pre>
      </div>

      <h3>异步生图</h3>
      <div class="code-block">
        <div class="code-header"><span>python</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code>import asyncio
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key="sk-tg-你的密钥",
    base_url="__PUBLIC_BASE_URL__/v1"
)

async def generate():
    result = await client.images.generate(
        model="gpt-image-2",
        prompt="未来主义太空站",
        size="1024x1024"
    )
    return result.data[0].url

url = asyncio.run(generate())
print(url)</code></pre>
      </div>
    </section>

    <section class="section" id="video-gen">
      <h2>🎬 14. 视频生成（Seedance）</h2>
      <p>Seedance 是 TokenGo 提供的 AI 视频生成接口，支持文生视频和图生视频。</p>

      <h3>支持的模型</h3>
      <div class="card">
        <table>
          <thead><tr><th>模型</th><th>分辨率</th><th>时长</th><th>单价</th></tr></thead>
          <tbody>
            <tr><td><code>seedance-1.0</code></td><td>720p</td><td>5 秒</td><td>¥0.5/次</td></tr>
            <tr><td><code>seedance-1.0-pro</code></td><td>1080p</td><td>10 秒</td><td>¥1.5/次</td></tr>
            <tr><td><code>seedance-1.0-lite</code></td><td>540p</td><td>5 秒</td><td>¥0.2/次</td></tr>
          </tbody>
        </table>
      </div>

      <h3>接口流程</h3>
      <ol>
        <li><b>提交任务：</b>POST <code>/v1/videos/generations</code> 返回 task_id</li>
        <li><b>轮询状态：</b>GET <code>/v1/videos/tasks/{task_id}</code></li>
        <li><b>获取结果：</b>status 为 <code>succeeded</code> 时返回视频 URL</li>
      </ol>

      <h3>参数说明</h3>
      <div class="card">
        <table>
          <thead><tr><th>参数</th><th>类型</th><th>必填</th><th>说明</th></tr></thead>
          <tbody>
            <tr><td><code>model</code></td><td>string</td><td>是</td><td>模型 ID</td></tr>
            <tr><td><code>prompt</code></td><td>string</td><td>是</td><td>视频描述</td></tr>
            <tr><td><code>image</code></td><td>string</td><td>否</td><td>图生视频的参考图 URL</td></tr>
            <tr><td><code>duration</code></td><td>int</td><td>否</td><td>时长（秒），默认 5</td></tr>
            <tr><td><code>resolution</code></td><td>string</td><td>否</td><td>分辨率，默认 720p</td></tr>
            <tr><td><code>fps</code></td><td>int</td><td>否</td><td>帧率，默认 30</td></tr>
          </tbody>
        </table>
      </div>

      <h3>调用示例</h3>
      <div class="code-block">
        <div class="code-header"><span>bash</span><button class="copy-btn" type="button">复制</button></div>
        <pre><code># 1. 提交任务
curl __PUBLIC_BASE_URL__/v1/videos/generations \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer sk-tg-你的密钥" \\
  -d '{
    "model": "seedance-1.0",
    "prompt": "一只小猫在草地上奔跑，阳光明媚",
    "duration": 5,
    "resolution": "720p"
  }'

# 返回：{"task_id": "task_abc123", "status": "pending"}

# 2. 查询状态
curl __PUBLIC_BASE_URL__/v1/videos/tasks/task_abc123 \\
  -H "Authorization: Bearer sk-tg-你的密钥"

# 3. 完成后返回
# {"status": "succeeded", "video_url": "https://..."}</code></pre>
      </div>

      <h3>计费说明</h3>
      <ul>
        <li>按次计费，任务提交后即扣费</li>
        <li>任务失败自动退还额度</li>
        <li>视频 URL 有效期 24 小时，请及时下载</li>
      </ul>
    </section>

    <section class="section" id="faq">
      <h2>❓ 15. 常见问题</h2>

      <h3>安装相关</h3>
      <div class="card">
        <h4>Q: npm install 报权限错误？</h4>
        <p>macOS/Linux 使用：<code>sudo npm install -g @anthropic-ai/claude-code</code>，或配置 npm 全局目录到当前用户。</p>

        <h4>Q: Windows 安装后命令找不到？</h4>
        <p>重启 PowerShell，或检查环境变量 PATH 是否包含 npm 全局目录（通常为 <code>%AppData%\\npm</code>）。</p>

        <h4>Q: Node.js 版本过低？</h4>
        <p>使用 nvm 管理多版本：<code>nvm install 20 &amp;&amp; nvm use 20</code></p>
      </div>

      <h3>连接与认证相关</h3>
      <div class="card">
        <h4>Q: 报错 401 Unauthorized？</h4>
        <ul>
          <li>检查 API Key 是否正确（格式 <code>sk-tg-xxxx</code>）</li>
          <li>检查密钥是否已过期或额度耗尽</li>
          <li>确认 Base URL 协议头（<code>http://</code>）和路径正确</li>
        </ul>

        <h4>Q: 报错 402 Payment Required？</h4>
        <p>额度已用尽，请前往<a href="__PUBLIC_BASE_URL__/dashboard">控制台</a>充值。</p>

        <h4>Q: 报错 429 Too Many Requests？</h4>
        <p>请求过于频繁，请降低调用频率（建议间隔 ≥1 秒），或联系客服提升速率限制。</p>

        <h4>Q: 连接超时？</h4>
        <ul>
          <li>检查网络是否正常</li>
          <li>确认 <code>__PUBLIC_BASE_URL__</code> 可访问</li>
          <li>图像/视频生成需设置 120s 以上超时</li>
        </ul>
      </div>

      <h3>使用相关</h3>
      <div class="card">
        <h4>Q: Claude Code 仍使用官方地址？</h4>
        <p>环境变量未生效。运行 <code>echo $ANTHROPIC_BASE_URL</code> 检查，确认重启终端或 source 配置文件。</p>

        <h4>Q: 流式响应如何启用？</h4>
        <p>请求体加 <code>"stream": true</code>，返回 SSE 格式数据。</p>

        <h4>Q: 支持哪些模型？</h4>
        <p>OpenAI 系列：<code>gpt-5.6</code> / <code>gpt-5.4-mini</code> / <code>codex-auto-review</code> 等；Claude 系列：<code>claude-sonnet-4-5</code> / <code>claude-fable-5</code> / <code>claude-haiku-4-5</code>。完整列表见 <a href="__PUBLIC_BASE_URL__/v1/models">/v1/models</a>。</p>

        <h4>Q: 如何查看用量明细？</h4>
        <p>登录控制台「使用记录」页面，可按时间、模型、API Key 筛选查看每次调用扣费。</p>

        <h4>Q: 如何联系客服？</h4>
        <p>在控制台「客服工单」提交工单，或邮件 <code>Commecy2014@gmail.com</code>。</p>
      </div>
    </section>

    <div class="no-results" id="noResults">
      <p style="font-size:48px;margin-bottom:12px">🔍</p>
      <p>未找到匹配的章节，换个关键词试试？</p>
    </div>

    <footer>© 2026 TokenGo. 保留所有权利。</footer>
  </main>
</div>

<script>
function toggleMenu(){
  document.getElementById('sidebar').classList.toggle('open');
}
const sections = document.querySelectorAll('.section');
const tocLinks = document.querySelectorAll('.toc a');
tocLinks.forEach(link => {
  link.addEventListener('click', function(e) {
    e.preventDefault();
    const targetId = this.getAttribute('href').slice(1);
    const target = document.getElementById(targetId);
    if (target) {
      target.scrollIntoView({behavior:'smooth', block:'start'});
      document.getElementById('sidebar').classList.remove('open');
    }
  });
});
function updateActiveSection() {
  let current = '';
  const scrollPos = window.scrollY + 120;
  sections.forEach(sec => {
    if (sec.offsetTop <= scrollPos) {
      current = sec.id;
    }
  });
  tocLinks.forEach(link => {
    link.classList.remove('active');
    if (link.getAttribute('href') === '#' + current) {
      link.classList.add('active');
    }
  });
}
window.addEventListener('scroll', updateActiveSection);
window.addEventListener('resize', updateActiveSection);
updateActiveSection();
function fallbackCopy(text, cb) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); cb(); } catch(e) {}
  document.body.removeChild(ta);
}
document.querySelectorAll('.copy-btn').forEach(btn => {
  btn.addEventListener('click', function() {
    const code = this.closest('.code-block').querySelector('code');
    const text = code.innerText;
    const done = () => {
      const orig = this.textContent;
      this.textContent = '已复制';
      this.classList.add('copied');
      setTimeout(() => {
        this.textContent = orig;
        this.classList.remove('copied');
      }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
    } else {
      fallbackCopy(text, done);
    }
  });
});
const searchInput = document.getElementById('searchInput');
const noResults = document.getElementById('noResults');
searchInput.addEventListener('input', function() {
  const q = this.value.trim().toLowerCase();
  let visibleCount = 0;
  sections.forEach(sec => {
    const text = sec.textContent.toLowerCase();
    const tocLink = document.querySelector('.toc a[href="#' + sec.id + '"]');
    if (!q || text.includes(q)) {
      sec.classList.remove('hidden');
      if (tocLink) tocLink.parentElement.style.display = '';
      visibleCount++;
    } else {
      sec.classList.add('hidden');
      if (tocLink) tocLink.parentElement.style.display = 'none';
    }
  });
  noResults.classList.toggle('show', visibleCount === 0);
});
</script>
</body>
</html>"""


# ============================================================================
# 管理员仪表盘 HTML
# ============================================================================

ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TokenGo 管理员控制台</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
:root {
  --primary:#2563eb; --accent:#059669; --danger:#dc2626; --warn:#d97706;
  --bg:#f8fafc; --card:#ffffff; --border:#e2e8f0; --text:#1e293b;
  --text-soft:#64748b; --shadow:0 1px 3px rgba(0,0,0,.08);
}
body { font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
       background:var(--bg); color:var(--text); font-size:14px; }
.header { background:var(--card); border-bottom:1px solid var(--border); padding:16px 24px;
          display:flex; justify-content:space-between; align-items:center; position:sticky; top:0; z-index:100; box-shadow:var(--shadow); }
.header h1 { font-size:18px; color:var(--primary); }
.header .user-area { display:flex; gap:12px; align-items:center; font-size:13px; color:var(--text-soft); }
.header a { color:var(--primary); text-decoration:none; font-size:13px; }
.container { max-width:1400px; margin:0 auto; padding:24px; }
.login-box { max-width:400px; margin:80px auto; background:var(--card); padding:40px;
             border-radius:12px; box-shadow:var(--shadow); }
.login-box h2 { text-align:center; margin-bottom:24px; color:var(--primary); }
.login-box input { width:100%; padding:10px 12px; margin-bottom:12px; border:1px solid var(--border);
                   border-radius:6px; font-size:14px; }
.login-box button { width:100%; padding:12px; background:var(--primary); color:#fff; border:none;
                    border-radius:6px; font-size:15px; cursor:pointer; }
.login-box button:hover { background:#1d4ed8; }
.login-box .hint { text-align:center; margin-top:16px; color:var(--text-soft); font-size:13px; }
.stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-bottom:24px; }
.stat-card { background:var(--card); padding:20px; border-radius:10px; box-shadow:var(--shadow); border-left:4px solid var(--primary); }
.stat-card .label { font-size:12px; color:var(--text-soft); margin-bottom:6px; }
.stat-card .value { font-size:24px; font-weight:700; color:var(--text); }
.stat-card .sub { font-size:12px; color:var(--text-soft); margin-top:4px; }
.stat-card.success { border-left-color:var(--accent); }
.stat-card.success .value { color:var(--accent); }
.stat-card.warn { border-left-color:var(--warn); }
.stat-card.danger { border-left-color:var(--danger); }
.panel { background:var(--card); border-radius:10px; box-shadow:var(--shadow); margin-bottom:24px; overflow:hidden; }
.panel-header { padding:16px 20px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; }
.panel-header h2 { font-size:16px; }
.panel-body { padding:0; }
.toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.toolbar input, .toolbar select { padding:8px 12px; border:1px solid var(--border); border-radius:6px; font-size:13px; }
.toolbar button { padding:8px 16px; background:var(--primary); color:#fff; border:none; border-radius:6px; cursor:pointer; font-size:13px; }
.toolbar button:hover { background:#1d4ed8; }
.toolbar button.btn-soft { background:#f1f5f9; color:var(--text); }
.toolbar button.btn-soft:hover { background:#e2e8f0; }
table { width:100%; border-collapse:collapse; }
table th { background:#f8fafc; padding:10px 12px; text-align:left; font-size:12px; color:var(--text-soft);
           border-bottom:1px solid var(--border); font-weight:600; white-space:nowrap; }
table td { padding:10px 12px; border-bottom:1px solid #f1f5f9; font-size:13px; }
table tr:hover { background:#f8fafc; }
.tag { display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }
.tag-active { background:#dcfce7; color:#166534; }
.tag-locked { background:#fee2e2; color:#991b1b; }
.tag-admin { background:#dbeafe; color:#1e40af; }
.tag-code { background:#e0e7ff; color:#3730a3; }
.tag-card { background:#fef3c7; color:#92400e; }
.pagination { padding:16px 20px; display:flex; justify-content:space-between; align-items:center; }
.pagination button { padding:6px 14px; border:1px solid var(--border); background:var(--card); border-radius:6px; cursor:pointer; font-size:13px; }
.pagination button:disabled { opacity:.4; cursor:not-allowed; }
.modal-overlay { position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,.5); z-index:1000; display:none; }
.modal-overlay.show { display:flex; align-items:flex-start; justify-content:center; padding:40px 20px; overflow-y:auto; }
.modal { background:var(--card); border-radius:12px; max-width:900px; width:100%; box-shadow:0 8px 32px rgba(0,0,0,.2); }
.modal-header { padding:16px 20px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }
.modal-header h3 { font-size:16px; }
.modal-close { background:none; border:none; font-size:24px; cursor:pointer; color:var(--text-soft); }
.modal-body { padding:20px; max-height:70vh; overflow-y:auto; }
.detail-section { margin-bottom:24px; }
.detail-section h4 { font-size:14px; color:var(--primary); margin-bottom:8px; padding-bottom:6px; border-bottom:1px solid var(--border); }
.detail-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin-bottom:16px; }
.detail-item { padding:8px 12px; background:#f8fafc; border-radius:6px; }
.detail-item .k { font-size:11px; color:var(--text-soft); }
.detail-item .v { font-size:14px; font-weight:600; }
.btn-danger { background:var(--danger) !important; }
.btn-danger:hover { background:#b91c1c !important; }
.btn-success { background:var(--accent) !important; }
.btn-success:hover { background:#047857 !important; }
.toast { position:fixed; bottom:24px; right:24px; background:var(--text); color:#fff; padding:12px 20px;
         border-radius:8px; box-shadow:0 4px 16px rgba(0,0,0,.2); z-index:2000; display:none; }
.toast.show { display:block; }
.empty { text-align:center; padding:40px; color:var(--text-soft); }
</style>
</head>
<body>

<div class="header">
  <h1>TokenGo 管理员控制台</h1>
  <div class="user-area" id="adminInfo" style="display:none">
    <span id="adminEmail"></span>
    <a href="/" target="_blank">前台首页</a>
    <a href="javascript:void(0)" onclick="logout()">退出</a>
  </div>
</div>

<!-- 登录 -->
<div class="login-box" id="loginBox">
  <h2>管理员登录</h2>
  <input type="text" id="loginInput" placeholder="管理员会话 Token 或 demo-master 主密钥">
  <button onclick="adminLogin()">登录控制台</button>
  <div class="hint">管理员邮箱：Commecy2014@gmail.com / 密码：admin123456<br>或直接使用主密钥 demo-master</div>
</div>

<!-- 主面板 -->
<div class="container" id="mainPanel" style="display:none">

  <!-- 平台统计 -->
  <div class="stats-grid" id="statsGrid"></div>

  <!-- 用户管理 -->
  <div class="panel">
    <div class="panel-header">
      <h2>用户管理（含兑换记录）</h2>
      <div class="toolbar">
        <input type="text" id="searchInput" placeholder="搜索邮箱/用户名/ID" onkeyup="if(event.key==='Enter')loadUsers(1)">
        <label style="font-size:13px;color:var(--text-soft)"><input type="checkbox" id="onlyRedeemers" onchange="loadUsers(1)"> 只看有兑换记录</label>
        <button onclick="loadUsers(1)">搜索</button>
        <button class="btn-soft" onclick="exportUsers()">导出 CSV</button>
      </div>
    </div>
    <div class="panel-body">
      <table id="usersTable">
        <thead><tr>
          <th>用户ID</th><th>邮箱</th><th>用户名</th><th>角色</th><th>状态</th>
          <th>注册时间</th><th>余额</th><th>兑换次数</th><th>兑换总额</th>
          <th>卡密兑换</th><th>充值总额</th><th>AI调用</th><th>操作</th>
        </tr></thead>
        <tbody id="usersBody"></tbody>
      </table>
      <div class="pagination" id="usersPagination"></div>
    </div>
  </div>

  <!-- 兑换历史 -->
  <div class="panel">
    <div class="panel-header">
      <h2>全平台兑换记录</h2>
      <div class="toolbar">
        <select id="sourceFilter" onchange="loadRedemptions(1)">
          <option value="">全部来源</option>
          <option value="code">兑换码</option>
          <option value="card">卡密</option>
        </select>
        <button onclick="loadRedemptions(1)">刷新</button>
      </div>
    </div>
    <div class="panel-body">
      <table>
        <thead><tr>
          <th>类型</th><th>兑换码/卡密</th><th>用户邮箱</th><th>用户名</th>
          <th>金额($)</th><th>状态</th><th>时间</th>
        </tr></thead>
        <tbody id="redemptionsBody"></tbody>
      </table>
      <div class="pagination" id="redemptionsPagination"></div>
    </div>
  </div>

  <!-- 安全防护说明 -->
  <div class="panel">
    <div class="panel-header"><h2>安全防护状态</h2></div>
    <div class="panel-body" style="padding:20px">
      <div class="detail-grid">
        <div class="detail-item"><div class="k">登录限流</div><div class="v" style="color:var(--accent)">已启用</div><div class="k" style="margin-top:4px">单IP每分钟最多 10 次</div></div>
        <div class="detail-item"><div class="k">账户锁定</div><div class="v" style="color:var(--accent)">已启用</div><div class="k" style="margin-top:4px">5次失败锁定15分钟</div></div>
        <div class="detail-item"><div class="k">CAPTCHA验证</div><div class="v" style="color:var(--accent)">已启用</div><div class="k" style="margin-top:4px">注册强制+登录失败2次起</div></div>
        <div class="detail-item"><div class="k">IP封禁</div><div class="v" style="color:var(--accent)">已启用</div><div class="k" style="margin-top:4px">累计20次失败封1小时</div></div>
        <div class="detail-item"><div class="k">密码加密</div><div class="v" style="color:var(--accent)">SHA256+盐</div><div class="k" style="margin-top:4px">传输 HTTPS 加密</div></div>
        <div class="detail-item"><div class="k">数据脱敏</div><div class="v" style="color:var(--accent)">已启用</div><div class="k" style="margin-top:4px">邮箱批量导出脱敏</div></div>
        <div class="detail-item"><div class="k">访问控制</div><div class="v" style="color:var(--accent)">RBAC</div><div class="k" style="margin-top:4px">管理员/普通用户隔离</div></div>
        <div class="detail-item"><div class="k">数据隔离</div><div class="v" style="color:var(--accent)">已启用</div><div class="k" style="margin-top:4px">用户只看自己数据</div></div>
      </div>
    </div>
  </div>

</div>

<!-- 用户详情模态框 -->
<div class="modal-overlay" id="userModal">
  <div class="modal">
    <div class="modal-header">
      <h3 id="modalTitle">用户详情</h3>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modalBody"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let ADMIN_TOKEN = localStorage.getItem('admin_token') || '';
let currentPage = 1, totalUsers = 0;

function toast(msg){ const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),3000); }

function api(url, opts={}){
  opts.headers = opts.headers || {};
  opts.headers['Authorization'] = 'Bearer ' + ADMIN_TOKEN;
  return fetch(url, opts).then(r => {
    if(r.status===401||r.status===403){ throw new Error('权限不足或未登录'); }
    return r.json();
  });
}

function adminLogin(){
  const token = document.getElementById('loginInput').value.trim();
  if(!token){ toast('请输入管理员 Token'); return; }
  ADMIN_TOKEN = token;
  // 验证 Token 有效性
  fetch('/api/admin/stats', { headers:{'Authorization':'Bearer '+token} })
    .then(r => { if(!r.ok) throw new Error('Token无效'); return r.json(); })
    .then(data => {
      localStorage.setItem('admin_token', token);
      document.getElementById('loginBox').style.display='none';
      document.getElementById('mainPanel').style.display='block';
      document.getElementById('adminInfo').style.display='flex';
      document.getElementById('adminEmail').textContent = '管理员已登录';
      loadStats(); loadUsers(1); loadRedemptions(1);
    })
    .catch(e => { toast('登录失败：'+e.message); ADMIN_TOKEN=''; });
}

function logout(){ localStorage.removeItem('admin_token'); location.reload(); }

function loadStats(){
  api('/api/admin/stats').then(data => {
    const grid = document.getElementById('statsGrid');
    grid.innerHTML = `
      <div class="stat-card"><div class="label">总用户数</div><div class="value">${data.users.total}</div><div class="sub">今日新增 ${data.users.today_new}</div></div>
      <div class="stat-card success"><div class="label">充值总额 (¥)</div><div class="value">${data.revenue.total_revenue_rmb}</div><div class="sub">${data.revenue.paid_orders} 笔订单 · 今日 ¥${data.revenue.today_revenue_rmb}</div></div>
      <div class="stat-card"><div class="label">兑换码兑换</div><div class="value">${data.redemptions.code_count}</div><div class="sub">总额 $${data.redemptions.code_amount}</div></div>
      <div class="stat-card"><div class="label">卡密兑换</div><div class="value">${data.redemptions.card_used}</div><div class="sub">库存 ${data.redemptions.card_unused_count} 张 / $${data.redemptions.card_unused_value}</div></div>
      <div class="stat-card"><div class="label">AI 调用总数</div><div class="value">${data.ai.total_calls}</div><div class="sub">Token ${data.ai.total_tokens} · 今日 ${data.ai.today_calls}</div></div>
      <div class="stat-card success"><div class="label">AI 总消耗 ($)</div><div class="value">${data.ai.total_cost_usd}</div><div class="sub">平均响应 ${data.avg_response_ms}ms</div></div>
      <div class="stat-card warn"><div class="label">待处理工单</div><div class="value">${data.tickets.open}</div><div class="sub">未关闭工单数</div></div>
    `;
  }).catch(e => toast('加载统计失败：'+e.message));
}

function loadUsers(page){
  currentPage = page;
  const search = document.getElementById('searchInput').value.trim();
  const onlyRedeemers = document.getElementById('onlyRedeemers').checked ? '&only_redeemers=true' : '';
  api(`/api/admin/users?page=${page}&page_size=20&search=${encodeURIComponent(search)}${onlyRedeemers}`)
    .then(data => {
      totalUsers = data.total;
      const body = document.getElementById('usersBody');
      if(!data.users.length){ body.innerHTML = '<tr><td colspan="13" class="empty">暂无用户数据</td></tr>'; }
      else {
        body.innerHTML = data.users.map(u => `
          <tr>
            <td title="${u.id}">${u.id.substring(0,12)}...</td>
            <td>${u.email}</td>
            <td>${u.username||'-'}</td>
            <td>${u.role==='admin'?'<span class="tag tag-admin">管理员</span>':'<span class="tag tag-active">用户</span>'}</td>
            <td>${u.is_locked||u.status==='locked'?'<span class="tag tag-locked">已锁定</span>':'<span class="tag tag-active">正常</span>'}</td>
            <td>${u.created_at_str}</td>
            <td>$${u.balance}</td>
            <td>${u.redemption_count}</td>
            <td>$${u.redemption_amount}</td>
            <td>${u.card_redeem_count}</td>
            <td>¥${u.total_recharge_rmb}</td>
            <td>${u.ai_calls}</td>
            <td>
              <button class="btn-soft" style="padding:4px 10px;font-size:12px" onclick="showUserDetail('${u.id}')">详情</button>
              ${u.role!=='admin' ? (u.status==='locked' ?
                `<button class="btn-success" style="padding:4px 10px;font-size:12px" onclick="toggleLock('${u.id}')">解锁</button>` :
                `<button class="btn-danger" style="padding:4px 10px;font-size:12px" onclick="toggleLock('${u.id}')">锁定</button>`) : ''}
            </td>
          </tr>`).join('');
      }
      // 分页
      const pg = document.getElementById('usersPagination');
      pg.innerHTML = `
        <span>共 ${data.total} 条，第 ${data.page}/${data.total_pages} 页</span>
        <div>
          <button ${data.page<=1?'disabled':''} onclick="loadUsers(${data.page-1})">上一页</button>
          <button ${data.page>=data.total_pages?'disabled':''} onclick="loadUsers(${data.page+1})">下一页</button>
        </div>`;
    }).catch(e => toast('加载用户失败：'+e.message));
}

function loadRedemptions(page){
  const source = document.getElementById('sourceFilter').value;
  api(`/api/admin/redemptions?page=${page}&page_size=30&source=${source}`)
    .then(data => {
      const body = document.getElementById('redemptionsBody');
      if(!data.redemptions.length){ body.innerHTML = '<tr><td colspan="7" class="empty">暂无兑换记录</td></tr>'; }
      else {
        body.innerHTML = data.redemptions.map(r => `
          <tr>
            <td><span class="tag ${r.type==='card'?'tag-card':'tag-code'}">${r.type==='card'?'卡密':'兑换码'}</span></td>
            <td title="${r.code}">${r.code.substring(0,20)}${r.code.length>20?'...':''}</td>
            <td>${r.user_email}</td>
            <td>${r.username||'-'}</td>
            <td style="color:var(--accent);font-weight:600">$${r.amount}</td>
            <td><span class="tag tag-active">${r.status}</span></td>
            <td>${r.created_at_str}</td>
          </tr>`).join('');
      }
      const pg = document.getElementById('redemptionsPagination');
      pg.innerHTML = `
        <span>共 ${data.total} 条，第 ${data.page}/${data.total_pages} 页</span>
        <div>
          <button ${data.page<=1?'disabled':''} onclick="loadRedemptions(${data.page-1})">上一页</button>
          <button ${data.page>=data.total_pages?'disabled':''} onclick="loadRedemptions(${data.page+1})">下一页</button>
        </div>`;
    }).catch(e => toast('加载兑换记录失败：'+e.message));
}

function showUserDetail(uid){
  api(`/api/admin/users/${uid}`).then(data => {
    const u = data.user;
    document.getElementById('modalTitle').textContent = `用户详情 - ${u.username||u.email}`;
    const body = document.getElementById('modalBody');
    let html = `
      <div class="detail-section">
        <h4>基本信息</h4>
        <div class="detail-grid">
          <div class="detail-item"><div class="k">用户ID</div><div class="v" style="font-size:12px">${u.id}</div></div>
          <div class="detail-item"><div class="k">邮箱</div><div class="v" style="font-size:12px">${u.email}</div></div>
          <div class="detail-item"><div class="k">用户名</div><div class="v">${u.username||'-'}</div></div>
          <div class="detail-item"><div class="k">角色</div><div class="v">${u.role==='admin'?'管理员':'普通用户'}</div></div>
          <div class="detail-item"><div class="k">状态</div><div class="v">${u.is_locked?'已锁定':'正常'}</div></div>
          <div class="detail-item"><div class="k">注册时间</div><div class="v" style="font-size:12px">${u.created_at_str}</div></div>
          <div class="detail-item"><div class="k">余额</div><div class="v" style="color:var(--accent)">$${u.balance}</div></div>
          <div class="detail-item"><div class="k">API密钥</div><div class="v" style="font-size:11px;word-break:break-all">${u.token_id||'-'}</div></div>
          <div class="detail-item"><div class="k">邀请码</div><div class="v">${u.invite_code||'-'}</div></div>
          <div class="detail-item"><div class="k">被邀请人</div><div class="v">${u.invited_by||'无'}</div></div>
          <div class="detail-item"><div class="k">累计返利</div><div class="v">$${u.total_rebate||0}</div></div>
          <div class="detail-item"><div class="k">可提现返利</div><div class="v">$${u.available_rebate||0}</div></div>
          <div class="detail-item"><div class="k">失败登录次数</div><div class="v">${u.failed_count||0}</div></div>
        </div>
      </div>`;
    // 兑换码历史
    html += `<div class="detail-section"><h4>兑换码历史 (${data.redemptions.length})</h4>`;
    if(data.redemptions.length){
      html += `<table><thead><tr><th>兑换码</th><th>金额</th><th>状态</th><th>时间</th></tr></thead><tbody>`;
      data.redemptions.forEach(r => { html += `<tr><td>${r.code}</td><td>$${r.amount}</td><td>${r.status}</td><td>${r.created_at_str}</td></tr>`; });
      html += `</tbody></table>`;
    } else { html += `<p class="empty">无兑换码记录</p>`; }
    html += `</div>`;
    // 卡密历史
    html += `<div class="detail-section"><h4>卡密兑换历史 (${data.cards.length})</h4>`;
    if(data.cards.length){
      html += `<table><thead><tr><th>卡密ID</th><th>面值</th><th>使用时间</th></tr></thead><tbody>`;
      data.cards.forEach(r => { html += `<tr><td style="font-size:11px">${r.id}</td><td>$${r.face_value}</td><td>${r.used_at_str}</td></tr>`; });
      html += `</tbody></table>`;
    } else { html += `<p class="empty">无卡密记录</p>`; }
    html += `</div>`;
    // 订单历史
    html += `<div class="detail-section"><h4>充值订单 (${data.orders.length})</h4>`;
    if(data.orders.length){
      html += `<table><thead><tr><th>订单ID</th><th>类型</th><th>金额</th><th>状态</th><th>支付方式</th><th>时间</th></tr></thead><tbody>`;
      data.orders.forEach(r => { html += `<tr><td style="font-size:11px">${r.id}</td><td>${r.type}</td><td>¥${r.amount}</td><td>${r.status}</td><td>${r.payment_method||'-'}</td><td>${r.created_at_str}</td></tr>`; });
      html += `</tbody></table>`;
    } else { html += `<p class="empty">无订单记录</p>`; }
    html += `</div>`;
    // AI 调用记录
    html += `<div class="detail-section"><h4>AI 调用记录 (${data.usages.length})</h4>`;
    if(data.usages.length){
      html += `<table><thead><tr><th>模型</th><th>Prompt</th><th>Completion</th><th>费用</th><th>响应(ms)</th><th>时间</th></tr></thead><tbody>`;
      data.usages.forEach(r => { html += `<tr><td>${r.model}</td><td>${r.prompt_tokens}</td><td>${r.completion_tokens}</td><td>$${r.cost}</td><td>${r.response_ms}</td><td>${r.created_at_str}</td></tr>`; });
      html += `</tbody></table>`;
    } else { html += `<p class="empty">无调用记录</p>`; }
    html += `</div>`;
    // 工单
    html += `<div class="detail-section"><h4>工单 (${data.tickets.length})</h4>`;
    if(data.tickets.length){
      html += `<table><thead><tr><th>标题</th><th>状态</th><th>时间</th></tr></thead><tbody>`;
      data.tickets.forEach(r => { html += `<tr><td>${r.title}</td><td>${r.status}</td><td>${r.created_at_str}</td></tr>`; });
      html += `</tbody></table>`;
    } else { html += `<p class="empty">无工单记录</p>`; }
    html += `</div>`;
    // 邀请的人
    html += `<div class="detail-section"><h4>邀请的用户 (${data.invitees.length})</h4>`;
    if(data.invitees.length){
      html += `<table><thead><tr><th>邮箱</th><th>用户名</th><th>注册时间</th></tr></thead><tbody>`;
      data.invitees.forEach(r => { html += `<tr><td>${r.email}</td><td>${r.username}</td><td>${r.created_at_str}</td></tr>`; });
      html += `</tbody></table>`;
    } else { html += `<p class="empty">无邀请记录</p>`; }
    html += `</div>`;
    body.innerHTML = html;
    document.getElementById('userModal').classList.add('show');
  }).catch(e => toast('加载详情失败：'+e.message));
}

function toggleLock(uid){
  if(!confirm('确定要锁定/解锁该用户？')) return;
  api(`/api/admin/users/${uid}/lock`, {method:'POST'}).then(data => {
    toast(data.new_status==='locked'?'用户已锁定':'用户已解锁');
    loadUsers(currentPage);
  }).catch(e => toast('操作失败：'+e.message));
}

function exportUsers(){
  // 数据泄露防护：CSV 导出时邮箱脱敏，仅管理员可导出
  api('/api/admin/users?page=1&page_size=200&search=' + document.getElementById('searchInput').value.trim())
    .then(data => {
      let csv = '用户ID,邮箱(脱敏),用户名,角色,状态,注册时间,余额,兑换次数,兑换总额,卡密兑换,充值总额,AI调用数\\n';
      data.users.forEach(u => {
        csv += `${u.id},${u.email},${u.username||''},${u.role},${u.status},${u.created_at_str},${u.balance},${u.redemption_count},${u.redemption_amount},${u.card_redeem_count},${u.total_recharge_rmb},${u.ai_calls}\\n`;
      });
      const blob = new Blob(['\\ufeff'+csv], {type:'text/csv;charset=utf-8'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `tokengo_users_${new Date().toISOString().slice(0,10)}.csv`;
      a.click();
      toast('CSV 已导出（邮箱已脱敏）');
    }).catch(e => toast('导出失败：'+e.message));
}

function closeModal(){ document.getElementById('userModal').classList.remove('show'); }

// 自动登录
if(ADMIN_TOKEN){
  document.getElementById('loginInput').value = ADMIN_TOKEN;
  adminLogin();
}
</script>
</body>
</html>"""


# ============================================================================
# 启动
# ============================================================================

@app.on_event("startup")
def _startup():
    init_db()


if __name__ == "__main__":
    import uvicorn
    init_db()
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
