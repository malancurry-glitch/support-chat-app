from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, abort
from flask_socketio import SocketIO, emit
import sqlite3
import datetime
import os
import requests
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

socketio = SocketIO(app, cors_allowed_origins="*")

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

ALLOWED_EXTENSIONS = {'png','jpg','jpeg','gif','webp','mp4','webm','pdf','txt'}


# ---------------- HELPERS ----------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS


def get_db():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn


def generate_ticket_id():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT id FROM tickets ORDER BY rowid DESC LIMIT 1")
    last = c.fetchone()

    if last:
        try:
            num = int(last["id"].replace("SUP-", ""))
        except:
            num = 1000
    else:
        num = 1000

    conn.close()
    return f"SUP-{num+1}"


# ---------------- TELEGRAM ----------------
def get_admins():
    ids = os.getenv("TELEGRAM_CHAT_IDS", "")
    return [i.strip() for i in ids.split(",") if i.strip()]


def send_telegram(text, buttons=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    for chat_id in get_admins():
        payload = {
            "chat_id": chat_id,
            "text": text
        }

        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}

        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)


# ---------------- TELEGRAM WEBHOOK ----------------
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    data = request.json

    print("📩 TELEGRAM:", data)

    # BUTTON CLICK
    if "callback_query" in data:
        cb = data["callback_query"]
        action = cb["data"]

        if action.startswith("close_"):
            ticket_id = action.replace("close_", "")

            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
            conn.commit()
            conn.close()

            send_telegram(f"✅ Ticket {ticket_id} closed")

        return "ok"

    # MESSAGE REPLY
    if "message" not in data:
        return "ok"

    text = data["message"].get("text", "")

    if ":" not in text:
        send_telegram("❌ Use format:\nSUP-1001: your message")
        return "ok"

    ticket_id, msg = text.split(":", 1)
    ticket_id = ticket_id.strip()
    msg = msg.strip()

    now = datetime.datetime.now().strftime('%H:%M')

    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
              (ticket_id, "admin", msg, now))
    conn.commit()
    conn.close()

    socketio.emit('new_message', {
        "ticket_id": ticket_id,
        "message": msg,
        "sender": "admin",
        "time": now
    })

    send_telegram(f"💬 Sent to {ticket_id}")

    return "ok"


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
        email = request.form.get('email')
        subject = request.form.get('subject')
        message = request.form.get('message')

        ticket_id = generate_ticket_id()
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = get_db()
        c = conn.cursor()

        c.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (ticket_id, email, subject, "Medium", "open", None, now))

        c.execute("INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
                  (ticket_id, "user", message, now))

        conn.commit()
        conn.close()

        # 🔥 TELEGRAM ALERT + BUTTONS
        send_telegram(
            f"🚨 New Ticket\n\nID: {ticket_id}\nUser: {email}\n{message}",
            buttons=[
                [{"text": "❌ Close", "callback_data": f"close_{ticket_id}"}]
            ]
        )

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

    emit('new_message', {
        "ticket_id": data['ticket_id'],
        "message": data['message'],
        "sender": data['sender'],
        "time": now
    }, broadcast=True)


# ---------------- RUN ----------------
if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=10000)