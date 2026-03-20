from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, abort
from flask_socketio import SocketIO, emit
import sqlite3
import datetime
import os
import smtplib
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")

socketio = SocketIO(app, cors_allowed_origins="*")

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = {'png','jpg','jpeg','gif','webp','mp4','webm','pdf','txt'}


# ---------------- HELPERS ----------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------- DATABASE ----------------
def get_db():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        password TEXT,
        role TEXT
    )''')

    c.execute('''
    CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
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

    if last:
        last_num = int(last["id"].replace("SUP-", ""))
        new_id = last_num + 1
    else:
        new_id = 1001

    conn.close()
    return f"SUP-{new_id}"


# ---------------- FILE ROUTES ----------------
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(path):
        abort(404)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)


# ---------------- EMAIL ----------------
def send_email(user_email, ticket_id, subject, message):
    try:
        sender = os.getenv("EMAIL")
        password = os.getenv("EMAIL_PASS")
        admin_email = os.getenv("ADMIN_EMAIL")

        if not sender or not password:
            return

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender, password)

            server.sendmail(sender, user_email,
                f"Subject: Ticket Received\n\nTicket ID: {ticket_id}")

            server.sendmail(sender, admin_email,
                f"Subject: New Ticket\n\n{subject}\n{message}")

    except Exception as e:
        print("Email error:", e)


# ---------------- AUTH ----------------
def require_login():
    return 'user' in session


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username')
        p = request.form.get('password')

        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=? AND password=?", (u, p))
        user = c.fetchone()
        conn.close()

        if user:
            session['user'] = u
            return redirect('/admin')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ---------------- CREATE TICKET ----------------
@app.route('/', methods=['GET', 'POST'])
def create_ticket():
    if request.method == 'POST':

        email = request.form.get('email')
        subject = request.form.get('subject')
        message = request.form.get('message')
        priority = request.form.get('priority', 'Low')
        file = request.files.get('file')

        ticket_id = generate_ticket_id()
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = get_db()
        c = conn.cursor()

        c.execute("""
        INSERT INTO tickets (id, email, subject, priority, status, assigned_to, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ticket_id, email, subject, priority, 'open', None, now))

        c.execute("""
        INSERT INTO messages (ticket_id, sender, message, timestamp)
        VALUES (?, ?, ?, ?)
        """, (ticket_id, 'user', message, now))

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

            c.execute("""
            INSERT INTO messages (ticket_id, sender, message, timestamp)
            VALUES (?, ?, ?, ?)
            """, (ticket_id, 'user', f"[FILE] {filename}", now))

        conn.commit()
        conn.close()

        send_email(email, ticket_id, subject, message)

        return redirect(url_for('view_ticket', ticket_id=ticket_id))

    return render_template('create_ticket.html')


@app.route('/chat')
def chat():
    return render_template('chat.html')


# ---------------- VIEW ----------------
@app.route('/ticket/<ticket_id>')
def view_ticket(ticket_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT sender, message, timestamp FROM messages WHERE ticket_id=?", (ticket_id,))
    messages = c.fetchall()

    conn.close()

    return render_template('ticket.html', messages=messages, ticket_id=ticket_id)


# ---------------- ADMIN ----------------
@app.route('/admin')
def admin_dashboard():
    if not require_login():
        return redirect('/login')

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM tickets ORDER BY created_at DESC")
    tickets = c.fetchall()

    conn.close()

    return render_template('admin.html', tickets=tickets)


# ---------------- FILE UPLOAD ----------------
@app.route('/upload/<ticket_id>', methods=['POST'])
def upload_file(ticket_id):
    file = request.files.get('file')

    if not file:
        return {"error": "No file"}, 400

    if not allowed_file(file.filename):
        return {"error": "Invalid file type"}, 400

    filename = secure_filename(file.filename)
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    c = conn.cursor()

    c.execute("""
    INSERT INTO messages (ticket_id, sender, message, timestamp)
    VALUES (?, ?, ?, ?)
    """, (ticket_id, 'user', f"[FILE] {filename}", now))

    conn.commit()
    conn.close()

    return {"status": "ok"}


# ---------------- SOCKET EVENTS ----------------
@socketio.on('start_ticket')
def start_ticket(data):
    email = data.get('email')

    ticket_id = generate_ticket_id()
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    c = conn.cursor()

    c.execute("""
    INSERT INTO tickets (id, email, subject, priority, status, assigned_to, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (ticket_id, email, "Live Chat", "Medium", "open", None, now))

    conn.commit()
    conn.close()

    emit('ticket_created', {'ticket_id': ticket_id})


@socketio.on('send_message')
def handle_message(data):
    now = datetime.datetime.now().strftime('%H:%M')

    conn = get_db()
    c = conn.cursor()

    c.execute("""
    INSERT INTO messages (ticket_id, sender, message, timestamp)
    VALUES (?, ?, ?, ?)
    """, (data['ticket_id'], data['sender'], data['message'], now))

    conn.commit()
    conn.close()

    emit('new_message', {
        "ticket_id": data['ticket_id'],
        "message": data['message'],
        "sender": data['sender'],
        "time": now
    }, broadcast=True)


@socketio.on('typing')
def typing(data):
    emit('typing', broadcast=True)


# ---------------- API ----------------
@app.route('/api/history/<ticket_id>')
def history(ticket_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT sender, message, timestamp FROM messages WHERE ticket_id=?", (ticket_id,))
    data = [{"sender": r["sender"], "message": r["message"], "time": r["timestamp"]} for r in c.fetchall()]

    conn.close()
    return jsonify(data)


# ---------------- RUN ----------------
if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=10000)