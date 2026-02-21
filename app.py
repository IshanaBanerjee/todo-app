from flask import Flask, render_template, request, redirect, session
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from flask import jsonify
import os
from authlib.integrations.flask_client import OAuth


app = Flask(__name__)

# only ONE secret key line
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

DB_NAME = "todo.db"

oauth = OAuth(app)

google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

from urllib.parse import urljoin
from flask import redirect, url_for, session, request

@app.route("/login/google")
def login_google():
    redirect_uri = url_for("auth_google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route("/auth/google/callback")
def auth_google_callback():
    token = google.authorize_access_token()
    userinfo = google.get("userinfo").json()

    email = userinfo.get("email")
    name = userinfo.get("name", "")

    # TODO: create/find user in DB using email
    # Example: store email in session for now
    session["user_id"] = email
    session["user_email"] = email
    session["user_name"] = name

    return redirect(url_for("dashboard"))



def db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )
    """)

    # category added
    cur.execute("""
    CREATE TABLE IF NOT EXISTS todos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        category TEXT DEFAULT 'Personal',
        due_date TEXT,
        is_done INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    conn.commit()
    conn.close()


def migrate_db():
    """Adds category column for users who already have todo.db created."""
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE todos ADD COLUMN category TEXT DEFAULT 'Personal'")
        conn.commit()
    except sqlite3.OperationalError:
        # Column already exists
        pass
    conn.close()


init_db()
migrate_db()


@app.route("/")
def home():
    return redirect("/dashboard") if "user_id" in session else redirect("/login")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = generate_password_hash(request.form["password"])

        try:
            conn = db()
            conn.execute("INSERT INTO users(username, password) VALUES (?,?)", (username, password))
            conn.commit()
            conn.close()
            return redirect("/login")
        except sqlite3.IntegrityError:
            return "Username already exists. Go back and try another."

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        conn = db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            return redirect("/dashboard")

        return "Invalid login. Try again."

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    # Category filter for tabs
    cat = request.args.get("cat", "All")
    allowed = {"All", "Work", "Personal", "Wishlist"}
    if cat not in allowed:
        cat = "All"

    # Add task
    if request.method == "POST":
        title = request.form["title"].strip()
        due_date = request.form.get("due_date") or None  # from datetime-local
        category = request.form.get("category", "Personal")

        if category not in {"Work", "Personal", "Wishlist"}:
            category = "Personal"

        # Convert "YYYY-MM-DDTHH:MM" -> "YYYY-MM-DD HH:MM" for nice sorting/comparison
        if due_date:
            due_date = due_date.replace("T", " ")

        conn = db()
        conn.execute(
            "INSERT INTO todos(user_id, title, category, due_date) VALUES (?,?,?,?)",
            (user_id, title, category, due_date)
        )
        conn.commit()
        conn.close()

        # Keep the current tab after adding
        return redirect(f"/dashboard?cat={cat}")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = db()

    # Build query based on selected tab
    base_query = """
        SELECT * FROM todos
        WHERE user_id=?
    """
    params = [user_id]

    if cat != "All":
        base_query += " AND category=?"
        params.append(cat)

    # Smart-ish sorting:
    # pending first, overdue pending first, then upcoming by due_date, then no due_date, completed last
    base_query += """
        ORDER BY
            is_done ASC,
            CASE
                WHEN is_done = 0 AND due_date IS NOT NULL AND due_date < ? THEN 0
                WHEN is_done = 0 AND due_date IS NOT NULL THEN 1
                WHEN is_done = 0 AND due_date IS NULL THEN 2
                ELSE 3
            END,
            due_date ASC,
            created_at DESC
    """
    params.append(now_str)

    todos = conn.execute(base_query, params).fetchall()
    conn.close()

    return render_template("dashboard.html", todos=todos, now=now_str, cat=cat, active_page="tasks")



@app.route("/toggle/<int:todo_id>", methods=["POST"])
def toggle(todo_id):
    if "user_id" not in session:
        return redirect("/login")
    user_id = session["user_id"]

    conn = db()
    conn.execute("""
        UPDATE todos
        SET is_done = CASE WHEN is_done=0 THEN 1 ELSE 0 END
        WHERE id=? AND user_id=?
    """, (todo_id, user_id))
    conn.commit()
    conn.close()

    return redirect(request.referrer or "/dashboard")

@app.route("/calendar-simple")
def calendar_simple():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    selected = request.args.get("date")  # YYYY-MM-DD

    conn = db()
    tasks = []
    if selected:
        tasks = conn.execute("""
            SELECT * FROM todos
            WHERE user_id=? AND due_date IS NOT NULL AND substr(due_date,1,10)=?
            ORDER BY due_date ASC
        """, (user_id, selected)).fetchall()
    conn.close()

    return render_template("calendar.html", selected=selected, tasks=tasks)

@app.route("/calendar")
def calendar():
    if "user_id" not in session:
        return redirect("/login")
    return render_template("calendar_ui.html", active_page="calendar")


@app.route("/delete/<int:todo_id>", methods=["POST"])
def delete(todo_id):
    if "user_id" not in session:
        return redirect("/login")
    user_id = session["user_id"]

    conn = db()
    conn.execute("DELETE FROM todos WHERE id=? AND user_id=?", (todo_id, user_id))
    conn.commit()
    conn.close()

    return redirect(request.referrer or "/dashboard")

@app.route("/api/events")
def api_events():
    if "user_id" not in session:
        return jsonify([])

    user_id = session["user_id"]

    conn = db()
    rows = conn.execute("""
        SELECT id, title, due_date, category, is_done
        FROM todos
        WHERE user_id=? AND due_date IS NOT NULL
    """, (user_id,)).fetchall()
    conn.close()

    events = []
    for r in rows:
        due = (r["due_date"] or "").strip()

        # If due date is missing, skip creating an event
        if not due:
            continue

        # Convert "YYYY-MM-DD HH:MM" -> "YYYY-MM-DDTHH:MM:00"
        if " " in due:
            date_part, time_part = due.split(" ", 1)
            start = f"{date_part}T{time_part}:00"
            all_day = False
        else:
            # Date-only (rare)
            start = due
            all_day = True

        events.append({
            "id": r["id"],
            "title": f"[{r['category']}] {r['title']}",
            "start": start,
            "allDay": all_day
        })

    return jsonify(events)

@app.route("/overview")
def overview():
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = db()

    # Completed count
    completed = conn.execute("""
        SELECT COUNT(*) as c
        FROM todos
        WHERE user_id=? AND is_done=1
    """, (user_id,)).fetchone()["c"]

    # Pending count
    pending = conn.execute("""
        SELECT COUNT(*) as c
        FROM todos
        WHERE user_id=? AND is_done=0
    """, (user_id,)).fetchone()["c"]

    # Next 7 days tasks
    next7 = conn.execute("""
        SELECT *
        FROM todos
        WHERE user_id=? 
          AND is_done=0
          AND due_date IS NOT NULL
          AND due_date >= ?
          AND due_date <= datetime(?, '+7 days')
        ORDER BY due_date ASC
    """, (user_id, now, now)).fetchall()

    # Pending tasks grouped by category
    rows = conn.execute("""
        SELECT category, COUNT(*) as cnt
        FROM todos
        WHERE user_id=? AND is_done=0
        GROUP BY category
    """, (user_id,)).fetchall()

    conn.close()

    labels = [r["category"] for r in rows]
    values = [r["cnt"] for r in rows]

    return render_template(
        "overview.html",
        completed=completed,
        pending=pending,
        next7=next7,
        labels=labels,
        values=values,
        active_page="overview"
    )

@app.route("/api/day")
def api_day():
    if "user_id" not in session:
        return jsonify([])

    user_id = session["user_id"]
    date_str = request.args.get("date")

    if not date_str:
        return jsonify([])

    conn = db()
    rows = conn.execute("""
        SELECT id, title, category, due_date, is_done
        FROM todos
        WHERE user_id=? AND substr(due_date,1,10)=?
        ORDER BY due_date ASC
    """, (user_id, date_str)).fetchall()
    conn.close()

    return jsonify([dict(r) for r in rows])

if __name__ == "__main__":
    app.run()

