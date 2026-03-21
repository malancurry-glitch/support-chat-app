from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
import sqlite3
import datetime
import os
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

socketio = SocketIO(app, cors_allowed_origins="*")


# ---------------- DATABASE ----------------
def get_db():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
        email TEXT,
        subject TEXT,
        priority TEXT,
        status TEXT,
        assigned_to TEXT,
        created_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id TEXT,
        sender TEXT,
        message TEXT,
        timestamp TEXT
    )''')

    conn.commit()
    conn.close()

init_db()


# ---------------- TELEGRAM ----------------
def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = os.getenv("TELEGRAM_CHAT_IDS")

    print("TOKEN:", token)
    print("CHAT_IDS:", chat_ids)

    if not token or not chat_ids:
        print("❌ TELEGRAM CONFIG MISSING")
        return

    for chat_id in chat_ids.split(","):
        chat_id = chat_id.strip()

        url = f"https://api.telegram.org/bot{token}/sendMessage"

        res = requests.post(url, json={
            "chat_id": chat_id,
            "text": text
        })

        print("📤 TELEGRAM:", res.text)


# ---------------- TELEGRAM WEBHOOK ----------------
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.get_json(force=True)
        print("📩 TELEGRAM RECEIVED:", data)

        if not data:
            return "ok"

        msg = data.get("message") or data.get("edited_message")
        if not msg:
            return "ok"

        text = msg.get("text", "").strip()
        print("TEXT:", text)

        # CLOSE
        if text.lower().startswith("close "):
            ticket_id = text.replace("close ", "").strip()

            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
            conn.commit()
            conn.close()

            send_telegram(f"🔒 Closed {ticket_id}")
            return "ok"

        # OPEN
        if text.lower().startswith("open "):
            ticket_id = text.replace("open ", "").strip()

            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE tickets SET status='open' WHERE id=?", (ticket_id,))
            conn.commit()
            conn.close()

            send_telegram(f"🟢 Opened {ticket_id}")
            return "ok"

        # REPLY
        if ":" not in text:
            send_telegram("❌ Use format:\nSUP-1001: message")
            return "ok"

        ticket_id, message = text.split(":", 1)
        ticket_id = ticket_id.strip()
        message = message.strip()

        now = datetime.datetime.now().strftime('%H:%M')

        conn = get_db()
        c = conn.cursor()

        c.execute(
            "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
            (ticket_id, "admin", message, now)
        )

        conn.commit()
        conn.close()

        socketio.emit('new_message', {
            "ticket_id": ticket_id,
            "message": message,
            "sender": "admin",
            "time": now
        })

        send_telegram(f"💬 Sent to {ticket_id}")

    except Exception as e:
        print("❌ TELEGRAM ERROR:", e)

    return "ok"


# ---------------- MAIN ----------------
@app.route('/', methods=['GET','POST'])
def create_ticket():
    if request.method == 'POST':
        print("FORM:", request.form)

        email = request.form.get('email')
        subject = request.form.get('subject')
        message = request.form.get('message')

        if not email or not subject or not message:
            return "Missing fields", 400

        ticket_id = "SUP-" + str(int(datetime.datetime.now().timestamp()))
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = get_db()
        c = conn.cursor()

        c.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (ticket_id, email, subject, "Medium", "open", None, now))

        c.execute("INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
                  (ticket_id, "user", message, now))

        conn.commit()
        conn.close()

        send_telegram(f"🚨 NEW TICKET\n{ticket_id}\n{message}")

        return redirect(url_for('view_ticket', ticket_id=ticket_id))

    return render_template('create_ticket.html')


# ---------------- VIEW ----------------
@app.route('/ticket/<ticket_id>')
def view_ticket(ticket_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT sender,message,timestamp FROM messages WHERE ticket_id=?", (ticket_id,))
    messages = c.fetchall()

    conn.close()

    return render_template('ticket.html', messages=messages, ticket_id=ticket_id)


@app.route('/admin')
def admin_dashboard():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM tickets ORDER BY created_at DESC")
    tickets = c.fetchall()

    conn.close()

    return render_template('admin.html', tickets=tickets)


# ---------------- SOCKET ----------------
@socketio.on('send_message')
def handle_message(data):
    now = datetime.datetime.now().strftime('%H:%M')

    conn = get_db()
    c = conn.cursor()

    c.execute("INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
              (data['ticket_id'], data['sender'], data['message'], now))

    conn.commit()
    conn.close()

    send_telegram(f"💬 {data['ticket_id']} ({data['sender']}): {data['message']}")

    emit('new_message', data, broadcast=True)


# ---------------- RUN ----------------
if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))