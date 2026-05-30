from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timedelta
import sqlite3
import os
import secrets

try:
    from jose import JWTError, jwt
except ImportError:
    raise RuntimeError("Run: pip install python-jose[cryptography]")

try:
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except ImportError:
    raise RuntimeError("Run: pip install passlib[bcrypt]")

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
        access = make_token({"sub": str(u["id"]), "cid": u["company_id"], "role": u["role"]},
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
        "SELECT id,email,full_name,role,is_active,created_at FROM users WHERE company_id=?",
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
        db.execute(
            "INSERT INTO users(company_id,email,password_hash,full_name,role) VALUES(?,?,?,?,?)",
            (cu["company_id"], req.email, hash_pw(req.password), req.full_name, req.role)
        )
        db.commit()
        return {"message": "User created"}
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
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    cu: dict = Depends(current_user)
):
    db = get_db()
    try:
        conds = ["je.company_id=?", "je.status='posted'"]
        params = [cu["company_id"]]
        if account_id:
            conds.append("jl.account_id=?"); params.append(account_id)
        if date_from:
            conds.append("je.date>=?"); params.append(date_from)
        if date_to:
            conds.append("je.date<=?"); params.append(date_to)
        where = " AND ".join(conds)
        rows = db.execute(f"""
            SELECT je.date, je.entry_number, je.description as entry_desc,
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
        result = []
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
            result.append(r)
        return result
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
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    cu: dict = Depends(current_user)
):
    db = get_db()
    try:
        conds = ["je.company_id=?", "je.status='posted'", "a.type IN ('revenue','expense')"]
        params = [cu["company_id"]]
        if date_from:
            conds.append("je.date>=?"); params.append(date_from)
        if date_to:
            conds.append("je.date<=?"); params.append(date_to)
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
async def balance_sheet(date_to: Optional[str] = None, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        conds = ["je.company_id=?", "je.status='posted'", "a.type IN ('asset','liability','equity')"]
        params = [cu["company_id"]]
        if date_to:
            conds.append("je.date<=?"); params.append(date_to)
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
    tax_number: Optional[str] = None
    credit_limit: float = 0
    payment_terms: int = 30
    notes: Optional[str] = None

class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
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
               COALESCE(SUM(CASE WHEN i.status NOT IN ('paid','void') THEN i.balance_due ELSE 0 END),0) as outstanding
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
            (cu["company_id"], req.name, req.email, req.phone, req.address, req.tax_number, req.credit_limit, req.payment_terms, req.notes)
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
    tax_number: Optional[str] = None
    payment_terms: int = 30
    notes: Optional[str] = None

class SupplierUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    tax_number: Optional[str] = None
    payment_terms: Optional[int] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None

@app.get("/api/suppliers")
async def list_suppliers(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT s.*,
               COALESCE(SUM(CASE WHEN b.status NOT IN ('paid','void') THEN b.balance_due ELSE 0 END),0) as outstanding
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
            SELECT i.*, c.name as customer_name
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
            SELECT b.*, s.name as supplier_name
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
    gl_account_id: int
    currency: str = "USD"

@app.get("/api/bank-accounts")
async def list_bank_accounts(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT ba.*, a.code as gl_code, a.name as gl_name,
               COALESCE(SUM(CASE WHEN je.status='posted' THEN jl.debit-jl.credit ELSE 0 END),0) as balance
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
    if cu["role"] != "admin": raise HTTPException(403, "Admin only")
    db = get_db()
    try:
        if not db.execute("SELECT id FROM accounts WHERE id=? AND company_id=?",
                          (req.gl_account_id, cu["company_id"])).fetchone():
            raise HTTPException(400, "GL account not found")
        cur = db.execute(
            "INSERT INTO bank_accounts(company_id,name,bank_name,account_number,gl_account_id,currency) VALUES(?,?,?,?,?,?)",
            (cu["company_id"], req.name, req.bank_name, req.account_number, req.gl_account_id, req.currency)
        )
        db.commit()
        return dict(db.execute("SELECT * FROM bank_accounts WHERE id=?", (cur.lastrowid,)).fetchone())
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
    code: str
    name: str
    category: Optional[str] = None
    unit: str = "unit"
    barcode: Optional[str] = None
    cost_price: float = 0
    selling_price: float = 0
    reorder_level: float = 0
    inventory_account_id: Optional[int] = None
    cogs_account_id: Optional[int] = None
    revenue_account_id: Optional[int] = None

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None
    barcode: Optional[str] = None
    cost_price: Optional[float] = None
    selling_price: Optional[float] = None
    reorder_level: Optional[float] = None
    is_active: Optional[bool] = None

class StockMovementCreate(BaseModel):
    product_id: int
    movement_type: str
    quantity: float = Field(..., gt=0)
    unit_cost: float = 0
    reference: Optional[str] = None
    notes: Optional[str] = None
    post_gl: bool = True

@app.get("/api/products")
async def list_products(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT *, current_stock * cost_price as stock_value,
               CASE WHEN reorder_level > 0 AND current_stock <= reorder_level THEN 1 ELSE 0 END as low_stock
        FROM products WHERE company_id=? ORDER BY code
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/products", status_code=201)
async def create_product(req: ProductCreate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        if db.execute("SELECT id FROM products WHERE company_id=? AND code=?",
                      (cu["company_id"], req.code)).fetchone():
            raise HTTPException(400, f"Product code '{req.code}' already exists")
        cur = db.execute(
            "INSERT INTO products(company_id,code,name,category,unit,barcode,cost_price,selling_price,reorder_level,inventory_account_id,cogs_account_id,revenue_account_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cu["company_id"], req.code, req.name, req.category, req.unit, req.barcode,
             req.cost_price, req.selling_price, req.reorder_level,
             req.inventory_account_id, req.cogs_account_id, req.revenue_account_id)
        )
        db.commit()
        return dict(db.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone())
    finally: db.close()

@app.put("/api/products/{pid}")
async def update_product(pid: int, req: ProductUpdate, cu: dict = Depends(current_user)):
    db = get_db()
    try:
        if not db.execute("SELECT id FROM products WHERE id=? AND company_id=?",
                          (pid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Product not found")
        fields = {k: v for k, v in req.dict().items() if v is not None}
        if fields:
            db.execute(f"UPDATE products SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                       (*fields.values(), pid))
            db.commit()
        return dict(db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone())
    finally: db.close()

@app.get("/api/stock/movements")
async def list_stock_movements(product_id: Optional[int] = None, cu: dict = Depends(current_user)):
    db = get_db()
    conds = ["sm.company_id=?"]; params: list = [cu["company_id"]]
    if product_id: conds.append("sm.product_id=?"); params.append(product_id)
    rows = db.execute(f"""
        SELECT sm.*, p.name as product_name, p.code as product_code, p.unit
        FROM stock_movements sm JOIN products p ON sm.product_id=p.id
        WHERE {' AND '.join(conds)} ORDER BY sm.created_at DESC LIMIT 300
    """, params).fetchall()
    db.close()
    return [dict(r) for r in rows]

@app.post("/api/stock/movement", status_code=201)
async def create_stock_movement(req: StockMovementCreate, cu: dict = Depends(current_user)):
    if req.movement_type not in ("in","out","adjustment"):
        raise HTTPException(400, "movement_type must be 'in', 'out', or 'adjustment'")
    db = get_db()
    try:
        prod = db.execute("SELECT * FROM products WHERE id=? AND company_id=?",
                          (req.product_id, cu["company_id"])).fetchone()
        if not prod: raise HTTPException(400, "Product not found")
        prod = dict(prod)

        unit_cost = req.unit_cost or prod["cost_price"]
        total_cost = round(req.quantity * unit_cost, 2)

        if req.movement_type == "in":
            # Weighted average cost
            new_total_val = prod["current_stock"] * prod["cost_price"] + req.quantity * unit_cost
            new_stock = prod["current_stock"] + req.quantity
            new_cost  = round(new_total_val / new_stock, 4) if new_stock > 0 else unit_cost
        elif req.movement_type == "out":
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
            if inv_acc and req.movement_type == "in" and req.unit_cost > 0:
                ap = get_acc_by_code(db, cu["company_id"], "2020") or get_acc_by_subtype(db, cu["company_id"], "payable")
                if ap:
                    eid = auto_post_gl(db, cu["company_id"], cu["id"],
                                       datetime.now().strftime("%Y-%m-%d"),
                                       f"Stock In: {prod['name']} × {req.quantity}",
                                       [(inv_acc["id"], total_cost, 0, f"Stock in: {prod['code']}"),
                                        (ap["id"], 0, total_cost, f"Stock in: {prod['name']}")])
            elif inv_acc and req.movement_type == "out":
                cogs = (db.execute("SELECT * FROM accounts WHERE id=? AND company_id=?",
                                   (prod["cogs_account_id"], cu["company_id"])).fetchone()
                        if prod["cogs_account_id"] else None) or \
                       get_acc_by_code(db, cu["company_id"], "5010") or \
                       get_acc_by_subtype(db, cu["company_id"], "cost_of_sales")
                if cogs:
                    eid = auto_post_gl(db, cu["company_id"], cu["id"],
                                       datetime.now().strftime("%Y-%m-%d"),
                                       f"COGS: {prod['name']} × {req.quantity}",
                                       [(cogs["id"], total_cost, 0, f"COGS: {prod['code']}"),
                                        (inv_acc["id"], 0, total_cost, f"COGS: {prod['name']}")])

        db.execute("UPDATE products SET current_stock=?, cost_price=? WHERE id=?",
                   (round(new_stock, 4), round(new_cost, 4), req.product_id))
        cur = db.execute(
            "INSERT INTO stock_movements(company_id,product_id,movement_type,quantity,unit_cost,total_cost,reference,notes,journal_entry_id,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (cu["company_id"], req.product_id, req.movement_type, req.quantity,
             unit_cost, total_cost, req.reference, req.notes, eid, cu["id"])
        )
        db.commit()
        return {"id": cur.lastrowid, "new_stock": round(new_stock, 4), "message": "Stock movement recorded"}
    except HTTPException: db.rollback(); raise
    except Exception as e: db.rollback(); raise HTTPException(500, str(e))
    finally: db.close()

@app.get("/api/reports/stock-valuation")
async def stock_valuation(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT *, current_stock * cost_price as stock_value
        FROM products WHERE company_id=? AND is_active=1
        ORDER BY category, name
    """, (cu["company_id"],)).fetchall()
    total = sum(r["stock_value"] for r in rows)
    db.close()
    return {"products": [dict(r) for r in rows], "total_value": round(total, 2)}

@app.get("/api/reports/low-stock")
async def low_stock_report(cu: dict = Depends(current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT * FROM products
        WHERE company_id=? AND is_active=1 AND reorder_level > 0 AND current_stock <= reorder_level
        ORDER BY (current_stock - reorder_level) ASC
    """, (cu["company_id"],)).fetchall()
    db.close()
    return [dict(r) for r in rows]

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
        if not db.execute("SELECT id FROM tax_periods WHERE id=? AND company_id=?",
                          (tid, cu["company_id"])).fetchone():
            raise HTTPException(404, "Tax period not found")
        db.execute("UPDATE tax_periods SET status='filed' WHERE id=?", (tid,))
        db.commit()
        return {"message": "Tax period marked as filed"}
    finally: db.close()

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

@app.get("/")
@app.get("/{path:path}")
async def serve_app(path: str = ""):
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"status": "T-Tech Accountant API running", "docs": "/docs"}

# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def migrate_db():
    """Add Phase 3 columns to existing tables without breaking current data."""
    db = get_db()
    migrations = [
        ("invoices",  "updated_at", "TEXT DEFAULT (datetime('now'))"),
        ("bills",     "updated_at", "TEXT DEFAULT (datetime('now'))"),
        ("customers", "updated_at", "TEXT DEFAULT (datetime('now'))"),
        ("suppliers", "updated_at", "TEXT DEFAULT (datetime('now'))"),
        ("accounts",  "updated_at", "TEXT DEFAULT (datetime('now'))"),
        ("journal_entries", "updated_at", "TEXT DEFAULT (datetime('now'))"),
    ]
    for table, col, defn in migrations:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
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
    uvicorn.run("ttech_api:app", host="0.0.0.0", port=8001, reload=True)
