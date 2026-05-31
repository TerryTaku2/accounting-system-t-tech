from fastapi import FastAPI, HTTPException, Depends, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timedelta
import sqlite3, os, secrets, hashlib, json, base64, re

try:
    from jose import JWTError, jwt
except ImportError:
    raise RuntimeError("Run: pip install python-jose[cryptography]")

try:
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except ImportError:
    raise RuntimeError("Run: pip install passlib[bcrypt]")

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DB_PATH      = os.path.join(BASE_DIR, "ttech.db")
STATIC_DIR   = os.path.join(BASE_DIR, "static")
SECRET_FILE  = os.path.join(BASE_DIR, ".ttech_secret")
ALGORITHM    = "HS256"
ACCESS_EXPIRE_MIN   = 480   # 8 hours
REFRESH_EXPIRE_DAYS = 30

def _load_secret() -> str:
    if os.path.exists(SECRET_FILE):
        return open(SECRET_FILE).read().strip()
    key = secrets.token_hex(32)
    open(SECRET_FILE, "w").write(key)
    return key

SECRET_KEY = _load_secret()

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        currency TEXT DEFAULT 'USD',
        currency_symbol TEXT DEFAULT '$',
        fiscal_year_start INTEGER DEFAULT 1,
        country TEXT, address TEXT, phone TEXT, email TEXT, tax_number TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT DEFAULT 'accountant',
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        sub_type TEXT,
        parent_id INTEGER,
        normal_balance TEXT NOT NULL,
        description TEXT,
        is_active INTEGER DEFAULT 1,
        is_system INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (parent_id) REFERENCES accounts(id),
        UNIQUE(company_id, code)
    );
    CREATE TABLE IF NOT EXISTS fiscal_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        year INTEGER NOT NULL,
        is_closed INTEGER DEFAULT 0,
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    CREATE TABLE IF NOT EXISTS journal_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        entry_number TEXT NOT NULL,
        date TEXT NOT NULL,
        description TEXT NOT NULL,
        reference TEXT,
        status TEXT DEFAULT 'posted',
        created_by INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (created_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS journal_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        description TEXT,
        debit REAL DEFAULT 0,
        credit REAL DEFAULT 0,
        FOREIGN KEY (entry_id) REFERENCES journal_entries(id) ON DELETE CASCADE,
        FOREIGN KEY (account_id) REFERENCES accounts(id)
    );
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT NOT NULL,
        entity TEXT NOT NULL,
        entity_id INTEGER,
        details TEXT,
        timestamp TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS anomaly_dismissals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id  INTEGER NOT NULL,
        rule_type   TEXT    NOT NULL,
        fingerprint TEXT    NOT NULL,
        dismissed_by INTEGER,
        dismissed_at TEXT   DEFAULT (datetime('now')),
        notes       TEXT,
        UNIQUE(company_id, fingerprint),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT, phone TEXT, address TEXT, tax_number TEXT,
        credit_limit REAL DEFAULT 0,
        payment_terms INTEGER DEFAULT 30,
        notes TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    CREATE TABLE IF NOT EXISTS suppliers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT, phone TEXT, address TEXT, tax_number TEXT,
        payment_terms INTEGER DEFAULT 30,
        notes TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        invoice_number TEXT NOT NULL,
        customer_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        due_date TEXT NOT NULL,
        status TEXT DEFAULT 'draft',
        subtotal REAL DEFAULT 0,
        tax_amount REAL DEFAULT 0,
        discount REAL DEFAULT 0,
        total REAL DEFAULT 0,
        amount_paid REAL DEFAULT 0,
        balance_due REAL DEFAULT 0,
        notes TEXT,
        journal_entry_id INTEGER,
        created_by INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (created_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS invoice_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        account_id INTEGER,
        quantity REAL DEFAULT 1,
        unit_price REAL DEFAULT 0,
        tax_rate REAL DEFAULT 0,
        tax_amount REAL DEFAULT 0,
        line_total REAL DEFAULT 0,
        FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        receipt_number TEXT NOT NULL,
        customer_id INTEGER NOT NULL,
        invoice_id INTEGER,
        date TEXT NOT NULL,
        amount REAL NOT NULL,
        payment_method TEXT DEFAULT 'cash',
        reference TEXT,
        bank_account_id INTEGER,
        journal_entry_id INTEGER,
        created_by INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    );
    CREATE TABLE IF NOT EXISTS bills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        bill_number TEXT NOT NULL,
        supplier_id INTEGER NOT NULL,
        supplier_ref TEXT,
        date TEXT NOT NULL,
        due_date TEXT NOT NULL,
        status TEXT DEFAULT 'unpaid',
        subtotal REAL DEFAULT 0,
        tax_amount REAL DEFAULT 0,
        total REAL DEFAULT 0,
        amount_paid REAL DEFAULT 0,
        balance_due REAL DEFAULT 0,
        notes TEXT,
        journal_entry_id INTEGER,
        created_by INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
        FOREIGN KEY (created_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS bill_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        account_id INTEGER,
        quantity REAL DEFAULT 1,
        unit_price REAL DEFAULT 0,
        tax_rate REAL DEFAULT 0,
        tax_amount REAL DEFAULT 0,
        line_total REAL DEFAULT 0,
        FOREIGN KEY (bill_id) REFERENCES bills(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS bill_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        payment_number TEXT NOT NULL,
        supplier_id INTEGER NOT NULL,
        bill_id INTEGER,
        date TEXT NOT NULL,
        amount REAL NOT NULL,
        payment_method TEXT DEFAULT 'bank_transfer',
        reference TEXT,
        bank_account_id INTEGER,
        journal_entry_id INTEGER,
        created_by INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
    );
    CREATE TABLE IF NOT EXISTS bank_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        bank_name TEXT,
        account_number TEXT,
        gl_account_id INTEGER NOT NULL,
        currency TEXT DEFAULT 'USD',
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (gl_account_id) REFERENCES accounts(id)
    );
    -- ── RECONCILIATION ──────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS bank_transactions (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id         INTEGER NOT NULL,
        bank_account_id    INTEGER NOT NULL,
        import_batch       TEXT,
        date               TEXT NOT NULL,
        description        TEXT,
        reference          TEXT,
        amount             REAL NOT NULL,
        balance            REAL,
        match_status       TEXT DEFAULT 'unmatched',
        journal_line_id    INTEGER,
        match_confidence   REAL DEFAULT 0,
        matched_at         TEXT,
        matched_by         INTEGER,
        created_at         TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id)      REFERENCES companies(id),
        FOREIGN KEY (bank_account_id) REFERENCES bank_accounts(id)
    );
    CREATE TABLE IF NOT EXISTS reconciliation_periods (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id         INTEGER NOT NULL,
        bank_account_id    INTEGER NOT NULL,
        period_name        TEXT NOT NULL,
        start_date         TEXT NOT NULL,
        end_date           TEXT NOT NULL,
        statement_balance  REAL NOT NULL,
        gl_balance         REAL DEFAULT 0,
        matched_count      INTEGER DEFAULT 0,
        unmatched_count    INTEGER DEFAULT 0,
        status             TEXT DEFAULT 'open',
        notes              TEXT,
        reconciled_at      TEXT,
        reconciled_by      INTEGER,
        created_at         TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id)      REFERENCES companies(id),
        FOREIGN KEY (bank_account_id) REFERENCES bank_accounts(id)
    );
    -- ── PHASE 4: INVENTORY ──────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        category TEXT,
        unit TEXT DEFAULT 'unit',
        barcode TEXT,
        cost_price REAL DEFAULT 0,
        selling_price REAL DEFAULT 0,
        current_stock REAL DEFAULT 0,
        reorder_level REAL DEFAULT 0,
        inventory_account_id INTEGER,
        cogs_account_id INTEGER,
        revenue_account_id INTEGER,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        UNIQUE(company_id, code)
    );
    CREATE TABLE IF NOT EXISTS stock_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        movement_type TEXT NOT NULL,
        quantity REAL NOT NULL,
        unit_cost REAL DEFAULT 0,
        total_cost REAL DEFAULT 0,
        reference TEXT,
        notes TEXT,
        journal_entry_id INTEGER,
        created_by INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (product_id) REFERENCES products(id)
    );
    -- ── PHASE 4: PAYROLL ────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        employee_number TEXT NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT, phone TEXT, id_number TEXT,
        position TEXT, department TEXT, hire_date TEXT,
        basic_salary REAL DEFAULT 0,
        pay_frequency TEXT DEFAULT 'monthly',
        paye_rate REAL DEFAULT 20,
        ss_rate REAL DEFAULT 3,
        bank_account TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    CREATE TABLE IF NOT EXISTS pay_components (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL,
        component_type TEXT NOT NULL,
        name TEXT NOT NULL,
        amount REAL DEFAULT 0,
        is_percentage INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY (employee_id) REFERENCES employees(id)
    );
    CREATE TABLE IF NOT EXISTS payroll_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        run_number TEXT NOT NULL,
        period_name TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        status TEXT DEFAULT 'draft',
        total_gross REAL DEFAULT 0,
        total_deductions REAL DEFAULT 0,
        total_net REAL DEFAULT 0,
        journal_entry_id INTEGER,
        created_by INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    CREATE TABLE IF NOT EXISTS payroll_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        employee_id INTEGER NOT NULL,
        basic_salary REAL DEFAULT 0,
        total_allowances REAL DEFAULT 0,
        gross_pay REAL DEFAULT 0,
        paye_tax REAL DEFAULT 0,
        social_security REAL DEFAULT 0,
        other_deductions REAL DEFAULT 0,
        total_deductions REAL DEFAULT 0,
        net_pay REAL DEFAULT 0,
        FOREIGN KEY (run_id) REFERENCES payroll_runs(id) ON DELETE CASCADE,
        FOREIGN KEY (employee_id) REFERENCES employees(id)
    );
    -- ── PHASE 4: FIXED ASSETS ───────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS fixed_assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        asset_number TEXT NOT NULL,
        name TEXT NOT NULL,
        category TEXT, description TEXT,
        purchase_date TEXT NOT NULL,
        purchase_cost REAL NOT NULL,
        salvage_value REAL DEFAULT 0,
        useful_life_years REAL DEFAULT 5,
        depreciation_method TEXT DEFAULT 'straight_line',
        depreciation_rate REAL DEFAULT 0,
        accumulated_depreciation REAL DEFAULT 0,
        book_value REAL DEFAULT 0,
        asset_account_id INTEGER,
        dep_expense_account_id INTEGER,
        accum_dep_account_id INTEGER,
        status TEXT DEFAULT 'active',
        disposal_date TEXT, disposal_proceeds REAL DEFAULT 0,
        created_by INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    CREATE TABLE IF NOT EXISTS depreciation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER NOT NULL,
        period TEXT NOT NULL,
        amount REAL NOT NULL,
        accumulated_total REAL NOT NULL,
        book_value_after REAL NOT NULL,
        journal_entry_id INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (asset_id) REFERENCES fixed_assets(id)
    );
    -- ── PHASE 4: TAX ────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS tax_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        period_type TEXT DEFAULT 'vat',
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        output_tax REAL DEFAULT 0,
        input_tax REAL DEFAULT 0,
        net_payable REAL DEFAULT 0,
        journal_entry_id INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    -- ── PHASE 5: MULTI-COMPANY ──────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS user_companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        company_id INTEGER NOT NULL,
        role TEXT DEFAULT 'accountant',
        UNIQUE(user_id, company_id),
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (company_id) REFERENCES companies(id)
    );
    -- ── PHASE 5: BUDGETING ──────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        scenario TEXT NOT NULL DEFAULT 'Base',
        amount REAL DEFAULT 0,
        notes TEXT,
        created_by INTEGER,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(company_id, account_id, year, month, scenario),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (account_id) REFERENCES accounts(id)
    );
    -- ── PHASE 5: API KEYS ───────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        key_prefix TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        permissions TEXT DEFAULT 'read',
        is_active INTEGER DEFAULT 1,
        last_used TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    -- ── PHASE 5: TWO-FACTOR AUTH ────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS user_2fa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL UNIQUE,
        totp_secret TEXT NOT NULL,
        is_enabled INTEGER DEFAULT 0,
        backup_codes TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    """)
    conn.commit()
    conn.close()

# ── Auth helpers ──────────────────────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return pwd_context.hash(pw)

def verify_pw(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def make_token(data: dict, expires: timedelta) -> str:
    payload = {**data, "exp": datetime.utcnow() + expires}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="T-Tech Accountant API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

NORMAL_BALANCE = {"asset": "debit", "expense": "debit",
                  "liability": "credit", "equity": "credit", "revenue": "credit"}

async def current_user(token: str = Depends(oauth2)):
    payload = decode_token(token)
    uid = payload.get("sub")
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=? AND is_active=1", (uid,)).fetchone()
    db.close()
    if not u:
        raise HTTPException(401, "User not found")
    return dict(u)

def audit(db, user_id, action, entity, entity_id=None, details=None):
    db.execute("INSERT INTO audit_log(user_id,action,entity,entity_id,details) VALUES(?,?,?,?,?)",
               (user_id, action, entity, entity_id, details))

# ── Pydantic models ───────────────────────────────────────────────────────────
class SetupReq(BaseModel):
    company_name: str
    currency: str = "USD"
    currency_symbol: str = "$"
    country: Optional[str] = None
    admin_name: str
    admin_email: str
    admin_password: str = Field(..., min_length=8)

class LoginReq(BaseModel):
    email: str
    password: str

class RefreshReq(BaseModel):
    refresh_token: str

class AccountCreate(BaseModel):
    code: str
    name: str
    type: str
    sub_type: Optional[str] = None
    parent_id: Optional[int] = None
    description: Optional[str] = None

class AccountUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

class JournalLine(BaseModel):
    account_id: int
    description: Optional[str] = None
    debit: float = Field(default=0, ge=0)
    credit: float = Field(default=0, ge=0)

class JournalCreate(BaseModel):
    date: str
    description: str
    reference: Optional[str] = None
    lines: List[JournalLine] = Field(..., min_items=2)

class UserCreate(BaseModel):
    email: str
    password: str = Field(..., min_length=8)
    full_name: str
    role: str = "accountant"
    module_permissions: Optional[List[str]] = None

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    module_permissions: Optional[List[str]] = None
    password: Optional[str] = None

class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    currency: Optional[str] = None
    currency_symbol: Optional[str] = None
    country: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    tax_number: Optional[str] = None
    fiscal_year_start: Optional[int] = None
    website: Optional[str] = None
    trading_name: Optional[str] = None
    city: Optional[str] = None
    registration_number: Optional[str] = None
    financial_year_start: Optional[str] = None
    default_tax_rate: Optional[float] = None
    logo_url: Optional[str] = None
    industry: Optional[str] = None

# ═══════════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/auth/status")
async def auth_status():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as c FROM companies").fetchone()["c"]
    db.close()
    return {"setup_required": count == 0}

@app.post("/api/auth/setup", status_code=201)
async def setup(req: SetupReq):
    db = get_db()
    try:
        if db.execute("SELECT COUNT(*) as c FROM companies").fetchone()["c"] > 0:
            raise HTTPException(400, "System already configured")
        cur = db.execute(
            "INSERT INTO companies(name,currency,currency_symbol,country) VALUES(?,?,?,?)",
            (req.company_name, req.currency, req.currency_symbol, req.country)
        )
        cid = cur.lastrowid
        db.execute(
            "INSERT INTO users(company_id,email,password_hash,full_name,role) VALUES(?,?,?,?,'admin')",
            (cid, req.admin_email, hash_pw(req.admin_password), req.admin_name)
        )
        db.commit()
        return {"message": "Setup complete", "company_id": cid}
    finally:
        db.close()

@app.post("/api/auth/login")
async def login(req: LoginReq):
    db = get_db()
    try:
        row = db.execute(
            """SELECT u.*, c.name as company_name, c.currency, c.currency_symbol
               FROM users u JOIN companies c ON u.company_id=c.id
               WHERE u.email=? AND u.is_active=1""",
            (req.email,)
        ).fetchone()
        if not row or not verify_pw(req.password, row["password_hash"]):
            raise HTTPException(401, "Invalid email or password")
        u = dict(row)
        # 2FA check (Phase 5)
        if u.get("totp_enabled"):
            temp = make_token({"sub": str(u["id"]), "type": "2fa_pending"},
                              timedelta(minutes=5))
            return {"requires_2fa": True, "temp_token": temp}
        access  = make_token({"sub": str(u["id"]), "cid": u["company_id"], "role": u["role"]},
                             timedelta(minutes=ACCESS_EXPIRE_MIN))
        refresh = make_token({"sub": str(u["id"]), "type": "refresh"},
                              timedelta(days=REFRESH_EXPIRE_DAYS))
        audit(db, u["id"], "login", "auth")
        db.commit()
        return {
            "access_token": access, "refresh_token": refresh, "token_type": "bearer",
            "user": {k: v for k, v in u.items() if k != "password_hash"}
        }
    finally:
        db.close()

@app.post("/api/auth/refresh")
async def refresh(req: RefreshReq):
    payload = decode_token(req.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(400, "Invalid token type")
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=? AND is_active=1", (payload["sub"],)).fetchone()
    db.close()
    if not u:
        raise HTTPException(401, "User not found")
    token = make_token({"sub": str(u["id"]), "cid": u["company_id"], "role": u["role"]},
                        timedelta(minutes=ACCESS_EXPIRE_MIN))
    return {"access_token": token, "token_type": "bearer"}

@app.get("/api/auth/me")
async def me(cu: dict = Depends(current_user)):
    db = get_db()
    company = db.execute("SELECT * FROM companies WHERE id=?", (cu["company_id"],)).fetchone()
    db.close()
    return {"user": {k: v for k, v in cu.items() if k != "password_hash"},
            "company": dict(company) if company else None}

# ═══════════════════════════════════════════════════════════════════════════════
# COMPANY
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/company")
async def get_company(cu: dict = Depends(current_user)):
    db = get_db()
    company = db.execute("SELECT * FROM companies WHERE id=?", (cu["company_id"],)).fetchone()
    db.close()
    if not company:
        raise HTTPException(404, "Company not found")
    return dict(company)

@app.put("/api/company")
async def update_company(req: CompanyUpdate, cu: dict = Depends(current_user)):
    if cu["role"] != "admin":
        raise HTTPException(403, "Admin only")
    db = get_db()
    try:
        fields = {k: v for k, v in req.dict().items() if v is not None}
        if fields:
            set_clause = ", ".join(f"{k}=?" for k in fields)
            db.execute(f"UPDATE companies SET {set_clause} WHERE id=?", (*fields.values(), cu["company_id"]))
            db.commit()
        return dict(db.execute("SELECT * FROM companies WHERE id=?", (cu["company_id"],)).fetchone())
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# USERS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/users")
async def list_users(cu: dict = Depends(current_user)):
    if cu["role"] != "admin":
        raise HTTPException(403, "Admin only")
    db = get_db()
    rows = db.execute(
        "SELECT id,email,full_name,role,is_active,module_permissions,created_at FROM users WHERE company_id=? ORDER BY id",
        (cu["company_id"],)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/users", status_code=201)
async def create_user(req: UserCreate, cu: dict = Depends(current_user)):
    if cu["role"] != "admin":
        raise HTTPException(403, "Admin only")
    if req.role not in ("admin", "accountant", "cashier", "auditor", "viewer"):
        raise HTTPException(400, "Invalid role")
    db = get_db()
    try:
        if db.execute("SELECT id FROM users WHERE email=?", (req.email,)).fetchone():
            raise HTTPException(400, "Email already registered")
        perms_json = json.dumps(req.module_permissions) if req.module_permissions is not None else None
        cur = db.execute(
            "INSERT INTO users(company_id,email,password_hash,full_name,role,module_permissions) VALUES(?,?,?,?,?,?)",
            (cu["company_id"], req.email, hash_pw(req.password), req.full_name, req.role, perms_json)
        )
        db.commit()
        u = db.execute(
            "SELECT id,email,full_name,role,is_active,module_permissions,created_at FROM users WHERE id=?",
            (cur.lastrowid,)
        ).fetchone()
        return dict(u)
    finally:
        db.close()

@app.put("/api/users/{uid}")
async def update_user(uid: int, req: UserUpdate, cu: dict = Depends(current_user)):
    if cu["role"] != "admin":
        raise HTTPException(403, "Admin only")
    db = get_db()
    try:
        user = db.execute(
            "SELECT id FROM users WHERE id=? AND company_id=?", (uid, cu["company_id"])
        ).fetchone()
        if not user:
            raise HTTPException(404, "User not found")
        sets, vals = [], []
        if req.full_name is not None:
            sets.append("full_name=?"); vals.append(req.full_name)
        if req.role is not None:
            if req.role not in ("admin","accountant","cashier","auditor","viewer"):
                raise HTTPException(400, "Invalid role")
            sets.append("role=?"); vals.append(req.role)
        if req.is_active is not None:
            sets.append("is_active=?"); vals.append(1 if req.is_active else 0)
        if req.module_permissions is not None:
            sets.append("module_permissions=?")
            vals.append(json.dumps(req.module_permissions) if req.module_permissions else None)
        if req.password:
            if len(req.password) < 8:
                raise HTTPException(400, "Password must be at least 8 characters")
            sets.append("password_hash=?"); vals.append(hash_pw(req.password))
        if sets:
            vals.append(uid)
            db.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)
            db.commit()
        u = db.execute(
            "SELECT id,email,full_name,role,is_active,module_permissions,created_at FROM users WHERE id=?",
            (uid,)
        ).fetchone()
        return dict(u)
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# CHART OF ACCOUNTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/accounts")
async def list_accounts(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT a.*,
               COALESCE(SUM(CASE WHEN je.status='posted' THEN jl.debit ELSE 0 END),0) as total_debit,
               COALESCE(SUM(CASE WHEN je.status='posted' THEN jl.credit ELSE 0 END),0) as total_credit
        FROM accounts a
        LEFT JOIN journal_lines jl ON jl.account_id=a.id
        LEFT JOIN journal_entries je ON jl.entry_id=je.id
        WHERE a.company_id=?
        GROUP BY a.id ORDER BY a.code
    """, (cu["company_id"],)).fetchall()
    db.close()
    result = []
    for r in rows:
        row = dict(r)
        if row["normal_balance"] == "debit":
            row["balance"] = row["total_debit"] - row["total_credit"]
        else:
            row["balance"] = row["total_credit"] - row["total_debit"]
        result.append(row)
    return result

@app.post("/api/accounts", status_code=201)
async def create_account(req: AccountCreate, cu: dict = Depends(current_user)):
    if req.type not in NORMAL_BALANCE:
        raise HTTPException(400, f"Invalid type '{req.type}'")
    db = get_db()
    try:
        if db.execute("SELECT id FROM accounts WHERE company_id=? AND code=?",
                      (cu["company_id"], req.code)).fetchone():
            raise HTTPException(400, f"Code {req.code} already exists")
        cur = db.execute(
            "INSERT INTO accounts(company_id,code,name,type,sub_type,parent_id,normal_balance,description) VALUES(?,?,?,?,?,?,?,?)",
            (cu["company_id"], req.code, req.name, req.type, req.sub_type,
             req.parent_id, NORMAL_BALANCE[req.type], req.description)
        )
        audit(db, cu["id"], "create_account", "accounts", cur.lastrowid)
        db.commit()
        return dict(db.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        db.close()

@app.put("/api/accounts/{aid}")
async def update_account(aid: int, req: AccountUpdate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        acc = db.execute("SELECT * FROM accounts WHERE id=? AND company_id=?",
                         (aid, cu["company_id"])).fetchone()
        if not acc:
            raise HTTPException(404, "Account not found")
        if acc["is_system"] and req.name:
            raise HTTPException(400, "Cannot rename system accounts")
        fields = {k: v for k, v in req.dict().items() if v is not None}
        if fields:
            set_clause = ", ".join(f"{k}=?" for k in fields)
            db.execute(f"UPDATE accounts SET {set_clause} WHERE id=?", (*fields.values(), aid))
            audit(db, cu["id"], "update_account", "accounts", aid)
            db.commit()
        return dict(db.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone())
    finally:
        db.close()

@app.delete("/api/accounts/{aid}")
async def delete_account(aid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        acc = db.execute("SELECT * FROM accounts WHERE id=? AND company_id=?",
                         (aid, cu["company_id"])).fetchone()
        if not acc:
            raise HTTPException(404, "Account not found")
        if acc["is_system"]:
            raise HTTPException(400, "Cannot delete system accounts")
        if db.execute("SELECT COUNT(*) as c FROM journal_lines WHERE account_id=?",
                      (aid,)).fetchone()["c"] > 0:
            raise HTTPException(400, "Account has transactions — cannot delete")
        db.execute("DELETE FROM accounts WHERE id=?", (aid,))
        audit(db, cu["id"], "delete_account", "accounts", aid)
        db.commit()
        return {"message": "Account deleted"}
    finally:
        db.close()

DEFAULT_ACCOUNTS = [
    # (code, name, type, sub_type, parent_code)
    ("1000","Current Assets","asset","current_asset",None),
    ("1010","Cash on Hand","asset","cash","1000"),
    ("1020","Petty Cash","asset","cash","1000"),
    ("1030","Bank Account - Main","asset","bank","1000"),
    ("1040","Bank Account - Savings","asset","bank","1000"),
    ("1100","Accounts Receivable","asset","receivable","1000"),
    ("1110","Trade Debtors","asset","receivable","1100"),
    ("1120","Other Debtors","asset","receivable","1100"),
    ("1200","Inventory","asset","inventory","1000"),
    ("1300","Prepaid Expenses","asset","current_asset","1000"),
    ("1400","Other Current Assets","asset","current_asset","1000"),
    ("1500","Non-Current Assets","asset","fixed_asset",None),
    ("1510","Land","asset","fixed_asset","1500"),
    ("1520","Buildings","asset","fixed_asset","1500"),
    ("1530","Vehicles","asset","fixed_asset","1500"),
    ("1540","Equipment & Machinery","asset","fixed_asset","1500"),
    ("1550","Computer Equipment","asset","fixed_asset","1500"),
    ("1560","Furniture & Fittings","asset","fixed_asset","1500"),
    ("1600","Accumulated Depreciation","asset","contra_asset","1500"),
    ("1610","Accum. Depr. - Buildings","asset","contra_asset","1600"),
    ("1620","Accum. Depr. - Vehicles","asset","contra_asset","1600"),
    ("1630","Accum. Depr. - Equipment","asset","contra_asset","1600"),
    ("1640","Accum. Depr. - Computers","asset","contra_asset","1600"),
    ("1650","Accum. Depr. - Furniture","asset","contra_asset","1600"),
    ("2000","Current Liabilities","liability","current_liability",None),
    ("2010","Accounts Payable","liability","payable","2000"),
    ("2020","Trade Creditors","liability","payable","2010"),
    ("2030","Other Creditors","liability","payable","2010"),
    ("2100","Accrued Liabilities","liability","current_liability","2000"),
    ("2200","Taxes Payable","liability","tax_payable","2000"),
    ("2210","VAT / Sales Tax Payable","liability","tax_payable","2200"),
    ("2220","PAYE Tax Payable","liability","tax_payable","2200"),
    ("2230","Income Tax Payable","liability","tax_payable","2200"),
    ("2300","Short-term Loans","liability","loan","2000"),
    ("2400","Deferred Revenue","liability","deferred","2000"),
    ("2500","Non-Current Liabilities","liability","non_current_liability",None),
    ("2510","Long-term Loans","liability","loan","2500"),
    ("2600","Other Long-term Liabilities","liability","non_current_liability","2500"),
    ("3000","Equity","equity","equity",None),
    ("3010","Share Capital","equity","share_capital","3000"),
    ("3020","Owner's Equity","equity","owners_equity","3000"),
    ("3100","Retained Earnings","equity","retained_earnings","3000"),
    ("3200","Current Year Profit / Loss","equity","retained_earnings","3000"),
    ("3300","Drawings","equity","drawings","3000"),
    ("4000","Revenue","revenue","revenue",None),
    ("4010","Sales Revenue","revenue","sales","4000"),
    ("4020","Service Revenue","revenue","service","4000"),
    ("4100","Other Income","revenue","other_income","4000"),
    ("4110","Interest Income","revenue","interest","4100"),
    ("4120","Gain on Sale of Assets","revenue","gain","4100"),
    ("4130","Rental Income","revenue","rental","4100"),
    ("5000","Cost of Sales","expense","cost_of_sales",None),
    ("5010","Cost of Goods Sold","expense","cost_of_sales","5000"),
    ("5020","Direct Labour","expense","direct_cost","5000"),
    ("5030","Direct Materials","expense","direct_cost","5000"),
    ("6000","Operating Expenses","expense","operating_expense",None),
    ("6010","Salaries & Wages","expense","payroll","6000"),
    ("6020","Employee Benefits","expense","payroll","6000"),
    ("6100","Rent & Occupancy","expense","overhead","6000"),
    ("6200","Utilities","expense","overhead","6000"),
    ("6210","Electricity","expense","overhead","6200"),
    ("6220","Water","expense","overhead","6200"),
    ("6300","Communication","expense","overhead","6000"),
    ("6310","Telephone","expense","overhead","6300"),
    ("6320","Internet","expense","overhead","6300"),
    ("6400","Transport & Travel","expense","overhead","6000"),
    ("6500","Marketing & Advertising","expense","overhead","6000"),
    ("6600","Professional Fees","expense","professional","6000"),
    ("6610","Accounting & Audit Fees","expense","professional","6600"),
    ("6620","Legal Fees","expense","professional","6600"),
    ("6700","Insurance","expense","overhead","6000"),
    ("6800","Repairs & Maintenance","expense","overhead","6000"),
    ("6900","Depreciation Expense","expense","depreciation","6000"),
    ("7000","Finance Costs","expense","finance",None),
    ("7010","Bank Charges & Fees","expense","finance","7000"),
    ("7020","Interest Expense","expense","finance","7000"),
    ("7100","Other Expenses","expense","other_expense",None),
    ("7110","Office Supplies","expense","other_expense","7100"),
    ("7120","Bad Debts","expense","other_expense","7100"),
    ("7130","Miscellaneous Expenses","expense","other_expense","7100"),
]

@app.post("/api/accounts/seed")
async def seed_accounts(cu: dict = Depends(current_user)):
    if cu["role"] != "admin":
        raise HTTPException(403, "Admin only")
    db = get_db()
    try:
        if db.execute("SELECT COUNT(*) as c FROM accounts WHERE company_id=?",
                      (cu["company_id"],)).fetchone()["c"] > 0:
            raise HTTPException(400, "Accounts already exist")
        code_map = {}
        for code, name, acc_type, sub_type, parent_code in DEFAULT_ACCOUNTS:
            parent_id = code_map.get(parent_code)
            cur = db.execute(
                "INSERT INTO accounts(company_id,code,name,type,sub_type,parent_id,normal_balance,is_system) VALUES(?,?,?,?,?,?,?,1)",
                (cu["company_id"], code, name, acc_type, sub_type, parent_id, NORMAL_BALANCE[acc_type])
            )
            code_map[code] = cur.lastrowid
        audit(db, cu["id"], "seed_accounts", "accounts", details=f"{len(DEFAULT_ACCOUNTS)} accounts")
        db.commit()
        return {"message": f"Seeded {len(DEFAULT_ACCOUNTS)} accounts successfully"}
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# JOURNAL ENTRIES
# ═══════════════════════════════════════════════════════════════════════════════

def next_entry_number(db, company_id: int) -> str:
    n = db.execute("SELECT COUNT(*) as c FROM journal_entries WHERE company_id=?",
                   (company_id,)).fetchone()["c"]
    return f"JE-{n + 1:06d}"

@app.get("/api/journals")
async def list_journals(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    search: Optional[str] = None,
    cu: dict = Depends(current_user)
):
    db = get_db()
    try:
        conds = ["je.company_id=?"]
        params = [cu["company_id"]]
        if status:
            conds.append("je.status=?"); params.append(status)
        if date_from:
            conds.append("je.date>=?"); params.append(date_from)
        if date_to:
            conds.append("je.date<=?"); params.append(date_to)
        if search:
            conds.append("(je.description LIKE ? OR je.entry_number LIKE ? OR je.reference LIKE ?)")
            params += [f"%{search}%", f"%{search}%", f"%{search}%"]
        where = " AND ".join(conds)
        total = db.execute(f"SELECT COUNT(*) as c FROM journal_entries je WHERE {where}", params).fetchone()["c"]
        offset = (page - 1) * per_page
        rows = db.execute(f"""
            SELECT je.*, u.full_name as created_by_name,
                   COALESCE(SUM(jl.debit),0) as total_amount
            FROM journal_entries je
            LEFT JOIN users u ON je.created_by=u.id
            LEFT JOIN journal_lines jl ON jl.entry_id=je.id
            WHERE {where}
            GROUP BY je.id ORDER BY je.date DESC, je.id DESC
            LIMIT ? OFFSET ?
        """, [*params, per_page, offset]).fetchall()
        return {"items": [dict(r) for r in rows], "total": total,
                "page": page, "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page)}
    finally:
        db.close()

@app.get("/api/journals/{eid}")
async def get_journal(eid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        entry = db.execute(
            "SELECT je.*, u.full_name as created_by_name FROM journal_entries je LEFT JOIN users u ON je.created_by=u.id WHERE je.id=? AND je.company_id=?",
            (eid, cu["company_id"])
        ).fetchone()
        if not entry:
            raise HTTPException(404, "Journal entry not found")
        lines = db.execute(
            "SELECT jl.*, a.code as account_code, a.name as account_name FROM journal_lines jl JOIN accounts a ON jl.account_id=a.id WHERE jl.entry_id=?",
            (eid,)
        ).fetchall()
        result = dict(entry)
        result["lines"] = [dict(l) for l in lines]
        return result
    finally:
        db.close()

@app.post("/api/journals", status_code=201)
async def create_journal(req: JournalCreate, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant", "cashier"):
        raise HTTPException(403, "Insufficient permissions")
    total_dr = round(sum(l.debit for l in req.lines), 6)
    total_cr = round(sum(l.credit for l in req.lines), 6)
    if abs(total_dr - total_cr) > 0.005:
        raise HTTPException(400, f"Debits ({total_dr:.2f}) must equal credits ({total_cr:.2f})")
    if total_dr == 0:
        raise HTTPException(400, "Journal entry cannot have zero amount")
    db = get_db()
    try:
        entry_num = next_entry_number(db, cu["company_id"])
        cur = db.execute(
            "INSERT INTO journal_entries(company_id,entry_number,date,description,reference,created_by) VALUES(?,?,?,?,?,?)",
            (cu["company_id"], entry_num, req.date, req.description, req.reference, cu["id"])
        )
        eid = cur.lastrowid
        for line in req.lines:
            acc = db.execute("SELECT id FROM accounts WHERE id=? AND company_id=? AND is_active=1",
                             (line.account_id, cu["company_id"])).fetchone()
            if not acc:
                raise HTTPException(400, f"Account {line.account_id} not found")
            db.execute(
                "INSERT INTO journal_lines(entry_id,account_id,description,debit,credit) VALUES(?,?,?,?,?)",
                (eid, line.account_id, line.description, line.debit, line.credit)
            )
        audit(db, cu["id"], "create_journal", "journal_entries", eid)
        db.commit()
        return {"id": eid, "entry_number": entry_num, "message": "Journal entry posted"}
    except HTTPException:
        db.rollback(); raise
    except Exception as e:
        db.rollback(); raise HTTPException(500, str(e))
    finally:
        db.close()

@app.post("/api/journals/{eid}/void")
async def void_journal(eid: int, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"):
        raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        entry = db.execute("SELECT * FROM journal_entries WHERE id=? AND company_id=?",
                           (eid, cu["company_id"])).fetchone()
        if not entry:
            raise HTTPException(404, "Journal entry not found")
        if entry["status"] == "void":
            raise HTTPException(400, "Already voided")
        db.execute("UPDATE journal_entries SET status='void' WHERE id=?", (eid,))
        audit(db, cu["id"], "void_journal", "journal_entries", eid)
        db.commit()
        return {"message": "Journal entry voided"}
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# GENERAL LEDGER
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/ledger")
async def general_ledger(
    account_id: Optional[int] = None,
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    from_date:  Optional[str] = None,   # alias accepted from frontend
    to_date:    Optional[str] = None,   # alias accepted from frontend
    cu: dict = Depends(current_user)
):
    d_from = date_from or from_date
    d_to   = date_to   or to_date
    db = get_db()
    try:
        conds = ["je.company_id=?", "je.status='posted'"]
        params = [cu["company_id"]]
        if account_id:
            conds.append("jl.account_id=?"); params.append(account_id)
        if d_from:
            conds.append("je.date>=?"); params.append(d_from)
        if d_to:
            conds.append("je.date<=?"); params.append(d_to)
        where = " AND ".join(conds)
        rows = db.execute(f"""
            SELECT je.date, je.entry_number,
                   COALESCE(jl.description, je.description) as description,
                   je.description as entry_desc,
                   je.reference, jl.description as line_desc,
                   a.code as account_code, a.name as account_name,
                   a.type as account_type, a.normal_balance,
                   jl.debit, jl.credit, jl.account_id
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id=je.id
            JOIN accounts a ON jl.account_id=a.id
            WHERE {where}
            ORDER BY je.date ASC, je.id ASC
        """, params).fetchall()
        transactions = []
        balances = {}
        for row in rows:
            r = dict(row)
            aid = r["account_id"]
            if aid not in balances:
                balances[aid] = 0.0
            if r["normal_balance"] == "debit":
                balances[aid] += r["debit"] - r["credit"]
            else:
                balances[aid] += r["credit"] - r["debit"]
            r["running_balance"] = round(balances[aid], 2)
            transactions.append(r)
        # Return structured response that frontend can use
        return {
            "transactions": transactions,
            "opening_balance": 0,
            "count": len(transactions),
        }
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/reports/trial-balance")
async def trial_balance(date_to: Optional[str] = None, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        date_cond = f"AND je.date<='{date_to}'" if date_to else ""
        rows = db.execute(f"""
            SELECT a.id, a.code, a.name, a.type, a.sub_type, a.normal_balance,
                   COALESCE(SUM(CASE WHEN je.status='posted' {date_cond} THEN jl.debit ELSE 0 END),0) as total_debit,
                   COALESCE(SUM(CASE WHEN je.status='posted' {date_cond} THEN jl.credit ELSE 0 END),0) as total_credit
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id=a.id
            LEFT JOIN journal_entries je ON jl.entry_id=je.id
            WHERE a.company_id=? AND a.is_active=1
            GROUP BY a.id
            HAVING total_debit>0 OR total_credit>0
            ORDER BY a.code
        """, (cu["company_id"],)).fetchall()
        items = []
        tot_dr = tot_cr = 0.0
        for row in rows:
            r = dict(row)
            net = r["total_debit"] - r["total_credit"]
            if r["normal_balance"] == "debit":
                r["balance_debit"] = max(net, 0)
                r["balance_credit"] = max(-net, 0)
            else:
                r["balance_credit"] = max(-net, 0)
                r["balance_debit"] = max(net, 0)
            tot_dr += r["balance_debit"]
            tot_cr += r["balance_credit"]
            items.append(r)
        return {"items": items, "total_debit": round(tot_dr, 2),
                "total_credit": round(tot_cr, 2),
                "balanced": abs(tot_dr - tot_cr) < 0.01}
    finally:
        db.close()

@app.get("/api/reports/income-statement")
async def income_statement(
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    from_date:  Optional[str] = None,   # alias
    to_date:    Optional[str] = None,   # alias
    cu: dict = Depends(current_user)
):
    d_from = date_from or from_date
    d_to   = date_to   or to_date
    db = get_db()
    try:
        conds = ["je.company_id=?", "je.status='posted'", "a.type IN ('revenue','expense')"]
        params = [cu["company_id"]]
        if d_from:
            conds.append("je.date>=?"); params.append(d_from)
        if d_to:
            conds.append("je.date<=?"); params.append(d_to)
        where = " AND ".join(conds)
        rows = db.execute(f"""
            SELECT a.id, a.code, a.name, a.type, a.sub_type, a.normal_balance,
                   COALESCE(SUM(jl.debit),0) as total_debit,
                   COALESCE(SUM(jl.credit),0) as total_credit
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id=a.id
            LEFT JOIN journal_entries je ON jl.entry_id=je.id
            WHERE {where}
            GROUP BY a.id HAVING total_debit>0 OR total_credit>0
            ORDER BY a.code
        """, params).fetchall()
        revenue, expenses = [], []
        tot_rev = tot_exp = 0.0
        for row in rows:
            r = dict(row)
            if r["type"] == "revenue":
                r["amount"] = r["total_credit"] - r["total_debit"]
                tot_rev += r["amount"]
                revenue.append(r)
            else:
                r["amount"] = r["total_debit"] - r["total_credit"]
                tot_exp += r["amount"]
                expenses.append(r)
        return {"revenue": revenue, "expenses": expenses,
                "total_revenue": round(tot_rev, 2), "total_expenses": round(tot_exp, 2),
                "net_profit": round(tot_rev - tot_exp, 2)}
    finally:
        db.close()

@app.get("/api/reports/balance-sheet")
async def balance_sheet(
    date_to: Optional[str] = None,
    as_of:   Optional[str] = None,   # alias accepted from frontend
    cu: dict = Depends(current_user)
):
    d_to = date_to or as_of
    db = get_db()
    try:
        conds = ["je.company_id=?", "je.status='posted'", "a.type IN ('asset','liability','equity')"]
        params = [cu["company_id"]]
        if d_to:
            conds.append("je.date<=?"); params.append(d_to)
        where = " AND ".join(conds)
        rows = db.execute(f"""
            SELECT a.id, a.code, a.name, a.type, a.sub_type, a.normal_balance,
                   COALESCE(SUM(jl.debit),0) as total_debit,
                   COALESCE(SUM(jl.credit),0) as total_credit
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id=a.id
            LEFT JOIN journal_entries je ON jl.entry_id=je.id
            WHERE {where}
            GROUP BY a.id ORDER BY a.code
        """, params).fetchall()
        assets, liabilities, equity = [], [], []
        tot_a = tot_l = tot_e = 0.0
        for row in rows:
            r = dict(row)
            if r["normal_balance"] == "debit":
                r["balance"] = r["total_debit"] - r["total_credit"]
            else:
                r["balance"] = r["total_credit"] - r["total_debit"]
            r["amount"] = r["balance"]   # alias for frontend compatibility
            if r["type"] == "asset":
                tot_a += r["balance"]; assets.append(r)
            elif r["type"] == "liability":
                tot_l += r["balance"]; liabilities.append(r)
            else:
                tot_e += r["balance"]; equity.append(r)
        return {"assets": assets, "liabilities": liabilities, "equity": equity,
                "total_assets": round(tot_a, 2), "total_liabilities": round(tot_l, 2),
                "total_equity": round(tot_e, 2)}
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/dashboard")
async def dashboard(cu: dict = Depends(current_user)):
    db = get_db()
    try:
        cid = cu["company_id"]
        today = datetime.now()
        m_start = today.replace(day=1).strftime("%Y-%m-%d")
        m_end = today.strftime("%Y-%m-%d")
        y_start = today.replace(month=1, day=1).strftime("%Y-%m-%d")

        total_accounts = db.execute(
            "SELECT COUNT(*) as c FROM accounts WHERE company_id=? AND is_active=1", (cid,)
        ).fetchone()["c"]
        total_journals = db.execute(
            "SELECT COUNT(*) as c FROM journal_entries WHERE company_id=? AND status='posted'", (cid,)
        ).fetchone()["c"]

        def amount_query(account_types, dr_or_cr, date_start=None, date_end=None):
            type_list = ",".join(f"'{t}'" for t in account_types)
            conds = [f"je.company_id={cid}", "je.status='posted'", f"a.type IN ({type_list})"]
            if date_start: conds.append(f"je.date>='{date_start}'")
            if date_end:   conds.append(f"je.date<='{date_end}'")
            col = "jl.credit - jl.debit" if dr_or_cr == "cr" else "jl.debit - jl.credit"
            row = db.execute(f"""
                SELECT COALESCE(SUM({col}),0) as v FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id=je.id
                JOIN accounts a ON jl.account_id=a.id
                WHERE {" AND ".join(conds)}
            """).fetchone()
            return row["v"] if row else 0

        month_revenue  = amount_query(["revenue"],  "cr", m_start, m_end)
        month_expenses = amount_query(["expense"],  "dr", m_start, m_end)
        ytd_revenue    = amount_query(["revenue"],  "cr", y_start, m_end)
        ytd_expenses   = amount_query(["expense"],  "dr", y_start, m_end)
        cash_balance   = amount_query(["asset"],    "dr")

        recent = db.execute("""
            SELECT je.entry_number, je.date, je.description, je.status,
                   COALESCE(SUM(jl.debit),0) as amount
            FROM journal_entries je
            LEFT JOIN journal_lines jl ON jl.entry_id=je.id
            WHERE je.company_id=?
            GROUP BY je.id ORDER BY je.created_at DESC LIMIT 8
        """, (cid,)).fetchall()

        return {
            "total_accounts": total_accounts,
            "total_journals": total_journals,
            "month_revenue": round(month_revenue, 2),
            "month_expenses": round(month_expenses, 2),
            "month_net": round(month_revenue - month_expenses, 2),
            "ytd_revenue": round(ytd_revenue, 2),
            "ytd_expenses": round(ytd_expenses, 2),
            "ytd_net": round(ytd_revenue - ytd_expenses, 2),
            "cash_balance": round(cash_balance, 2),
            "recent_journals": [dict(r) for r in recent],
        }
    finally:
        db.close()

# ── Live Cash-Flow Dashboard ──────────────────────────────────────────────────

@app.get("/api/dashboard/cashflow")
async def cashflow_dashboard(
    horizon: int = Query(30, ge=7, le=90),
    cu: dict = Depends(current_user)
):
    from datetime import date as _date, timedelta
    db   = get_db()
    cid  = cu["company_id"]
    today = datetime.now().date()

    try:
        # ── Current cash: all cash/bank-sub_type asset accounts ──────────────
        cash_rows = db.execute("""
            SELECT a.id, a.code, a.name,
                   COALESCE(SUM(jl.debit - jl.credit), 0) as balance
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id = a.id
            LEFT JOIN journal_entries je
                   ON jl.entry_id = je.id AND je.status = 'posted' AND je.company_id = ?
            WHERE a.company_id = ? AND a.type = 'asset'
              AND (a.sub_type IN ('cash','bank','cash_and_bank')
                   OR a.code IN ('1010','1020','1030','1040'))
            GROUP BY a.id ORDER BY a.code
        """, (cid, cid)).fetchall()

        cash_accounts  = [dict(r) for r in cash_rows]
        current_cash   = round(sum(r["balance"] for r in cash_rows), 2)

        # ── Upcoming receivables (invoices due within horizon days) ──────────
        horizon_end  = (today + timedelta(days=horizon)).strftime("%Y-%m-%d")
        today_s      = today.strftime("%Y-%m-%d")

        receivables = db.execute("""
            SELECT i.id, i.invoice_number, i.due_date, i.balance_due,
                   c.name as party
            FROM invoices i JOIN customers c ON i.customer_id = c.id
            WHERE i.company_id = ? AND i.status NOT IN ('paid','void','draft')
              AND i.balance_due > 0
              AND i.due_date BETWEEN ? AND ?
            ORDER BY i.due_date
        """, (cid, today_s, horizon_end)).fetchall()

        overdue_inv = db.execute("""
            SELECT COUNT(*) as cnt, COALESCE(SUM(balance_due),0) as total
            FROM invoices
            WHERE company_id=? AND status NOT IN ('paid','void')
              AND due_date < ? AND balance_due > 0
        """, (cid, today_s)).fetchone()

        # ── Upcoming payables (bills due within horizon days) ─────────────────
        payables = db.execute("""
            SELECT b.id, b.bill_number, b.due_date, b.balance_due,
                   s.name as party
            FROM bills b JOIN suppliers s ON b.supplier_id = s.id
            WHERE b.company_id = ? AND b.status NOT IN ('paid','void')
              AND b.balance_due > 0
              AND b.due_date BETWEEN ? AND ?
            ORDER BY b.due_date
        """, (cid, today_s, horizon_end)).fetchall()

        overdue_bill = db.execute("""
            SELECT COUNT(*) as cnt, COALESCE(SUM(balance_due),0) as total
            FROM bills
            WHERE company_id=? AND status NOT IN ('paid','void')
              AND due_date < ? AND balance_due > 0
        """, (cid, today_s)).fetchone()

        # ── Daily forecast ────────────────────────────────────────────────────
        recv_by_day = {}
        for r in receivables:
            recv_by_day.setdefault(r["due_date"], 0)
            recv_by_day[r["due_date"]] += r["balance_due"]

        pay_by_day = {}
        for b in payables:
            pay_by_day.setdefault(b["due_date"], 0)
            pay_by_day[b["due_date"]] += b["balance_due"]

        daily = []
        running = current_cash
        for i in range(horizon):
            d     = (today + timedelta(days=i)).strftime("%Y-%m-%d")
            c_in  = round(recv_by_day.get(d, 0), 2)
            c_out = round(pay_by_day.get(d,  0), 2)
            running = round(running + c_in - c_out, 2)
            daily.append({"date": d, "cash_in": c_in, "cash_out": c_out,
                           "balance": running})

        # ── 7-day / 30-day totals ─────────────────────────────────────────────
        d7_end = (today + timedelta(days=7)).strftime("%Y-%m-%d")
        in_7d  = round(sum(v for k, v in recv_by_day.items() if k <= d7_end), 2)
        out_7d = round(sum(v for k, v in pay_by_day.items()  if k <= d7_end), 2)
        in_30d = round(sum(recv_by_day.values()), 2)
        out_30d= round(sum(pay_by_day.values()),  2)
        proj_30d = round(current_cash + in_30d - out_30d, 2)

        # ── Monthly burn (avg last 3 months of expenses) ──────────────────────
        three_ago = (today - timedelta(days=90)).strftime("%Y-%m-%d")
        burn_row = db.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) / 3.0 as monthly_burn
            FROM accounts a
            JOIN journal_lines jl ON jl.account_id = a.id
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE a.company_id=? AND je.company_id=? AND je.status='posted'
              AND a.type='expense' AND je.date >= ?
        """, (cid, cid, three_ago)).fetchone()
        monthly_burn = round(burn_row["monthly_burn"] if burn_row else 0, 2)
        runway_days  = int(current_cash / monthly_burn * 30) if monthly_burn > 0 else 999

        # ── Minimum balance in forecast window ───────────────────────────────
        min_day  = min(daily, key=lambda d: d["balance"]) if daily else None
        min_bal  = min_day["balance"] if min_day else current_cash
        min_date = min_day["date"]    if min_day else today_s

        # ── Smart alerts ──────────────────────────────────────────────────────
        alerts = []
        if overdue_inv and overdue_inv["cnt"] > 0:
            alerts.append({"type": "danger",
                           "icon": "🔴",
                           "msg": f"{int(overdue_inv['cnt'])} overdue invoice{'s' if overdue_inv['cnt']>1 else ''} "
                                  f"totalling {round(overdue_inv['total'], 2):.2f}"})
        if overdue_bill and overdue_bill["cnt"] > 0:
            alerts.append({"type": "warning",
                           "icon": "🟡",
                           "msg": f"{int(overdue_bill['cnt'])} overdue bill{'s' if overdue_bill['cnt']>1 else ''} "
                                  f"totalling {round(overdue_bill['total'], 2):.2f}"})
        if min_bal < 0:
            alerts.append({"type": "danger",
                           "icon": "🚨",
                           "msg": f"Cash is projected to go NEGATIVE ({min_bal:.2f}) around {min_date}"})
        elif min_bal < monthly_burn * 0.5 and monthly_burn > 0:
            alerts.append({"type": "warning",
                           "icon": "⚠️",
                           "msg": f"Cash may fall to {min_bal:.2f} around {min_date} — less than half a month's expenses"})
        elif runway_days < 30 and monthly_burn > 0:
            alerts.append({"type": "warning",
                           "icon": "⚠️",
                           "msg": f"Cash runway is only {runway_days} days at current burn rate"})

        # ── Next payable due ──────────────────────────────────────────────────
        next_payables = [dict(r) for r in payables[:5]]
        next_receivables = [dict(r) for r in receivables[:5]]

        # ── Today's net ───────────────────────────────────────────────────────
        today_row = next((d for d in daily if d["date"] == today_s), None)

        return {
            "current_cash":      current_cash,
            "cash_accounts":     cash_accounts,
            "in_7d":             in_7d,
            "out_7d":            out_7d,
            "in_30d":            in_30d,
            "out_30d":           out_30d,
            "projected_balance": proj_30d,
            "monthly_burn":      monthly_burn,
            "runway_days":       runway_days,
            "daily_forecast":    daily,
            "upcoming_receivables": next_receivables,
            "upcoming_payables":    next_payables,
            "overdue_invoices":  dict(overdue_inv) if overdue_inv else {"cnt": 0, "total": 0},
            "overdue_bills":     dict(overdue_bill) if overdue_bill else {"cnt": 0, "total": 0},
            "alerts":            alerts,
            "horizon_days":      horizon,
            "today":             today_s,
        }
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_acc_by_code(db, company_id, code):
    return db.execute(
        "SELECT * FROM accounts WHERE company_id=? AND code=? AND is_active=1", (company_id, code)
    ).fetchone()

def get_acc_by_subtype(db, company_id, sub_type):
    return db.execute(
        "SELECT * FROM accounts WHERE company_id=? AND sub_type=? AND is_active=1 ORDER BY code LIMIT 1",
        (company_id, sub_type)
    ).fetchone()

def auto_post_gl(db, company_id, user_id, date, description, lines):
    """lines = [(account_id, debit, credit, desc), ...]"""
    entry_num = next_entry_number(db, company_id)
    cur = db.execute(
        "INSERT INTO journal_entries(company_id,entry_number,date,description,created_by) VALUES(?,?,?,?,?)",
        (company_id, entry_num, date, description, user_id)
    )
    eid = cur.lastrowid
    for account_id, debit, credit, desc in lines:
        db.execute(
            "INSERT INTO journal_lines(entry_id,account_id,description,debit,credit) VALUES(?,?,?,?,?)",
            (eid, account_id, desc, round(debit, 2), round(credit, 2))
        )
    return eid

# ═══════════════════════════════════════════════════════════════════════════════
# CUSTOMERS
# ═══════════════════════════════════════════════════════════════════════════════

class CustomerCreate(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    currency: Optional[str] = None
    tax_number: Optional[str] = None
    credit_limit: float = 0
    payment_terms: int = 30
    notes: Optional[str] = None

class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    currency: Optional[str] = None
    tax_number: Optional[str] = None
    credit_limit: Optional[float] = None
    payment_terms: Optional[int] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None

@app.get("/api/customers")
async def list_customers(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT c.*,
               COALESCE(SUM(CASE WHEN i.status NOT IN ('paid','void') THEN i.balance_due ELSE 0 END),0) as outstanding,
               COALESCE(SUM(CASE WHEN i.status NOT IN ('paid','void') THEN i.balance_due ELSE 0 END),0) as outstanding_balance
        FROM customers c
        LEFT JOIN invoices i ON i.customer_id=c.id
        WHERE c.company_id=? GROUP BY c.id ORDER BY c.name
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/customers", status_code=201)
async def create_customer(req: CustomerCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO customers(company_id,name,email,phone,address,tax_number,credit_limit,payment_terms,notes) VALUES(?,?,?,?,?,?,?,?,?)",
            (cu["company_id"], req.name, req.email, req.phone,
             " ".join(filter(None, [req.address, req.city, req.country])) or req.address,
             req.tax_number, req.credit_limit, req.payment_terms, req.notes)
        )
        db.commit()
        return dict(db.execute("SELECT * FROM customers WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        db.close()

@app.put("/api/customers/{cid}")
async def update_customer(cid: int, req: CustomerUpdate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        if not db.execute("SELECT id FROM customers WHERE id=? AND company_id=?", (cid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Customer not found")
        fields = {k: v for k, v in req.dict().items() if v is not None}
        if fields:
            db.execute(f"UPDATE customers SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), cid))
            db.commit()
        return dict(db.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone())
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# SUPPLIERS
# ═══════════════════════════════════════════════════════════════════════════════

class SupplierCreate(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    currency: Optional[str] = None
    tax_number: Optional[str] = None
    payment_terms: int = 30
    notes: Optional[str] = None

class SupplierUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    currency: Optional[str] = None
    tax_number: Optional[str] = None
    payment_terms: Optional[int] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None

@app.get("/api/suppliers")
async def list_suppliers(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT s.*,
               COALESCE(SUM(CASE WHEN b.status NOT IN ('paid','void') THEN b.balance_due ELSE 0 END),0) as outstanding,
               COALESCE(SUM(CASE WHEN b.status NOT IN ('paid','void') THEN b.balance_due ELSE 0 END),0) as outstanding_balance
        FROM suppliers s
        LEFT JOIN bills b ON b.supplier_id=s.id
        WHERE s.company_id=? GROUP BY s.id ORDER BY s.name
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/suppliers", status_code=201)
async def create_supplier(req: SupplierCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO suppliers(company_id,name,email,phone,address,tax_number,payment_terms,notes) VALUES(?,?,?,?,?,?,?,?)",
            (cu["company_id"], req.name, req.email, req.phone, req.address, req.tax_number, req.payment_terms, req.notes)
        )
        db.commit()
        return dict(db.execute("SELECT * FROM suppliers WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        db.close()

@app.put("/api/suppliers/{sid}")
async def update_supplier(sid: int, req: SupplierUpdate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        if not db.execute("SELECT id FROM suppliers WHERE id=? AND company_id=?", (sid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Supplier not found")
        fields = {k: v for k, v in req.dict().items() if v is not None}
        if fields:
            db.execute(f"UPDATE suppliers SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), sid))
            db.commit()
        return dict(db.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone())
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# INVOICES (AR)
# ═══════════════════════════════════════════════════════════════════════════════

class InvoiceLineIn(BaseModel):
    description: str
    account_id: Optional[int] = None
    quantity: float = 1
    unit_price: float = 0
    tax_rate: float = 0

class InvoiceCreate(BaseModel):
    customer_id: int
    date: str
    due_date: str
    notes: Optional[str] = None
    discount: float = 0
    lines: List[InvoiceLineIn] = Field(..., min_items=1)

def next_inv_num(db, company_id):
    n = db.execute("SELECT COUNT(*) as c FROM invoices WHERE company_id=?", (company_id,)).fetchone()["c"]
    return f"INV-{n+1:06d}"

@app.get("/api/invoices")
async def list_invoices(
    customer_id: Optional[int] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    overdue: Optional[bool] = None,
    cu: dict = Depends(current_user)
):
    db = get_db()
    try:
        conds = ["i.company_id=?"]
        params: list = [cu["company_id"]]
        if customer_id: conds.append("i.customer_id=?"); params.append(customer_id)
        if status:      conds.append("i.status=?");      params.append(status)
        if date_from:   conds.append("i.date>=?");       params.append(date_from)
        if date_to:     conds.append("i.date<=?");       params.append(date_to)
        if overdue:
            today_s = datetime.now().strftime("%Y-%m-%d")
            conds.append(f"i.due_date<'{today_s}' AND i.status NOT IN ('paid','void')")
        where = " AND ".join(conds)
        rows = db.execute(f"""
            SELECT i.*, i.total as total_amount, c.name as customer_name
            FROM invoices i JOIN customers c ON i.customer_id=c.id
            WHERE {where} ORDER BY i.date DESC, i.id DESC
        """, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

@app.get("/api/invoices/{inv_id}")
async def get_invoice(inv_id: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        inv = db.execute("""
            SELECT i.*, c.name as customer_name, c.email as customer_email,
                   c.address as customer_address, c.tax_number as customer_tax
            FROM invoices i JOIN customers c ON i.customer_id=c.id
            WHERE i.id=? AND i.company_id=?
        """, (inv_id, cu["company_id"])).fetchone()
        if not inv: raise HTTPException(404, "Invoice not found")
        lines = db.execute("""
            SELECT il.*, a.code as account_code, a.name as account_name
            FROM invoice_lines il LEFT JOIN accounts a ON il.account_id=a.id
            WHERE il.invoice_id=?
        """, (inv_id,)).fetchall()
        pmts = db.execute(
            "SELECT * FROM receipts WHERE invoice_id=? ORDER BY date DESC", (inv_id,)
        ).fetchall()
        r = dict(inv)
        r["lines"] = [dict(l) for l in lines]
        r["payments"] = [dict(p) for p in pmts]
        return r
    finally:
        db.close()

@app.post("/api/invoices", status_code=201)
async def create_invoice(req: InvoiceCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        cust = db.execute("SELECT * FROM customers WHERE id=? AND company_id=?",
                          (req.customer_id, cu["company_id"])).fetchone()
        if not cust: raise HTTPException(400, "Customer not found")

        subtotal = tax_total = 0.0
        proc = []
        for ln in req.lines:
            net = round(ln.quantity * ln.unit_price, 4)
            tax = round(net * ln.tax_rate / 100, 4)
            proc.append((ln.description, ln.account_id, ln.quantity, ln.unit_price, ln.tax_rate, tax, net))
            subtotal += net; tax_total += tax

        discount = req.discount or 0.0
        total = round(subtotal + tax_total - discount, 2)
        inv_num = next_inv_num(db, cu["company_id"])

        cur = db.execute(
            "INSERT INTO invoices(company_id,invoice_number,customer_id,date,due_date,status,subtotal,tax_amount,discount,total,balance_due,notes,created_by) VALUES(?,?,?,?,?,'draft',?,?,?,?,?,?,?)",
            (cu["company_id"], inv_num, req.customer_id, req.date, req.due_date,
             round(subtotal,2), round(tax_total,2), discount, total, total, req.notes, cu["id"])
        )
        inv_id = cur.lastrowid
        for desc, acc_id, qty, up, tr, tax, net in proc:
            db.execute(
                "INSERT INTO invoice_lines(invoice_id,description,account_id,quantity,unit_price,tax_rate,tax_amount,line_total) VALUES(?,?,?,?,?,?,?,?)",
                (inv_id, desc, acc_id, qty, up, tr, round(tax,2), round(net,2))
            )

        # Auto-post GL: DR Trade Debtors, CR Revenue accounts, CR VAT Payable
        ar = get_acc_by_code(db, cu["company_id"], "1110") or get_acc_by_subtype(db, cu["company_id"], "receivable")
        if ar:
            gl = [(ar["id"], total, 0, f"Invoice {inv_num} – {cust['name']}")]
            for desc, acc_id, qty, up, tr, tax, net in proc:
                if acc_id:
                    gl.append((acc_id, 0, round(net,2), f"{inv_num}: {desc}"))
            if tax_total > 0:
                vat = get_acc_by_code(db, cu["company_id"], "2210") or get_acc_by_subtype(db, cu["company_id"], "tax_payable")
                if vat:
                    gl.append((vat["id"], 0, round(tax_total,2), f"VAT – {inv_num}"))
            eid = auto_post_gl(db, cu["company_id"], cu["id"], req.date,
                               f"Invoice {inv_num} – {cust['name']}", gl)
            db.execute("UPDATE invoices SET status='sent', journal_entry_id=? WHERE id=?", (eid, inv_id))

        db.commit()
        return {"id": inv_id, "invoice_number": inv_num, "total": total, "message": "Invoice created"}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

@app.post("/api/invoices/{inv_id}/void")
async def void_invoice(inv_id: int, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin","accountant"): raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        inv = db.execute("SELECT * FROM invoices WHERE id=? AND company_id=?", (inv_id, cu["company_id"])).fetchone()
        if not inv: raise HTTPException(404, "Invoice not found")
        if inv["status"] == "void": raise HTTPException(400, "Already voided")
        if inv["amount_paid"] > 0: raise HTTPException(400, "Cannot void invoice with payments recorded")
        if inv["journal_entry_id"]:
            db.execute("UPDATE journal_entries SET status='void' WHERE id=?", (inv["journal_entry_id"],))
        db.execute("UPDATE invoices SET status='void' WHERE id=?", (inv_id,))
        audit(db, cu["id"], "void_invoice", "invoices", inv_id)
        db.commit()
        return {"message": "Invoice voided"}
    finally: db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# RECEIPTS  (AR payments)
# ═══════════════════════════════════════════════════════════════════════════════

class ReceiptCreate(BaseModel):
    customer_id: int
    invoice_id: Optional[int] = None
    date: str
    amount: float = Field(..., gt=0)
    payment_method: str = "cash"
    reference: Optional[str] = None
    bank_account_id: Optional[int] = None

def next_rcpt_num(db, company_id):
    n = db.execute("SELECT COUNT(*) as c FROM receipts WHERE company_id=?", (company_id,)).fetchone()["c"]
    return f"RCP-{n+1:06d}"

@app.get("/api/receipts")
async def list_receipts(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT r.*, c.name as customer_name, i.invoice_number
        FROM receipts r
        JOIN customers c ON r.customer_id=c.id
        LEFT JOIN invoices i ON r.invoice_id=i.id
        WHERE r.company_id=? ORDER BY r.date DESC, r.id DESC
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/receipts", status_code=201)
async def create_receipt(req: ReceiptCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        cust = db.execute("SELECT * FROM customers WHERE id=? AND company_id=?",
                          (req.customer_id, cu["company_id"])).fetchone()
        if not cust: raise HTTPException(400, "Customer not found")

        inv = None
        if req.invoice_id:
            inv = db.execute("SELECT * FROM invoices WHERE id=? AND company_id=?",
                             (req.invoice_id, cu["company_id"])).fetchone()
            if not inv: raise HTTPException(400, "Invoice not found")
            if inv["status"] == "void": raise HTTPException(400, "Invoice is voided")
            if req.amount > inv["balance_due"] + 0.005:
                raise HTTPException(400, f"Amount exceeds balance due ({inv['balance_due']:.2f})")

        rcpt_num = next_rcpt_num(db, cu["company_id"])
        ar = get_acc_by_code(db, cu["company_id"], "1110") or get_acc_by_subtype(db, cu["company_id"], "receivable")
        if not ar: raise HTTPException(400, "No AR account found in Chart of Accounts")

        dr_id = None
        if req.bank_account_id:
            bk = db.execute("SELECT * FROM bank_accounts WHERE id=? AND company_id=?",
                            (req.bank_account_id, cu["company_id"])).fetchone()
            if bk: dr_id = bk["gl_account_id"]
        if not dr_id:
            cash = get_acc_by_code(db, cu["company_id"], "1010") or get_acc_by_subtype(db, cu["company_id"], "cash")
            if cash: dr_id = cash["id"]

        eid = None
        if dr_id:
            eid = auto_post_gl(db, cu["company_id"], cu["id"], req.date,
                               f"Receipt {rcpt_num} – {cust['name']}",
                               [(dr_id, req.amount, 0, f"Receipt {rcpt_num}"),
                                (ar["id"], 0, req.amount, f"Receipt {rcpt_num}")])

        cur = db.execute(
            "INSERT INTO receipts(company_id,receipt_number,customer_id,invoice_id,date,amount,payment_method,reference,bank_account_id,journal_entry_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cu["company_id"], rcpt_num, req.customer_id, req.invoice_id, req.date,
             req.amount, req.payment_method, req.reference, req.bank_account_id, eid, cu["id"])
        )
        if inv:
            new_paid = round(inv["amount_paid"] + req.amount, 2)
            new_bal  = max(round(inv["balance_due"] - req.amount, 2), 0)
            status   = "paid" if new_bal <= 0.005 else "partial"
            db.execute("UPDATE invoices SET amount_paid=?,balance_due=?,status=? WHERE id=?",
                       (new_paid, new_bal, status, req.invoice_id))

        db.commit()
        return {"id": cur.lastrowid, "receipt_number": rcpt_num, "message": "Receipt recorded"}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# BILLS  (AP – supplier invoices)
# ═══════════════════════════════════════════════════════════════════════════════

class BillLineIn(BaseModel):
    description: str
    account_id: Optional[int] = None
    quantity: float = 1
    unit_price: float = 0
    tax_rate: float = 0

class BillCreate(BaseModel):
    supplier_id: int
    supplier_ref: Optional[str] = None
    date: str
    due_date: str
    notes: Optional[str] = None
    lines: List[BillLineIn] = Field(..., min_items=1)

def next_bill_num(db, company_id):
    n = db.execute("SELECT COUNT(*) as c FROM bills WHERE company_id=?", (company_id,)).fetchone()["c"]
    return f"BILL-{n+1:06d}"

@app.get("/api/bills")
async def list_bills(
    supplier_id: Optional[int] = None,
    status: Optional[str] = None,
    overdue: Optional[bool] = None,
    cu: dict = Depends(current_user)
):
    db = get_db()
    try:
        conds = ["b.company_id=?"]; params: list = [cu["company_id"]]
        if supplier_id: conds.append("b.supplier_id=?"); params.append(supplier_id)
        if status:      conds.append("b.status=?");      params.append(status)
        if overdue:
            today_s = datetime.now().strftime("%Y-%m-%d")
            conds.append(f"b.due_date<'{today_s}' AND b.status NOT IN ('paid','void')")
        where = " AND ".join(conds)
        rows = db.execute(f"""
            SELECT b.*, b.total as total_amount, s.name as supplier_name
            FROM bills b JOIN suppliers s ON b.supplier_id=s.id
            WHERE {where} ORDER BY b.date DESC, b.id DESC
        """, params).fetchall()
        return [dict(r) for r in rows]
    finally: db.close()

@app.get("/api/bills/{bid}")
async def get_bill(bid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        bill = db.execute("""
            SELECT b.*, s.name as supplier_name, s.email as supplier_email
            FROM bills b JOIN suppliers s ON b.supplier_id=s.id
            WHERE b.id=? AND b.company_id=?
        """, (bid, cu["company_id"])).fetchone()
        if not bill: raise HTTPException(404, "Bill not found")
        lines = db.execute("""
            SELECT bl.*, a.code as account_code, a.name as account_name
            FROM bill_lines bl LEFT JOIN accounts a ON bl.account_id=a.id
            WHERE bl.bill_id=?
        """, (bid,)).fetchall()
        pmts = db.execute("SELECT * FROM bill_payments WHERE bill_id=? ORDER BY date DESC", (bid,)).fetchall()
        r = dict(bill); r["lines"] = [dict(l) for l in lines]; r["payments"] = [dict(p) for p in pmts]
        return r
    finally: db.close()

@app.post("/api/bills", status_code=201)
async def create_bill(req: BillCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        supp = db.execute("SELECT * FROM suppliers WHERE id=? AND company_id=?",
                          (req.supplier_id, cu["company_id"])).fetchone()
        if not supp: raise HTTPException(400, "Supplier not found")

        subtotal = tax_total = 0.0; proc = []
        for ln in req.lines:
            net = round(ln.quantity * ln.unit_price, 4)
            tax = round(net * ln.tax_rate / 100, 4)
            proc.append((ln.description, ln.account_id, ln.quantity, ln.unit_price, ln.tax_rate, tax, net))
            subtotal += net; tax_total += tax

        total = round(subtotal + tax_total, 2)
        bill_num = next_bill_num(db, cu["company_id"])
        cur = db.execute(
            "INSERT INTO bills(company_id,bill_number,supplier_id,supplier_ref,date,due_date,status,subtotal,tax_amount,total,balance_due,notes,created_by) VALUES(?,?,?,?,?,?,'unpaid',?,?,?,?,?,?)",
            (cu["company_id"], bill_num, req.supplier_id, req.supplier_ref, req.date, req.due_date,
             round(subtotal,2), round(tax_total,2), total, total, req.notes, cu["id"])
        )
        bid = cur.lastrowid
        for desc, acc_id, qty, up, tr, tax, net in proc:
            db.execute(
                "INSERT INTO bill_lines(bill_id,description,account_id,quantity,unit_price,tax_rate,tax_amount,line_total) VALUES(?,?,?,?,?,?,?,?)",
                (bid, desc, acc_id, qty, up, tr, round(tax,2), round(net,2))
            )

        # Auto-post GL: DR Expense accounts, CR Trade Creditors
        ap = get_acc_by_code(db, cu["company_id"], "2020") or get_acc_by_subtype(db, cu["company_id"], "payable")
        if ap:
            gl = [(ap["id"], 0, total, f"Bill {bill_num} – {supp['name']}")]
            for desc, acc_id, qty, up, tr, tax, net in proc:
                if acc_id:
                    gl.append((acc_id, round(net+tax,2), 0, f"{bill_num}: {desc}"))
            eid = auto_post_gl(db, cu["company_id"], cu["id"], req.date,
                               f"Bill {bill_num} – {supp['name']}", gl)
            db.execute("UPDATE bills SET journal_entry_id=? WHERE id=?", (eid, bid))

        db.commit()
        return {"id": bid, "bill_number": bill_num, "total": total, "message": "Bill created"}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

@app.post("/api/bills/{bid}/void")
async def void_bill(bid: int, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin","accountant"): raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        bill = db.execute("SELECT * FROM bills WHERE id=? AND company_id=?", (bid, cu["company_id"])).fetchone()
        if not bill: raise HTTPException(404, "Bill not found")
        if bill["status"] == "void": raise HTTPException(400, "Already voided")
        if bill["amount_paid"] > 0: raise HTTPException(400, "Cannot void bill with payments")
        if bill["journal_entry_id"]:
            db.execute("UPDATE journal_entries SET status='void' WHERE id=?", (bill["journal_entry_id"],))
        db.execute("UPDATE bills SET status='void' WHERE id=?", (bid,))
        db.commit()
        return {"message": "Bill voided"}
    finally: db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# BILL PAYMENTS  (AP payments)
# ═══════════════════════════════════════════════════════════════════════════════

class BillPaymentCreate(BaseModel):
    supplier_id: int
    bill_id: Optional[int] = None
    date: str
    amount: float = Field(..., gt=0)
    payment_method: str = "bank_transfer"
    reference: Optional[str] = None
    bank_account_id: Optional[int] = None

def next_pay_num(db, company_id):
    n = db.execute("SELECT COUNT(*) as c FROM bill_payments WHERE company_id=?", (company_id,)).fetchone()["c"]
    return f"PAY-{n+1:06d}"

@app.get("/api/bill-payments")
async def list_bill_payments(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT bp.*, s.name as supplier_name, b.bill_number
        FROM bill_payments bp
        JOIN suppliers s ON bp.supplier_id=s.id
        LEFT JOIN bills b ON bp.bill_id=b.id
        WHERE bp.company_id=? ORDER BY bp.date DESC, bp.id DESC
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/bill-payments", status_code=201)
async def create_bill_payment(req: BillPaymentCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        supp = db.execute("SELECT * FROM suppliers WHERE id=? AND company_id=?",
                          (req.supplier_id, cu["company_id"])).fetchone()
        if not supp: raise HTTPException(400, "Supplier not found")

        bill = None
        if req.bill_id:
            bill = db.execute("SELECT * FROM bills WHERE id=? AND company_id=?",
                              (req.bill_id, cu["company_id"])).fetchone()
            if not bill: raise HTTPException(400, "Bill not found")
            if req.amount > bill["balance_due"] + 0.005:
                raise HTTPException(400, f"Amount exceeds balance due ({bill['balance_due']:.2f})")

        pay_num = next_pay_num(db, cu["company_id"])
        ap = get_acc_by_code(db, cu["company_id"], "2020") or get_acc_by_subtype(db, cu["company_id"], "payable")
        if not ap: raise HTTPException(400, "No AP account found")

        cr_id = None
        if req.bank_account_id:
            bk = db.execute("SELECT * FROM bank_accounts WHERE id=? AND company_id=?",
                            (req.bank_account_id, cu["company_id"])).fetchone()
            if bk: cr_id = bk["gl_account_id"]
        if not cr_id:
            bk = get_acc_by_code(db, cu["company_id"], "1030") or get_acc_by_subtype(db, cu["company_id"], "bank")
            if bk: cr_id = bk["id"]

        eid = None
        if cr_id:
            eid = auto_post_gl(db, cu["company_id"], cu["id"], req.date,
                               f"Bill Payment {pay_num} – {supp['name']}",
                               [(ap["id"], req.amount, 0, f"Payment {pay_num}"),
                                (cr_id, 0, req.amount, f"Payment {pay_num}")])

        cur = db.execute(
            "INSERT INTO bill_payments(company_id,payment_number,supplier_id,bill_id,date,amount,payment_method,reference,bank_account_id,journal_entry_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cu["company_id"], pay_num, req.supplier_id, req.bill_id, req.date,
             req.amount, req.payment_method, req.reference, req.bank_account_id, eid, cu["id"])
        )
        if bill:
            new_paid = round(bill["amount_paid"] + req.amount, 2)
            new_bal  = max(round(bill["balance_due"] - req.amount, 2), 0)
            status   = "paid" if new_bal <= 0.005 else "partial"
            db.execute("UPDATE bills SET amount_paid=?,balance_due=?,status=? WHERE id=?",
                       (new_paid, new_bal, status, req.bill_id))

        db.commit()
        return {"id": cur.lastrowid, "payment_number": pay_num, "message": "Payment recorded"}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# BANK ACCOUNTS
# ═══════════════════════════════════════════════════════════════════════════════

class BankAccountCreate(BaseModel):
    name: str
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    gl_account_id: Optional[int] = None
    currency: Optional[str] = "USD"
    opening_balance: Optional[float] = 0
    description: Optional[str] = None

class BankAccountUpdate(BaseModel):
    name: Optional[str] = None
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    gl_account_id: Optional[int] = None
    currency: Optional[str] = None
    opening_balance: Optional[float] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

@app.get("/api/bank-accounts")
async def list_bank_accounts(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT ba.*,
               a.code  as gl_account_code, a.name  as gl_account_name,
               a.code  as gl_code,         a.name  as gl_name,
               COALESCE(SUM(CASE WHEN je.status='posted' THEN jl.debit-jl.credit ELSE 0 END),0)
                   + COALESCE(ba.opening_balance, 0) as balance
        FROM bank_accounts ba
        LEFT JOIN accounts a ON ba.gl_account_id=a.id
        LEFT JOIN journal_lines jl ON jl.account_id=ba.gl_account_id
        LEFT JOIN journal_entries je ON jl.entry_id=je.id
        WHERE ba.company_id=? GROUP BY ba.id ORDER BY ba.name
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/bank-accounts", status_code=201)
async def create_bank_account(req: BankAccountCreate, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"): raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        if req.gl_account_id and not db.execute("SELECT id FROM accounts WHERE id=? AND company_id=?",
                          (req.gl_account_id, cu["company_id"])).fetchone():
            raise HTTPException(400, "GL account not found")
        cur = db.execute(
            "INSERT INTO bank_accounts(company_id,name,bank_name,account_number,gl_account_id,currency,"
            "opening_balance,description) VALUES(?,?,?,?,?,?,?,?)",
            (cu["company_id"], req.name, req.bank_name, req.account_number, req.gl_account_id,
             req.currency or "USD", req.opening_balance or 0, req.description)
        )
        db.commit()
        return dict(db.execute("SELECT * FROM bank_accounts WHERE id=?", (cur.lastrowid,)).fetchone())
    finally: db.close()

@app.put("/api/bank-accounts/{bid}")
async def update_bank_account(bid: int, req: BankAccountUpdate, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"): raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        if not db.execute("SELECT id FROM bank_accounts WHERE id=? AND company_id=?",
                          (bid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Bank account not found")
        fields = {k: v for k, v in req.dict().items() if v is not None}
        if fields:
            db.execute(f"UPDATE bank_accounts SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                       (*fields.values(), bid))
            db.commit()
        return dict(db.execute("SELECT * FROM bank_accounts WHERE id=?", (bid,)).fetchone())
    finally: db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# BANK RECONCILIATION
# ═══════════════════════════════════════════════════════════════════════════════

import csv as _csv, io as _io

def _parse_bank_csv(content: str) -> list:
    """Parse bank statement CSV → list of {date, description, reference, amount, balance}."""
    content = content.lstrip('﻿').strip()
    # Detect delimiter
    sample = content[:2000]
    delim  = ';' if sample.count(';') > sample.count(',') else (
             '\t' if sample.count('\t') > sample.count(',') else ',')
    reader  = _csv.DictReader(_io.StringIO(content), delimiter=delim)
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]

    def find_col(*candidates):
        for c in candidates:
            for i, h in enumerate(headers):
                if c in h:
                    return reader.fieldnames[i]
        return None

    date_col   = find_col('transaction date', 'trans date', 'value date', 'date')
    desc_col   = find_col('description', 'narrative', 'particulars', 'memo', 'details', 'remarks')
    ref_col    = find_col('reference', 'ref', 'cheque', 'check', 'chq')
    amt_col    = find_col('amount')
    debit_col  = find_col('debit', 'withdrawal', 'withdrawl', 'dr amount', 'dr')
    credit_col = find_col('credit', 'deposit', 'cr amount', 'cr')
    bal_col    = find_col('running balance', 'closing balance', 'balance')

    def parse_date(s):
        from datetime import datetime
        for fmt in ('%Y-%m-%d','%d/%m/%Y','%m/%d/%Y','%d-%m-%Y',
                    '%Y/%m/%d','%d %b %Y','%d-%b-%Y','%d %B %Y'):
            try: return datetime.strptime(s.strip(), fmt).strftime('%Y-%m-%d')
            except: pass
        return None

    def parse_amt(s):
        try: return float(str(s).strip().replace(',','').replace(' ',''))
        except: return 0.0

    rows = []
    for row in reader:
        date = parse_date(row.get(date_col, '') if date_col else '')
        if not date:
            continue
        desc = (row.get(desc_col, '') if desc_col else '').strip()
        ref  = (row.get(ref_col,  '') if ref_col  else '').strip()
        bal  = parse_amt(row.get(bal_col, 0) if bal_col else 0)
        if amt_col:
            amount = parse_amt(row.get(amt_col, 0))
        elif debit_col and credit_col:
            dr = parse_amt(row.get(debit_col,  0))
            cr = parse_amt(row.get(credit_col, 0))
            amount = cr - dr           # positive = money in, negative = money out
        else:
            continue
        if amount == 0:
            continue
        rows.append({'date': date, 'description': desc, 'reference': ref,
                     'amount': amount, 'balance': bal})
    return rows


def _match_score(btxn: dict, jl: dict, je: dict) -> float:
    """Return 0-100 confidence that bank transaction btxn matches GL line jl/je."""
    from datetime import datetime

    # ── Amount (40 pts) — amount sign determines debit vs credit direction ──
    bank_abs = abs(btxn['amount'])
    gl_amt   = jl['debit'] if btxn['amount'] > 0 else jl['credit']
    if gl_amt <= 0:
        return 0.0
    diff = abs(bank_abs - gl_amt)
    if diff > bank_abs * 0.10:          # more than 10% off → no match
        return 0.0
    if diff < 0.01:   score = 40
    elif diff < 1.00: score = 25
    else:             score = 10

    # ── Date (30 pts) ──────────────────────────────────────────────────────
    try:
        bd = datetime.strptime(btxn['date'], '%Y-%m-%d')
        gd = datetime.strptime(je['date'],   '%Y-%m-%d')
        d  = abs((bd - gd).days)
        score += 30 if d == 0 else 22 if d <= 1 else 15 if d <= 3 else 8 if d <= 7 else 0
    except Exception:
        pass

    # ── Description / reference overlap (20 pts) ──────────────────────────
    bank_text = f"{btxn.get('description','')} {btxn.get('reference','')}".lower()
    gl_text   = (f"{jl.get('description','')} {je.get('description','')} "
                 f"{je.get('entry_number','')} {je.get('reference','')}").lower()
    bank_w = {w for w in bank_text.split() if len(w) > 3}
    gl_w   = {w for w in gl_text.split()   if len(w) > 3}
    if bank_w and gl_w:
        overlap = len(bank_w & gl_w)
        score  += min(20, int(overlap / min(len(bank_w), len(gl_w)) * 20))

    # ── Reference exact match bonus (10 pts) ──────────────────────────────
    ref = btxn.get('reference', '').lower()
    if ref and len(ref) > 3 and ref in gl_text:
        score += 10

    return min(float(score), 100.0)


# ── POST /api/bank-accounts/{bid}/import-statement ─────────────────────────
class StatementImport(BaseModel):
    csv_content: str
    import_batch: Optional[str] = None

@app.post("/api/bank-accounts/{bid}/import-statement", status_code=201)
async def import_statement(bid: int, req: StatementImport, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"):
        raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        ba = db.execute("SELECT * FROM bank_accounts WHERE id=? AND company_id=?",
                        (bid, cu["company_id"])).fetchone()
        if not ba: raise HTTPException(404, "Bank account not found")

        rows = _parse_bank_csv(req.csv_content)
        if not rows: raise HTTPException(400, "No valid transactions found in CSV. Check column headers include Date, Description, and Amount/Debit/Credit.")

        batch = req.import_batch or datetime.now().strftime("IMP-%Y%m%d-%H%M%S")
        inserted = 0
        for r in rows:
            # Skip if identical transaction already imported in same batch
            dup = db.execute(
                "SELECT id FROM bank_transactions WHERE bank_account_id=? AND date=? "
                "AND amount=? AND description=? AND import_batch=?",
                (bid, r['date'], r['amount'], r['description'], batch)
            ).fetchone()
            if dup: continue
            db.execute(
                "INSERT INTO bank_transactions(company_id,bank_account_id,import_batch,"
                "date,description,reference,amount,balance) VALUES(?,?,?,?,?,?,?,?)",
                (cu["company_id"], bid, batch, r['date'], r['description'],
                 r['reference'], r['amount'], r['balance'])
            )
            inserted += 1
        db.commit()
        return {"message": f"Imported {inserted} transactions", "batch": batch, "total": inserted}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(400, f"Import failed: {str(e)}")
    finally: db.close()


# ── GET /api/bank-accounts/{bid}/transactions ──────────────────────────────
@app.get("/api/bank-accounts/{bid}/transactions")
async def list_bank_transactions(
    bid: int,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    cu: dict = Depends(current_user)
):
    db = get_db()
    try:
        conds  = ["bt.bank_account_id=?", "bt.company_id=?"]
        params: list = [bid, cu["company_id"]]
        if status:    conds.append("bt.match_status=?");  params.append(status)
        if date_from: conds.append("bt.date>=?");         params.append(date_from)
        if date_to:   conds.append("bt.date<=?");         params.append(date_to)
        rows = db.execute(f"""
            SELECT bt.*,
                   jl.description as gl_line_desc,
                   je.entry_number, je.description as je_desc, je.date as je_date,
                   a.code as gl_account_code, a.name as gl_account_name
            FROM bank_transactions bt
            LEFT JOIN journal_lines jl ON bt.journal_line_id = jl.id
            LEFT JOIN journal_entries je ON jl.entry_id = je.id
            LEFT JOIN accounts a ON jl.account_id = a.id
            WHERE {' AND '.join(conds)}
            ORDER BY bt.date DESC, bt.id DESC
        """, params).fetchall()
        return [dict(r) for r in rows]
    finally: db.close()


# ── GET /api/bank-accounts/{bid}/unmatched-gl ──────────────────────────────
@app.get("/api/bank-accounts/{bid}/unmatched-gl")
async def unmatched_gl_lines(
    bid: int,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    cu: dict = Depends(current_user)
):
    """Return journal lines for this bank account's GL account that aren't yet matched."""
    db = get_db()
    try:
        ba = db.execute("SELECT gl_account_id FROM bank_accounts WHERE id=? AND company_id=?",
                        (bid, cu["company_id"])).fetchone()
        if not ba: raise HTTPException(404, "Bank account not found")

        conds  = ["jl.account_id=?", "je.status='posted'", "je.company_id=?"]
        params: list = [ba["gl_account_id"], cu["company_id"]]
        # Exclude lines already matched
        conds.append(
            "jl.id NOT IN (SELECT journal_line_id FROM bank_transactions "
            "WHERE journal_line_id IS NOT NULL AND company_id=? AND match_status='matched')"
        )
        params.append(cu["company_id"])
        if date_from: conds.append("je.date>=?"); params.append(date_from)
        if date_to:   conds.append("je.date<=?"); params.append(date_to)

        rows = db.execute(f"""
            SELECT jl.id as line_id, jl.debit, jl.credit, jl.description as line_desc,
                   je.id as entry_id, je.entry_number, je.date, je.description, je.reference
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id = je.id
            WHERE {' AND '.join(conds)}
            ORDER BY je.date DESC, je.id DESC
        """, params).fetchall()
        return [dict(r) for r in rows]
    finally: db.close()


# ── POST /api/bank-accounts/{bid}/auto-match ───────────────────────────────
@app.post("/api/bank-accounts/{bid}/auto-match")
async def auto_match(bid: int, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"):
        raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        ba = db.execute("SELECT * FROM bank_accounts WHERE id=? AND company_id=?",
                        (bid, cu["company_id"])).fetchone()
        if not ba: raise HTTPException(404, "Bank account not found")

        # Unmatched bank transactions
        btxns = db.execute(
            "SELECT * FROM bank_transactions WHERE bank_account_id=? AND company_id=? "
            "AND match_status='unmatched' ORDER BY date",
            (bid, cu["company_id"])
        ).fetchall()

        # Available GL lines for this bank account (not yet matched)
        gl_rows = db.execute("""
            SELECT jl.id as line_id, jl.debit, jl.credit, jl.description as line_desc,
                   je.id as entry_id, je.entry_number, je.date, je.description, je.reference
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id=je.id
            WHERE jl.account_id=? AND je.status='posted' AND je.company_id=?
              AND jl.id NOT IN (
                SELECT journal_line_id FROM bank_transactions
                WHERE journal_line_id IS NOT NULL AND company_id=? AND match_status='matched'
              )
        """, (ba["gl_account_id"], cu["company_id"], cu["company_id"])).fetchall()

        # Build a mutable pool of available GL lines (scored per bank txn)
        gl_pool = [dict(r) for r in gl_rows]
        matched = 0; suggested = 0

        for btxn in btxns:
            b = dict(btxn)
            best_score = 0.0
            best_gl    = None

            for gl in gl_pool:
                je = {"date": gl["date"], "description": gl["description"],
                      "entry_number": gl["entry_number"], "reference": gl["reference"]}
                jl = {"debit": gl["debit"], "credit": gl["credit"],
                      "description": gl["line_desc"]}
                s = _match_score(b, jl, je)
                if s > best_score:
                    best_score = s
                    best_gl    = gl

            if best_gl and best_score >= 80:
                # Auto-match
                db.execute(
                    "UPDATE bank_transactions SET match_status='matched', journal_line_id=?, "
                    "match_confidence=?, matched_at=datetime('now'), matched_by=? WHERE id=?",
                    (best_gl["line_id"], best_score, cu["id"], b["id"])
                )
                gl_pool = [g for g in gl_pool if g["line_id"] != best_gl["line_id"]]
                matched += 1
            elif best_gl and best_score >= 50:
                # Suggest (store confidence but don't lock)
                db.execute(
                    "UPDATE bank_transactions SET match_confidence=?, journal_line_id=? WHERE id=?",
                    (best_score, best_gl["line_id"], b["id"])
                )
                suggested += 1

        db.commit()
        total = len(btxns)
        return {"matched": matched, "suggested": suggested,
                "unmatched": total - matched - suggested,
                "total": total,
                "message": f"Auto-matched {matched} of {total} transactions ({suggested} suggested)"}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()


# ── POST /api/reconciliation/match ─────────────────────────────────────────
class ManualMatch(BaseModel):
    bank_transaction_id: int
    journal_line_id: int

@app.post("/api/reconciliation/match")
async def manual_match(req: ManualMatch, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"):
        raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        bt = db.execute(
            "SELECT * FROM bank_transactions WHERE id=? AND company_id=?",
            (req.bank_transaction_id, cu["company_id"])
        ).fetchone()
        if not bt: raise HTTPException(404, "Bank transaction not found")

        # Verify GL line exists and belongs to company
        jl = db.execute(
            "SELECT jl.*, je.date, je.entry_number FROM journal_lines jl "
            "JOIN journal_entries je ON jl.entry_id=je.id "
            "WHERE jl.id=? AND je.company_id=?",
            (req.journal_line_id, cu["company_id"])
        ).fetchone()
        if not jl: raise HTTPException(404, "Journal line not found")

        # Release any previous match on this GL line
        db.execute(
            "UPDATE bank_transactions SET match_status='unmatched', journal_line_id=NULL, "
            "match_confidence=0 WHERE journal_line_id=? AND company_id=? AND id!=?",
            (req.journal_line_id, cu["company_id"], req.bank_transaction_id)
        )

        b  = dict(bt)
        je = {"date": jl["date"], "entry_number": jl["entry_number"],
              "description": jl["description"], "reference": ""}
        jld= {"debit": jl["debit"], "credit": jl["credit"], "description": jl["description"]}
        confidence = _match_score(b, jld, je)

        db.execute(
            "UPDATE bank_transactions SET match_status='matched', journal_line_id=?, "
            "match_confidence=?, matched_at=datetime('now'), matched_by=? WHERE id=?",
            (req.journal_line_id, confidence, cu["id"], req.bank_transaction_id)
        )
        db.commit()
        return {"message": "Matched", "confidence": confidence}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()


# ── POST /api/reconciliation/unmatch/{tid} ─────────────────────────────────
@app.post("/api/reconciliation/unmatch/{tid}")
async def unmatch_transaction(tid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        if not db.execute("SELECT id FROM bank_transactions WHERE id=? AND company_id=?",
                          (tid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Transaction not found")
        db.execute(
            "UPDATE bank_transactions SET match_status='unmatched', journal_line_id=NULL, "
            "match_confidence=0, matched_at=NULL, matched_by=NULL WHERE id=?", (tid,)
        )
        db.commit()
        return {"message": "Unmatched"}
    finally: db.close()


# ── POST /api/reconciliation/exclude/{tid} ─────────────────────────────────
@app.post("/api/reconciliation/exclude/{tid}")
async def exclude_transaction(tid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        if not db.execute("SELECT id FROM bank_transactions WHERE id=? AND company_id=?",
                          (tid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Transaction not found")
        db.execute(
            "UPDATE bank_transactions SET match_status='excluded', journal_line_id=NULL, "
            "match_confidence=0 WHERE id=?", (tid,)
        )
        db.commit()
        return {"message": "Excluded"}
    finally: db.close()


# ── GET /api/bank-accounts/{bid}/reconciliation-summary ────────────────────
@app.get("/api/bank-accounts/{bid}/reconciliation-summary")
async def recon_summary(bid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        ba = db.execute("SELECT * FROM bank_accounts WHERE id=? AND company_id=?",
                        (bid, cu["company_id"])).fetchone()
        if not ba: raise HTTPException(404, "Bank account not found")
        ba = dict(ba)

        stats = db.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN match_status='matched'  THEN 1 ELSE 0 END) as matched,
                SUM(CASE WHEN match_status='excluded' THEN 1 ELSE 0 END) as excluded,
                SUM(CASE WHEN match_status='unmatched' AND match_confidence>=50 THEN 1 ELSE 0 END) as suggested,
                SUM(CASE WHEN match_status='unmatched' AND (match_confidence<50 OR match_confidence IS NULL) THEN 1 ELSE 0 END) as unmatched,
                SUM(amount) as net_total,
                MIN(date) as earliest_date, MAX(date) as latest_date
            FROM bank_transactions
            WHERE bank_account_id=? AND company_id=?
        """, (bid, cu["company_id"])).fetchone()

        periods = db.execute(
            "SELECT * FROM reconciliation_periods WHERE bank_account_id=? AND company_id=? "
            "ORDER BY end_date DESC LIMIT 5",
            (bid, cu["company_id"])
        ).fetchall()

        # GL balance for this bank account
        gl_bal = db.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as bal
            FROM journal_lines jl
            JOIN journal_entries je ON jl.entry_id=je.id
            WHERE jl.account_id=? AND je.status='posted' AND je.company_id=?
        """, (ba["gl_account_id"] or 0, cu["company_id"])).fetchone()

        return {
            "bank_account": ba,
            "stats": dict(stats),
            "gl_balance": round((gl_bal["bal"] if gl_bal else 0) + (ba.get("opening_balance") or 0), 2),
            "periods": [dict(p) for p in periods]
        }
    finally: db.close()


# ── POST /api/reconciliation/complete ──────────────────────────────────────
class CompleteRecon(BaseModel):
    bank_account_id: int
    period_name: str
    start_date: str
    end_date: str
    statement_balance: float
    notes: Optional[str] = None

@app.post("/api/reconciliation/complete", status_code=201)
async def complete_reconciliation(req: CompleteRecon, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"):
        raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        ba = db.execute("SELECT * FROM bank_accounts WHERE id=? AND company_id=?",
                        (req.bank_account_id, cu["company_id"])).fetchone()
        if not ba: raise HTTPException(404, "Bank account not found")
        ba = dict(ba)

        stats = db.execute("""
            SELECT
                SUM(CASE WHEN match_status='matched'  THEN 1 ELSE 0 END) as matched,
                SUM(CASE WHEN match_status='unmatched' THEN 1 ELSE 0 END) as unmatched
            FROM bank_transactions
            WHERE bank_account_id=? AND company_id=? AND date BETWEEN ? AND ?
        """, (req.bank_account_id, cu["company_id"], req.start_date, req.end_date)).fetchone()

        gl_bal = db.execute("""
            SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as bal
            FROM journal_lines jl JOIN journal_entries je ON jl.entry_id=je.id
            WHERE jl.account_id=? AND je.status='posted' AND je.company_id=?
              AND je.date<=?
        """, (ba["gl_account_id"] or 0, cu["company_id"], req.end_date)).fetchone()
        gl_balance = round((gl_bal["bal"] if gl_bal else 0) + (ba.get("opening_balance") or 0), 2)

        cur = db.execute(
            "INSERT INTO reconciliation_periods(company_id,bank_account_id,period_name,"
            "start_date,end_date,statement_balance,gl_balance,matched_count,unmatched_count,"
            "status,notes,reconciled_at,reconciled_by) VALUES(?,?,?,?,?,?,?,?,?,'reconciled',?,datetime('now'),?)",
            (cu["company_id"], req.bank_account_id, req.period_name,
             req.start_date, req.end_date, req.statement_balance, gl_balance,
             stats["matched"] or 0, stats["unmatched"] or 0, req.notes, cu["id"])
        )
        db.commit()
        difference = round(req.statement_balance - gl_balance, 2)
        return {
            "id": cur.lastrowid, "gl_balance": gl_balance,
            "statement_balance": req.statement_balance,
            "difference": difference,
            "balanced": abs(difference) < 0.01,
            "message": "Reconciliation period saved"
        }
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# AGING REPORTS
# ═══════════════════════════════════════════════════════════════════════════════

def _bucket(rows, amount_field="balance_due"):
    today_s = datetime.now().strftime("%Y-%m-%d")
    result = {"current":[],"d1_30":[],"d31_60":[],"d61_90":[],"over_90":[],
              "t_current":0,"t_1_30":0,"t_31_60":0,"t_61_90":0,"t_over_90":0,"grand_total":0}
    for r in rows:
        row = dict(r)
        days = row.get("days_overdue") or 0
        amt  = row.get(amount_field, 0) or 0
        if   days <= 0:  key = "current";  tk = "t_current"
        elif days <= 30: key = "d1_30";    tk = "t_1_30"
        elif days <= 60: key = "d31_60";   tk = "t_31_60"
        elif days <= 90: key = "d61_90";   tk = "t_61_90"
        else:            key = "over_90";  tk = "t_over_90"
        result[key].append(row); result[tk] += amt; result["grand_total"] += amt
    for k in ["t_current","t_1_30","t_31_60","t_61_90","t_over_90","grand_total"]:
        result[k] = round(result[k], 2)
    return result

@app.get("/api/reports/ar-aging")
async def ar_aging(cu: dict = Depends(current_user)):
    db = get_db()
    today_s = datetime.now().strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT i.*, c.name as customer_name,
               CAST(julianday(?) - julianday(i.due_date) AS INTEGER) as days_overdue
        FROM invoices i JOIN customers c ON i.customer_id=c.id
        WHERE i.company_id=? AND i.status NOT IN ('paid','void','draft')
        ORDER BY c.name, i.due_date
    """, (today_s, cu["company_id"])).fetchall()
    db.close()
    return _bucket(rows)

@app.get("/api/reports/ap-aging")
async def ap_aging(cu: dict = Depends(current_user)):
    db = get_db()
    today_s = datetime.now().strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT b.*, s.name as supplier_name,
               CAST(julianday(?) - julianday(b.due_date) AS INTEGER) as days_overdue
        FROM bills b JOIN suppliers s ON b.supplier_id=s.id
        WHERE b.company_id=? AND b.status NOT IN ('paid','void')
        ORDER BY s.name, b.due_date
    """, (today_s, cu["company_id"])).fetchall()
    db.close()
    return _bucket(rows)

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — INVENTORY
# ═══════════════════════════════════════════════════════════════════════════════

class ProductCreate(BaseModel):
    sku: Optional[str] = None          # frontend field name; stored as `code`
    name: str
    category: Optional[str] = None
    unit: str = "unit"
    barcode: Optional[str] = None
    cost_price: float = 0
    selling_price: float = 0
    current_stock: float = 0
    reorder_level: float = 0
    description: Optional[str] = None
    inventory_account_id: Optional[int] = None
    cogs_account_id: Optional[int] = None
    revenue_account_id: Optional[int] = None

class ProductUpdate(BaseModel):
    sku: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None
    barcode: Optional[str] = None
    cost_price: Optional[float] = None
    selling_price: Optional[float] = None
    reorder_level: Optional[float] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

class StockMovementCreate(BaseModel):
    product_id: int
    type: str                          # frontend field name; stored as `movement_type`
    quantity: float = Field(..., gt=0)
    date: Optional[str] = None
    unit_cost: float = 0
    reference: Optional[str] = None
    notes: Optional[str] = None
    post_gl: bool = True

@app.get("/api/products/barcode/{barcode}")
async def get_product_by_barcode(barcode: str, cu: dict = Depends(current_user)):
    db = get_db()
    row = db.execute(
        "SELECT *, code as sku, current_stock * cost_price as stock_value FROM products "
        "WHERE company_id=? AND (barcode=? OR code=?) AND is_active=1 LIMIT 1",
        (cu["company_id"], barcode, barcode)
    ).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, f"No product found for barcode '{barcode}'")
    return dict(row)

@app.get("/api/products")
async def list_products(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT *, code as sku, current_stock * cost_price as stock_value,
               CASE WHEN reorder_level > 0 AND current_stock <= reorder_level THEN 1 ELSE 0 END as low_stock
        FROM products WHERE company_id=? ORDER BY code
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/products", status_code=201)
async def create_product(req: ProductCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        # Auto-generate code from name if sku not supplied
        code = (req.sku or '').strip()
        if not code:
            base = ''.join(c for c in req.name.upper() if c.isalnum())[:8]
            n = db.execute("SELECT COUNT(*) as c FROM products WHERE company_id=?",
                           (cu["company_id"],)).fetchone()["c"]
            code = f"{base}-{n+1:04d}"
        if db.execute("SELECT id FROM products WHERE company_id=? AND code=?",
                      (cu["company_id"], code)).fetchone():
            raise HTTPException(400, f"SKU '{code}' already exists")
        cur = db.execute(
            "INSERT INTO products(company_id,code,name,category,unit,barcode,cost_price,selling_price,"
            "current_stock,reorder_level,description,inventory_account_id,cogs_account_id,revenue_account_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cu["company_id"], code, req.name, req.category, req.unit or "unit", req.barcode,
             req.cost_price, req.selling_price, req.current_stock, req.reorder_level,
             req.description, req.inventory_account_id, req.cogs_account_id, req.revenue_account_id)
        )
        db.commit()
        row = dict(db.execute("SELECT *, code as sku FROM products WHERE id=?",
                              (cur.lastrowid,)).fetchone())
        return row
    finally: db.close()

@app.put("/api/products/{pid}")
async def update_product(pid: int, req: ProductUpdate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        if not db.execute("SELECT id FROM products WHERE id=? AND company_id=?",
                          (pid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Product not found")
        raw = req.dict()
        # Map sku → code for the DB column
        if raw.get("sku") is not None:
            raw["code"] = raw.pop("sku")
        else:
            raw.pop("sku", None)
        fields = {k: v for k, v in raw.items() if v is not None}
        if fields:
            db.execute(f"UPDATE products SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                       (*fields.values(), pid))
            db.commit()
        return dict(db.execute("SELECT *, code as sku FROM products WHERE id=?", (pid,)).fetchone())
    finally: db.close()

@app.get("/api/stock/movements")
async def list_stock_movements(product_id: Optional[int] = None, cu: dict = Depends(current_user)):
    db = get_db()
    conds = ["sm.company_id=?"]; params: list = [cu["company_id"]]
    if product_id: conds.append("sm.product_id=?"); params.append(product_id)
    rows = db.execute(f"""
        SELECT sm.*, sm.movement_type as type, p.name as product_name, p.code as product_code,
               p.code as sku, p.unit
        FROM stock_movements sm JOIN products p ON sm.product_id=p.id
        WHERE {' AND '.join(conds)} ORDER BY sm.created_at DESC LIMIT 300
    """, params).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/stock/movements", status_code=201)
@app.post("/api/stock/movement", status_code=201)  # keep old URL for compatibility
async def create_stock_movement(req: StockMovementCreate, cu: dict = Depends(current_user)):
    movement_type = req.type
    if movement_type not in ("in","out","adjustment"):
        raise HTTPException(400, "type must be 'in', 'out', or 'adjustment'")
    db = get_db()
    try:
        prod = db.execute("SELECT * FROM products WHERE id=? AND company_id=?",
                          (req.product_id, cu["company_id"])).fetchone()
        if not prod: raise HTTPException(400, "Product not found")
        prod = dict(prod)

        unit_cost = req.unit_cost or prod["cost_price"]
        total_cost = round(req.quantity * unit_cost, 2)
        move_date = req.date or datetime.now().strftime("%Y-%m-%d")

        if movement_type == "in":
            # Weighted average cost
            new_total_val = prod["current_stock"] * prod["cost_price"] + req.quantity * unit_cost
            new_stock = prod["current_stock"] + req.quantity
            new_cost  = round(new_total_val / new_stock, 4) if new_stock > 0 else unit_cost
        elif movement_type == "out":
            if req.quantity > prod["current_stock"] + 0.0001:
                raise HTTPException(400, f"Insufficient stock ({prod['current_stock']} {prod['unit']} available)")
            new_stock = prod["current_stock"] - req.quantity
            new_cost  = prod["cost_price"]
        else:  # adjustment
            new_stock = req.quantity
            new_cost  = prod["cost_price"]

        eid = None
        if req.post_gl and total_cost > 0:
            inv_acc = (db.execute("SELECT * FROM accounts WHERE id=? AND company_id=?",
                                  (prod["inventory_account_id"], cu["company_id"])).fetchone()
                       if prod["inventory_account_id"] else None) or \
                      get_acc_by_code(db, cu["company_id"], "1200") or \
                      get_acc_by_subtype(db, cu["company_id"], "inventory")
            if inv_acc and movement_type == "in" and req.unit_cost > 0:
                ap = get_acc_by_code(db, cu["company_id"], "2020") or get_acc_by_subtype(db, cu["company_id"], "payable")
                if ap:
                    eid = auto_post_gl(db, cu["company_id"], cu["id"], move_date,
                                       f"Stock In: {prod['name']} × {req.quantity}",
                                       [(inv_acc["id"], total_cost, 0, f"Stock in: {prod['code']}"),
                                        (ap["id"], 0, total_cost, f"Stock in: {prod['name']}")])
            elif inv_acc and movement_type == "out":
                cogs = (db.execute("SELECT * FROM accounts WHERE id=? AND company_id=?",
                                   (prod["cogs_account_id"], cu["company_id"])).fetchone()
                        if prod["cogs_account_id"] else None) or \
                       get_acc_by_code(db, cu["company_id"], "5010") or \
                       get_acc_by_subtype(db, cu["company_id"], "cost_of_sales")
                if cogs:
                    eid = auto_post_gl(db, cu["company_id"], cu["id"], move_date,
                                       f"COGS: {prod['name']} × {req.quantity}",
                                       [(cogs["id"], total_cost, 0, f"COGS: {prod['code']}"),
                                        (inv_acc["id"], 0, total_cost, f"COGS: {prod['name']}")])

        db.execute("UPDATE products SET current_stock=?, cost_price=? WHERE id=?",
                   (round(new_stock, 4), round(new_cost, 4), req.product_id))
        cur = db.execute(
            "INSERT INTO stock_movements(company_id,product_id,movement_type,quantity,unit_cost,total_cost,reference,notes,journal_entry_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cu["company_id"], req.product_id, movement_type, req.quantity,
             unit_cost, total_cost, req.reference, req.notes, eid, cu["id"])
        )
        db.commit()
        return {"id": cur.lastrowid, "new_stock": round(new_stock, 4), "message": "Stock movement recorded"}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

class PosSaleItem(BaseModel):
    product_id: int
    quantity: float = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)

class PosSaleCreate(BaseModel):
    items: List[PosSaleItem] = Field(..., min_items=1)
    payment_method: str = "cash"
    discount: float = 0
    tax_amount: float = 0
    total: float = 0
    reference: Optional[str] = None
    notes: Optional[str] = None

@app.post("/api/pos/sale", status_code=201)
async def pos_sale(req: PosSaleCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        sale_ref = req.reference or f"POS-{db.execute('SELECT COUNT(*) as c FROM stock_movements WHERE company_id=?', (cu['company_id'],)).fetchone()['c']+1:06d}"
        movement_ids = []
        for item in req.items:
            prod = db.execute("SELECT * FROM products WHERE id=? AND company_id=?",
                              (item.product_id, cu["company_id"])).fetchone()
            if not prod:
                raise HTTPException(400, f"Product {item.product_id} not found")
            prod = dict(prod)
            if item.quantity > prod["current_stock"] + 0.0001:
                raise HTTPException(400, f"Insufficient stock for '{prod['name']}' ({prod['current_stock']} available)")
            total_cost = round(item.quantity * prod["cost_price"], 2)
            new_stock  = round(prod["current_stock"] - item.quantity, 4)

            # COGS GL entry
            eid = None
            if total_cost > 0:
                inv_acc  = (get_acc_by_code(db, cu["company_id"], "1200") or
                            get_acc_by_subtype(db, cu["company_id"], "inventory"))
                cogs_acc = (db.execute("SELECT * FROM accounts WHERE id=? AND company_id=?",
                                       (prod["cogs_account_id"], cu["company_id"])).fetchone()
                            if prod["cogs_account_id"] else None) or \
                           get_acc_by_code(db, cu["company_id"], "5010") or \
                           get_acc_by_subtype(db, cu["company_id"], "cost_of_sales")
                if inv_acc and cogs_acc:
                    eid = auto_post_gl(db, cu["company_id"], cu["id"], today,
                                       f"COGS — POS {sale_ref}: {prod['name']}",
                                       [(cogs_acc["id"], total_cost, 0, f"COGS {prod['code']}"),
                                        (inv_acc["id"], 0, total_cost, f"COGS {prod['name']}")])

            db.execute("UPDATE products SET current_stock=? WHERE id=?", (new_stock, item.product_id))
            cur = db.execute(
                "INSERT INTO stock_movements(company_id,product_id,movement_type,quantity,unit_cost,"
                "total_cost,reference,notes,journal_entry_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cu["company_id"], item.product_id, "out", item.quantity, prod["cost_price"],
                 total_cost, sale_ref, req.notes or "POS Sale", eid, cu["id"])
            )
            movement_ids.append(cur.lastrowid)

        # Revenue GL entry
        rev_total = round(req.total or sum(i.quantity * i.unit_price for i in req.items), 2)
        if rev_total > 0:
            cash_acc = (get_acc_by_code(db, cu["company_id"], "1010") or
                        get_acc_by_subtype(db, cu["company_id"], "cash"))
            rev_acc  = (get_acc_by_code(db, cu["company_id"], "4010") or
                        get_acc_by_subtype(db, cu["company_id"], "sales"))
            if cash_acc and rev_acc:
                auto_post_gl(db, cu["company_id"], cu["id"], today,
                             f"POS Sale {sale_ref}",
                             [(cash_acc["id"], rev_total, 0, f"POS {sale_ref}"),
                              (rev_acc["id"], 0, rev_total, f"POS {sale_ref}")])

        db.commit()
        return {"reference": sale_ref, "items_sold": len(movement_ids), "total": rev_total,
                "message": "Sale processed successfully"}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

@app.get("/api/reports/stock-valuation")
async def stock_valuation(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT *, code as sku, current_stock as quantity_on_hand,
               cost_price as average_cost, current_stock * cost_price as total_value
        FROM products WHERE company_id=? AND is_active=1
        ORDER BY category, name
    """, (cu["company_id"],)).fetchall()
    total = sum(r["total_value"] for r in rows)
    db.close()
    items = [dict(r) for r in rows]
    return {"products": items, "items": items, "total_value": round(total, 2)}

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — PAYROLL
# ═══════════════════════════════════════════════════════════════════════════════

class EmployeeCreate(BaseModel):
    first_name: str
    last_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    id_number: Optional[str] = None
    position: Optional[str] = None
    department: Optional[str] = None
    hire_date: Optional[str] = None
    basic_salary: float = 0
    pay_frequency: str = "monthly"
    paye_rate: float = 20
    ss_rate: float = 3
    bank_account: Optional[str] = None

class EmployeeUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    id_number: Optional[str] = None
    position: Optional[str] = None
    department: Optional[str] = None
    hire_date: Optional[str] = None
    basic_salary: Optional[float] = None
    pay_frequency: Optional[str] = None
    paye_rate: Optional[float] = None
    ss_rate: Optional[float] = None
    bank_account: Optional[str] = None
    is_active: Optional[bool] = None

class PayrollRunCreate(BaseModel):
    period_name: str
    period_start: str
    period_end: str
    employee_ids: Optional[List[int]] = None

def next_emp_num(db, company_id):
    n = db.execute("SELECT COUNT(*) as c FROM employees WHERE company_id=?", (company_id,)).fetchone()["c"]
    return f"EMP-{n+1:04d}"

def next_payroll_num(db, company_id):
    n = db.execute("SELECT COUNT(*) as c FROM payroll_runs WHERE company_id=?", (company_id,)).fetchone()["c"]
    return f"PR-{n+1:04d}"

def calc_pay_line(emp: dict, allowances: float) -> dict:
    gross  = round(emp["basic_salary"] + allowances, 2)
    paye   = round(gross * emp["paye_rate"] / 100, 2)
    ss     = round(gross * emp["ss_rate"] / 100, 2)
    net    = round(gross - paye - ss, 2)
    return {"basic_salary": emp["basic_salary"], "total_allowances": allowances,
            "gross_pay": gross, "paye_tax": paye, "social_security": ss,
            "other_deductions": 0, "total_deductions": round(paye+ss, 2), "net_pay": net}

@app.get("/api/employees")
async def list_employees(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("SELECT * FROM employees WHERE company_id=? ORDER BY last_name,first_name",
                      (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/employees", status_code=201)
async def create_employee(req: EmployeeCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        num = next_emp_num(db, cu["company_id"])
        cur = db.execute(
            "INSERT INTO employees(company_id,employee_number,first_name,last_name,email,phone,id_number,position,department,hire_date,basic_salary,pay_frequency,paye_rate,ss_rate,bank_account) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cu["company_id"], num, req.first_name, req.last_name, req.email, req.phone,
             req.id_number, req.position, req.department, req.hire_date,
             req.basic_salary, req.pay_frequency, req.paye_rate, req.ss_rate, req.bank_account)
        )
        db.commit()
        return dict(db.execute("SELECT * FROM employees WHERE id=?", (cur.lastrowid,)).fetchone())
    finally: db.close()

@app.put("/api/employees/{eid}")
async def update_employee(eid: int, req: EmployeeUpdate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        if not db.execute("SELECT id FROM employees WHERE id=? AND company_id=?",
                          (eid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Employee not found")
        fields = {k: v for k, v in req.dict().items() if v is not None}
        if fields:
            db.execute(f"UPDATE employees SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                       (*fields.values(), eid))
            db.commit()
        return dict(db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone())
    finally: db.close()

@app.get("/api/payroll-runs")
async def list_payroll_runs(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("SELECT * FROM payroll_runs WHERE company_id=? ORDER BY period_start DESC",
                      (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.get("/api/payroll-runs/{rid}")
async def get_payroll_run(rid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        run = db.execute("SELECT * FROM payroll_runs WHERE id=? AND company_id=?",
                         (rid, cu["company_id"])).fetchone()
        if not run: raise HTTPException(404, "Payroll run not found")
        lines = db.execute("""
            SELECT pl.*, e.first_name, e.last_name, e.employee_number, e.position, e.department
            FROM payroll_lines pl JOIN employees e ON pl.employee_id=e.id
            WHERE pl.run_id=? ORDER BY e.last_name
        """, (rid,)).fetchall()
        r = dict(run); r["lines"] = [dict(l) for l in lines]
        return r
    finally: db.close()

@app.post("/api/payroll-runs", status_code=201)
async def create_payroll_run(req: PayrollRunCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        num = next_payroll_num(db, cu["company_id"])
        cur = db.execute(
            "INSERT INTO payroll_runs(company_id,run_number,period_name,period_start,period_end,status,created_by) VALUES(?,?,?,?,?,'draft',?)",
            (cu["company_id"], num, req.period_name, req.period_start, req.period_end, cu["id"])
        )
        rid = cur.lastrowid
        if req.employee_ids:
            ph = ",".join("?" * len(req.employee_ids))
            emps = db.execute(
                f"SELECT * FROM employees WHERE id IN ({ph}) AND company_id=? AND is_active=1",
                (*req.employee_ids, cu["company_id"])
            ).fetchall()
        else:
            emps = db.execute("SELECT * FROM employees WHERE company_id=? AND is_active=1",
                              (cu["company_id"],)).fetchall()
        tot_gross = tot_ded = tot_net = 0.0
        for emp in emps:
            emp = dict(emp)
            allow_row = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN is_percentage=0 THEN amount ELSE basic_salary*amount/100.0 END),0) as t FROM pay_components WHERE employee_id=? AND component_type='allowance' AND is_active=1",
                (emp["id"],)
            ).fetchone()
            ln = calc_pay_line(emp, round(allow_row["t"] or 0, 2))
            db.execute(
                "INSERT INTO payroll_lines(run_id,employee_id,basic_salary,total_allowances,gross_pay,paye_tax,social_security,other_deductions,total_deductions,net_pay) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, emp["id"], ln["basic_salary"], ln["total_allowances"], ln["gross_pay"],
                 ln["paye_tax"], ln["social_security"], ln["other_deductions"], ln["total_deductions"], ln["net_pay"])
            )
            tot_gross += ln["gross_pay"]; tot_ded += ln["total_deductions"]; tot_net += ln["net_pay"]
        db.execute("UPDATE payroll_runs SET total_gross=?,total_deductions=?,total_net=? WHERE id=?",
                   (round(tot_gross,2), round(tot_ded,2), round(tot_net,2), rid))
        db.commit()
        return {"id": rid, "run_number": num, "total_gross": tot_gross, "total_net": tot_net,
                "message": "Payroll run created"}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

@app.post("/api/payroll-runs/{rid}/approve")
async def approve_payroll_run(rid: int, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin","accountant"): raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        run = db.execute("SELECT * FROM payroll_runs WHERE id=? AND company_id=?",
                         (rid, cu["company_id"])).fetchone()
        if not run: raise HTTPException(404, "Payroll run not found")
        if run["status"] != "draft": raise HTTPException(400, "Only draft runs can be approved")
        run = dict(run)
        lines = db.execute("SELECT * FROM payroll_lines WHERE run_id=?", (rid,)).fetchall()
        sal_acc  = get_acc_by_code(db, cu["company_id"], "6010") or get_acc_by_subtype(db, cu["company_id"], "payroll")
        paye_acc = get_acc_by_code(db, cu["company_id"], "2220")
        ss_acc   = get_acc_by_code(db, cu["company_id"], "2100")
        bank_acc = get_acc_by_code(db, cu["company_id"], "1030") or get_acc_by_subtype(db, cu["company_id"], "bank")
        eid = None
        if sal_acc and bank_acc:
            gl = [(sal_acc["id"], run["total_gross"], 0, f"Gross Pay — {run['run_number']}")]
            tot_paye = round(sum(l["paye_tax"] for l in lines), 2)
            tot_ss   = round(sum(l["social_security"] for l in lines), 2)
            if paye_acc and tot_paye > 0:
                gl.append((paye_acc["id"], 0, tot_paye, f"PAYE Tax — {run['run_number']}"))
            if ss_acc and tot_ss > 0:
                gl.append((ss_acc["id"], 0, tot_ss, f"Social Security — {run['run_number']}"))
            gl.append((bank_acc["id"], 0, run["total_net"], f"Net Pay — {run['run_number']}"))
            eid = auto_post_gl(db, cu["company_id"], cu["id"], run["period_end"],
                               f"Payroll {run['run_number']} — {run['period_name']}", gl)
        db.execute("UPDATE payroll_runs SET status='approved', journal_entry_id=? WHERE id=?", (eid, rid))
        db.commit()
        return {"message": "Payroll approved and posted to GL"}
    finally: db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — FIXED ASSETS
# ═══════════════════════════════════════════════════════════════════════════════

class FixedAssetCreate(BaseModel):
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    purchase_date: str
    purchase_cost: float = Field(..., gt=0)
    salvage_value: float = 0
    useful_life_years: float = 5
    depreciation_method: str = "straight_line"
    depreciation_rate: float = 0
    asset_account_id: Optional[int] = None
    dep_expense_account_id: Optional[int] = None
    accum_dep_account_id: Optional[int] = None

class DepreciationReq(BaseModel):
    period: str
    depreciation_date: Optional[str] = None

def next_asset_num(db, company_id):
    n = db.execute("SELECT COUNT(*) as c FROM fixed_assets WHERE company_id=?", (company_id,)).fetchone()["c"]
    return f"FA-{n+1:04d}"

@app.get("/api/fixed-assets")
async def list_fixed_assets(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("SELECT * FROM fixed_assets WHERE company_id=? ORDER BY asset_number",
                      (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.get("/api/fixed-assets/{aid}")
async def get_fixed_asset(aid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        asset = db.execute("SELECT * FROM fixed_assets WHERE id=? AND company_id=?",
                           (aid, cu["company_id"])).fetchone()
        if not asset: raise HTTPException(404, "Asset not found")
        log = db.execute("SELECT * FROM depreciation_log WHERE asset_id=? ORDER BY period",
                         (aid,)).fetchall()
        r = dict(asset); r["depreciation_log"] = [dict(l) for l in log]
        return r
    finally: db.close()

@app.post("/api/fixed-assets", status_code=201)
async def create_fixed_asset(req: FixedAssetCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        num = next_asset_num(db, cu["company_id"])
        rate = (round(100 / req.useful_life_years, 4)
                if req.depreciation_method == "straight_line" and req.useful_life_years > 0
                else req.depreciation_rate)
        cur = db.execute(
            "INSERT INTO fixed_assets(company_id,asset_number,name,category,description,purchase_date,purchase_cost,salvage_value,useful_life_years,depreciation_method,depreciation_rate,book_value,asset_account_id,dep_expense_account_id,accum_dep_account_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cu["company_id"], num, req.name, req.category, req.description, req.purchase_date,
             req.purchase_cost, req.salvage_value, req.useful_life_years,
             req.depreciation_method, rate, req.purchase_cost,
             req.asset_account_id, req.dep_expense_account_id, req.accum_dep_account_id, cu["id"])
        )
        db.commit()
        return dict(db.execute("SELECT * FROM fixed_assets WHERE id=?", (cur.lastrowid,)).fetchone())
    finally: db.close()

@app.post("/api/fixed-assets/{aid}/depreciate")
async def run_depreciation(aid: int, req: DepreciationReq, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin","accountant"): raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        asset = db.execute("SELECT * FROM fixed_assets WHERE id=? AND company_id=?",
                           (aid, cu["company_id"])).fetchone()
        if not asset: raise HTTPException(404, "Asset not found")
        if asset["status"] != "active": raise HTTPException(400, "Asset is not active")
        if db.execute("SELECT id FROM depreciation_log WHERE asset_id=? AND period=?",
                      (aid, req.period)).fetchone():
            raise HTTPException(400, f"Period {req.period} already depreciated")
        asset = dict(asset)
        depreciable = asset["purchase_cost"] - asset["salvage_value"]
        if asset["depreciation_method"] == "straight_line":
            amt = round(depreciable / asset["useful_life_years"] / 12, 2)
        else:
            amt = round(asset["book_value"] * asset["depreciation_rate"] / 100 / 12, 2)
        if asset["book_value"] - amt < asset["salvage_value"]:
            amt = max(round(asset["book_value"] - asset["salvage_value"], 2), 0)
        if amt <= 0: raise HTTPException(400, "Asset is fully depreciated")
        new_accum = round(asset["accumulated_depreciation"] + amt, 2)
        new_book  = round(asset["book_value"] - amt, 2)
        dep_date  = req.depreciation_date or datetime.now().strftime("%Y-%m-%d")
        dep_exp  = (db.execute("SELECT * FROM accounts WHERE id=? AND company_id=?",
                               (asset["dep_expense_account_id"], cu["company_id"])).fetchone()
                    if asset["dep_expense_account_id"] else None) or \
                   get_acc_by_code(db, cu["company_id"], "6900") or \
                   get_acc_by_subtype(db, cu["company_id"], "depreciation")
        acc_dep  = (db.execute("SELECT * FROM accounts WHERE id=? AND company_id=?",
                               (asset["accum_dep_account_id"], cu["company_id"])).fetchone()
                    if asset["accum_dep_account_id"] else None) or \
                   get_acc_by_code(db, cu["company_id"], "1600") or \
                   get_acc_by_subtype(db, cu["company_id"], "contra_asset")
        eid = None
        if dep_exp and acc_dep:
            eid = auto_post_gl(db, cu["company_id"], cu["id"], dep_date,
                               f"Depreciation {req.period}: {asset['name']}",
                               [(dep_exp["id"], amt, 0, f"Depr. {asset['asset_number']}"),
                                (acc_dep["id"], 0, amt, f"Depr. {asset['asset_number']}")])
        db.execute("UPDATE fixed_assets SET accumulated_depreciation=?,book_value=? WHERE id=?",
                   (new_accum, new_book, aid))
        db.execute("INSERT INTO depreciation_log(asset_id,period,amount,accumulated_total,book_value_after,journal_entry_id) VALUES(?,?,?,?,?,?)",
                   (aid, req.period, amt, new_accum, new_book, eid))
        db.commit()
        return {"period": req.period, "amount": amt, "accumulated": new_accum, "book_value": new_book}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

@app.post("/api/fixed-assets/{aid}/dispose")
async def dispose_asset(aid: int, body: dict, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin","accountant"): raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        asset = db.execute("SELECT * FROM fixed_assets WHERE id=? AND company_id=?",
                           (aid, cu["company_id"])).fetchone()
        if not asset: raise HTTPException(404, "Asset not found")
        if asset["status"] != "active": raise HTTPException(400, "Asset already disposed")
        db.execute("UPDATE fixed_assets SET status='disposed',disposal_date=?,disposal_proceeds=? WHERE id=?",
                   (body.get("disposal_date", datetime.now().strftime("%Y-%m-%d")),
                    float(body.get("proceeds", 0)), aid))
        db.commit()
        return {"message": "Asset disposed"}
    finally: db.close()

@app.get("/api/reports/asset-register")
async def asset_register(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("SELECT * FROM fixed_assets WHERE company_id=? ORDER BY category,asset_number",
                      (cu["company_id"],)).fetchall()
    rows = [dict(r) for r in rows]
    db.close()
    return {"assets": rows,
            "total_cost":        round(sum(r["purchase_cost"] for r in rows), 2),
            "total_accumulated": round(sum(r["accumulated_depreciation"] for r in rows), 2),
            "total_book_value":  round(sum(r["book_value"] for r in rows), 2)}

@app.get("/api/reports/depreciation-schedule/{aid}")
async def dep_schedule(aid: int, cu: dict = Depends(current_user)):
    db = get_db()
    asset = db.execute("SELECT * FROM fixed_assets WHERE id=? AND company_id=?",
                       (aid, cu["company_id"])).fetchone()
    if not asset: raise HTTPException(404, "Asset not found")
    asset = dict(asset)
    schedule = []
    book = asset["book_value"]; accum = asset["accumulated_depreciation"]
    depreciable = asset["purchase_cost"] - asset["salvage_value"]
    for yr in range(1, int(asset["useful_life_years"]) + 3):
        if book <= asset["salvage_value"] + 0.005: break
        annual = (round(depreciable / asset["useful_life_years"], 2)
                  if asset["depreciation_method"] == "straight_line"
                  else round(book * asset["depreciation_rate"] / 100, 2))
        if book - annual < asset["salvage_value"]:
            annual = max(round(book - asset["salvage_value"], 2), 0)
        accum = round(accum + annual, 2); book = round(book - annual, 2)
        schedule.append({"year": yr, "annual_depreciation": annual,
                          "accumulated": accum, "book_value": book})
    db.close()
    return {"asset": asset, "schedule": schedule}

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — TAX PERIODS
# ═══════════════════════════════════════════════════════════════════════════════

class TaxPeriodCreate(BaseModel):
    name: str
    period_type: str = "vat"
    start_date: str
    end_date: str

@app.get("/api/tax-periods")
async def list_tax_periods(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("SELECT * FROM tax_periods WHERE company_id=? ORDER BY start_date DESC",
                      (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/tax-periods", status_code=201)
async def create_tax_period(req: TaxPeriodCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO tax_periods(company_id,name,period_type,start_date,end_date) VALUES(?,?,?,?,?)",
            (cu["company_id"], req.name, req.period_type, req.start_date, req.end_date)
        )
        db.commit()
        return dict(db.execute("SELECT * FROM tax_periods WHERE id=?", (cur.lastrowid,)).fetchone())
    finally: db.close()

@app.get("/api/tax-periods/{tid}/summary")
async def tax_summary(tid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        period = db.execute("SELECT * FROM tax_periods WHERE id=? AND company_id=?",
                            (tid, cu["company_id"])).fetchone()
        if not period: raise HTTPException(404, "Tax period not found")
        period = dict(period)
        output = db.execute("""
            SELECT COALESCE(SUM(tax_amount),0) as t FROM invoices
            WHERE company_id=? AND date BETWEEN ? AND ? AND status NOT IN ('void','draft')
        """, (cu["company_id"], period["start_date"], period["end_date"])).fetchone()["t"]
        input_t = db.execute("""
            SELECT COALESCE(SUM(tax_amount),0) as t FROM bills
            WHERE company_id=? AND date BETWEEN ? AND ? AND status != 'void'
        """, (cu["company_id"], period["start_date"], period["end_date"])).fetchone()["t"]
        net = round(output - input_t, 2)
        db.execute("UPDATE tax_periods SET output_tax=?,input_tax=?,net_payable=? WHERE id=?",
                   (round(output,2), round(input_t,2), net, tid))
        db.commit()
        inv_lines = db.execute("""
            SELECT i.invoice_number, i.date, c.name as party, i.subtotal, i.tax_amount
            FROM invoices i JOIN customers c ON i.customer_id=c.id
            WHERE i.company_id=? AND i.date BETWEEN ? AND ?
              AND i.status NOT IN ('void','draft') AND i.tax_amount > 0 ORDER BY i.date
        """, (cu["company_id"], period["start_date"], period["end_date"])).fetchall()
        bill_lines = db.execute("""
            SELECT b.bill_number, b.date, s.name as party, b.subtotal, b.tax_amount
            FROM bills b JOIN suppliers s ON b.supplier_id=s.id
            WHERE b.company_id=? AND b.date BETWEEN ? AND ?
              AND b.status != 'void' AND b.tax_amount > 0 ORDER BY b.date
        """, (cu["company_id"], period["start_date"], period["end_date"])).fetchall()
        period.update({"output_tax": round(output,2), "input_tax": round(input_t,2), "net_payable": net,
                       "output_lines": [dict(r) for r in inv_lines],
                       "input_lines":  [dict(r) for r in bill_lines]})
        return period
    finally: db.close()

@app.post("/api/tax-periods/{tid}/file")
async def file_tax_period(tid: int, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin","accountant"): raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        period = db.execute("SELECT * FROM tax_periods WHERE id=? AND company_id=?",
                            (tid, cu["company_id"])).fetchone()
        if not period: raise HTTPException(404, "Tax period not found")
        if period["status"] == "filed": raise HTTPException(400, "Already filed")
        period = dict(period)
        # Recalculate to make sure figures are current
        output = db.execute(
            "SELECT COALESCE(SUM(tax_amount),0) as t FROM invoices WHERE company_id=? AND date BETWEEN ? AND ? AND status NOT IN ('void','draft')",
            (cu["company_id"], period["start_date"], period["end_date"])
        ).fetchone()["t"]
        input_t = db.execute(
            "SELECT COALESCE(SUM(tax_amount),0) as t FROM bills WHERE company_id=? AND date BETWEEN ? AND ? AND status != 'void'",
            (cu["company_id"], period["start_date"], period["end_date"])
        ).fetchone()["t"]
        net = round(output - input_t, 2)
        db.execute("UPDATE tax_periods SET output_tax=?,input_tax=?,net_payable=? WHERE id=?",
                   (round(output,2), round(input_t,2), net, tid))
        eid = None
        if net != 0:
            vat_payable = get_acc_by_code(db, cu["company_id"], "2210") or \
                          get_acc_by_subtype(db, cu["company_id"], "tax_payable")
            bank = get_acc_by_code(db, cu["company_id"], "1030") or \
                   get_acc_by_subtype(db, cu["company_id"], "bank")
            if vat_payable and bank:
                if net > 0:
                    # Net payable — DR VAT Payable, CR Bank
                    gl = [(vat_payable["id"], net, 0, f"VAT filed: {period['name']}"),
                          (bank["id"], 0, net, f"VAT payment: {period['name']}")]
                else:
                    # Net refund — DR Bank, CR VAT Payable
                    gl = [(bank["id"], abs(net), 0, f"VAT refund: {period['name']}"),
                          (vat_payable["id"], 0, abs(net), f"VAT refund: {period['name']}")]
                eid = auto_post_gl(db, cu["company_id"], cu["id"], datetime.now().strftime("%Y-%m-%d"),
                                   f"VAT Return filed — {period['name']}", gl)
        db.execute("UPDATE tax_periods SET status='filed', journal_entry_id=? WHERE id=?", (eid, tid))
        db.commit()
        return {"message": "Tax period filed and GL entry posted", "net_payable": net, "journal_entry_id": eid}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — PAY COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════════

class PayComponentCreate(BaseModel):
    component_type: str  # "allowance" | "deduction"
    name: str
    amount: float = Field(..., gt=0)
    is_percentage: bool = False

@app.get("/api/employees/{eid}/pay-components")
async def list_pay_components(eid: int, cu: dict = Depends(current_user)):
    db = get_db()
    if not db.execute("SELECT id FROM employees WHERE id=? AND company_id=?",
                      (eid, cu["company_id"])).fetchone():
        db.close(); raise HTTPException(404, "Employee not found")
    rows = db.execute(
        "SELECT * FROM pay_components WHERE employee_id=? ORDER BY component_type, name", (eid,)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/employees/{eid}/pay-components", status_code=201)
async def add_pay_component(eid: int, req: PayComponentCreate, cu: dict = Depends(current_user)):
    if req.component_type not in ("allowance", "deduction"):
        raise HTTPException(400, "component_type must be 'allowance' or 'deduction'")
    db = get_db()
    try:
        if not db.execute("SELECT id FROM employees WHERE id=? AND company_id=?",
                          (eid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Employee not found")
        cur = db.execute(
            "INSERT INTO pay_components(employee_id,component_type,name,amount,is_percentage) VALUES(?,?,?,?,?)",
            (eid, req.component_type, req.name, req.amount, 1 if req.is_percentage else 0)
        )
        db.commit()
        return dict(db.execute("SELECT * FROM pay_components WHERE id=?", (cur.lastrowid,)).fetchone())
    finally: db.close()

@app.delete("/api/pay-components/{cid}")
async def delete_pay_component(cid: int, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        row = db.execute(
            "SELECT pc.* FROM pay_components pc JOIN employees e ON pc.employee_id=e.id WHERE pc.id=? AND e.company_id=?",
            (cid, cu["company_id"])
        ).fetchone()
        if not row: raise HTTPException(404, "Component not found")
        db.execute("DELETE FROM pay_components WHERE id=?", (cid,))
        db.commit()
        return {"message": "Component removed"}
    finally: db.close()

@app.get("/api/reports/low-stock")
async def low_stock_report_route(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT *, code as sku, current_stock * cost_price as stock_value,
               (reorder_level - current_stock) as shortage
        FROM products
        WHERE company_id=? AND is_active=1 AND reorder_level > 0 AND current_stock <= reorder_level
        ORDER BY (current_stock / NULLIF(reorder_level,0)) ASC
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — MULTI-COMPANY
# ═══════════════════════════════════════════════════════════════════════════════

class CompanyCreate(BaseModel):
    name: str
    currency: str = "USD"
    currency_symbol: str = "$"
    country: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    tax_number: Optional[str] = None
    fiscal_year_start: int = 1

class SwitchCompanyReq(BaseModel):
    company_id: int

@app.get("/api/companies")
async def list_my_companies(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT c.*, uc.role as my_role
        FROM companies c
        JOIN user_companies uc ON uc.company_id = c.id
        WHERE uc.user_id = ?
        ORDER BY c.name
    """, (cu["id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/companies", status_code=201)
async def create_company(req: CompanyCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO companies(name,currency,currency_symbol,country,address,phone,email,tax_number,fiscal_year_start) VALUES(?,?,?,?,?,?,?,?,?)",
            (req.name, req.currency, req.currency_symbol, req.country,
             req.address, req.phone, req.email, req.tax_number, req.fiscal_year_start)
        )
        cid = cur.lastrowid
        # Give the creating user admin access
        db.execute(
            "INSERT OR IGNORE INTO user_companies(user_id, company_id, role) VALUES(?,?,'admin')",
            (cu["id"], cid)
        )
        db.commit()
        company = dict(db.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone())
        company["my_role"] = "admin"
        return company
    except Exception as e:
        db.rollback(); raise HTTPException(500, str(e))
    finally:
        db.close()

@app.post("/api/auth/switch-company")
async def switch_company(req: SwitchCompanyReq, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        uc = db.execute(
            "SELECT * FROM user_companies WHERE user_id=? AND company_id=?",
            (cu["id"], req.company_id)
        ).fetchone()
        if not uc:
            raise HTTPException(403, "You do not have access to that company")
        uc = dict(uc)
        # Issue new tokens with updated cid and role
        access = make_token(
            {"sub": str(cu["id"]), "cid": req.company_id, "role": uc["role"]},
            timedelta(minutes=ACCESS_EXPIRE_MIN)
        )
        refresh = make_token(
            {"sub": str(cu["id"]), "type": "refresh"},
            timedelta(days=REFRESH_EXPIRE_DAYS)
        )
        company = dict(db.execute("SELECT * FROM companies WHERE id=?", (req.company_id,)).fetchone())
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
                "company": company, "role": uc["role"]}
    finally:
        db.close()

@app.post("/api/companies/{cid}/invite")
async def invite_user_to_company(cid: int, body: dict, cu: dict = Depends(current_user)):
    if cu["role"] != "admin": raise HTTPException(403, "Admin only")
    if cu["company_id"] != cid: raise HTTPException(403, "Wrong company")
    db = get_db()
    try:
        email = body.get("email", "").strip().lower()
        role  = body.get("role", "accountant")
        if not email: raise HTTPException(400, "Email required")
        target = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not target: raise HTTPException(404, f"No user found with email {email}")
        db.execute(
            "INSERT OR REPLACE INTO user_companies(user_id, company_id, role) VALUES(?,?,?)",
            (target["id"], cid, role)
        )
        db.commit()
        return {"message": f"{email} granted {role} access to this company"}
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — BUDGETING
# ═══════════════════════════════════════════════════════════════════════════════

class BudgetUpsert(BaseModel):
    account_id: int
    year: int
    month: int = Field(..., ge=1, le=12)
    amount: float = Field(..., ge=0)
    scenario: str = "Base"
    notes: Optional[str] = None

@app.get("/api/budgets")
async def list_budgets(
    year: int = Query(...),
    scenario: str = Query("Base"),
    cu: dict = Depends(current_user)
):
    db = get_db()
    rows = db.execute("""
        SELECT b.*, a.code as account_code, a.name as account_name,
               a.type as account_type, a.normal_balance
        FROM budgets b JOIN accounts a ON b.account_id=a.id
        WHERE b.company_id=? AND b.year=? AND b.scenario=?
        ORDER BY a.code, b.month
    """, (cu["company_id"], year, scenario)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.get("/api/budget-scenarios")
async def list_scenarios(year: int = Query(...), cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT scenario, COUNT(*) as entry_count
        FROM budgets WHERE company_id=? AND year=?
        GROUP BY scenario ORDER BY scenario
    """, (cu["company_id"], year)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/budgets", status_code=201)
async def upsert_budget(req: BudgetUpsert, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"):
        raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        if not db.execute("SELECT id FROM accounts WHERE id=? AND company_id=?",
                          (req.account_id, cu["company_id"])).fetchone():
            raise HTTPException(400, "Account not found")

        # Manual upsert — avoids depending on the exact UNIQUE constraint variant in SQLite
        existing = db.execute(
            "SELECT id FROM budgets WHERE company_id=? AND account_id=? AND year=? AND month=? AND scenario=?",
            (cu["company_id"], req.account_id, req.year, req.month, req.scenario)
        ).fetchone()

        if existing:
            db.execute(
                "UPDATE budgets SET amount=?, notes=?, created_by=? WHERE id=?",
                (req.amount, req.notes, cu["id"], existing["id"])
            )
        else:
            db.execute(
                "INSERT INTO budgets(company_id, account_id, year, month, amount, scenario, notes, created_by) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (cu["company_id"], req.account_id, req.year, req.month,
                 req.amount, req.scenario, req.notes, cu["id"])
            )

        db.commit()
        return {"message": "Budget saved", "scenario": req.scenario, "year": req.year, "month": req.month}
    except HTTPException:
        db.rollback(); raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Budget save failed: {str(e)}")
    finally:
        db.close()

@app.post("/api/budget-scenarios/copy")
async def copy_scenario(body: dict, cu: dict = Depends(current_user)):
    """Copy all entries from one scenario to another, applying an optional multiplier."""
    if cu["role"] not in ("admin", "accountant"):
        raise HTTPException(403, "Insufficient permissions")
    src        = body.get("from_scenario", "Base")
    dst        = (body.get("to_scenario") or "").strip()
    year       = int(body.get("year", datetime.now().year))
    multiplier = float(body.get("multiplier", 1.0))
    if not dst:
        raise HTTPException(400, "to_scenario is required")
    if dst == src:
        raise HTTPException(400, "New scenario name must differ from source")
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM budgets WHERE company_id=? AND year=? AND scenario=?",
            (cu["company_id"], year, src)
        ).fetchall()
        if not rows:
            raise HTTPException(404, f"No budget entries found for scenario '{src}' in {year}")
        copied = 0
        for r in rows:
            new_amount = round(r["amount"] * multiplier, 2)
            existing = db.execute(
                "SELECT id FROM budgets WHERE company_id=? AND account_id=? AND year=? AND month=? AND scenario=?",
                (cu["company_id"], r["account_id"], r["year"], r["month"], dst)
            ).fetchone()
            if existing:
                db.execute("UPDATE budgets SET amount=? WHERE id=?", (new_amount, existing["id"]))
            else:
                db.execute(
                    "INSERT INTO budgets(company_id, account_id, year, month, amount, scenario, notes, created_by) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (cu["company_id"], r["account_id"], r["year"], r["month"],
                     new_amount, dst, r["notes"], cu["id"])
                )
            copied += 1
        db.commit()
        return {"message": f"Scenario '{dst}' created with {copied} entries from '{src}' (×{multiplier})",
                "copied": copied, "scenario": dst}
    except HTTPException:
        db.rollback(); raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Scenario copy failed: {str(e)}")
    finally:
        db.close()

@app.delete("/api/budgets/{bid}")
async def delete_budget(bid: int, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"): raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        if not db.execute("SELECT id FROM budgets WHERE id=? AND company_id=?",
                          (bid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Budget entry not found")
        db.execute("DELETE FROM budgets WHERE id=?", (bid,))
        db.commit()
        return {"message": "Budget entry deleted"}
    finally:
        db.close()

def _get_actuals_by_account_month(db, company_id: int, year: int) -> dict:
    """Returns {account_id: {month: net_amount}} for revenue/expense accounts."""
    rows = db.execute("""
        SELECT a.id as account_id, a.code, a.name, a.type, a.normal_balance,
               CAST(strftime('%m', je.date) AS INTEGER) as month,
               COALESCE(SUM(jl.debit),0)  as dr,
               COALESCE(SUM(jl.credit),0) as cr
        FROM accounts a
        LEFT JOIN journal_lines jl ON jl.account_id=a.id
        LEFT JOIN journal_entries je ON jl.entry_id=je.id
            AND je.status='posted' AND strftime('%Y', je.date)=?
        WHERE a.company_id=? AND a.type IN ('revenue','expense')
        GROUP BY a.id, month
    """, (str(year), company_id)).fetchall()
    result: dict = {}
    for r in rows:
        if not r["month"]: continue
        aid = r["account_id"]
        if aid not in result:
            result[aid] = {"code": r["code"], "name": r["name"],
                           "type": r["type"], "normal_balance": r["normal_balance"],
                           "months": {}}
        net = (r["cr"] - r["dr"] if r["normal_balance"] == "credit" else r["dr"] - r["cr"])
        result[aid]["months"][r["month"]] = round(net, 2)
    return result

@app.get("/api/reports/budget-vs-actual")
async def budget_vs_actual(
    year: int = Query(...),
    scenario: str = Query("Base"),
    cu: dict = Depends(current_user)
):
    db = get_db()
    try:
        MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        actuals_map = _get_actuals_by_account_month(db, cu["company_id"], year)
        budgets = db.execute("""
            SELECT b.account_id, b.month, b.amount, a.code, a.name, a.type, a.normal_balance
            FROM budgets b JOIN accounts a ON b.account_id=a.id
            WHERE b.company_id=? AND b.year=? AND b.scenario=?
        """, (cu["company_id"], year, scenario)).fetchall()

        acc_map: dict = {}
        for aid, info in actuals_map.items():
            acc_map[aid] = {**info,
                            "account_id": aid,
                            "months": {m: {"actual": info["months"].get(m, 0.0), "budget": 0.0}
                                       for m in range(1, 13)}}
        for b in budgets:
            aid = b["account_id"]
            mon = int(b["month"])
            if aid not in acc_map:
                acc_map[aid] = {"account_id": aid, "code": b["code"], "name": b["name"],
                                "type": b["type"], "normal_balance": b["normal_balance"],
                                "months": {m: {"actual": 0.0, "budget": 0.0} for m in range(1,13)}}
            acc_map[aid]["months"][mon]["budget"] = float(b["amount"])

        rows = []
        for aid, acc in sorted(acc_map.items(), key=lambda x: x[1]["code"]):
            total_actual = sum(m["actual"] for m in acc["months"].values())
            total_budget = sum(m["budget"] for m in acc["months"].values())
            rows.append({**acc, "account_id": aid,
                         "total_actual": round(total_actual, 2),
                         "total_budget": round(total_budget, 2),
                         "total_variance": round(total_actual - total_budget, 2),
                         "months": {str(k): {**v, "variance": round(v["actual"]-v["budget"],2)}
                                    for k, v in acc["months"].items()}})
        return {"year": year, "scenario": scenario, "months": MONTHS, "rows": rows}
    finally:
        db.close()

@app.get("/api/reports/rolling-forecast")
async def rolling_forecast(year: int = Query(...), scenario: str = Query("Base"),
                            cu: dict = Depends(current_user)):
    """YTD actuals + budget for remaining months + 3-month forward extrapolation."""
    db = get_db()
    try:
        MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
        today  = datetime.now()
        cur_month = today.month if today.year == year else (12 if today.year > year else 0)

        actuals_map = _get_actuals_by_account_month(db, cu["company_id"], year)
        budgets = db.execute("""
            SELECT b.account_id, b.month, b.amount, a.code, a.name, a.type, a.normal_balance
            FROM budgets b JOIN accounts a ON b.account_id=a.id
            WHERE b.company_id=? AND b.year=? AND b.scenario=?
        """, (cu["company_id"], year, scenario)).fetchall()
        budget_map: dict = {}
        for b in budgets:
            budget_map.setdefault(b["account_id"], {})[int(b["month"])] = float(b["amount"])

        rows = []
        all_ids = set(actuals_map.keys()) | set(budget_map.keys())
        for aid in all_ids:
            a_info = actuals_map.get(aid, {})
            b_months = budget_map.get(aid, {})
            months_data = {}
            ytd_actuals = []
            for m in range(1, 13):
                actual  = a_info.get("months", {}).get(m, None)
                budget  = b_months.get(m, 0.0)
                if m <= cur_month:
                    val = actual if actual is not None else 0.0
                    ytd_actuals.append(val)
                    months_data[m] = {"type": "actual", "value": round(val, 2), "budget": round(budget, 2)}
                else:
                    if budget > 0:
                        val = budget
                        kind = "budget"
                    elif ytd_actuals:
                        val = sum(ytd_actuals) / len(ytd_actuals)  # rolling average
                        kind = "forecast"
                    else:
                        val = 0.0; kind = "forecast"
                    months_data[m] = {"type": kind, "value": round(val, 2), "budget": round(budget, 2)}

            total_ytd      = sum(v["value"] for k, v in months_data.items() if v["type"] == "actual")
            total_forecast = sum(v["value"] for k, v in months_data.items() if v["type"] != "actual")
            total_budget   = sum(v["budget"] for v in months_data.values())

            # Find account meta from whichever source has it
            meta = a_info or {}
            code = meta.get("code","")
            name = meta.get("name","")
            acc_type = meta.get("type","")
            if not code:
                b_row = next((b for b in budgets if b["account_id"] == aid), None)
                if b_row: code=b_row["code"]; name=b_row["name"]; acc_type=b_row["type"]

            rows.append({"account_id": aid, "code": code, "name": name, "type": acc_type,
                         "months": months_data,
                         "total_ytd": round(total_ytd, 2),
                         "total_forecast": round(total_forecast, 2),
                         "full_year_forecast": round(total_ytd + total_forecast, 2),
                         "total_budget": round(total_budget, 2)})
        rows.sort(key=lambda r: r["code"])
        return {"year": year, "scenario": scenario, "current_month": cur_month,
                "months": MONTHS, "rows": rows}
    finally:
        db.close()

@app.get("/api/reports/scenario-comparison")
async def scenario_comparison(year: int = Query(...), cu: dict = Depends(current_user)):
    """Compare all scenarios for a year side-by-side."""
    db = get_db()
    try:
        scenarios_raw = db.execute(
            "SELECT DISTINCT scenario FROM budgets WHERE company_id=? AND year=? ORDER BY scenario",
            (cu["company_id"], year)
        ).fetchall()
        scenarios = [r["scenario"] for r in scenarios_raw]
        if not scenarios:
            return {"year": year, "scenarios": [], "rows": []}

        actuals_map = _get_actuals_by_account_month(db, cu["company_id"], year)

        # Load all budget entries across all scenarios
        all_budgets = db.execute("""
            SELECT b.account_id, b.month, b.amount, b.scenario,
                   a.code, a.name, a.type
            FROM budgets b JOIN accounts a ON b.account_id=a.id
            WHERE b.company_id=? AND b.year=?
        """, (cu["company_id"], year)).fetchall()

        # {account_id: {scenario: annual_total}}
        acc_totals: dict = {}
        acc_meta: dict   = {}
        for b in all_budgets:
            aid = b["account_id"]
            acc_meta[aid] = {"code": b["code"], "name": b["name"], "type": b["type"]}
            acc_totals.setdefault(aid, {})
            acc_totals[aid][b["scenario"]] = acc_totals[aid].get(b["scenario"], 0) + float(b["amount"])

        # Add actual totals
        for aid, info in actuals_map.items():
            if aid not in acc_meta:
                acc_meta[aid] = {"code": info["code"], "name": info["name"], "type": info["type"]}
            actual_total = sum(info["months"].values())
            acc_totals.setdefault(aid, {})
            acc_totals[aid]["__actual__"] = round(actual_total, 2)

        rows = []
        for aid in sorted(acc_totals.keys(), key=lambda a: acc_meta.get(a, {}).get("code", "")):
            meta = acc_meta.get(aid, {})
            row  = {"account_id": aid, "code": meta.get("code",""), "name": meta.get("name",""),
                    "type": meta.get("type",""), "actual": acc_totals[aid].get("__actual__", 0)}
            for s in scenarios:
                row[s] = round(acc_totals[aid].get(s, 0), 2)
            rows.append(row)

        return {"year": year, "scenarios": scenarios, "rows": rows}
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — API KEYS
# ═══════════════════════════════════════════════════════════════════════════════

def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

def _generate_api_key() -> tuple:
    raw = "ttech_" + secrets.token_urlsafe(32)
    return raw, raw[:12], _hash_api_key(raw)

async def api_key_user(x_api_key: Optional[str] = Header(None)):
    """Resolve an API-key header to a user dict, or return None."""
    if not x_api_key:
        return None
    key_hash = _hash_api_key(x_api_key)
    db = get_db()
    try:
        row = db.execute("""
            SELECT ak.*, u.id as uid, u.company_id, u.role, u.full_name, u.email, u.is_active
            FROM api_keys ak JOIN users u ON ak.user_id=u.id
            WHERE ak.key_hash=? AND ak.is_active=1 AND u.is_active=1
        """, (key_hash,)).fetchone()
        if not row: return None
        db.execute("UPDATE api_keys SET last_used=datetime('now') WHERE id=?", (row["id"],))
        db.commit()
        return {"id": row["uid"], "company_id": row["company_id"],
                "role": row["role"], "full_name": row["full_name"],
                "email": row["email"], "is_active": row["is_active"]}
    finally:
        db.close()

class ApiKeyCreate(BaseModel):
    name: str
    permissions: str = "read"

@app.get("/api/api-keys")
async def list_api_keys(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT ak.id, ak.name, ak.key_prefix, ak.permissions, ak.is_active, ak.last_used, ak.created_at,
               u.full_name as created_by_name
        FROM api_keys ak JOIN users u ON ak.user_id=u.id
        WHERE ak.company_id=?
        ORDER BY ak.created_at DESC
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/api-keys", status_code=201)
async def create_api_key(req: ApiKeyCreate, cu: dict = Depends(current_user)):
    if cu["role"] != "admin": raise HTTPException(403, "Admin only")
    raw, prefix, key_hash = _generate_api_key()
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO api_keys(company_id, user_id, name, key_prefix, key_hash, permissions) VALUES(?,?,?,?,?,?)",
            (cu["company_id"], cu["id"], req.name, prefix, key_hash, req.permissions)
        )
        db.commit()
        return {"id": cur.lastrowid, "name": req.name, "key_prefix": prefix,
                "permissions": req.permissions, "full_key": raw,
                "message": "Store this key now — it will not be shown again."}
    finally:
        db.close()

@app.delete("/api/api-keys/{kid}")
async def revoke_api_key(kid: int, cu: dict = Depends(current_user)):
    if cu["role"] != "admin": raise HTTPException(403, "Admin only")
    db = get_db()
    try:
        if not db.execute("SELECT id FROM api_keys WHERE id=? AND company_id=?",
                          (kid, cu["company_id"])).fetchone():
            raise HTTPException(404, "API key not found")
        db.execute("UPDATE api_keys SET is_active=0 WHERE id=?", (kid,))
        db.commit()
        return {"message": "API key revoked"}
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — TWO-FACTOR AUTHENTICATION (TOTP)
# ═══════════════════════════════════════════════════════════════════════════════

TWO_FA_TOKEN_EXPIRE = timedelta(minutes=5)

def _make_2fa_temp_token(user_id: int) -> str:
    return make_token({"sub": str(user_id), "type": "2fa_pending"}, TWO_FA_TOKEN_EXPIRE)

def _verify_totp(secret: str, code: str) -> bool:
    if not PYOTP_AVAILABLE:
        return code == "000000"
    return pyotp.TOTP(secret).verify(code, valid_window=1)

def _generate_backup_codes() -> list:
    return [secrets.token_hex(4).upper() for _ in range(8)]

@app.get("/api/auth/2fa/status")
async def twofa_status(cu: dict = Depends(current_user)):
    db = get_db()
    row = db.execute("SELECT is_enabled FROM user_2fa WHERE user_id=?", (cu["id"],)).fetchone()
    db.close()
    return {"enabled": bool(row and row["is_enabled"]),
            "pyotp_available": PYOTP_AVAILABLE}

@app.post("/api/auth/2fa/setup")
async def twofa_setup(cu: dict = Depends(current_user)):
    if not PYOTP_AVAILABLE:
        raise HTTPException(501, "pyotp not installed — run: pip install pyotp")
    db = get_db()
    try:
        secret = pyotp.random_base32()
        company = db.execute("SELECT name FROM companies WHERE id=?",
                             (cu["company_id"],)).fetchone()
        issuer = (company["name"] if company else "T-Tech Accountant").replace(" ", "")
        otp_url = pyotp.TOTP(secret).provisioning_uri(
            name=cu["email"], issuer_name=issuer
        )
        # Store as pending (not yet enabled)
        db.execute("""
            INSERT INTO user_2fa(user_id, totp_secret, is_enabled)
            VALUES(?,?,0)
            ON CONFLICT(user_id) DO UPDATE SET totp_secret=excluded.totp_secret, is_enabled=0
        """, (cu["id"], secret))
        db.commit()
        return {"otp_url": otp_url, "secret": secret,
                "message": "Scan the QR code with your authenticator app, then verify."}
    finally:
        db.close()

@app.post("/api/auth/2fa/enable")
async def twofa_enable(body: dict, cu: dict = Depends(current_user)):
    code = str(body.get("code", "")).strip()
    db = get_db()
    try:
        row = db.execute("SELECT * FROM user_2fa WHERE user_id=?", (cu["id"],)).fetchone()
        if not row: raise HTTPException(400, "Run /api/auth/2fa/setup first")
        if not _verify_totp(row["totp_secret"], code):
            raise HTTPException(400, "Invalid TOTP code")
        backup = _generate_backup_codes()
        db.execute("UPDATE user_2fa SET is_enabled=1, backup_codes=? WHERE user_id=?",
                   (json.dumps(backup), cu["id"]))
        db.execute("UPDATE users SET totp_enabled=1 WHERE id=?", (cu["id"],))
        db.commit()
        return {"message": "2FA enabled successfully",
                "backup_codes": backup,
                "warning": "Save these backup codes — they will not be shown again."}
    finally:
        db.close()

@app.post("/api/auth/2fa/disable")
async def twofa_disable(body: dict, cu: dict = Depends(current_user)):
    code = str(body.get("code", "")).strip()
    db = get_db()
    try:
        row = db.execute("SELECT * FROM user_2fa WHERE user_id=? AND is_enabled=1",
                         (cu["id"],)).fetchone()
        if not row: raise HTTPException(400, "2FA is not enabled")
        if not _verify_totp(row["totp_secret"], code):
            # Also accept backup codes
            backup = json.loads(row["backup_codes"] or "[]")
            if code.upper() not in backup:
                raise HTTPException(400, "Invalid code")
        db.execute("UPDATE user_2fa SET is_enabled=0 WHERE user_id=?", (cu["id"],))
        db.execute("UPDATE users SET totp_enabled=0 WHERE id=?", (cu["id"],))
        db.commit()
        return {"message": "2FA disabled"}
    finally:
        db.close()

@app.post("/api/auth/2fa/verify")
async def twofa_verify(body: dict):
    """Second step of login when 2FA is enabled."""
    temp_token = body.get("temp_token", "")
    code       = str(body.get("code", "")).strip()
    try:
        payload = decode_token(temp_token)
    except Exception:
        raise HTTPException(401, "Invalid or expired session")
    if payload.get("type") != "2fa_pending":
        raise HTTPException(401, "Invalid token type")
    db = get_db()
    try:
        uid = int(payload["sub"])
        u   = db.execute(
            """SELECT u.*, c.name as company_name, c.currency, c.currency_symbol
               FROM users u JOIN companies c ON u.company_id=c.id
               WHERE u.id=? AND u.is_active=1""", (uid,)
        ).fetchone()
        if not u: raise HTTPException(401, "User not found")
        u = dict(u)
        row = db.execute("SELECT * FROM user_2fa WHERE user_id=?", (uid,)).fetchone()
        if not row: raise HTTPException(400, "2FA not configured")

        verified = _verify_totp(row["totp_secret"], code)
        if not verified:
            backup = json.loads(row["backup_codes"] or "[]")
            if code.upper() in backup:
                # Consume backup code
                backup.remove(code.upper())
                db.execute("UPDATE user_2fa SET backup_codes=? WHERE user_id=?",
                           (json.dumps(backup), uid))
                db.commit()
                verified = True
        if not verified:
            raise HTTPException(400, "Invalid TOTP code")

        access  = make_token({"sub": str(uid), "cid": u["company_id"], "role": u["role"]},
                             timedelta(minutes=ACCESS_EXPIRE_MIN))
        refresh = make_token({"sub": str(uid), "type": "refresh"},
                             timedelta(days=REFRESH_EXPIRE_DAYS))
        audit(db, uid, "login_2fa", "auth"); db.commit()
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
                "user": {k: v for k, v in u.items() if k != "password_hash"}}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — CASH FLOW STATEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/reports/cash-flow")
async def cash_flow(
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    from_date:  Optional[str] = None,   # alias
    to_date:    Optional[str] = None,   # alias
    cu: dict = Depends(current_user)
):
    date_from = date_from or from_date
    date_to   = date_to   or to_date
    db = get_db()
    try:
        cid   = cu["company_id"]
        today = datetime.now().strftime("%Y-%m-%d")
        d_from = date_from or datetime.now().replace(month=1, day=1).strftime("%Y-%m-%d")
        d_to   = date_to   or today

        def acct_flow(types, subtypes=None):
            """Net movement for account types in the period."""
            type_sql = ",".join(f"'{t}'" for t in types)
            sub_sql  = ""
            if subtypes:
                sub_sql = " AND a.sub_type IN (" + ",".join(f"'{s}'" for s in subtypes) + ")"
            rows = db.execute(f"""
                SELECT a.normal_balance,
                       COALESCE(SUM(jl.debit),0)  as dr,
                       COALESCE(SUM(jl.credit),0) as cr
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id=je.id
                JOIN accounts a ON jl.account_id=a.id
                WHERE je.company_id=? AND je.status='posted'
                  AND je.date BETWEEN ? AND ?
                  AND a.type IN ({type_sql}){sub_sql}
                GROUP BY a.normal_balance
            """, (cid, d_from, d_to)).fetchall()
            total = 0.0
            for r in rows:
                net = r["dr"] - r["cr"]
                total += -net if r["normal_balance"] == "credit" else net
            return round(total, 2)

        # Opening & closing cash balances
        def cash_balance(at_date: str) -> float:
            r = db.execute("""
                SELECT COALESCE(SUM(jl.debit - jl.credit), 0) as bal
                FROM journal_lines jl
                JOIN journal_entries je ON jl.entry_id=je.id
                JOIN accounts a ON jl.account_id=a.id
                WHERE je.company_id=? AND je.status='posted'
                  AND je.date <= ?
                  AND a.sub_type IN ('cash','bank')
            """, (cid, at_date)).fetchone()
            return round(r["bal"] if r else 0, 2)

        # Day-before opening date
        from datetime import date as _date
        d_from_dt = _date.fromisoformat(d_from)
        opening_date = (d_from_dt - timedelta(days=1)).isoformat()

        opening_cash = cash_balance(opening_date)
        closing_cash = cash_balance(d_to)

        # Operating activities (indirect method)
        revenue       = acct_flow(["revenue"])
        expenses      = acct_flow(["expense"])
        net_profit    = round(revenue - expenses, 2)
        depreciation  = acct_flow(["expense"], ["depreciation"])
        ar_change     = acct_flow(["asset"], ["receivable"])
        inventory_chg = acct_flow(["asset"], ["inventory"])
        ap_change     = -acct_flow(["liability"], ["payable"])
        operating     = round(net_profit + depreciation - ar_change - inventory_chg + ap_change, 2)

        # Investing activities
        fa_purchases  = acct_flow(["asset"], ["fixed_asset"])
        accum_dep     = acct_flow(["asset"], ["contra_asset"])
        investing     = round(-(fa_purchases + accum_dep), 2)

        # Financing activities
        loans_net     = -acct_flow(["liability"], ["loan"])
        equity_net    = -acct_flow(["equity"], ["share_capital", "owners_equity"])
        drawings      = acct_flow(["equity"], ["drawings"])
        financing     = round(loans_net + equity_net - drawings, 2)

        net_change = round(operating + investing + financing, 2)

        return {
            "period": {"from": d_from, "to": d_to},
            "opening_cash": opening_cash,
            "closing_cash": closing_cash,
            "net_change_in_cash": net_change,
            "operating": {
                "net_profit": net_profit,
                "add_depreciation": depreciation,
                "change_in_receivables": -ar_change,
                "change_in_inventory": -inventory_chg,
                "change_in_payables": ap_change,
                "total": operating,
            },
            "investing": {
                "fixed_asset_additions": -fa_purchases,
                "total": investing,
            },
            "financing": {
                "net_borrowings": loans_net,
                "equity_contributions": equity_net,
                "drawings": -drawings,
                "total": financing,
            },
        }
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════════
# ANOMALY DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

import hashlib as _hashlib

def _fp(*args) -> str:
    """Stable fingerprint for an anomaly — used to track dismissals."""
    return _hashlib.md5("|".join(str(a) for a in args).encode()).hexdigest()[:16]

def _run_anomaly_scan(db, company_id: int) -> list:
    """
    Run all detection rules and return list of anomaly dicts.
    Each anomaly has: rule, severity, title, description, fingerprint,
    amount, date, entity_type, entity_id, links[]
    """
    results = []
    today = datetime.now()
    today_s = today.strftime("%Y-%m-%d")

    # ── RULE 1: Duplicate journal entries (same total, same date) ─────────────
    try:
        dups = db.execute("""
            SELECT je.date,
                   ROUND(SUM(jl.debit),2) as amount,
                   COUNT(DISTINCT je.id) as cnt,
                   GROUP_CONCAT(je.entry_number ORDER BY je.id) as entry_numbers,
                   GROUP_CONCAT(je.id ORDER BY je.id) as entry_ids
            FROM journal_entries je
            JOIN journal_lines jl ON jl.entry_id = je.id
            WHERE je.company_id = ? AND je.status = 'posted'
              AND je.date >= date('now','-90 days')
            GROUP BY je.date, ROUND(SUM(jl.debit),2)
            HAVING cnt > 1 AND amount > 0
            ORDER BY amount DESC
        """, (company_id,)).fetchall()
        for d in dups:
            fp = _fp("duplicate", d["date"], d["amount"])
            first_id = int(d["entry_ids"].split(",")[0]) if d["entry_ids"] else None
            results.append({
                "rule": "duplicate_amount", "severity": "high",
                "title": "Possible duplicate posting",
                "description": (f"{d['cnt']} journal entries each totalling "
                                f"{d['amount']:.2f} on {d['date']}. "
                                f"Entries: {d['entry_numbers']}"),
                "amount": d["amount"], "date": d["date"],
                "entity_type": "journal_entry", "entity_id": first_id,
                "extra_ids": d["entry_ids"],
                "fingerprint": fp,
            })
    except Exception:
        pass

    # ── RULE 2: Accounts with reversed normal balance ─────────────────────────
    try:
        reversed_accs = db.execute("""
            SELECT a.id, a.code, a.name, a.type, a.normal_balance,
                   COALESCE(SUM(jl.debit - jl.credit),0) as net
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id = a.id
            LEFT JOIN journal_entries je ON jl.entry_id = je.id
                   AND je.status='posted' AND je.company_id = ?
            WHERE a.company_id = ? AND a.is_active = 1
              AND a.type NOT IN ('contra_asset')
              AND a.sub_type NOT IN ('contra_asset','drawings')
            GROUP BY a.id
            HAVING (a.normal_balance='debit'  AND net < -1)
                OR (a.normal_balance='credit' AND net >  1)
        """, (company_id, company_id)).fetchall()
        for acc in reversed_accs:
            fp = _fp("reversed_balance", acc["id"])
            results.append({
                "rule": "reversed_balance", "severity": "high",
                "title": f"Reversed balance — {acc['code']} {acc['name']}",
                "description": (f"{acc['type'].title()} account '{acc['name']}' "
                                f"has a {('credit' if acc['net']<0 else 'debit')} balance of "
                                f"{abs(acc['net']):.2f}. Expected normal balance: {acc['normal_balance']}."),
                "amount": acc["net"], "date": today_s,
                "entity_type": "account", "entity_id": acc["id"],
                "fingerprint": fp,
            })
    except Exception:
        pass

    # ── RULE 3: Future-dated journal entries ──────────────────────────────────
    try:
        future = db.execute("""
            SELECT je.id, je.entry_number, je.date,
                   COALESCE(SUM(jl.debit),0) as amount
            FROM journal_entries je
            JOIN journal_lines jl ON jl.entry_id = je.id
            WHERE je.company_id = ? AND je.status = 'posted'
              AND je.date > date('now','+1 day')
            GROUP BY je.id ORDER BY je.date
        """, (company_id,)).fetchall()
        for e in future:
            fp = _fp("future_dated", e["id"])
            results.append({
                "rule": "future_dated", "severity": "high",
                "title": f"Future-dated entry — {e['entry_number']}",
                "description": (f"Journal entry {e['entry_number']} is dated {e['date']} "
                                f"({(datetime.strptime(e['date'],'%Y-%m-%d').date()-today.date()).days}d ahead). "
                                f"Verify this is intentional."),
                "amount": e["amount"], "date": e["date"],
                "entity_type": "journal_entry", "entity_id": e["id"],
                "fingerprint": fp,
            })
    except Exception:
        pass

    # ── RULE 4: Unusually large single transaction (>5× 90-day avg) ──────────
    try:
        avg_row = db.execute("""
            SELECT AVG(total) as avg_amount
            FROM (
                SELECT ROUND(SUM(jl.debit),2) as total
                FROM journal_entries je
                JOIN journal_lines jl ON jl.entry_id = je.id
                WHERE je.company_id=? AND je.status='posted'
                  AND je.date >= date('now','-90 days')
                GROUP BY je.id
            )
        """, (company_id,)).fetchone()
        avg_amt = avg_row["avg_amount"] or 0
        if avg_amt > 0:
            threshold = max(avg_amt * 5, 1000)
            large = db.execute("""
                SELECT je.id, je.entry_number, je.date,
                       ROUND(SUM(jl.debit),2) as amount
                FROM journal_entries je
                JOIN journal_lines jl ON jl.entry_id = je.id
                WHERE je.company_id=? AND je.status='posted'
                  AND je.date >= date('now','-30 days')
                GROUP BY je.id
                HAVING amount > ?
                ORDER BY amount DESC LIMIT 5
            """, (company_id, threshold)).fetchall()
            for e in large:
                fp = _fp("large_transaction", e["id"])
                results.append({
                    "rule": "large_transaction", "severity": "medium",
                    "title": f"Unusually large entry — {e['entry_number']}",
                    "description": (f"{e['entry_number']} is {e['amount']:.2f} — "
                                    f"{e['amount']/avg_amt:.1f}× the 90-day average of {avg_amt:.2f}. "
                                    f"Verify this is correct."),
                    "amount": e["amount"], "date": e["date"],
                    "entity_type": "journal_entry", "entity_id": e["id"],
                    "fingerprint": fp,
                })
    except Exception:
        pass

    # ── RULE 5: Round-number large transactions ────────────────────────────────
    try:
        rounds = db.execute("""
            SELECT je.id, je.entry_number, je.date,
                   ROUND(SUM(jl.debit),2) as amount
            FROM journal_entries je
            JOIN journal_lines jl ON jl.entry_id = je.id
            WHERE je.company_id=? AND je.status='posted'
              AND je.date >= date('now','-30 days')
            GROUP BY je.id
            HAVING amount >= 1000
               AND CAST(ROUND(amount) AS INTEGER) % 500 = 0
            ORDER BY amount DESC LIMIT 8
        """, (company_id,)).fetchall()
        for e in rounds:
            fp = _fp("round_number", e["id"])
            results.append({
                "rule": "round_number", "severity": "low",
                "title": f"Round-number transaction — {e['entry_number']}",
                "description": (f"{e['entry_number']} is an exact round number "
                                f"({e['amount']:.2f}). Benford's Law flags round amounts "
                                f"in large volumes as a pattern worth verifying."),
                "amount": e["amount"], "date": e["date"],
                "entity_type": "journal_entry", "entity_id": e["id"],
                "fingerprint": fp,
            })
    except Exception:
        pass

    # ── RULE 6: Weekend/holiday postings ──────────────────────────────────────
    try:
        from datetime import datetime as _dt
        weekend = db.execute("""
            SELECT je.id, je.entry_number, je.date,
                   u.full_name as posted_by,
                   ROUND(SUM(jl.debit),2) as amount
            FROM journal_entries je
            JOIN journal_lines jl ON jl.entry_id = je.id
            LEFT JOIN users u ON je.created_by = u.id
            WHERE je.company_id=? AND je.status='posted'
              AND je.date >= date('now','-30 days')
            GROUP BY je.id
        """, (company_id,)).fetchall()
        for e in weekend:
            try:
                wd = _dt.strptime(e["date"], "%Y-%m-%d").weekday()
                if wd >= 5:  # Saturday=5, Sunday=6
                    day_name = "Saturday" if wd == 5 else "Sunday"
                    fp = _fp("weekend_posting", e["id"])
                    results.append({
                        "rule": "weekend_posting", "severity": "medium",
                        "title": f"Weekend posting — {e['entry_number']}",
                        "description": (f"{e['entry_number']} ({e['amount']:.2f}) was posted on a {day_name} "
                                        f"by {e['posted_by'] or 'unknown'}. "
                                        f"Weekend postings lack normal oversight controls."),
                        "amount": e["amount"], "date": e["date"],
                        "entity_type": "journal_entry", "entity_id": e["id"],
                        "fingerprint": fp,
                    })
            except Exception:
                pass
    except Exception:
        pass

    # ── RULE 7: High-velocity posting by single user (5+ entries in 60 min) ───
    try:
        velocity_raw = db.execute("""
            SELECT created_by, full_name,
                   strftime('%Y-%m-%d %H',created_at) as hour_bucket,
                   COUNT(*) as cnt
            FROM (
                SELECT je.created_by, u.full_name, je.created_at
                FROM journal_entries je
                LEFT JOIN users u ON je.created_by=u.id
                WHERE je.company_id=? AND je.date >= date('now','-7 days')
            )
            GROUP BY created_by, hour_bucket
            HAVING cnt >= 5
        """, (company_id,)).fetchall()
        seen_users = set()
        for v in velocity_raw:
            if v["created_by"] and v["created_by"] not in seen_users:
                seen_users.add(v["created_by"])
                fp = _fp("velocity", v["created_by"], v["hour_bucket"])
                results.append({
                    "rule": "velocity", "severity": "medium",
                    "title": f"High-velocity posting — {v['full_name'] or 'User '+str(v['created_by'])}",
                    "description": (f"{v['full_name'] or 'A user'} posted {v['cnt']} journal entries "
                                    f"in one hour ({v['hour_bucket']}:00). "
                                    f"Unusually high activity — verify no batch errors."),
                    "amount": None, "date": v["hour_bucket"][:10] if v["hour_bucket"] else today_s,
                    "entity_type": "user", "entity_id": v["created_by"],
                    "fingerprint": fp,
                })
    except Exception:
        pass

    # ── RULE 8: Stale invoices (open > 90 days) ───────────────────────────────
    try:
        stale = db.execute("""
            SELECT i.id, i.invoice_number, i.date, i.due_date, i.balance_due,
                   c.name as customer,
                   CAST(julianday('now') - julianday(i.date) AS INTEGER) as age_days
            FROM invoices i JOIN customers c ON i.customer_id=c.id
            WHERE i.company_id=? AND i.status NOT IN ('paid','void','draft')
              AND i.balance_due > 0
              AND julianday('now') - julianday(i.date) > 90
            ORDER BY age_days DESC LIMIT 5
        """, (company_id,)).fetchall()
        for inv in stale:
            fp = _fp("stale_invoice", inv["id"])
            results.append({
                "rule": "stale_invoice", "severity": "low",
                "title": f"Stale invoice — {inv['invoice_number']}",
                "description": (f"{inv['invoice_number']} for {inv['customer']} "
                                f"({inv['balance_due']:.2f} outstanding) "
                                f"has been open for {inv['age_days']} days. "
                                f"Consider follow-up or bad-debt provision."),
                "amount": inv["balance_due"], "date": inv["date"],
                "entity_type": "invoice", "entity_id": inv["id"],
                "fingerprint": fp,
            })
    except Exception:
        pass

    # ── RULE 9: Duplicate invoices (same customer + amount + date) ───────────
    try:
        dup_invs = db.execute("""
            SELECT i.date, i.customer_id, c.name AS customer,
                   ROUND(i.total, 2) AS amount,
                   COUNT(*) AS cnt,
                   GROUP_CONCAT(i.invoice_number ORDER BY i.id) AS invoice_numbers,
                   MIN(i.id) AS first_id
            FROM invoices i
            JOIN customers c ON i.customer_id = c.id
            WHERE i.company_id = ? AND i.status NOT IN ('void','draft')
              AND i.date >= date('now','-90 days')
            GROUP BY i.date, i.customer_id, ROUND(i.total, 2)
            HAVING cnt > 1 AND amount > 0
            ORDER BY i.date DESC
        """, (company_id,)).fetchall()
        for d in dup_invs:
            fp = _fp("dup_invoice", d["date"], d["customer_id"], d["amount"])
            results.append({
                "rule": "duplicate_invoice", "severity": "high",
                "title": f"Duplicate invoice — {d['customer']}",
                "description": (f"{d['cnt']} invoices for {d['customer']} each totalling "
                                f"{d['amount']:.2f} on {d['date']}. "
                                f"Numbers: {d['invoice_numbers']}. "
                                f"Possible double-billing — void the duplicate before it is paid."),
                "amount": d["amount"], "date": d["date"],
                "entity_type": "invoice", "entity_id": d["first_id"],
                "fingerprint": fp,
            })
    except Exception:
        pass

    # ── RULE 10: Overdue ZIMRA / VAT filing (open period > 30 days past end) ─
    try:
        overdue_periods = db.execute("""
            SELECT id, name, start_date, end_date, net_payable
            FROM tax_periods
            WHERE company_id = ? AND status = 'open'
              AND end_date < date('now', '-30 days')
            ORDER BY end_date
        """, (company_id,)).fetchall()
        for tp in overdue_periods:
            days_late = (today - datetime.strptime(tp["end_date"], "%Y-%m-%d")).days
            fp = _fp("vat_overdue", tp["id"])
            results.append({
                "rule": "vat_overdue", "severity": "high",
                "title": f"Overdue VAT return — {tp['name']}",
                "description": (f"Tax period '{tp['name']}' ended {tp['end_date']} "
                                f"and is {days_late} days overdue with ZIMRA. "
                                f"Estimated net payable: {tp['net_payable']:.2f}. "
                                f"Late filing attracts a penalty — file immediately."),
                "amount": tp["net_payable"], "date": tp["end_date"],
                "entity_type": "tax_period", "entity_id": tp["id"],
                "fingerprint": fp,
            })
    except Exception:
        pass

    # ── RULE 11: Taxable invoices with no VAT period covering the month ───────
    try:
        taxable_months = db.execute("""
            SELECT strftime('%Y-%m', date) AS ym,
                   COALESCE(SUM(tax_amount), 0) AS vat_total,
                   COUNT(*) AS inv_cnt
            FROM invoices
            WHERE company_id = ? AND status NOT IN ('void','draft')
              AND tax_amount > 0
              AND date >= date('now', '-90 days')
              AND date < date('now', '-1 day')
            GROUP BY ym
        """, (company_id,)).fetchall()
        for tm in taxable_months:
            ym = tm["ym"]
            if not ym:
                continue
            covered = db.execute("""
                SELECT id FROM tax_periods
                WHERE company_id = ?
                  AND start_date <= ? AND end_date >= ?
            """, (company_id, f"{ym}-28", f"{ym}-01")).fetchone()
            if not covered:
                month_str = datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
                fp = _fp("vat_uncovered", company_id, ym)
                results.append({
                    "rule": "vat_uncovered", "severity": "medium",
                    "title": f"No ZIMRA return for {month_str}",
                    "description": (f"{tm['inv_cnt']} taxable invoice{'s' if tm['inv_cnt']>1 else ''} "
                                    f"in {month_str} collected {tm['vat_total']:.2f} VAT, "
                                    f"but no tax period has been created for this month. "
                                    f"Create and file a ZIMRA VAT return to clear the liability."),
                    "amount": tm["vat_total"], "date": f"{ym}-01",
                    "entity_type": "tax_period", "entity_id": None,
                    "fingerprint": fp,
                })
    except Exception:
        pass

    # Sort: high → medium → low, then by date desc
    order = {"high": 0, "medium": 1, "low": 2}
    results.sort(key=lambda x: (order.get(x["severity"], 9), x.get("date") or ""))
    return results


# ── GET /api/anomalies ────────────────────────────────────────────────────────
@app.get("/api/anomalies")
async def list_anomalies(cu: dict = Depends(current_user)):
    db = get_db()
    try:
        all_anomalies = _run_anomaly_scan(db, cu["company_id"])

        # Filter out dismissed
        dismissed_fps = {
            r["fingerprint"] for r in db.execute(
                "SELECT fingerprint FROM anomaly_dismissals WHERE company_id=?",
                (cu["company_id"],)
            ).fetchall()
        }

        active = [a for a in all_anomalies if a["fingerprint"] not in dismissed_fps]

        counts = {"high": 0, "medium": 0, "low": 0, "total": len(active)}
        for a in active:
            counts[a["severity"]] = counts.get(a["severity"], 0) + 1

        return {"anomalies": active, "counts": counts, "scanned_at": datetime.utcnow().isoformat()}
    finally:
        db.close()


# ── POST /api/anomalies/dismiss ───────────────────────────────────────────────
class DismissAnomaly(BaseModel):
    fingerprint: str
    rule_type:   str
    notes:       Optional[str] = None

@app.post("/api/anomalies/dismiss")
async def dismiss_anomaly(req: DismissAnomaly, cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant"):
        raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        db.execute(
            "INSERT OR REPLACE INTO anomaly_dismissals"
            "(company_id, rule_type, fingerprint, dismissed_by, notes) VALUES(?,?,?,?,?)",
            (cu["company_id"], req.rule_type, req.fingerprint, cu["id"], req.notes)
        )
        db.commit()
        return {"message": "Anomaly dismissed"}
    finally:
        db.close()


# ── POST /api/anomalies/restore ───────────────────────────────────────────────
@app.post("/api/anomalies/restore")
async def restore_anomaly(body: dict, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        db.execute(
            "DELETE FROM anomaly_dismissals WHERE company_id=? AND fingerprint=?",
            (cu["company_id"], body.get("fingerprint"))
        )
        db.commit()
        return {"message": "Restored"}
    finally:
        db.close()


# ── GET /api/audit-log ────────────────────────────────────────────────────────
@app.get("/api/audit-log")
async def list_audit_log(
    action:    Optional[str] = None,
    entity:    Optional[str] = None,
    user_id:   Optional[int] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    search:    Optional[str] = None,
    page:      int = Query(1, ge=1),
    per_page:  int = Query(50, ge=1, le=200),
    cu: dict = Depends(current_user)
):
    if cu["role"] not in ("admin", "accountant", "auditor"):
        raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        conds  = ["u.company_id=?"]
        params: list = [cu["company_id"]]
        if action:    conds.append("al.action LIKE ?");   params.append(f"%{action}%")
        if entity:    conds.append("al.entity=?");        params.append(entity)
        if user_id:   conds.append("al.user_id=?");       params.append(user_id)
        if date_from: conds.append("al.timestamp>=?");    params.append(date_from)
        if date_to:   conds.append("al.timestamp<=?");    params.append(date_to + " 23:59:59")
        if search:
            conds.append("(al.action LIKE ? OR al.entity LIKE ? OR al.details LIKE ?)")
            params += [f"%{search}%"] * 3
        where = " AND ".join(conds)
        total = db.execute(
            f"SELECT COUNT(*) as c FROM audit_log al LEFT JOIN users u ON al.user_id=u.id WHERE {where}",
            params
        ).fetchone()["c"]
        offset = (page - 1) * per_page
        rows = db.execute(f"""
            SELECT al.*, u.full_name as user_name, u.email as user_email, u.role as user_role
            FROM audit_log al
            LEFT JOIN users u ON al.user_id = u.id
            WHERE {where}
            ORDER BY al.timestamp DESC
            LIMIT ? OFFSET ?
        """, [*params, per_page, offset]).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "total": total, "page": page, "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page)
        }
    finally:
        db.close()


# ── GET /api/audit-log/summary ────────────────────────────────────────────────
@app.get("/api/audit-log/summary")
async def audit_summary(cu: dict = Depends(current_user)):
    if cu["role"] not in ("admin", "accountant", "auditor"):
        raise HTTPException(403, "Insufficient permissions")
    db = get_db()
    try:
        cid = cu["company_id"]
        top_actions = db.execute("""
            SELECT al.action, COUNT(*) as cnt
            FROM audit_log al LEFT JOIN users u ON al.user_id=u.id
            WHERE u.company_id=?
            GROUP BY al.action ORDER BY cnt DESC LIMIT 10
        """, (cid,)).fetchall()
        top_users = db.execute("""
            SELECT u.full_name, u.email, COUNT(*) as cnt,
                   MAX(al.timestamp) as last_action
            FROM audit_log al JOIN users u ON al.user_id=u.id
            WHERE u.company_id=?
            GROUP BY al.user_id ORDER BY cnt DESC LIMIT 5
        """, (cid,)).fetchall()
        today_count = db.execute("""
            SELECT COUNT(*) as c FROM audit_log al LEFT JOIN users u ON al.user_id=u.id
            WHERE u.company_id=? AND al.timestamp >= date('now')
        """, (cid,)).fetchone()["c"]
        week_count = db.execute("""
            SELECT COUNT(*) as c FROM audit_log al LEFT JOIN users u ON al.user_id=u.id
            WHERE u.company_id=? AND al.timestamp >= date('now','-7 days')
        """, (cid,)).fetchone()["c"]
        return {
            "today_count": today_count, "week_count": week_count,
            "top_actions": [dict(r) for r in top_actions],
            "top_users":   [dict(r) for r in top_users],
        }
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# STATIC FILES & FRONTEND
# ═══════════════════════════════════════════════════════════════════════════════

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/manifest.json")
async def manifest():
    p = os.path.join(STATIC_DIR, "manifest.json")
    return FileResponse(p, media_type="application/json") if os.path.exists(p) else {}

@app.get("/sw.js")
async def sw():
    p = os.path.join(STATIC_DIR, "sw.js")
    return FileResponse(p, media_type="application/javascript") if os.path.exists(p) else {}

_PAGE_MAP = {
    "":                  "index.html",
    "dashboard":         "dashboard.html",
    "accounts":          "accounts.html",
    "journals":          "journals.html",
    "ledger":            "journals.html",
    "reports":           "reports.html",
    "trial-balance":     "reports.html",
    "income-statement":  "reports.html",
    "balance-sheet":     "reports.html",
    "cash-flow":         "reports.html",
    "ar-aging":          "reports.html",
    "ap-aging":          "reports.html",
    "budgets":           "budgets.html",
    "budget-report":     "budgets.html",
    "rolling-forecast":  "budgets.html",
    "scenario-planning": "budgets.html",
    "customers":         "customers.html",
    "invoices":          "customers.html",
    "receipts":          "customers.html",
    "suppliers":         "suppliers.html",
    "bills":             "suppliers.html",
    "bill-payments":     "suppliers.html",
    "banking":           "banking.html",
    "bank-accounts":     "banking.html",
    "reconciliation":    "reconciliation.html",
    "inventory":         "inventory.html",
    "stock":             "inventory.html",
    "stock-valuation":   "inventory.html",
    "low-stock":         "inventory.html",
    "payroll":           "payroll.html",
    "payroll-runs":      "payroll.html",
    "employees":         "payroll.html",
    "assets":            "assets.html",
    "fixed-assets":      "assets.html",
    "asset-register":    "assets.html",
    "tax-periods":       "assets.html",
    "audit":             "audit.html",
    "settings":          "settings.html",
    "companies":         "settings.html",
    "api-keys":          "settings.html",
}

@app.get("/")
@app.get("/{path:path}")
async def serve_app(path: str = ""):
    html_file = _PAGE_MAP.get(path.strip("/"), "index.html")
    file_path = os.path.join(STATIC_DIR, html_file)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    # Fallback — serve index for any unrecognised path
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"status": "T-Tech Accountant API", "docs": "/docs"}

# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def migrate_db():
    """Non-destructive migrations for all phases."""
    db = get_db()
    col_migrations = [
        ("invoices",       "updated_at",       "TEXT DEFAULT (datetime('now'))"),
        ("bills",          "updated_at",       "TEXT DEFAULT (datetime('now'))"),
        ("customers",      "updated_at",       "TEXT DEFAULT (datetime('now'))"),
        ("suppliers",      "updated_at",       "TEXT DEFAULT (datetime('now'))"),
        ("accounts",       "updated_at",       "TEXT DEFAULT (datetime('now'))"),
        ("journal_entries","updated_at",       "TEXT DEFAULT (datetime('now'))"),
        ("companies",      "logo_url",         "TEXT"),
        ("companies",      "website",          "TEXT"),
        ("companies",      "industry",         "TEXT"),
        ("users",          "totp_enabled",         "INTEGER DEFAULT 0"),
    ]
    # Fix budgets UNIQUE constraint — must include 'scenario' for multi-scenario support.
    # The old check used 'scenario' not in sql which is wrong: scenario appears as a column
    # name even when it's missing from the UNIQUE constraint. We now parse the UNIQUE clause.
    try:
        # Clean up any leftover temp table from a previously failed migration
        db.execute("DROP TABLE IF EXISTS budgets_old")

        tbl = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='budgets'"
        ).fetchone()

        if tbl:
            tbl_sql = tbl["sql"]
            # Extract all UNIQUE(...) clauses and check if any include 'scenario'
            unique_clauses = re.findall(r'UNIQUE\s*\(([^)]+)\)', tbl_sql, re.IGNORECASE)
            scenario_in_unique = any('scenario' in clause for clause in unique_clauses)

            if not scenario_in_unique:
                # Rebuild table with scenario in the UNIQUE constraint
                db.execute("ALTER TABLE budgets RENAME TO budgets_old")
                db.execute("""
                    CREATE TABLE budgets (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id  INTEGER NOT NULL,
                        account_id  INTEGER NOT NULL,
                        year        INTEGER NOT NULL,
                        month       INTEGER NOT NULL,
                        scenario    TEXT    NOT NULL DEFAULT 'Base',
                        amount      REAL    DEFAULT 0,
                        notes       TEXT,
                        created_by  INTEGER,
                        created_at  TEXT    DEFAULT (datetime('now')),
                        UNIQUE(company_id, account_id, year, month, scenario),
                        FOREIGN KEY (company_id)  REFERENCES companies(id),
                        FOREIGN KEY (account_id)  REFERENCES accounts(id)
                    )""")
                # Check whether old table has a scenario column
                old_cols = [
                    r[1] for r in db.execute("PRAGMA table_info(budgets_old)").fetchall()
                ]
                if 'scenario' in old_cols:
                    db.execute("""
                        INSERT OR IGNORE INTO budgets
                            (company_id,account_id,year,month,scenario,amount,notes,created_by,created_at)
                        SELECT company_id,account_id,year,month,
                               COALESCE(scenario,'Base'),amount,notes,created_by,created_at
                        FROM budgets_old
                    """)
                else:
                    db.execute("""
                        INSERT OR IGNORE INTO budgets
                            (company_id,account_id,year,month,scenario,amount,notes,created_by,created_at)
                        SELECT company_id,account_id,year,month,
                               'Base',amount,notes,created_by,created_at
                        FROM budgets_old
                    """)
                db.execute("DROP TABLE budgets_old")
                db.commit()
    except Exception:
        # If rebuild failed, ensure temp table is cleaned up before continuing
        try:
            db.execute("DROP TABLE IF EXISTS budgets_old")
            db.commit()
        except Exception:
            pass
    col_migrations = [
        ("invoices",       "updated_at",              "TEXT DEFAULT (datetime('now'))"),
        ("bills",          "updated_at",              "TEXT DEFAULT (datetime('now'))"),
        ("customers",      "updated_at",              "TEXT DEFAULT (datetime('now'))"),
        ("suppliers",      "updated_at",              "TEXT DEFAULT (datetime('now'))"),
        ("accounts",       "updated_at",              "TEXT DEFAULT (datetime('now'))"),
        ("journal_entries","updated_at",              "TEXT DEFAULT (datetime('now'))"),
        ("companies",      "logo_url",                "TEXT"),
        ("companies",      "website",                 "TEXT"),
        ("companies",      "industry",                "TEXT"),
        ("companies",      "trading_name",            "TEXT"),
        ("companies",      "city",                    "TEXT"),
        ("companies",      "registration_number",     "TEXT"),
        ("companies",      "financial_year_start",    "TEXT"),
        ("companies",      "default_tax_rate",        "REAL DEFAULT 0"),
        ("products",       "description",             "TEXT"),
        ("bank_accounts",  "opening_balance",         "REAL DEFAULT 0"),
        ("bank_accounts",  "description",             "TEXT"),
        ("customers",      "city",                    "TEXT"),
        ("customers",      "country",                 "TEXT"),
        ("customers",      "currency",                "TEXT"),
        ("suppliers",      "city",                    "TEXT"),
        ("suppliers",      "country",                 "TEXT"),
        ("suppliers",      "currency",                "TEXT"),
        ("users",          "totp_enabled",            "INTEGER DEFAULT 0"),
        ("users",          "module_permissions",      "TEXT DEFAULT NULL"),
        ("bank_accounts",  "opening_balance",         "REAL DEFAULT 0"),
        ("bank_accounts",  "description",             "TEXT"),
    ]
    for table, col, defn in col_migrations:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        except Exception:
            pass
    # Populate user_companies from existing users.company_id relationships
    try:
        users = db.execute("SELECT id, company_id, role FROM users").fetchall()
        for u in users:
            try:
                db.execute(
                    "INSERT OR IGNORE INTO user_companies(user_id, company_id, role) VALUES(?,?,?)",
                    (u["id"], u["company_id"], u["role"])
                )
            except Exception:
                pass
    except Exception:
        pass
    db.commit()
    db.close()

# ── sync meta endpoint (used by frontend to know last-changed timestamps) ─────

@app.get("/api/sync/meta")
async def sync_meta(cu: dict = Depends(current_user)):
    db = get_db()
    cid = cu["company_id"]
    def count(table, extra=""):
        return db.execute(f"SELECT COUNT(*) as c FROM {table} WHERE company_id={cid}{extra}").fetchone()["c"]
    def latest(table):
        r = db.execute(f"SELECT MAX(created_at) as m FROM {table} WHERE company_id={cid}").fetchone()
        return r["m"] if r else None
    result = {
        "accounts":      {"count": count("accounts"), "latest": latest("accounts")},
        "customers":     {"count": count("customers"), "latest": latest("customers")},
        "suppliers":     {"count": count("suppliers"), "latest": latest("suppliers")},
        "invoices":      {"count": count("invoices"),  "latest": latest("invoices")},
        "bills":         {"count": count("bills"),     "latest": latest("bills")},
        "journals":      {"count": count("journal_entries"), "latest": latest("journal_entries")},
        "server_time":   datetime.utcnow().isoformat(),
    }
    db.close()
    return result

@app.on_event("startup")
async def startup():
    init_db()
    migrate_db()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    reload = os.environ.get("ENV", "development") != "production"
    uvicorn.run("ttech_api:app", host="0.0.0.0", port=port, reload=reload)
