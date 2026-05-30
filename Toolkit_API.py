from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime, timedelta
import sqlite3
import os
import smtplib
import statistics
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from math import ceil
from collections import defaultdict

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Database", "Nexus.db"))

app = FastAPI(title="NeXus Toolkit API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ============ PYDANTIC MODELS ============

class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1)
    category: Optional[str] = None
    unit: Optional[str] = None
    current_stock: float = 0
    reorder_level: float = 0
    cost_price: float = 0
    selling_price: float = 0
    barcode: str = Field(..., min_length=1)


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None
    current_stock: Optional[float] = None
    reorder_level: Optional[float] = None
    cost_price: Optional[float] = None
    selling_price: Optional[float] = None
    barcode: Optional[str] = None


class LoanCreate(BaseModel):
    lender_name: str = Field(..., min_length=1)
    loan_type: str = Field(..., pattern="^(business|personal|microfinance|p2p)$")
    principal: float = Field(..., gt=0)
    interest_rate: float = Field(..., ge=0)
    term_months: int = Field(..., gt=0)
    start_date: str
    notes: Optional[str] = None


class LoanUpdate(BaseModel):
    interest_rate: Optional[float] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class PaymentCreate(BaseModel):
    amount: float = Field(..., gt=0)
    payment_date: str
    reference: Optional[str] = None


class SaleCreate(BaseModel):
    product_name: str = Field(..., min_length=1)
    quantity: float = Field(..., gt=0)
    unit_price: float = Field(..., gt=0)
    unit_cost: float = Field(..., ge=0)
    date: str


class ExpenseCreate(BaseModel):
    category: str = Field(..., pattern="^(Rent|Utilities|Salaries|Transport|Marketing|Inventory|Other)$")
    description: Optional[str] = None
    amount: float = Field(..., gt=0)
    date: str


class CapitalCreate(BaseModel):
    amount: float = Field(..., gt=0)
    date: str
    notes: Optional[str] = None


class BookkeepingEntry(BaseModel):
    description: Optional[str] = None
    amount: float = Field(..., gt=0)
    date: str
    category: Optional[str] = None
    supplier: Optional[str] = None


class LoanCalculatorRequest(BaseModel):
    principal: float = Field(..., gt=0)
    rate: float = Field(..., ge=0)
    term_months: int = Field(..., gt=0)


class NotificationSettingsUpdate(BaseModel):
    notification_email: Optional[str] = None
    inventory_alerts: Optional[bool] = None
    loan_alerts: Optional[bool] = None
    loan_days_before: Optional[int] = None


class SmtpConfigModel(BaseModel):
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""


# ============ EMAIL ============

def send_email(to_email: str, subject: str, html_body: str) -> bool:
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")

    if not smtp_user or not smtp_pass:
        print(f"[Email] SMTP not configured. Would send to {to_email}: {subject}")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"[Email] Failed: {e}")
        return False


def run_daily_notifications():
    conn = get_db()
    try:
        users = conn.execute("""
            SELECT u.ID, u.email, u.business_name,
                   COALESCE(ns.notification_email, u.email) as notify_email,
                   COALESCE(ns.inventory_alerts, 1) as inventory_alerts,
                   COALESCE(ns.loan_alerts, 1) as loan_alerts,
                   COALESCE(ns.loan_days_before, 3) as loan_days_before
            FROM Users u
            LEFT JOIN Notification_Settings ns ON u.ID = ns.user_id
        """).fetchall()

        for user in users:
            if user["inventory_alerts"]:
                low_stock = conn.execute(
                    "SELECT name, current_stock, reorder_level FROM Inventory WHERE user_id=? AND current_stock<=reorder_level",
                    (user["ID"],)
                ).fetchall()
                if low_stock:
                    items_html = "".join(
                        f"<li><b>{i['name']}</b>: {i['current_stock']} units (reorder at {i['reorder_level']})</li>"
                        for i in low_stock
                    )
                    html = f"""<h2>Low Stock Alert — {user['business_name']}</h2>
                    <p>The following items are at or below their reorder level:</p>
                    <ul>{items_html}</ul>
                    <p>Log in to NeXus Toolkit to restock.</p>"""
                    send_email(user["notify_email"], f"[NeXus] Low Stock Alert", html)

            if user["loan_alerts"]:
                days_before = user["loan_days_before"]
                alert_date = (datetime.now() + timedelta(days=days_before)).strftime("%Y-%m-%d")
                today = datetime.now().strftime("%Y-%m-%d")
                upcoming = conn.execute("""
                    SELECT lender_name, principal, amount_paid, next_due_date
                    FROM Loans WHERE user_id=? AND status='active' AND next_due_date BETWEEN ? AND ?
                """, (user["ID"], today, alert_date)).fetchall()
                if upcoming:
                    loans_html = "".join(
                        f"<li><b>{l['lender_name']}</b>: Due {l['next_due_date']}, Balance ${l['principal']-l['amount_paid']:.2f}</li>"
                        for l in upcoming
                    )
                    html = f"""<h2>Loan Payment Reminder — {user['business_name']}</h2>
                    <p>Payments due in the next {days_before} days:</p>
                    <ul>{loans_html}</ul>"""
                    send_email(user["notify_email"], f"[NeXus] Loan Payment Due Soon", html)
    finally:
        conn.close()


if SCHEDULER_AVAILABLE:
    _scheduler = AsyncIOScheduler()


def _run_db_migrations():
    conn = get_db()
    try:
        try:
            conn.execute("ALTER TABLE Inventory ADD COLUMN barcode TEXT")
            conn.commit()
        except Exception:
            pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS SMTP_Config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                smtp_host TEXT DEFAULT 'smtp.gmail.com',
                smtp_port INTEGER DEFAULT 587,
                smtp_user TEXT,
                smtp_pass TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _load_smtp_from_db():
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM SMTP_Config LIMIT 1").fetchone()
        if row and row["smtp_user"]:
            os.environ["SMTP_HOST"] = row["smtp_host"] or "smtp.gmail.com"
            os.environ["SMTP_PORT"] = str(row["smtp_port"] or 587)
            os.environ["SMTP_USER"] = row["smtp_user"]
            os.environ["SMTP_PASS"] = row["smtp_pass"] or ""
    except Exception:
        pass
    finally:
        conn.close()


@app.on_event("startup")
async def startup_event():
    _run_db_migrations()
    _load_smtp_from_db()
    if SCHEDULER_AVAILABLE:
        _scheduler.add_job(run_daily_notifications, "cron", hour=8, minute=0)
        _scheduler.start()


@app.on_event("shutdown")
async def shutdown_event():
    if SCHEDULER_AVAILABLE:
        _scheduler.shutdown()


# ============ INVENTORY MANAGEMENT ============

@app.get("/api/inventory/summary")
async def inventory_summary(user_id: int = Query(...)):
    conn = get_db()
    try:
        products = conn.execute(
            "SELECT current_stock, cost_price, selling_price FROM Inventory WHERE user_id=?", (user_id,)
        ).fetchall()
        item_count = len(products)
        stock_value = sum(p["current_stock"] * p["cost_price"] for p in products)
        potential_value = sum(p["current_stock"] * p["selling_price"] for p in products)
        return {"itemCount": item_count, "stockValue": round(stock_value, 2), "potentialValue": round(potential_value, 2)}
    finally:
        conn.close()


@app.get("/api/inventory/products")
async def list_products(user_id: int = Query(...)):
    conn = get_db()
    try:
        products = conn.execute(
            "SELECT * FROM Inventory WHERE user_id=? ORDER BY name", (user_id,)
        ).fetchall()
        return [dict(p) for p in products]
    finally:
        conn.close()


@app.post("/api/inventory/products")
async def create_product(product: ProductCreate, user_id: int = Query(...)):
    conn = get_db()
    try:
        cursor = conn.execute(
            """INSERT INTO Inventory (user_id,name,category,unit,current_stock,reorder_level,cost_price,selling_price,barcode)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (user_id, product.name, product.category, product.unit,
             product.current_stock, product.reorder_level, product.cost_price, product.selling_price, product.barcode)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM Inventory WHERE id=?", (cursor.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.put("/api/inventory/products/{product_id}")
async def update_product(product_id: int, updates: ProductUpdate):
    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM Inventory WHERE id=?", (product_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Product not found")
        fields = {k: v for k, v in updates.dict(exclude_unset=True).items()}
        if fields:
            clause = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE Inventory SET {clause}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         list(fields.values()) + [product_id])
            conn.commit()
        return dict(conn.execute("SELECT * FROM Inventory WHERE id=?", (product_id,)).fetchone())
    finally:
        conn.close()


@app.delete("/api/inventory/products/{product_id}")
async def delete_product(product_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM Inventory WHERE id=?", (product_id,))
        conn.commit()
        return {"message": "Product deleted"}
    finally:
        conn.close()


@app.get("/api/inventory/alerts")
async def inventory_alerts(user_id: int = Query(...)):
    conn = get_db()
    try:
        alerts = conn.execute(
            "SELECT id,name,current_stock,reorder_level FROM Inventory WHERE user_id=? AND current_stock<=reorder_level",
            (user_id,)
        ).fetchall()
        return [dict(a) for a in alerts]
    finally:
        conn.close()


@app.get("/api/inventory/lookup")
async def lookup_inventory(user_id: int = Query(...), q: str = Query(...)):
    """Look up a product by barcode value or name — used by the barcode scanner."""
    conn = get_db()
    try:
        # Exact barcode match first
        product = conn.execute(
            "SELECT * FROM Inventory WHERE user_id=? AND barcode=?", (user_id, q)
        ).fetchone()
        if not product:
            # Partial name match fallback
            product = conn.execute(
                "SELECT * FROM Inventory WHERE user_id=? AND LOWER(name) LIKE LOWER(?)",
                (user_id, f"%{q}%")
            ).fetchone()
        if not product:
            return {"found": False, "product": None}
        return {"found": True, "product": dict(product)}
    finally:
        conn.close()


# ============ LOAN TRACKER ============

@app.get("/api/loans/summary")
async def loans_summary(user_id: int = Query(...)):
    conn = get_db()
    try:
        loans = conn.execute(
            "SELECT principal,amount_paid,status FROM Loans WHERE user_id=?", (user_id,)
        ).fetchall()
        outstanding = sum(l["principal"] - l["amount_paid"] for l in loans if l["status"] == "active")
        paid = sum(l["amount_paid"] for l in loans)
        return {"outstanding": round(outstanding, 2), "paid": round(paid, 2)}
    finally:
        conn.close()


@app.get("/api/loans")
async def list_loans(user_id: int = Query(...)):
    conn = get_db()
    try:
        loans = conn.execute(
            "SELECT * FROM Loans WHERE user_id=? ORDER BY start_date DESC", (user_id,)
        ).fetchall()
        return [dict(l) for l in loans]
    finally:
        conn.close()


@app.get("/api/loans/{loan_id}")
async def get_loan(loan_id: int):
    conn = get_db()
    try:
        loan = conn.execute("SELECT * FROM Loans WHERE id=?", (loan_id,)).fetchone()
        if not loan:
            raise HTTPException(status_code=404, detail="Loan not found")
        monthly_rate = loan["interest_rate"] / 100 / 12
        n = loan["term_months"]
        if monthly_rate > 0:
            monthly_payment = loan["principal"] * (monthly_rate * (1 + monthly_rate) ** n) / ((1 + monthly_rate) ** n - 1)
        else:
            monthly_payment = loan["principal"] / n
        schedule = []
        remaining = loan["principal"]
        for month in range(1, n + 1):
            interest = remaining * monthly_rate
            principal_payment = monthly_payment - interest
            remaining -= principal_payment
            schedule.append({
                "month": month,
                "payment": round(monthly_payment, 2),
                "interest": round(interest, 2),
                "principal": round(principal_payment, 2),
                "remainingBalance": round(max(0, remaining), 2)
            })
        payments = conn.execute(
            "SELECT * FROM Loan_Payments WHERE loan_id=? ORDER BY payment_date", (loan_id,)
        ).fetchall()
        result = dict(loan)
        result["paymentSchedule"] = schedule
        result["payments"] = [dict(p) for p in payments]
        return result
    finally:
        conn.close()


@app.post("/api/loans")
async def create_loan(loan: LoanCreate, user_id: int = Query(...)):
    conn = get_db()
    try:
        next_due = (datetime.strptime(loan.start_date, "%Y-%m-%d") + timedelta(days=30)).strftime("%Y-%m-%d")
        cursor = conn.execute(
            """INSERT INTO Loans (user_id,lender_name,loan_type,principal,interest_rate,
               term_months,start_date,notes,next_due_date) VALUES (?,?,?,?,?,?,?,?,?)""",
            (user_id, loan.lender_name, loan.loan_type, loan.principal,
             loan.interest_rate, loan.term_months, loan.start_date, loan.notes, next_due)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM Loans WHERE id=?", (cursor.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.post("/api/loans/{loan_id}/payments")
async def record_payment(loan_id: int, payment: PaymentCreate):
    conn = get_db()
    try:
        loan = conn.execute("SELECT * FROM Loans WHERE id=?", (loan_id,)).fetchone()
        if not loan:
            raise HTTPException(status_code=404, detail="Loan not found")
        conn.execute(
            "INSERT INTO Loan_Payments (loan_id,amount,payment_date,reference) VALUES (?,?,?,?)",
            (loan_id, payment.amount, payment.payment_date, payment.reference)
        )
        new_paid = loan["amount_paid"] + payment.amount
        status = "completed" if new_paid >= loan["principal"] else loan["status"]
        next_due = (datetime.strptime(payment.payment_date, "%Y-%m-%d") + timedelta(days=30)).strftime("%Y-%m-%d")
        conn.execute(
            "UPDATE Loans SET amount_paid=?, status=?, next_due_date=? WHERE id=?",
            (new_paid, status, next_due, loan_id)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM Loans WHERE id=?", (loan_id,)).fetchone())
    finally:
        conn.close()


@app.get("/api/loans/overdue")
async def overdue_loans(user_id: int = Query(...)):
    conn = get_db()
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        loans = conn.execute(
            "SELECT * FROM Loans WHERE user_id=? AND status='active' AND next_due_date<?", (user_id, today)
        ).fetchall()
        result = []
        for loan in loans:
            days = (datetime.now() - datetime.strptime(loan["next_due_date"], "%Y-%m-%d")).days
            result.append({
                "id": loan["id"], "lenderName": loan["lender_name"],
                "overdueAmount": round(loan["principal"] - loan["amount_paid"], 2),
                "daysOverdue": days
            })
        return result
    finally:
        conn.close()


@app.put("/api/loans/{loan_id}")
async def update_loan(loan_id: int, updates: LoanUpdate):
    conn = get_db()
    try:
        if not conn.execute("SELECT id FROM Loans WHERE id=?", (loan_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Loan not found")
        fields = {k: v for k, v in updates.dict(exclude_unset=True).items()}
        if fields:
            clause = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE Loans SET {clause} WHERE id=?", list(fields.values()) + [loan_id])
            conn.commit()
        return dict(conn.execute("SELECT * FROM Loans WHERE id=?", (loan_id,)).fetchone())
    finally:
        conn.close()


@app.delete("/api/loans/{loan_id}")
async def delete_loan(loan_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM Loans WHERE id=?", (loan_id,))
        conn.commit()
        return {"message": "Loan deleted"}
    finally:
        conn.close()


# ============ FINANCE TRACK ============

@app.get("/api/finance/dashboard")
async def finance_dashboard(user_id: int = Query(...)):
    conn = get_db()
    try:
        sales = conn.execute(
            "SELECT quantity,unit_price,unit_cost FROM Sales WHERE user_id=?", (user_id,)
        ).fetchall()
        expenses = conn.execute("SELECT amount FROM Expenses WHERE user_id=?", (user_id,)).fetchall()
        total_revenue = sum(s["quantity"] * s["unit_price"] for s in sales)
        total_cost = sum(s["quantity"] * s["unit_cost"] for s in sales)
        total_expenses = sum(e["amount"] for e in expenses)
        gross_profit = total_revenue - total_cost
        net_profit = gross_profit - total_expenses
        margin = (net_profit / total_revenue * 100) if total_revenue > 0 else 0
        return {
            "revenue": round(total_revenue, 2), "expenses": round(total_expenses, 2),
            "netProfit": round(net_profit, 2), "grossProfit": round(gross_profit, 2),
            "margin": round(margin, 1)
        }
    finally:
        conn.close()


@app.get("/api/finance/sales")
async def list_sales(user_id: int = Query(...)):
    conn = get_db()
    try:
        sales = conn.execute(
            """SELECT *, (quantity*unit_price) as total_revenue,
               (quantity*unit_cost) as total_cost,
               (quantity*(unit_price-unit_cost)) as profit
               FROM Sales WHERE user_id=? ORDER BY date DESC""", (user_id,)
        ).fetchall()
        return [dict(s) for s in sales]
    finally:
        conn.close()


@app.post("/api/finance/sales")
async def record_sale(sale: SaleCreate, user_id: int = Query(...)):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO Sales (user_id,product_name,quantity,unit_price,unit_cost,date) VALUES (?,?,?,?,?,?)",
            (user_id, sale.product_name, sale.quantity, sale.unit_price, sale.unit_cost, sale.date)
        )
        conn.commit()

        # Auto-deduct inventory stock — match by name first, then barcode
        inventory_deducted = False
        new_stock = None
        low_stock_alert = False
        inv = conn.execute(
            "SELECT id, current_stock, reorder_level, name FROM Inventory WHERE user_id=? AND LOWER(name)=LOWER(?)",
            (user_id, sale.product_name)
        ).fetchone()
        if not inv:
            inv = conn.execute(
                "SELECT id, current_stock, reorder_level, name FROM Inventory WHERE user_id=? AND barcode=?",
                (user_id, sale.product_name)
            ).fetchone()
        if inv:
            new_stock = round(inv["current_stock"] - sale.quantity, 4)
            conn.execute(
                "UPDATE Inventory SET current_stock=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_stock, inv["id"])
            )
            conn.commit()
            inventory_deducted = True
            low_stock_alert = new_stock <= inv["reorder_level"]

        result = dict(conn.execute("SELECT * FROM Sales WHERE id=?", (cursor.lastrowid,)).fetchone())
        result["inventory_deducted"] = inventory_deducted
        result["new_stock"] = new_stock
        result["low_stock_alert"] = low_stock_alert
        return result
    finally:
        conn.close()


@app.get("/api/finance/expenses")
async def list_expenses(user_id: int = Query(...)):
    conn = get_db()
    try:
        expenses = conn.execute(
            "SELECT * FROM Expenses WHERE user_id=? ORDER BY date DESC", (user_id,)
        ).fetchall()
        return [dict(e) for e in expenses]
    finally:
        conn.close()


@app.post("/api/finance/expenses")
async def record_expense(expense: ExpenseCreate, user_id: int = Query(...)):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO Expenses (user_id,category,description,amount,date) VALUES (?,?,?,?,?)",
            (user_id, expense.category, expense.description, expense.amount, expense.date)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM Expenses WHERE id=?", (cursor.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.get("/api/finance/capital")
async def list_capital(user_id: int = Query(...)):
    conn = get_db()
    try:
        capital = conn.execute(
            "SELECT * FROM Capital WHERE user_id=? ORDER BY date DESC", (user_id,)
        ).fetchall()
        return [dict(c) for c in capital]
    finally:
        conn.close()


@app.post("/api/finance/capital")
async def add_capital(capital: CapitalCreate, user_id: int = Query(...)):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO Capital (user_id,amount,date,notes) VALUES (?,?,?,?)",
            (user_id, capital.amount, capital.date, capital.notes)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM Capital WHERE id=?", (cursor.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.get("/api/finance/insights")
async def finance_insights(user_id: int = Query(...)):
    conn = get_db()
    try:
        current_month = datetime.now().strftime("%Y-%m")
        sales = conn.execute(
            "SELECT * FROM Sales WHERE user_id=? AND date LIKE ?", (user_id, f"{current_month}%")
        ).fetchall()
        messages = []
        if len(sales) == 0:
            messages.append("No sales recorded this month. Start logging your sales to get insights!")
        else:
            total_revenue = sum(s["quantity"] * s["unit_price"] for s in sales)
            messages.append(f"You've made ${total_revenue:,.2f} in sales this month.")
        alerts = conn.execute(
            "SELECT name,current_stock,reorder_level FROM Inventory WHERE user_id=? AND current_stock<=reorder_level",
            (user_id,)
        ).fetchall()
        for alert in alerts:
            messages.append(f"⚠️ {alert['name']} is low on stock ({alert['current_stock']} units, reorder at {alert['reorder_level']})")
        if not alerts:
            messages.append("Inventory levels are healthy.")
        today = datetime.now().strftime("%Y-%m-%d")
        overdue = conn.execute(
            "SELECT * FROM Loans WHERE user_id=? AND status='active' AND next_due_date<?", (user_id, today)
        ).fetchall()
        if overdue:
            messages.append(f"⚠️ You have {len(overdue)} overdue loan payment(s). Review your loans section.")
        else:
            messages.append("All loan payments are up to date.")
        return {"messages": messages}
    finally:
        conn.close()


@app.get("/api/finance/report")
async def finance_report(user_id: int = Query(...), period: str = Query("month")):
    conn = get_db()
    try:
        date_filter = datetime.now().strftime("%Y-%m") if period == "month" else datetime.now().strftime("%Y")
        sales = conn.execute(
            "SELECT * FROM Sales WHERE user_id=? AND date LIKE ?", (user_id, f"{date_filter}%")
        ).fetchall()
        expenses = conn.execute(
            "SELECT * FROM Expenses WHERE user_id=? AND date LIKE ?", (user_id, f"{date_filter}%")
        ).fetchall()
        revenue = sum(s["quantity"] * s["unit_price"] for s in sales)
        total_expenses = sum(e["amount"] for e in expenses)
        chart_data = {}
        for s in sales:
            day = s["date"]
            chart_data.setdefault(day, {"revenue": 0, "expenses": 0})
            chart_data[day]["revenue"] += s["quantity"] * s["unit_price"]
        for e in expenses:
            day = e["date"]
            chart_data.setdefault(day, {"revenue": 0, "expenses": 0})
            chart_data[day]["expenses"] += e["amount"]
        return {
            "period": period, "revenue": round(revenue, 2),
            "expenses": round(total_expenses, 2), "netProfit": round(revenue - total_expenses, 2),
            "chartData": {k: v for k, v in sorted(chart_data.items())}
        }
    finally:
        conn.close()


# ============ BOOKKEEPING ============

@app.get("/api/bookkeeping/summary")
async def bookkeeping_summary(user_id: int = Query(...)):
    conn = get_db()
    try:
        entries = conn.execute(
            "SELECT type,amount FROM Bookkeeping WHERE user_id=?", (user_id,)
        ).fetchall()
        total_sales = sum(e["amount"] for e in entries if e["type"] == "sale")
        total_purchases = sum(e["amount"] for e in entries if e["type"] == "purchase")
        total_expenses = sum(e["amount"] for e in entries if e["type"] == "expense")
        return {"totalSales": round(total_sales, 2), "totalPurchases": round(total_purchases, 2), "totalExpenses": round(total_expenses, 2)}
    finally:
        conn.close()


@app.get("/api/bookkeeping/entries")
async def list_bookkeeping_entries(user_id: int = Query(...), type: Optional[str] = Query(None)):
    conn = get_db()
    try:
        if type:
            entries = conn.execute(
                "SELECT * FROM Bookkeeping WHERE user_id=? AND type=? ORDER BY date DESC", (user_id, type)
            ).fetchall()
        else:
            entries = conn.execute(
                "SELECT * FROM Bookkeeping WHERE user_id=? ORDER BY date DESC", (user_id,)
            ).fetchall()
        return [dict(e) for e in entries]
    finally:
        conn.close()


@app.post("/api/bookkeeping/entries/sale")
async def bookkeeping_sale(entry: BookkeepingEntry, user_id: int = Query(...)):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO Bookkeeping (user_id,type,description,amount,date,category) VALUES (?,'sale',?,?,?,?)",
            (user_id, entry.description, entry.amount, entry.date, entry.category)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM Bookkeeping WHERE id=?", (cursor.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.post("/api/bookkeeping/entries/purchase")
async def bookkeeping_purchase(entry: BookkeepingEntry, user_id: int = Query(...)):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO Bookkeeping (user_id,type,description,amount,date,supplier) VALUES (?,'purchase',?,?,?,?)",
            (user_id, entry.description, entry.amount, entry.date, entry.supplier)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM Bookkeeping WHERE id=?", (cursor.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.post("/api/bookkeeping/entries/expense")
async def bookkeeping_expense(entry: BookkeepingEntry, user_id: int = Query(...)):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO Bookkeeping (user_id,type,description,amount,date,category) VALUES (?,'expense',?,?,?,?)",
            (user_id, entry.description, entry.amount, entry.date, entry.category)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM Bookkeeping WHERE id=?", (cursor.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.delete("/api/bookkeeping/entries/{entry_id}")
async def delete_bookkeeping_entry(entry_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM Bookkeeping WHERE id=?", (entry_id,))
        conn.commit()
        return {"message": "Entry deleted"}
    finally:
        conn.close()


@app.get("/api/bookkeeping/profit")
async def bookkeeping_profit(user_id: int = Query(...)):
    conn = get_db()
    try:
        entries = conn.execute("SELECT type,amount FROM Bookkeeping WHERE user_id=?", (user_id,)).fetchall()
        sales = sum(e["amount"] for e in entries if e["type"] == "sale")
        purchases = sum(e["amount"] for e in entries if e["type"] == "purchase")
        expenses = sum(e["amount"] for e in entries if e["type"] == "expense")
        profit = sales - purchases - expenses
        margin = (profit / sales * 100) if sales > 0 else 0
        return {"profit": round(profit, 2), "margin": round(margin, 1)}
    finally:
        conn.close()


# ============ FINANCIAL STATEMENTS ============

def _get_financials(conn, user_id: int, date_filter: str):
    """Helper: fetch aggregated financials for a date prefix filter."""
    sales = conn.execute(
        "SELECT quantity,unit_price,unit_cost,date FROM Sales WHERE user_id=? AND date LIKE ?",
        (user_id, f"{date_filter}%")
    ).fetchall()
    expenses = conn.execute(
        "SELECT category,amount,date FROM Expenses WHERE user_id=? AND date LIKE ?",
        (user_id, f"{date_filter}%")
    ).fetchall()
    purchases = conn.execute(
        "SELECT amount,date FROM Bookkeeping WHERE user_id=? AND type='purchase' AND date LIKE ?",
        (user_id, f"{date_filter}%")
    ).fetchall()
    loan_payments = conn.execute(
        """SELECT lp.amount, lp.payment_date as date FROM Loan_Payments lp
           JOIN Loans l ON lp.loan_id=l.id WHERE l.user_id=? AND lp.payment_date LIKE ?""",
        (user_id, f"{date_filter}%")
    ).fetchall()
    capital_ins = conn.execute(
        "SELECT amount,date FROM Capital WHERE user_id=? AND date LIKE ?",
        (user_id, f"{date_filter}%")
    ).fetchall()
    return sales, expenses, purchases, loan_payments, capital_ins


@app.get("/api/statements/income")
async def income_statement(user_id: int = Query(...), period: str = Query(None)):
    """Monthly income statement in formal + simple format."""
    conn = get_db()
    try:
        if not period:
            period = datetime.now().strftime("%Y-%m")

        sales, expenses, _, _, _ = _get_financials(conn, user_id, period)

        revenue = sum(s["quantity"] * s["unit_price"] for s in sales)
        cogs = sum(s["quantity"] * s["unit_cost"] for s in sales)
        gross_profit = revenue - cogs
        gross_margin = (gross_profit / revenue * 100) if revenue > 0 else 0

        expense_by_category = defaultdict(float)
        for e in expenses:
            expense_by_category[e["category"]] += e["amount"]
        total_opex = sum(expense_by_category.values())

        net_profit = gross_profit - total_opex
        net_margin = (net_profit / revenue * 100) if revenue > 0 else 0

        # Formal statement
        formal = {
            "title": "INCOME STATEMENT",
            "period": period,
            "sections": [
                {"label": "REVENUE", "items": [{"label": "Sales Revenue", "amount": round(revenue, 2)}],
                 "total": round(revenue, 2)},
                {"label": "COST OF GOODS SOLD", "items": [{"label": "Cost of Goods Sold", "amount": round(cogs, 2)}],
                 "total": round(cogs, 2)},
                {"label": "GROSS PROFIT", "items": [], "total": round(gross_profit, 2),
                 "highlight": True, "margin": round(gross_margin, 1)},
                {"label": "OPERATING EXPENSES",
                 "items": [{"label": k, "amount": round(v, 2)} for k, v in expense_by_category.items()],
                 "total": round(total_opex, 2)},
                {"label": "NET PROFIT / (LOSS)", "items": [], "total": round(net_profit, 2),
                 "highlight": True, "margin": round(net_margin, 1), "is_bottom_line": True}
            ]
        }

        # Simple statement
        profit_emoji = "✅" if net_profit >= 0 else "❌"
        simple = {
            "title": "YOUR BUSINESS REPORT",
            "period": period,
            "lines": [
                {"label": "💰 Money that came in (from sales)", "value": f"${revenue:,.2f}"},
                {"label": "🛒 Money spent buying stock (cost of goods)", "value": f"${cogs:,.2f}"},
                {"label": "📊 What you kept after buying stock", "value": f"${gross_profit:,.2f}",
                 "highlight": True},
                {"label": "💸 Business running costs (rent, salaries, etc.)", "value": f"${total_opex:,.2f}"},
            ] + [{"label": f"   - {k}", "value": f"${v:,.2f}"} for k, v in expense_by_category.items()] + [
                {"label": f"{profit_emoji} YOUR PROFIT THIS MONTH", "value": f"${net_profit:,.2f}",
                 "highlight": True, "big": True},
                {"label": "📈 For every $1 you made, you kept",
                 "value": f"${net_margin/100:.2f}" if revenue > 0 else "N/A"}
            ]
        }

        return {"formal": formal, "simple": simple}
    finally:
        conn.close()


@app.get("/api/statements/cashflow")
async def cash_flow_statement(user_id: int = Query(...), date: str = Query(None)):
    """Daily cash flow. date = YYYY-MM-DD. Defaults to today."""
    conn = get_db()
    try:
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

        # Daily inflows/outflows
        day_sales = conn.execute(
            "SELECT SUM(quantity*unit_price) as total FROM Sales WHERE user_id=? AND date=?", (user_id, date)
        ).fetchone()["total"] or 0

        day_capital = conn.execute(
            "SELECT SUM(amount) as total FROM Capital WHERE user_id=? AND date=?", (user_id, date)
        ).fetchone()["total"] or 0

        day_expenses = conn.execute(
            "SELECT SUM(amount) as total FROM Expenses WHERE user_id=? AND date=?", (user_id, date)
        ).fetchone()["total"] or 0

        day_purchases = conn.execute(
            "SELECT SUM(amount) as total FROM Bookkeeping WHERE user_id=? AND type='purchase' AND date=?",
            (user_id, date)
        ).fetchone()["total"] or 0

        day_loan_payments = conn.execute(
            """SELECT SUM(lp.amount) as total FROM Loan_Payments lp
               JOIN Loans l ON lp.loan_id=l.id WHERE l.user_id=? AND lp.payment_date=?""",
            (user_id, date)
        ).fetchone()["total"] or 0

        # Opening balance: cumulative net from all days before this date
        hist_sales = conn.execute(
            "SELECT SUM(quantity*unit_price) as t FROM Sales WHERE user_id=? AND date<?", (user_id, date)
        ).fetchone()["t"] or 0
        hist_capital = conn.execute(
            "SELECT SUM(amount) as t FROM Capital WHERE user_id=? AND date<?", (user_id, date)
        ).fetchone()["t"] or 0
        hist_expenses = conn.execute(
            "SELECT SUM(amount) as t FROM Expenses WHERE user_id=? AND date<?", (user_id, date)
        ).fetchone()["t"] or 0
        hist_purchases = conn.execute(
            "SELECT SUM(amount) as t FROM Bookkeeping WHERE user_id=? AND type='purchase' AND date<?",
            (user_id, date)
        ).fetchone()["t"] or 0
        hist_loan_payments = conn.execute(
            """SELECT SUM(lp.amount) as t FROM Loan_Payments lp
               JOIN Loans l ON lp.loan_id=l.id WHERE l.user_id=? AND lp.payment_date<?""",
            (user_id, date)
        ).fetchone()["t"] or 0

        opening_balance = (hist_sales + hist_capital) - (hist_expenses + hist_purchases + hist_loan_payments)
        total_inflows = day_sales + day_capital
        total_outflows = day_expenses + day_purchases + day_loan_payments
        net_change = total_inflows - total_outflows
        closing_balance = opening_balance + net_change

        formal = {
            "title": "CASH FLOW STATEMENT",
            "date": date,
            "openingBalance": round(opening_balance, 2),
            "inflows": {
                "label": "CASH INFLOWS",
                "items": [
                    {"label": "Sales Revenue", "amount": round(day_sales, 2)},
                    {"label": "Capital Contributions", "amount": round(day_capital, 2)},
                ],
                "total": round(total_inflows, 2)
            },
            "outflows": {
                "label": "CASH OUTFLOWS",
                "items": [
                    {"label": "Operating Expenses", "amount": round(day_expenses, 2)},
                    {"label": "Stock Purchases", "amount": round(day_purchases, 2)},
                    {"label": "Loan Payments", "amount": round(day_loan_payments, 2)},
                ],
                "total": round(total_outflows, 2)
            },
            "netChange": round(net_change, 2),
            "closingBalance": round(closing_balance, 2)
        }

        direction = "more than" if net_change >= 0 else "less than"
        cash_emoji = "✅" if net_change >= 0 else "⚠️"
        simple = {
            "title": "YOUR DAILY CASH SUMMARY",
            "date": date,
            "lines": [
                {"label": "🏦 Money you had at the start of the day", "value": f"${opening_balance:,.2f}"},
                {"label": "💰 Money that came IN today", "value": f"${total_inflows:,.2f}", "highlight": True},
                {"label": "   From sales", "value": f"${day_sales:,.2f}"},
                {"label": "   From owner contributions", "value": f"${day_capital:,.2f}"},
                {"label": "💸 Money that went OUT today", "value": f"${total_outflows:,.2f}"},
                {"label": "   Business expenses", "value": f"${day_expenses:,.2f}"},
                {"label": "   Stock purchases", "value": f"${day_purchases:,.2f}"},
                {"label": "   Loan payments", "value": f"${day_loan_payments:,.2f}"},
                {"label": f"{cash_emoji} Net cash movement today", "value": f"${net_change:,.2f}", "highlight": True},
                {"label": "🏦 Money at end of day", "value": f"${closing_balance:,.2f}", "big": True},
            ]
        }

        return {"formal": formal, "simple": simple}
    finally:
        conn.close()


@app.get("/api/statements/balance")
async def balance_sheet(user_id: int = Query(...)):
    """Balance sheet as of today. Guaranteed Assets = Liabilities + Equity."""
    conn = get_db()
    try:
        today = datetime.now().strftime("%Y-%m-%d")

        # ── Inventory (physical stock at cost) ───────────────────────────
        inventory_value = conn.execute(
            "SELECT COALESCE(SUM(current_stock*cost_price),0) as v FROM Inventory WHERE user_id=?",
            (user_id,)
        ).fetchone()["v"]

        # ── Transaction aggregates ───────────────────────────────────────
        total_sales_revenue = conn.execute(
            "SELECT COALESCE(SUM(quantity*unit_price),0) as t FROM Sales WHERE user_id=?", (user_id,)
        ).fetchone()["t"]
        total_cogs = conn.execute(
            "SELECT COALESCE(SUM(quantity*unit_cost),0) as t FROM Sales WHERE user_id=?", (user_id,)
        ).fetchone()["t"]
        total_expenses = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM Expenses WHERE user_id=?", (user_id,)
        ).fetchone()["t"]
        total_purchases = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM Bookkeeping WHERE user_id=? AND type='purchase'",
            (user_id,)
        ).fetchone()["t"]
        total_loan_payments_out = conn.execute(
            """SELECT COALESCE(SUM(lp.amount),0) as t FROM Loan_Payments lp
               JOIN Loans l ON lp.loan_id=l.id WHERE l.user_id=?""", (user_id,)
        ).fetchone()["t"]
        total_capital = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM Capital WHERE user_id=?", (user_id,)
        ).fetchone()["t"]
        total_loan_principal_received = conn.execute(
            "SELECT COALESCE(SUM(principal),0) as t FROM Loans WHERE user_id=?", (user_id,)
        ).fetchone()["t"]

        # ── Cash position (actual cash flows — COGS is NOT a cash outflow) ──
        # Cash leaves when you buy stock (purchases) and pay expenses/loans,
        # not when an accounting COGS entry is made at point of sale.
        cash = (total_sales_revenue + total_capital + total_loan_principal_received) - \
               (total_expenses + total_purchases + total_loan_payments_out)
        # Negative cash = bank overdraft (shown as a liability, not negative asset)
        cash_asset = max(0.0, cash)
        overdraft = max(0.0, -cash)

        # ── Liabilities ──────────────────────────────────────────────────
        # All loans (completed loans contribute 0 since amount_paid = principal)
        outstanding_loans = conn.execute(
            "SELECT COALESCE(SUM(principal-amount_paid),0) as t FROM Loans WHERE user_id=?",
            (user_id,)
        ).fetchone()["t"]
        outstanding_loans = max(0.0, outstanding_loans)
        total_liabilities = outstanding_loans + overdraft

        # ── Equity ───────────────────────────────────────────────────────
        retained_earnings = total_sales_revenue - total_cogs - total_expenses

        # Inventory reconciliation: absorbs any difference between the physical
        # inventory value and what transaction records imply (Purchases - COGS).
        # Common cause: initial stock entered without a corresponding purchase record,
        # or unit costs in sales differ from inventory cost prices.
        expected_inventory_from_txns = total_purchases - total_cogs
        inventory_reconciliation = round(inventory_value - expected_inventory_from_txns, 2)

        total_equity = total_capital + retained_earnings + inventory_reconciliation
        total_liabilities_and_equity = total_liabilities + total_equity
        total_assets = cash_asset + inventory_value

        # ── Formal statement ─────────────────────────────────────────────
        assets_items = [
            {"label": "Cash & Cash Equivalents", "amount": round(cash_asset, 2)},
            {"label": "Inventory (at cost)", "amount": round(inventory_value, 2)},
        ]

        liabilities_items = [{"label": "Outstanding Loans", "amount": round(outstanding_loans, 2)}]
        if overdraft > 0.005:
            liabilities_items.append({"label": "Bank Overdraft", "amount": round(overdraft, 2)})

        equity_items = [
            {"label": "Capital Contributions", "amount": round(total_capital, 2)},
            {"label": "Retained Earnings", "amount": round(retained_earnings, 2)},
        ]
        if abs(inventory_reconciliation) > 0.005:
            eq_label = "Opening Stock Adjustment" if inventory_reconciliation > 0 else "Stock Reconciliation Adj."
            equity_items.append({"label": eq_label, "amount": round(inventory_reconciliation, 2)})

        formal = {
            "title": "BALANCE SHEET",
            "as_of": today,
            "assets": {
                "label": "ASSETS",
                "current": assets_items,
                "total": round(total_assets, 2)
            },
            "liabilities": {
                "label": "LIABILITIES",
                "current": liabilities_items,
                "total": round(total_liabilities, 2)
            },
            "equity": {
                "label": "OWNER'S EQUITY",
                "items": equity_items,
                "total": round(total_equity, 2)
            },
            "totalLiabilitiesAndEquity": round(total_liabilities_and_equity, 2),
            "balanced": abs(total_assets - total_liabilities_and_equity) < 0.02
        }

        # ── Simple statement ─────────────────────────────────────────────
        simple_lines = [
            {"label": "WHAT YOU OWN (Assets)", "value": f"${total_assets:,.2f}", "section": True},
            {"label": "   Cash in hand / at bank", "value": f"${cash_asset:,.2f}"},
            {"label": "   Stock on your shelves (at cost)", "value": f"${inventory_value:,.2f}"},
        ]
        liab_total = total_liabilities
        debt_lines = [{"label": "   Outstanding loan balances", "value": f"${outstanding_loans:,.2f}"}]
        if overdraft > 0.005:
            debt_lines.append({"label": "   Bank overdraft", "value": f"${overdraft:,.2f}"})
        simple_lines.append({"label": "WHAT YOU OWE (Debts)", "value": f"${liab_total:,.2f}", "section": True})
        simple_lines.extend(debt_lines)
        simple_lines += [
            {"label": "YOUR SHARE OF THE BUSINESS (Equity)", "value": f"${total_equity:,.2f}",
             "section": True, "highlight": True},
            {"label": "   Money you put in", "value": f"${total_capital:,.2f}"},
            {"label": "   Profits kept in business", "value": f"${retained_earnings:,.2f}"},
        ]
        if abs(inventory_reconciliation) > 0.005:
            simple_lines.append({"label": "   Stock value adjustment", "value": f"${inventory_reconciliation:,.2f}"})

        simple = {
            "title": "WHAT YOUR BUSINESS OWNS AND OWES",
            "as_of": today,
            "lines": simple_lines
        }

        return {"formal": formal, "simple": simple}
    finally:
        conn.close()


# ============ CREDIT SCORE ============

@app.get("/api/credit-score/{user_id}")
async def get_credit_score(user_id: int):
    conn = get_db()
    try:
        score = 300
        factors = {}

        # 1. Loan repayment history (max +255)
        loans = conn.execute("SELECT * FROM Loans WHERE user_id=?", (user_id,)).fetchall()
        if loans:
            today = datetime.now().strftime("%Y-%m-%d")
            overdue_count = sum(1 for l in loans if l["status"] == "active" and l["next_due_date"] < today)
            active_count = sum(1 for l in loans if l["status"] == "active")
            completed_count = sum(1 for l in loans if l["status"] == "completed")
            total = active_count + completed_count
            on_time_ratio = completed_count / total if total > 0 else 0.5
            overdue_penalty = (overdue_count / max(active_count, 1))
            loan_score = max(0, min(255, int(200 * (1 - overdue_penalty) + 55 * on_time_ratio)))
            score += loan_score
            factors["loan_history"] = {"score": loan_score, "max": 255, "overdue": overdue_count, "completed": completed_count}
        else:
            score += 127
            factors["loan_history"] = {"score": 127, "max": 255, "note": "No loan history"}

        # 2. Revenue consistency (max +212)
        monthly_rev = conn.execute(
            "SELECT strftime('%Y-%m', date) as month, SUM(quantity*unit_price) as rev FROM Sales WHERE user_id=? GROUP BY month ORDER BY month",
            (user_id,)
        ).fetchall()
        if len(monthly_rev) >= 2:
            revenues = [r["rev"] for r in monthly_rev]
            avg = statistics.mean(revenues)
            cv = statistics.stdev(revenues) / avg if avg > 0 else 1
            rev_score = max(0, min(212, int(212 * (1 - min(cv, 1)))))
            if revenues[-1] > revenues[0]:
                rev_score = min(212, rev_score + 20)
            score += rev_score
            factors["revenue_consistency"] = {"score": rev_score, "max": 212, "months": len(monthly_rev)}
        elif len(monthly_rev) == 1:
            score += 106
            factors["revenue_consistency"] = {"score": 106, "max": 212, "note": "Only 1 month of data"}
        else:
            factors["revenue_consistency"] = {"score": 0, "max": 212, "note": "No revenue recorded"}

        # 3. Profitability (max +170)
        fin = conn.execute(
            "SELECT COALESCE(SUM(quantity*unit_price),0) as rev, COALESCE(SUM(quantity*unit_cost),0) as cogs FROM Sales WHERE user_id=?",
            (user_id,)
        ).fetchone()
        total_exp = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM Expenses WHERE user_id=?", (user_id,)
        ).fetchone()["t"]
        margin = 0
        if fin["rev"] > 0:
            margin = (fin["rev"] - fin["cogs"] - total_exp) / fin["rev"]
            if margin > 0.2:
                profit_score = 170
            elif margin > 0.1:
                profit_score = 127
            elif margin > 0:
                profit_score = 85
            else:
                profit_score = 0
        else:
            profit_score = 0
        score += profit_score
        factors["profitability"] = {"score": profit_score, "max": 170, "margin": round(margin * 100, 1)}

        # 4. Debt-to-asset ratio (max +127)
        debt = conn.execute(
            "SELECT COALESCE(SUM(principal-amount_paid),0) as d FROM Loans WHERE user_id=? AND status='active'",
            (user_id,)
        ).fetchone()["d"]
        inv_val = conn.execute(
            "SELECT COALESCE(SUM(current_stock*cost_price),0) as v FROM Inventory WHERE user_id=?", (user_id,)
        ).fetchone()["v"]
        total_assets = inv_val + max(0, fin["rev"] - fin["cogs"] - total_exp)
        if debt == 0:
            debt_score = 127
        elif total_assets > 0:
            ratio = debt / total_assets
            debt_score = 127 if ratio < 0.3 else (85 if ratio < 0.5 else (42 if ratio < 0.7 else 0))
        else:
            debt_score = 42
        score += debt_score
        factors["debt_ratio"] = {"score": debt_score, "max": 127,
                                 "ratio": round(debt / max(total_assets, 1), 2)}

        # 5. Business activity (max +85)
        thirty_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        recent = conn.execute(
            "SELECT COUNT(*) as c FROM Sales WHERE user_id=? AND date>=?", (user_id, thirty_ago)
        ).fetchone()["c"]
        activity_score = 85 if recent >= 15 else (42 if recent >= 5 else 0)
        score += activity_score
        factors["business_activity"] = {"score": activity_score, "max": 85, "recent_transactions": recent}

        final_score = min(850, max(300, score))
        if final_score >= 750:
            grade, color = "Excellent", "#48bb78"
        elif final_score >= 650:
            grade, color = "Good", "#68d391"
        elif final_score >= 550:
            grade, color = "Fair", "#f6ad55"
        elif final_score >= 450:
            grade, color = "Poor", "#fc8181"
        else:
            grade, color = "Very Poor", "#e53e3e"

        return {"score": final_score, "grade": grade, "color": color,
                "maxScore": 850, "minScore": 300, "factors": factors,
                "computed_at": datetime.now().isoformat()}
    finally:
        conn.close()


# ============ AUDIT TOOL ============

@app.get("/api/audit/{user_id}")
async def audit_report(user_id: int):
    conn = get_db()
    try:
        flags = []

        # 1. Duplicate bookkeeping entries
        duplicates = conn.execute("""
            SELECT type, amount, date, COUNT(*) as cnt
            FROM Bookkeeping WHERE user_id=?
            GROUP BY type, amount, date HAVING cnt > 1
        """, (user_id,)).fetchall()
        for dup in duplicates:
            flags.append({"severity": "warning", "category": "Duplicate Entry",
                          "message": f"Possible duplicate: {dup['cnt']}x {dup['type']} of ${dup['amount']:.2f} on {dup['date']}",
                          "date": dup["date"]})

        # 2. Expense anomaly (this month > 2x historical average per category)
        current_month = datetime.now().strftime("%Y-%m")
        rows = conn.execute("""
            SELECT category, strftime('%Y-%m', date) as month, SUM(amount) as total
            FROM Expenses WHERE user_id=? GROUP BY category, month
        """, (user_id,)).fetchall()
        cat_history = defaultdict(list)
        cat_current = {}
        for r in rows:
            if r["month"] == current_month:
                cat_current[r["category"]] = r["total"]
            else:
                cat_history[r["category"]].append(r["total"])
        for cat, cur_amt in cat_current.items():
            history = cat_history.get(cat, [])
            if len(history) >= 2:
                avg = statistics.mean(history)
                if avg > 0 and cur_amt > avg * 2:
                    flags.append({"severity": "alert", "category": "Expense Anomaly",
                                  "message": f"{cat} expenses (${cur_amt:.2f}) are {cur_amt/avg:.1f}x above average (${avg:.2f})",
                                  "date": current_month})

        # 3. No sales in 7+ days while holding active loans
        active_loans = conn.execute(
            "SELECT COUNT(*) as c FROM Loans WHERE user_id=? AND status='active'", (user_id,)
        ).fetchone()["c"]
        if active_loans > 0:
            seven_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            recent_sales = conn.execute(
                "SELECT COUNT(*) as c FROM Sales WHERE user_id=? AND date>=?", (user_id, seven_ago)
            ).fetchone()["c"]
            if recent_sales == 0:
                flags.append({"severity": "alert", "category": "Revenue Gap",
                              "message": "No sales in the past 7 days while you have active loans",
                              "date": datetime.now().strftime("%Y-%m-%d")})

        # 4. Loans > 30 days overdue
        thirty_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        long_overdue = conn.execute("""
            SELECT lender_name, next_due_date, principal, amount_paid
            FROM Loans WHERE user_id=? AND status='active' AND next_due_date<?
        """, (user_id, thirty_ago)).fetchall()
        for loan in long_overdue:
            days_overdue = (datetime.now() - datetime.strptime(loan["next_due_date"], "%Y-%m-%d")).days
            flags.append({"severity": "critical", "category": "Overdue Loan",
                          "message": f"Loan from {loan['lender_name']} is {days_overdue} days overdue. Balance: ${loan['principal']-loan['amount_paid']:.2f}",
                          "date": loan["next_due_date"]})

        # 5. Sustained losses (3+ consecutive months negative)
        monthly_rev = conn.execute(
            "SELECT strftime('%Y-%m', date) as m, SUM(quantity*unit_price) as rev FROM Sales WHERE user_id=? GROUP BY m ORDER BY m",
            (user_id,)
        ).fetchall()
        monthly_exp = conn.execute(
            "SELECT strftime('%Y-%m', date) as m, SUM(amount) as exp FROM Expenses WHERE user_id=? GROUP BY m ORDER BY m",
            (user_id,)
        ).fetchall()
        monthly = {}
        for r in monthly_rev:
            monthly[r["m"]] = {"rev": r["rev"], "exp": 0}
        for e in monthly_exp:
            if e["m"] in monthly:
                monthly[e["m"]]["exp"] = e["exp"]
        sorted_months = sorted(monthly.keys())
        neg_count = sum(1 for m in sorted_months[-3:] if monthly[m]["rev"] < monthly[m]["exp"])
        if neg_count >= 3:
            flags.append({"severity": "critical", "category": "Sustained Losses",
                          "message": "Business has had negative cash flow for 3+ consecutive months",
                          "date": datetime.now().strftime("%Y-%m-%d")})

        critical = sum(1 for f in flags if f["severity"] == "critical")
        alerts = sum(1 for f in flags if f["severity"] == "alert")
        warnings = sum(1 for f in flags if f["severity"] == "warning")
        if critical > 0:
            risk_level, risk_color = "High Risk", "#e53e3e"
        elif alerts > 0:
            risk_level, risk_color = "Medium Risk", "#f6ad55"
        elif warnings > 0:
            risk_level, risk_color = "Low Risk", "#ecc94b"
        else:
            risk_level, risk_color = "Clean", "#48bb78"

        return {"flags": flags, "summary": {"critical": critical, "alerts": alerts, "warnings": warnings,
                                            "risk_level": risk_level, "risk_color": risk_color},
                "computed_at": datetime.now().isoformat()}
    finally:
        conn.close()


# ============ NOTIFICATION SETTINGS ============

@app.get("/api/notifications/settings")
async def get_notification_settings(user_id: int = Query(...)):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM Notification_Settings WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            user = conn.execute("SELECT email FROM Users WHERE ID=?", (user_id,)).fetchone()
            return {
                "user_id": user_id,
                "notification_email": user["email"] if user else "",
                "inventory_alerts": True,
                "loan_alerts": True,
                "loan_days_before": 3
            }
        return dict(row)
    finally:
        conn.close()


@app.put("/api/notifications/settings")
async def update_notification_settings(user_id: int = Query(...), settings: NotificationSettingsUpdate = None):
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM Notification_Settings WHERE user_id=?", (user_id,)).fetchone()
        if not existing:
            user = conn.execute("SELECT email FROM Users WHERE ID=?", (user_id,)).fetchone()
            default_email = user["email"] if user else ""
            conn.execute(
                """INSERT INTO Notification_Settings (user_id, notification_email, inventory_alerts, loan_alerts, loan_days_before)
                   VALUES (?,?,1,1,3)""",
                (user_id, default_email)
            )
        if settings:
            fields = {k: v for k, v in settings.dict(exclude_unset=True).items() if v is not None}
            if fields:
                clause = ", ".join(f"{k}=?" for k in fields)
                conn.execute(f"UPDATE Notification_Settings SET {clause} WHERE user_id=?",
                             list(fields.values()) + [user_id])
        conn.commit()
        return dict(conn.execute("SELECT * FROM Notification_Settings WHERE user_id=?", (user_id,)).fetchone())
    finally:
        conn.close()


@app.post("/api/notifications/trigger")
async def trigger_notifications(user_id: int = Query(...), background_tasks: BackgroundTasks = None):
    """Manually trigger notification check for a user."""
    background_tasks.add_task(run_daily_notifications)
    return {"message": "Notification check triggered"}


# ============ SMTP CONFIGURATION ============

@app.get("/api/smtp/config")
async def get_smtp_config():
    conn = get_db()
    try:
        row = conn.execute("SELECT smtp_host, smtp_port, smtp_user, updated_at FROM SMTP_Config LIMIT 1").fetchone()
        if not row:
            return {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_user": "", "configured": False}
        return {**dict(row), "configured": bool(row["smtp_user"])}
    finally:
        conn.close()


@app.put("/api/smtp/config")
async def save_smtp_config(config: SmtpConfigModel):
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM SMTP_Config LIMIT 1").fetchone()
        if existing:
            conn.execute(
                "UPDATE SMTP_Config SET smtp_host=?, smtp_port=?, smtp_user=?, smtp_pass=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (config.smtp_host, config.smtp_port, config.smtp_user, config.smtp_pass, existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO SMTP_Config (smtp_host, smtp_port, smtp_user, smtp_pass) VALUES (?,?,?,?)",
                (config.smtp_host, config.smtp_port, config.smtp_user, config.smtp_pass)
            )
        conn.commit()
        os.environ["SMTP_HOST"] = config.smtp_host
        os.environ["SMTP_PORT"] = str(config.smtp_port)
        os.environ["SMTP_USER"] = config.smtp_user
        os.environ["SMTP_PASS"] = config.smtp_pass
        return {"message": "SMTP settings saved and applied.", "configured": True}
    finally:
        conn.close()


@app.post("/api/notifications/test")
async def test_notification_email(user_id: int = Query(...)):
    conn = get_db()
    try:
        user = conn.execute("SELECT email, business_name FROM Users WHERE ID=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        notif = conn.execute("SELECT notification_email FROM Notification_Settings WHERE user_id=?", (user_id,)).fetchone()
        to_email = (notif["notification_email"] if notif else None) or user["email"]
        html = f"""
        <h2 style="color:#1e3a5f">NeXus Toolkit — Test Email</h2>
        <p>Hello from <strong>NeXus Toolkit</strong>!</p>
        <p>Your email notifications are working correctly for <strong>{user['business_name']}</strong>.</p>
        <ul>
            <li>Low stock alerts when inventory falls below your threshold</li>
            <li>Loan payment reminders before due dates</li>
        </ul>
        <p style="color:#718096;font-size:12px">Sent from NeXus Toolkit API</p>
        """
        success = send_email(to_email, "[NeXus] Test Notification Email", html)
        return {
            "success": success,
            "sent_to": to_email,
            "message": "Test email sent successfully!" if success else "SMTP not configured. Add your credentials in Settings → Email Setup."
        }
    finally:
        conn.close()


# ============ CREDITOR SURVEILLANCE ============

@app.get("/api/creditor/sme-list")
async def creditor_sme_list():
    """List all SMEs on the platform (read-only for creditors)."""
    conn = get_db()
    try:
        users = conn.execute(
            "SELECT ID, business_name, owner_name, city, sector, years_active FROM Users ORDER BY business_name"
        ).fetchall()
        return [dict(u) for u in users]
    finally:
        conn.close()


@app.get("/api/creditor/sme/{user_id}/overview")
async def creditor_sme_overview(user_id: int):
    """Creditor read-only view of an SME's key financials."""
    conn = get_db()
    try:
        user = conn.execute(
            "SELECT ID, business_name, owner_name, city, sector, employees, years_active FROM Users WHERE ID=?",
            (user_id,)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="SME not found")

        # Monthly revenue trend (last 6 months)
        revenue_trend = conn.execute("""
            SELECT strftime('%Y-%m', date) as month, ROUND(SUM(quantity*unit_price),2) as revenue,
                   ROUND(SUM(quantity*unit_cost),2) as cogs
            FROM Sales WHERE user_id=?
            GROUP BY month ORDER BY month DESC LIMIT 6
        """, (user_id,)).fetchall()

        # Active loans summary
        loans = conn.execute(
            "SELECT lender_name, principal, amount_paid, status, next_due_date FROM Loans WHERE user_id=? AND status='active'",
            (user_id,)
        ).fetchall()
        total_debt = sum(l["principal"] - l["amount_paid"] for l in loans)

        # Inventory value
        inv_value = conn.execute(
            "SELECT COALESCE(SUM(current_stock*cost_price),0) as v FROM Inventory WHERE user_id=?", (user_id,)
        ).fetchone()["v"]

        return {
            "business": dict(user),
            "revenueTrend": [dict(r) for r in reversed(revenue_trend)],
            "totalOutstandingDebt": round(total_debt, 2),
            "activeLoans": len(loans),
            "inventoryValue": round(inv_value, 2),
            "activeLoansDetail": [dict(l) for l in loans]
        }
    finally:
        conn.close()


# ============ LOAN CALCULATOR ============

@app.post("/api/calculator/loan")
async def calculate_loan(request: LoanCalculatorRequest):
    monthly_rate = request.rate / 100 / 12
    n = request.term_months
    if monthly_rate > 0:
        monthly_payment = request.principal * (monthly_rate * (1 + monthly_rate) ** n) / ((1 + monthly_rate) ** n - 1)
    else:
        monthly_payment = request.principal / n
    total_payment = monthly_payment * n
    total_interest = total_payment - request.principal
    schedule = []
    remaining = request.principal
    for month in range(1, n + 1):
        interest = remaining * monthly_rate
        principal_payment = monthly_payment - interest
        remaining -= principal_payment
        schedule.append({
            "month": month, "payment": round(monthly_payment, 2),
            "interest": round(interest, 2), "principal": round(principal_payment, 2),
            "remainingBalance": round(max(0, remaining), 2)
        })
    return {
        "monthlyPayment": round(monthly_payment, 2), "totalInterest": round(total_interest, 2),
        "totalPayment": round(total_payment, 2), "schedule": schedule
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)
