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

    conn.commit()
    conn.close()

init_db()


# ---------------- TICKET ID ----------------
def generate_ticket_id():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT id FROM tickets ORDER BY rowid DESC LIMIT 1")
    last = c.fetchone()

    if last and last["id"]:
        try:
            num = int(last["id"].replace("SUP-", ""))
        except:
            num = 1000
    else:
        num = 1000

    conn.close()
    return f"SUP-{num+1}"

# ---------------- TELEGRAM SEND ----------------
def send_telegram(text):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not token or not chat_id:
            print("⚠️ Telegram not configured")
            return

        res = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            }
        )

        print("📤 TEXT SENT:", res.status_code)

    except Exception as e:
        print("❌ Telegram send error:", e)


# ---------------- TELEGRAM SEND WITH BUTTONS ----------------
def send_telegram_with_buttons(text, ticket_id):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not token or not chat_id:
            print("⚠️ Telegram not configured")
            return

        buttons = {
            "inline_keyboard": [
                [
                    {"text": "💬 Reply", "callback_data": f"reply_{ticket_id}"},
                    {"text": "🔒 Close", "callback_data": f"close_{ticket_id}"}
                ]
            ]
        }

        res = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "reply_markup": buttons
            }
        )

        print("📤 BUTTON MESSAGE SENT:", res.status_code)

    except Exception as e:
        print("❌ BUTTON ERROR:", e)


# ---------------- DOWNLOAD TELEGRAM FILE ----------------
def download_telegram_file(file_id):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")

        file_info = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id}
        ).json()

        if not file_info.get("ok"):
            print("❌ TELEGRAM FILE ERROR:", file_info)
            return None

        file_path = file_info["result"]["file_path"]

        file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        file_data = requests.get(file_url).content

        filename = file_path.split("/")[-1]
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)

        with open(save_path, "wb") as f:
            f.write(file_data)

        print("📥 FILE DOWNLOADED:", filename)

        return filename

    except Exception as e:
        print("❌ FILE DOWNLOAD ERROR:", e)
        return None


# ---------------- SEND FILE TO TELEGRAM ----------------
def send_telegram_file(file_path, ticket_id, name="User", email=""):
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not token or not chat_id:
            print("⚠️ Telegram not configured")
            return

        ext = file_path.split(".")[-1].lower()

        # 🔥 IMPROVED CAPTION (includes NAME)
        caption = f"""📎 File from ticket

ID: {ticket_id}
Name: {name}
Email: {email}
"""

        # 🔥 DETECT FILE TYPE
        if ext in ["jpg","jpeg","png","gif","webp"]:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            key = "photo"
        elif ext in ["mp4","webm","ogg"]:
            url = f"https://api.telegram.org/bot{token}/sendVideo"
            key = "video"
        else:
            url = f"https://api.telegram.org/bot{token}/sendDocument"
            key = "document"

        with open(file_path, "rb") as f:
            res = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": caption
                },
                files={key: f}
            )

        print("📤 FILE SENT:", res.status_code)

    except Exception as e:
        print("❌ TELEGRAM FILE ERROR:", e)


# ---------------- TELEGRAM RECEIVE ----------------
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    try:
        data = request.get_json(force=True)
        print("📩 TELEGRAM RECEIVED:", data)

        # ✅ HANDLE BUTTON CLICK FIRST
        if "callback_query" in data:
            query = data["callback_query"]
            action = query["data"]

            print("BUTTON CLICK:", action)

            if action.startswith("close_"):
                ticket_id = action.replace("close_", "")

                conn = get_db()
                c = conn.cursor()
                c.execute("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
                conn.commit()
                conn.close()

                send_telegram(f"🔒 Ticket {ticket_id} closed")

            return "ok"

        # ⬇️ THEN YOUR NORMAL MESSAGE LOGIC
        msg_obj = (
            data.get("message")
            or data.get("edited_message")
            or data.get("channel_post")
        )

        if not msg_obj:
            print("❌ No message object")
            return "ok"

        # ---------------- 📸 IMAGE ----------------
        if "photo" in msg_obj:
            file_id = msg_obj["photo"][-1]["file_id"]
            filename = download_telegram_file(file_id)

            if filename:
                caption = msg_obj.get("caption", "").strip()

                # 🔥 FIX: allow no colon
                ticket_id = caption.replace(":", "").strip()

                if not ticket_id.startswith("SUP-"):
                    send_telegram("❌ Send like:\nSUP-1001 (in caption)")
                    return "ok"

                now = datetime.datetime.now().strftime('%H:%M')

                conn = get_db()
                c = conn.cursor()
                c.execute(
                    "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
                    (ticket_id, "admin", f"[FILE] {filename}", now)
                )
                conn.commit()
                conn.close()

                socketio.emit('new_message', {
                    "ticket_id": ticket_id,
                    "message": f"[FILE] {filename}",
                    "sender": "admin",
                    "time": now
                })

                send_telegram(f"📷 Image received → {ticket_id}")

            return "ok"


        # ---------------- 🎥 VIDEO ----------------
        if "video" in msg_obj:
            file_id = msg_obj["video"]["file_id"]
            filename = download_telegram_file(file_id)

            if filename:
                caption = msg_obj.get("caption", "").strip()
                ticket_id = caption.replace(":", "").strip()

                if not ticket_id.startswith("SUP-"):
                    send_telegram("❌ Send like:\nSUP-1001 (in caption)")
                    return "ok"

                now = datetime.datetime.now().strftime('%H:%M')

                conn = get_db()
                c = conn.cursor()
                c.execute(
                    "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
                    (ticket_id, "admin", f"[FILE] {filename}", now)
                )
                conn.commit()
                conn.close()

                socketio.emit('new_message', {
                    "ticket_id": ticket_id,
                    "message": f"[FILE] {filename}",
                    "sender": "admin",
                    "time": now
                })

                send_telegram(f"🎥 Video received → {ticket_id}")

            return "ok"


        # ---------------- TEXT (UNCHANGED) ----------------
        text = msg_obj.get("text", "").strip()
        print("TEXT:", text)

        if not text:
            return "ok"

        if text.lower().startswith("close "):
            ticket_id = text.replace("close ", "").strip()

            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
            conn.commit()
            conn.close()

            send_telegram(f"🔒 Ticket {ticket_id} closed")
            return "ok"

        if text.lower().startswith("open "):
            ticket_id = text.replace("open ", "").strip()

            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE tickets SET status='open' WHERE id=?", (ticket_id,))
            conn.commit()
            conn.close()

            send_telegram(f"🟢 Ticket {ticket_id} reopened")
            return "ok"

        if ":" not in text:
            send_telegram("❌ Use format:\nSUP-1001: your message")
            return "ok"

        ticket_id, msg = text.split(":", 1)
        ticket_id = ticket_id.strip()
        msg = msg.strip()

        now = datetime.datetime.now().strftime('%H:%M')

        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
            (ticket_id, "admin", msg, now)
        )
        conn.commit()
        conn.close()

        socketio.emit('new_message', {
            "ticket_id": ticket_id,
            "message": msg,
            "sender": "admin",
            "time": now
        })

        send_telegram(f"💬 Sent to {ticket_id}")

    except Exception as e:
        print("❌ TELEGRAM ERROR:", e)

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

        # 🔥 ADD NAME
        name = request.form.get('name')
        email = request.form.get('email')
        subject = request.form.get('subject')
        message = request.form.get('message')
        file = request.files.get('file')

        ticket_id = generate_ticket_id()
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = get_db()
        c = conn.cursor()

        # ⚠️ MAKE SURE YOUR TABLE HAS name COLUMN
        c.execute(
            "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, name, email, subject, "Medium", "open", None, now)
        )

        # SAVE TEXT MESSAGE
        c.execute(
            "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
            (ticket_id, "user", message, now)
        )

        # 🔥 HANDLE FILE
        if file and file.filename:
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)

            c.execute(
                "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?)",
                (ticket_id, "user", f"[FILE] {filename}", now)
            )

            # 🔥 SEND FILE WITH NAME + EMAIL
            try:
                send_telegram_file(file_path, ticket_id, name, email)
            except Exception as e:
                print("File send error:", e)

        conn.commit()
        conn.close()

        # 🔥 TELEGRAM MESSAGE (FIXED)
        send_telegram(f"""
🚨 New Ticket

ID: {ticket_id}
Name: {name}
Email: {email}
Subject: {subject}

{message}
""")

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

    # ✅ FIX: pass ticket_id
    send_telegram_file(file_path, ticket_id, email="User")

    return {"status": "ok"}



@app.route('/close/<ticket_id>')
def close_ticket(ticket_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("UPDATE tickets SET status='closed' WHERE id=?", (ticket_id,))
    conn.commit()
    conn.close()

    return "ok"


@app.route('/open/<ticket_id>')
def open_ticket(ticket_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("UPDATE tickets SET status='open' WHERE id=?", (ticket_id,))
    conn.commit()
    conn.close()

    return "ok"




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

    # 🔥 SEND TEXT
    send_telegram(f"""
💬 Message

Ticket: {data['ticket_id']}
From: {data['sender']}

{data['message']}
""")

    # 🔥 SEND FILE IF MESSAGE IS FILE
    if data['message'].startswith("[FILE]"):
        filename = data['message'].replace("[FILE] ", "")
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        send_telegram_file(file_path)

    emit('new_message', {
        "ticket_id": data['ticket_id'],
        "message": data['message'],
        "sender": data['sender'],
        "time": now
    }, broadcast=True)


# ---------------- RUN ----------------
if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))