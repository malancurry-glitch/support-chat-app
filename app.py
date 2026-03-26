from flask import Flask, render_template, request, redirect, url_for, jsonify, session, send_from_directory, abort
from flask_socketio import SocketIO, emit, join_room
import sqlite3
import datetime
import os
import requests
import logging
import random

from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

# ---------------- LOAD ENV ----------------
load_dotenv()

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ---------------- GLOBAL STATE ----------------
TRANSFER_STATE = {}
agent_workload = {}

# 🔥 ALWAYS USE LOWERCASE KEYS (IMPORTANT)
AGENT_CHAT_MAP = {
    "monkeyleft": "8363465972",
    "yash220419955": "7664954283",
    "mhfggsx": "7689530513",
    "mate_him": "5993053888",
}

ONLINE_AGENTS = set()
TICKET_PRIORITY = {}
TICKET_TAGS = {}
INTERNAL_NOTES = {}

# ---------------- APP ----------------
app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

socketio = SocketIO(app, cors_allowed_origins="*")

# ---------------- FILE SYSTEM ----------------
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

ALLOWED_EXTENSIONS = {
    'png','jpg','jpeg','gif','webp',
    'mp4','webm',
    'pdf','txt'
}

# ---------------- HELPERS ----------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS


def get_db():
    conn = sqlite3.connect('database.db', timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def debug_log(title, data=None):
    logging.debug(f"\n🔥 {title}")
    if data:
        logging.debug(data)

# ---------------- INIT DB ----------------
def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''
    CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
        name TEXT,
        email TEXT,
        subject TEXT,
        priority TEXT,
        status TEXT,
        assigned_to TEXT,
        created_at TEXT
    )''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id TEXT,
        sender TEXT,
        message TEXT,
        timestamp TEXT
    )''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT
    )''')

    conn.commit()
    conn.close()

init_db()

# ---------------- TICKET ID ----------------
def generate_ticket_id():
    conn = get_db()
    c = conn.cursor()

    while True:
        ticket_id = str(random.randint(100000, 999999))

        c.execute("SELECT id FROM tickets WHERE id=?", (ticket_id,))
        if not c.fetchone():
            break

    conn.close()
    return ticket_id


# ---------------- TELEGRAM SEND ----------------
def send_telegram(text, ticket_id=None):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")

        conn = get_db()
        c = conn.cursor()

        assigned = None
        if ticket_id:
            c.execute("SELECT assigned_to FROM tickets WHERE id=?", (ticket_id,))
            row = c.fetchone()
            assigned = row["assigned_to"] if row else None

        conn.close()

        # 🔥 BEFORE ASSIGNMENT → SEND TO ALL AGENTS
        if not assigned:
            chat_ids = os.getenv("TELEGRAM_CHAT_IDS", "").split(",")

            for chat_id in chat_ids:
                chat_id = chat_id.strip()
                if not chat_id:
                    continue

                res = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=10
                )

                print("📤 TELEGRAM ALL:", chat_id, res.status_code, res.text)

        # 🔥 AFTER ASSIGNMENT → SEND ONLY TO ASSIGNED AGENT
        else:
            chat_id = AGENT_CHAT_MAP.get(assigned)

            if chat_id:
                res = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                    timeout=10
                )

                print("📤 TELEGRAM ASSIGNED:", assigned, res.status_code, res.text)
            else:
                print("❌ Assigned agent has no chat_id:", assigned)

    except Exception as e:
        print("❌ Telegram send error:", e)


# ---------------- AUTO ASSIGN LEAST BUSY ----------------
def get_least_busy_agent():
    if not agent_workload:
        return None

    return min(agent_workload, key=agent_workload.get)

# ---------------- TELEGRAM SEND WITH BUTTONS ----------------
def send_telegram_with_buttons(text, ticket_id):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_ids = os.getenv("TELEGRAM_CHAT_IDS", "").split(",")

        buttons = {
            "inline_keyboard": [
                [
                    {"text": "🟢 Claim", "callback_data": f"claim_{ticket_id}"},
                    {"text": "🔁 Transfer", "callback_data": f"transfer_{ticket_id}"}
                ],
                [
                    {"text": "🔒 Close", "callback_data": f"close_{ticket_id}"},
                    {"text": "🔥 Priority", "callback_data": f"priority_{ticket_id}"}
                ],
                [
                    {"text": "📝 Note", "callback_data": f"note_{ticket_id}"},
                    {"text": "🤖 AI Reply", "callback_data": f"ai_{ticket_id}"}
                ]
            ]
        }

        for chat_id in chat_ids:
            chat_id = chat_id.strip()
            if not chat_id:
                continue

            res = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "reply_markup": buttons
                },
                timeout=10
            )

            print("📤 BUTTON:", chat_id, res.status_code, res.text)

    except Exception as e:
        print("❌ BUTTON ERROR:", e)


# ---------------- DOWNLOAD TELEGRAM FILE ----------------
def download_telegram_file(file_id):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")

        file_info = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
            timeout=10
        ).json()

        if not file_info.get("ok"):
            print("❌ FILE INFO ERROR:", file_info)
            return None

        file_path = file_info["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"

        content = requests.get(file_url, timeout=10).content
        filename = file_path.split("/")[-1]

        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        with open(save_path, "wb") as f:
            f.write(content)

        print("📥 FILE DOWNLOADED:", filename)
        return filename

    except Exception as e:
        print("❌ FILE DOWNLOAD ERROR:", e)
        return None







def send_telegram_file(file_path, ticket_id, name=None, email=None):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")

        conn = get_db()
        c = conn.cursor()

        assigned = None
        if ticket_id:
            c.execute("SELECT assigned_to FROM tickets WHERE id=?", (ticket_id,))
            row = c.fetchone()
            assigned = row["assigned_to"] if row else None

        conn.close()

        ext = file_path.split(".")[-1].lower()

        if ext in ["jpg","jpeg","png","gif","webp"]:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            key = "photo"
        elif ext in ["mp4","webm","ogg"]:
            url = f"https://api.telegram.org/bot{token}/sendVideo"
            key = "video"
        else:
            url = f"https://api.telegram.org/bot{token}/sendDocument"
            key = "document"

        caption = f"📎 Ticket #{ticket_id}"

        # 🔥 BEFORE ASSIGNMENT → ALL AGENTS
        if not assigned:
            chat_ids = os.getenv("TELEGRAM_CHAT_IDS", "").split(",")

            for chat_id in chat_ids:
                chat_id = chat_id.strip()
                if not chat_id:
                    continue

                with open(file_path, "rb") as f:
                    res = requests.post(
                        url,
                        data={"chat_id": chat_id, "caption": caption},
                        files={key: f},
                        timeout=15
                    )

                    print("📤 FILE ALL:", chat_id, res.status_code)

        # 🔥 AFTER ASSIGNMENT → ONLY ASSIGNED
        else:
            chat_id = AGENT_CHAT_MAP.get(assigned)

            if chat_id:
                with open(file_path, "rb") as f:
                    res = requests.post(
                        url,
                        data={"chat_id": chat_id, "caption": caption},
                        files={key: f},
                        timeout=15
                    )

                    print("📤 FILE ASSIGNED:", assigned, res.status_code)
            else:
                print("❌ No chat_id for assigned agent:", assigned)

    except Exception as e:
        print("❌ TELEGRAM FILE ERROR:", e)




# ---------------- TELEGRAM RECEIVE ----------------
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        import re

        data = request.get_json(force=True)

        # ---------------- BUTTON CLICK ----------------
        if "callback_query" in data:
            query = data["callback_query"]
            action = query["data"]

            user = query["from"]
            agent = (user.get("username") or user.get("first_name")).lower()
            chat_id = str(user["id"])

            AGENT_CHAT_MAP[agent] = chat_id
            ONLINE_AGENTS.add(agent)   # 🔥 presence tracking

            conn = get_db()
            c = conn.cursor()

            # ---------------- CLAIM ----------------
            if action.startswith("claim_"):
                ticket_id = action.replace("claim_", "")

                c.execute("SELECT assigned_to FROM tickets WHERE id=?", (ticket_id,))
                row = c.fetchone()

                if row and row["assigned_to"]:
                    send_telegram(f"❌ Already assigned to {row['assigned_to']}")
                else:
                    # 🔥 AUTO ASSIGN LOGIC (Zendesk style)
                    c.execute("UPDATE tickets SET assigned_to=? WHERE id=?", (agent, ticket_id))
                    conn.commit()

                    agent_workload[agent] = agent_workload.get(agent, 0) + 1

                    socketio.emit("assigned", {
                        "ticket_id": ticket_id,
                        "agent": agent
                    }, room=ticket_id)

                    send_telegram(f"✅ You are now assigned to ticket #{ticket_id}", ticket_id)

                conn.close()
                return "ok"

            # ---------------- TRANSFER ----------------
            if action.startswith("transfer_"):
                ticket_id = action.replace("transfer_", "")

                socketio.emit("agent_transferring", {
                    "ticket_id": ticket_id,
                    "from": agent
                }, room=ticket_id)

                buttons = {"inline_keyboard": []}

                for a in AGENT_CHAT_MAP.keys():
                    if a == agent:
                        continue

                    status = "🟢" if a in ONLINE_AGENTS else "⚫"
                    load = agent_workload.get(a, 0)

                    buttons["inline_keyboard"].append([
                        {"text": f"{status} {a} ({load})", "callback_data": f"transfer_to|{ticket_id}|{a}"}
                    ])

                requests.post(
                    f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"🔁 Select agent to transfer ticket #{ticket_id}",
                        "reply_markup": buttons
                    }
                )

                return "ok"

            # ---------------- TRANSFER SELECT ----------------
            if action.startswith("transfer_to|"):
                _, ticket_id, new_agent = action.split("|")

                c.execute("SELECT assigned_to FROM tickets WHERE id=?", (ticket_id,))
                row = c.fetchone()
                old_agent = row["assigned_to"]

                if new_agent == old_agent:
                    send_telegram("❌ Already assigned")
                    return "ok"

                c.execute("UPDATE tickets SET assigned_to=? WHERE id=?", (new_agent, ticket_id))
                conn.commit()

                agent_workload[old_agent] = max(0, agent_workload.get(old_agent, 1) - 1)
                agent_workload[new_agent] = agent_workload.get(new_agent, 0) + 1

                # 🔥 notify both
                send_telegram(f"📩 Assigned to #{ticket_id}", ticket_id)
                send_telegram(f"ℹ️ Ticket transferred to {new_agent}", ticket_id)

                socketio.emit("agent_transfer", {
                    "ticket_id": ticket_id,
                    "to": new_agent
                }, room=ticket_id)

                conn.close()
                return "ok"

            # ---------------- PRIORITY ----------------
            if action.startswith("priority_"):
                ticket_id = action.split("_")[1]
                TICKET_PRIORITY[ticket_id] = "High"
                send_telegram(f"🔥 Priority set to HIGH for #{ticket_id}")
                return "ok"

            # ---------------- AI REPLY ----------------
            if action.startswith("ai_"):
                ticket_id = action.split("_")[1]
                send_telegram(f"🤖 Suggested reply:\nHello, we are reviewing your issue.", ticket_id)
                return "ok"

            # ---------------- CLOSE ----------------
            if action.startswith("close_"):
                ticket_id = action.replace("close_", "")

                c.execute("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
                conn.commit()

                socketio.emit("ticket_closed", {}, room=ticket_id)

                send_telegram(f"🔒 Ticket #{ticket_id} closed")

                conn.close()
                return "ok"

        # ---------------- NORMAL MESSAGE ----------------
        msg_obj = data.get("message")
        if not msg_obj:
            return "ok"

        now = datetime.datetime.now().strftime('%H:%M')

        user = msg_obj.get("from", {})
        agent = (user.get("username") or user.get("first_name")).lower()
        chat_id = str(msg_obj.get("chat", {}).get("id"))

        AGENT_CHAT_MAP[agent] = chat_id
        ONLINE_AGENTS.add(agent)

        conn = get_db()
        c = conn.cursor()

        # ---------------- TAG AUTO DETECTION ----------------
        text = msg_obj.get("text", "").lower()
        if "refund" in text:
            TICKET_TAGS.setdefault(ticket_id, []).append("refund")

        # ---------------- TEXT ----------------
        match = re.match(r"#?\s*(\d+)\s*:\s*(.+)", text)
        if not match:
            send_telegram("❌ Use: #123456: message")
            return "ok"

        ticket_id = match.group(1)
        msg = match.group(2)

        c.execute("SELECT assigned_to FROM tickets WHERE id=?", (ticket_id,))
        row = c.fetchone()

        if row["assigned_to"] != agent:
            send_telegram(f"❌ Assigned to {row['assigned_to']}")
            return "ok"

        c.execute("INSERT INTO messages VALUES (NULL,?,?,?,?)",
                  (ticket_id, "admin", msg, now))

        conn.commit()
        conn.close()

        socketio.emit("new_message", {
            "ticket_id": ticket_id,
            "message": msg,
            "sender": "admin",
            "time": now,
            "agent": agent
        }, room=ticket_id)

    except Exception as e:
        print("❌ TELEGRAM ERROR:", e)

    return "ok"


# ---------------- CREATE ADMIN ----------------
@app.route('/create-admin')
def create_admin():
    conn = get_db()
    c = conn.cursor()

    c.execute(
        "INSERT INTO users (username,password,role) VALUES (?,?,?)",
        ("admin", generate_password_hash("admin123"), "admin")
    )

    conn.commit()
    conn.close()

    return "Admin created"

# ---------------- FILE ROUTES ----------------
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)


# ---------------- CREATE TICKET ----------------
@app.route('/', methods=['GET','POST'])
def create_ticket():
    if request.method == 'POST':

        name = request.form.get('name')
        email = request.form.get('email')
        subject = request.form.get('subject')
        message = request.form.get('message')
        file = request.files.get('file')

        ticket_id = generate_ticket_id()
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = get_db()
        c = conn.cursor()

        # CREATE TICKET
        c.execute(
            "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, name, email, subject, "Medium", "open", None, now)
        )

        # SAVE MESSAGE
        c.execute(
            "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
            (ticket_id, "user", message, now)
        )

        # ---------------- FILE ----------------
        if file and file.filename:
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)

            c.execute(
                "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
                (ticket_id, "user", f"[FILE] {filename}", now)
            )

            try:
                send_telegram_file(file_path, ticket_id, name, email)
            except Exception as e:
                print("File send error:", e)

        conn.commit()
        conn.close()

        # 🔥 IMPORTANT: SEND WITH BUTTONS (NOT NORMAL MESSAGE)
        send_telegram_with_buttons(f"""
🚨 New Ticket

ID: {ticket_id}
Name: {name}
Email: {email}
Subject: {subject}

{message}
""", ticket_id)

        return redirect(url_for('view_ticket', ticket_id=ticket_id))

    return render_template('create_ticket.html')


@app.route('/ticket/<ticket_id>')
def view_ticket(ticket_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT sender,message,timestamp FROM messages WHERE ticket_id=?", (ticket_id,))
    messages = c.fetchall()

    conn.close()

    return render_template('ticket.html', messages=messages, ticket_id=ticket_id)




@app.route('/upload/<ticket_id>', methods=['POST'])
def upload_file(ticket_id):
    file = request.files.get('file')

    if not file:
        return {"error": "No file"}, 400

    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    c = conn.cursor()

    c.execute(
        "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
        (ticket_id, "user", f"[FILE] {filename}", now)
    )

    conn.commit()
    conn.close()

    # 🔥 FIX: SEND TO UI INSTANTLY
    socketio.emit('new_message', {
        "ticket_id": ticket_id,
        "message": f"[FILE] {filename}",
        "sender": "user",
        "time": now,
        "agent": "User"
    }, room=ticket_id)

    send_telegram_file(file_path, ticket_id, email="User")

    return {"status": "ok"}


@app.route('/close/<ticket_id>')
def close_ticket(ticket_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT assigned_to FROM tickets WHERE id=?", (ticket_id,))
    row = c.fetchone()

    if row and row["assigned_to"]:
        agent = row["assigned_to"]
        agent_workload[agent] = max(0, agent_workload.get(agent, 1) - 1)

    c.execute("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
    conn.commit()
    conn.close()

    socketio.emit("ticket_closed", {}, room=ticket_id)

    return "ok"

@app.route('/open/<ticket_id>')
def open_ticket(ticket_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("UPDATE tickets SET status='open' WHERE id=?", (ticket_id,))
    conn.commit()
    conn.close()

    return "ok"




@app.route('/delete/<ticket_id>')
def delete_ticket(ticket_id):
    conn=get_db()
    c=conn.cursor()
    c.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))
    c.execute("DELETE FROM messages WHERE ticket_id=?", (ticket_id,))
    conn.commit()
    conn.close()
    return "ok"


@app.route('/assign/<ticket_id>', methods=['POST'])
def assign(ticket_id):
    agent = request.json.get("agent")
    conn=get_db()
    c=conn.cursor()
    c.execute("UPDATE tickets SET assigned_to=? WHERE id=?", (agent,ticket_id))
    conn.commit()
    conn.close()
    return "ok"


@app.route('/ai/<ticket_id>')
def ai_reply(ticket_id):
    return jsonify({"reply":"Hello, we are reviewing your issue."})


@app.route('/api/history/<ticket_id>')
def history(ticket_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT sender, message, timestamp FROM messages WHERE ticket_id=?", (ticket_id,))
    data = [
        {"sender": r[0], "message": r[1], "time": r[2]}
        for r in c.fetchall()
    ]

    conn.close()
    return jsonify(data)




@app.route('/my-tickets', methods=['GET','POST'])
def my_tickets():
    if request.method == 'POST':
        email = request.form.get('email')

        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT id, subject, status, created_at FROM tickets WHERE email=? ORDER BY created_at DESC", (email,))
        tickets = c.fetchall()

        conn.close()

        return render_template('my_tickets.html', tickets=tickets, email=email)

    return render_template('my_tickets.html', tickets=None)



@app.route('/admin')
def admin_dashboard():
    if "admin" not in session:
        return redirect("/admin-login")

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM tickets ORDER BY created_at DESC")
    tickets = c.fetchall()

    conn.close()

    return render_template('admin.html', tickets=tickets)


@app.route('/admin-login', methods=['GET','POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT * FROM users WHERE username=?", (username,))
        user = c.fetchone()

        conn.close()

        if user and check_password_hash(user["password"], password):
            session["admin"] = username
            session["role"] = user["role"]
            return redirect("/admin")

        return "Invalid login"

    return render_template("admin_login.html")


@app.route('/logout')
def logout():
    session.clear()
    return redirect("/admin-login")


@app.route('/admin/stats')
def admin_stats():
    if "admin" not in session:
        return jsonify({"error": "Unauthorized"}), 403

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM tickets")
    total = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM tickets WHERE status='open'")
    open_t = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM tickets WHERE status='closed'")
    closed_t = c.fetchone()[0]

    conn.close()

    return jsonify({
        "total": total,
        "open": open_t,
        "closed": closed_t
    })




# ---------------- SOCKET ----------------
@socketio.on('send_message')
def handle_message(data):
    try:
        debug_log("SOCKET INPUT", data)

        now = datetime.datetime.now().strftime('%H:%M')

        ticket_id = str(data.get('ticket_id', '')).strip()
        sender = data.get('sender')
        message = (data.get('message') or "").strip()

        if not ticket_id or not message:
            debug_log("INVALID MESSAGE", {"ticket_id": ticket_id, "message": message})
            return

        conn = get_db()
        c = conn.cursor()

        # 🔥 CHECK TICKET EXISTS
        c.execute("SELECT assigned_to FROM tickets WHERE id=?", (ticket_id,))
        row = c.fetchone()

        if not row:
            debug_log("TICKET NOT FOUND", ticket_id)
            return

        assigned_to = row["assigned_to"]

        # 🔥 SAFE AGENT NAME
        agent_name = (session.get("admin") or "").lower()

        # 🔥 TRACK ONLINE AGENT
        if agent_name:
            ONLINE_AGENTS.add(agent_name)

        # ---------------- ADMIN MESSAGE ----------------
        if sender == "admin":

            if not agent_name:
                debug_log("NO ADMIN SESSION")
                return

            # 🔥 AUTO ASSIGN FIRST RESPONDER
            if not assigned_to:
                assigned_to = agent_name

                c.execute(
                    "UPDATE tickets SET assigned_to=? WHERE id=?",
                    (agent_name, ticket_id)
                )

                # 🔥 TRACK WORKLOAD
                agent_workload[agent_name] = agent_workload.get(agent_name, 0) + 1

                debug_log("AUTO ASSIGNED", agent_name)

                socketio.emit("assigned", {
                    "ticket_id": ticket_id,
                    "agent": agent_name
                }, room=ticket_id)

                # 🔥 NOTIFY TELEGRAM AGENT
                chat_id = AGENT_CHAT_MAP.get(agent_name)
                if chat_id:
                    requests.post(
                        f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": f"📩 You are now assigned to ticket #{ticket_id}"
                        }
                    )

            # 🔥 LOCK SYSTEM
            elif assigned_to != agent_name:
                debug_log("BLOCKED MESSAGE", {
                    "assigned_to": assigned_to,
                    "attempted": agent_name
                })

                socketio.emit("blocked", {
                    "message": f"This ticket is assigned to {assigned_to}"
                })
                return

        # ---------------- SAVE MESSAGE ----------------
        c.execute(
            "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
            (ticket_id, sender, message, now)
        )

        conn.commit()
        conn.close()

        debug_log("MESSAGE SAVED", {
            "ticket_id": ticket_id,
            "sender": sender,
            "message": message
        })

        # ---------------- TELEGRAM SEND ----------------
        try:
            if sender == "admin":
                send_telegram(f"""
💬 Message

Ticket: #{ticket_id}
From: {agent_name}

{message}
""", ticket_id)
            else:
                # 🔥 USER MESSAGE → notify assigned agent
                send_telegram(f"""
📩 New Customer Message

Ticket: #{ticket_id}

{message}
""", ticket_id)

        except Exception as tg_error:
            logging.error("❌ TELEGRAM SEND FAILED")
            logging.exception(tg_error)

        # ---------------- REALTIME ----------------
        socketio.emit('new_message', {
            "ticket_id": ticket_id,
            "message": message,
            "sender": sender,
            "time": now,
            "agent": agent_name or "user"
        }, room=ticket_id)

    except Exception as e:
        logging.error("❌ SOCKET ERROR")
        logging.exception(e)


# ---------------- AGENT JOIN ----------------
@socketio.on('agent_join')
def agent_join(data):
    try:
        ticket_id = str(data.get('ticket_id', '')).strip()
        if not ticket_id:
            return

        agent_name = (session.get("admin") or "").lower()
        if not agent_name:
            return

        ONLINE_AGENTS.add(agent_name)

        now = datetime.datetime.now().strftime('%d %b %Y, %I:%M %p')

        socketio.emit('agent_join', {
            "agent": agent_name,
            "time": now
        }, room=ticket_id)

    except Exception as e:
        logging.exception("❌ agent_join error")


# ---------------- AGENT LEAVE ----------------
@socketio.on('agent_leave')
def agent_leave(data):
    try:
        ticket_id = str(data.get('ticket_id', '')).strip()
        if not ticket_id:
            return

        agent_name = (session.get("admin") or "").lower()
        if not agent_name:
            return

        ONLINE_AGENTS.discard(agent_name)

        now = datetime.datetime.now().strftime('%d %b %Y, %I:%M %p')

        socketio.emit('agent_leave', {
            "agent": agent_name,
            "time": now
        }, room=ticket_id)

    except Exception as e:
        logging.exception("❌ agent_leave error")


# ---------------- AGENT TRANSFER ----------------
@socketio.on("agent_transfer")
def agent_transfer(data):
    try:
        ticket_id = data.get("ticket_id")
        new_agent = (data.get("to") or "").lower()

        if not ticket_id or not new_agent:
            return

        # 🔥 GET CURRENT ASSIGNED AGENT FROM DB
        conn = get_db()
        c = conn.cursor()

        c.execute("SELECT assigned_to FROM tickets WHERE id=?", (ticket_id,))
        row = c.fetchone()
        old_agent = row["assigned_to"] if row else None

        # 🔥 UPDATE DB
        c.execute("UPDATE tickets SET assigned_to=? WHERE id=?", (new_agent, ticket_id))
        conn.commit()
        conn.close()

        # 🔥 FIXED WORKLOAD LOGIC
        if old_agent:
            agent_workload[old_agent] = max(0, agent_workload.get(old_agent, 1) - 1)

        agent_workload[new_agent] = agent_workload.get(new_agent, 0) + 1

        # 🔥 REALTIME UI UPDATE
        socketio.emit("agent_transfer", {
            "ticket_id": ticket_id,
            "to": new_agent
        }, room=ticket_id)

    except Exception as e:
        logging.exception("❌ agent_transfer error")


# ---------------- DISCONNECT ----------------
@socketio.on('disconnect')
def handle_disconnect():
    try:
        if "admin" in session:
            agent_name = (session.get("admin") or "").lower()

            if agent_name:
                ONLINE_AGENTS.discard(agent_name)

                now = datetime.datetime.now().strftime('%d %b %Y, %I:%M %p')

                socketio.emit('agent_leave', {
                    "agent": agent_name,
                    "time": now
                })

    except Exception as e:
        logging.exception("❌ disconnect error")

# ---------------- JOIN ROOM ----------------
@socketio.on('join_ticket')
def join_ticket(data):
    try:
        ticket_id = str(data.get('ticket_id', '')).strip()
        if not ticket_id:
            return

        join_room(ticket_id)

        print(f"Joined room {ticket_id}")

        if "admin" in session:
            agent_name = (session.get("admin") or "").lower()

            if agent_name:
                ONLINE_AGENTS.add(agent_name)

                now = datetime.datetime.now().strftime('%d %b %Y, %I:%M %p')

                socketio.emit('agent_join', {
                    "agent": agent_name,
                    "time": now
                }, room=ticket_id)

    except Exception as e:
        logging.exception("❌ join_ticket error")

# ---------------- TYPING ----------------
@socketio.on('typing')
def typing(data):
    try:
        ticket_id = str(data.get('ticket_id', '')).strip()
        if not ticket_id:
            return

        agent_name = (session.get("admin") or "").lower()

        if not agent_name:
            return

        socketio.emit('typing', {
            "agent": agent_name
        }, room=ticket_id, include_self=False)

    except Exception as e:
        logging.exception("❌ typing error")


# ---------------- SEEN ----------------
@socketio.on('seen')
def seen(data):
    try:
        ticket_id = str(data.get('ticket_id', '')).strip()
        if not ticket_id:
            return

        socketio.emit('seen', {
            "ticket_id": ticket_id
        }, room=ticket_id)

    except Exception as e:
        logging.exception("❌ seen error")


@socketio.on_error_default
def default_error_handler(e):
    print("Socket error:", e)



# ---------------- RUN ----------------
if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))