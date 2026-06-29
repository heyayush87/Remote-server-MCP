from fastmcp import FastMCP
import os
import sqlite3
import aiosqlite
import tempfile
import json
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

# ── Paths ─────────────────────────────────────────────────────────────────────
# Use /tmp — always writable in Docker/cloud containers.
# os.path.dirname(__file__) is the app directory, which is read-only after build.
TEMP_DIR = tempfile.gettempdir()
DB_PATH = os.path.join(TEMP_DIR, "expenses.db")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

print(f"Database path: {DB_PATH}")

mcp = FastMCP("ExpenseTracker")


# ── Async DB helper for tool handlers ─────────────────────────────────────────
def get_db():
    return aiosqlite.connect(DB_PATH, timeout=5)


# ── Schema init: plain sync sqlite3, safe at import time ──────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("""
            CREATE TABLE IF NOT EXISTS expenses(
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                amount      REAL NOT NULL,
                category    TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note        TEXT DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS budgets(
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                month    TEXT NOT NULL,
                amount   REAL NOT NULL,
                UNIQUE(category, month)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS recurring(
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                amount      REAL NOT NULL,
                category    TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note        TEXT DEFAULT '',
                frequency   TEXT NOT NULL CHECK(frequency IN ('daily','weekly','monthly')),
                next_date   TEXT NOT NULL,
                active      INTEGER DEFAULT 1
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS income(
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                date    TEXT NOT NULL,
                amount  REAL NOT NULL,
                source  TEXT DEFAULT '',
                note    TEXT DEFAULT ''
            )
        """)
        # Verify write access
        c.execute("INSERT OR IGNORE INTO expenses(date, amount, category) VALUES ('2000-01-01', 0, 'test')")
        c.execute("DELETE FROM expenses WHERE category = 'test'")
        c.commit()
        print("Database initialized successfully with write access")

init_db()


# ── Expense CRUD ──────────────────────────────────────────────────────────────

@mcp.tool()
async def add_expense(date: str, amount: float, category: str,
                    subcategory: str = "", note: str = ""):
    """Add a new expense entry to the database."""
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO expenses(date, amount, category, subcategory, note) VALUES (?,?,?,?,?)",
            (date, amount, category, subcategory, note)
        )
        await db.commit()
        return {"status": "ok", "id": cur.lastrowid}


@mcp.tool()
async def get_expense(id: int):
    """Fetch a single expense entry by its ID."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM expenses WHERE id = ?", (id,))
        row = await cur.fetchone()
        if not row:
            return {"status": "error", "message": f"No expense found with id {id}"}
        return dict(row)


@mcp.tool()
async def list_expenses(start_date: str, end_date: str):
    """List expense entries within an inclusive date range."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT id, date, amount, category, subcategory, note
            FROM expenses
            WHERE date BETWEEN ? AND ?
            ORDER BY date DESC, id DESC""",
            (start_date, end_date)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@mcp.tool()
async def update_expense(id: int, date: str = None, amount: float = None,
                    category: str = None, subcategory: str = None,
                    note: str = None):
    """Update one or more fields of an existing expense entry."""
    async with get_db() as db:
        cur = await db.execute("SELECT id FROM expenses WHERE id = ?", (id,))
        if not await cur.fetchone():
            return {"status": "error", "message": f"No expense found with id {id}"}

        fields = {"date": date, "amount": amount, "category": category,
                "subcategory": subcategory, "note": note}
        updates = {k: v for k, v in fields.items() if v is not None}
        if not updates:
            return {"status": "error", "message": "No valid fields provided to update."}

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        await db.execute(
            f"UPDATE expenses SET {set_clause} WHERE id = ?",
            list(updates.values()) + [id]
        )
        await db.commit()

        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM expenses WHERE id = ?", (id,))
        row = await cur.fetchone()
        return {"status": "ok", "updated": dict(row)}


@mcp.tool()
async def delete_expense(id: int):
    """Delete an expense entry by its ID."""
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM expenses WHERE id = ?", (id,))
        row = await cur.fetchone()
        if not row:
            return {"status": "error", "message": f"No expense found with id {id}"}
        deleted = dict(row)
        await db.execute("DELETE FROM expenses WHERE id = ?", (id,))
        await db.commit()
        return {"status": "ok", "deleted": deleted}


# ── Search & export ───────────────────────────────────────────────────────────

@mcp.tool()
async def search_expenses(keyword: str = None, category: str = None,
                        subcategory: str = None, min_amount: float = None,
                        max_amount: float = None, start_date: str = None,
                        end_date: str = None):
    """Search and filter expenses by keyword, category, subcategory, or amount range."""
    async with get_db() as db:
        query = "SELECT * FROM expenses WHERE 1=1"
        params = []
        if keyword:
            query += " AND LOWER(note) LIKE ?"
            params.append(f"%{keyword.lower()}%")
        if category:
            query += " AND LOWER(category) = LOWER(?)"
            params.append(category)
        if subcategory:
            query += " AND LOWER(subcategory) = LOWER(?)"
            params.append(subcategory)
        if min_amount is not None:
            query += " AND amount >= ?"
            params.append(min_amount)
        if max_amount is not None:
            query += " AND amount <= ?"
            params.append(max_amount)
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date DESC, id DESC"

        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        return {"results": [dict(r) for r in rows], "count": len(rows)}


@mcp.tool()
async def summarize(start_date: str, end_date: str, category: str = None):
    """Summarize expenses by category within an inclusive date range."""
    async with get_db() as db:
        query = """
            SELECT category, subcategory,
                COUNT(*)     AS count,
                SUM(amount)  AS total,
                AVG(amount)  AS average,
                MIN(amount)  AS min,
                MAX(amount)  AS max
            FROM expenses
            WHERE date BETWEEN ? AND ?
        """
        params = [start_date, end_date]
        if category:
            query += " AND LOWER(category) = LOWER(?)"
            params.append(category)
        query += " GROUP BY category, subcategory ORDER BY total DESC"

        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = [dict(r) for r in await cur.fetchall()]
        grand_total = sum(r["total"] for r in rows)
        return {"summary": rows, "grand_total": grand_total}


@mcp.tool()
async def export_expenses(start_date: str, end_date: str,
                        format: str = "csv", category: str = None):
    """Export expenses as CSV or JSON for a given date range. format: 'csv' or 'json'."""
    async with get_db() as db:
        query = "SELECT * FROM expenses WHERE date BETWEEN ? AND ?"
        params = [start_date, end_date]
        if category:
            query += " AND LOWER(category) = LOWER(?)"
            params.append(category)
        query += " ORDER BY date DESC"

        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = [dict(r) for r in await cur.fetchall()]

        if format == "json":
            return {"format": "json", "count": len(rows), "data": rows}

        # CSV
        if not rows:
            return {"format": "csv", "count": 0, "data": ""}
        cols = list(rows[0].keys())
        lines = [",".join(cols)]
        for row in rows:
            values = []
            for col in cols:
                val = str(row[col] or "")
                if "," in val or '"' in val:
                    val = f'"{val.replace(chr(34), chr(34)*2)}"'
                values.append(val)
            lines.append(",".join(values))
        return {"format": "csv", "count": len(rows), "data": "\n".join(lines)}


# ── Budget ────────────────────────────────────────────────────────────────────

@mcp.tool()
async def set_budget(category: str, month: str, amount: float):
    """Set a monthly budget for a category and see remaining balance. month format: YYYY-MM."""
    async with get_db() as db:
        await db.execute(
            """INSERT INTO budgets(category, month, amount) VALUES (?, ?, ?)
            ON CONFLICT(category, month) DO UPDATE SET amount = excluded.amount""",
            (category, month, amount)
        )
        await db.commit()
        cur = await db.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM expenses
            WHERE LOWER(category) = LOWER(?) AND date BETWEEN ? AND ?""",
            (category, f"{month}-01", f"{month}-31")
        )
        spent = (await cur.fetchone())[0]
        return {
            "status": "ok", "category": category, "month": month,
            "budget": amount, "spent": spent,
            "remaining": amount - spent, "over_budget": spent > amount
        }


@mcp.tool()
async def get_budget_status(month: str):
    """Get budget vs actual spending for all categories in a given month. month format: YYYY-MM."""
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM budgets WHERE month = ?", (month,))
        budgets = await cur.fetchall()
        cur = await db.execute(
            """SELECT LOWER(category), SUM(amount)
            FROM expenses WHERE date BETWEEN ? AND ?
            GROUP BY LOWER(category)""",
            (f"{month}-01", f"{month}-31")
        )
        spend_map = dict(await cur.fetchall())
        report = []
        for _, category, _, budget_amount in budgets:
            spent = spend_map.get(category.lower(), 0)
            report.append({
                "category": category, "budget": budget_amount,
                "spent": spent, "remaining": budget_amount - spent,
                "over_budget": spent > budget_amount
            })
        return {"month": month, "report": report}


# ── Recurring ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def add_recurring(amount: float, category: str, frequency: str,
                        start_date: str, subcategory: str = "", note: str = ""):
    """Register a recurring expense. frequency: 'daily', 'weekly', or 'monthly'."""
    if frequency not in ("daily", "weekly", "monthly"):
        return {"status": "error",
                "message": "frequency must be 'daily', 'weekly', or 'monthly'"}
    async with get_db() as db:
        cur = await db.execute(
            """INSERT INTO recurring(amount, category, subcategory, note, frequency, next_date)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (amount, category, subcategory, note, frequency, start_date)
        )
        await db.commit()
        return {"status": "ok", "id": cur.lastrowid, "amount": amount,
                "category": category, "frequency": frequency, "next_date": start_date}


@mcp.tool()
async def process_recurring():
    """Log all due recurring expenses and advance their next_date. Call this daily."""
    today = date.today().isoformat()
    logged = []
    async with get_db() as db:
        cur = await db.execute(
            "SELECT * FROM recurring WHERE active = 1 AND next_date <= ?", (today,)
        )
        due = await cur.fetchall()

        for row in due:
            rid, amount, category, subcategory, note, frequency, next_date, _ = row
            await db.execute(
                """INSERT INTO expenses(date, amount, category, subcategory, note)
                VALUES (?, ?, ?, ?, ?)""",
                (next_date, amount, category, subcategory,
                f"{note} (recurring)".strip())
            )
            nd = date.fromisoformat(next_date)
            if frequency == "daily":
                nd += timedelta(days=1)
            elif frequency == "weekly":
                nd += timedelta(weeks=1)
            elif frequency == "monthly":
                nd += relativedelta(months=1)

            next_str = nd.isoformat()
            await db.execute(
                "UPDATE recurring SET next_date = ? WHERE id = ?", (next_str, rid)
            )
            logged.append({"recurring_id": rid, "note": note, "amount": amount,
                        "logged_date": next_date, "next_date": next_str})

        await db.commit()

    return {"status": "ok", "processed": len(logged), "entries": logged}


# ── Income & balance ──────────────────────────────────────────────────────────

@mcp.tool()
async def add_income(date: str, amount: float, source: str = "", note: str = ""):
    """Log an income entry (salary, freelance, etc.)."""
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO income(date, amount, source, note) VALUES (?, ?, ?, ?)",
            (date, amount, source, note)
        )
        await db.commit()
        return {"status": "ok", "id": cur.lastrowid}


@mcp.tool()
async def get_balance(start_date: str, end_date: str):
    """Compare total income vs expenses and return the net balance for a period."""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM income WHERE date BETWEEN ? AND ?",
            (start_date, end_date)
        )
        income = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE date BETWEEN ? AND ?",
            (start_date, end_date)
        )
        expenses = (await cur.fetchone())[0]

        net = income - expenses
        return {
            "period": {"start_date": start_date, "end_date": end_date},
            "income": income, "expenses": expenses,
            "net_balance": net,
            "status": "surplus" if net >= 0 else "deficit"
        }


# ── Resource ──────────────────────────────────────────────────────────────────

@mcp.resource("expense://categories", mime_type="application/json")
def categories():
    """Read categories from file, fall back to defaults if file not found."""
    default_categories = {
        "categories": [
            "Food & Dining", "Transportation", "Shopping",
            "Entertainment", "Bills & Utilities", "Healthcare",
            "Travel", "Education", "Business", "Other"
        ]
    }
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return json.dumps(default_categories, indent=2)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)