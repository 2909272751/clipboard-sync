import os, sqlite3, datetime, secrets, re, mimetypes, gzip
import io
import json
import time
import zipfile
import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, session, flash, send_from_directory, jsonify, send_file, abort
from flask_socketio import SocketIO, emit, join_room
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
secret_key = os.environ.get("SECRET_KEY", "")
if len(secret_key) < 32 or secret_key == "replace-with-a-random-64-character-value":
    raise RuntimeError("SECRET_KEY must be a persistent random value of at least 32 characters")
app.secret_key = secret_key
app.config.update(
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024,
    SEND_FILE_MAX_AGE_DEFAULT=31536000,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
)
trusted_proxy_hops = max(0, int(os.environ.get("TRUST_PROXY_HOPS", "0")))
if trusted_proxy_hops:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=trusted_proxy_hops,
        x_proto=trusted_proxy_hops,
        x_host=trusted_proxy_hops,
    )
configured_origins = [item.strip() for item in os.environ.get("ALLOWED_ORIGINS", "").split(",") if item.strip()]
socketio = SocketIO(app, cors_allowed_origins=configured_origins or None, async_mode="threading")

DB = os.environ.get("DB_PATH", "/data/db.sqlite")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/data/uploads")
MAGISK_TEMPLATE_DIR = Path(os.environ.get("MAGISK_TEMPLATE_DIR", "/app/magisk-template"))
WINDOWS_TEMPLATE_DIR = Path(os.environ.get("WINDOWS_TEMPLATE_DIR", "/app/windows-client"))
APP_VERSION = "1.3.3"
MAGISK_VERSION = "1.3.0"
WINDOWS_VERSION = "1.3.0"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

_rate_lock = threading.Lock()
_rate_events = {}
_socket_lock = threading.Lock()
_socket_devices = {}

def rate_limited(bucket, key, maximum, window_seconds):
    now = time.monotonic()
    identity = (bucket, key)
    with _rate_lock:
        recent = [stamp for stamp in _rate_events.get(identity, []) if now - stamp < window_seconds]
        if len(recent) >= maximum:
            _rate_events[identity] = recent
            return True
        recent.append(now)
        _rate_events[identity] = recent
    return False

def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def client_ip():
    # ProxyFix has already normalized this value when explicitly enabled.
    return request.remote_addr or "unknown"

def csrf_token():
    value = session.get("_csrf_token")
    if not value:
        value = secrets.token_urlsafe(32)
        session["_csrf_token"] = value
    return value

app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["app_version"] = APP_VERSION

def format_timestamp(value):
    if not value:
        return "从未"
    return datetime.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")

app.jinja_env.globals["format_timestamp"] = format_timestamp

@app.before_request
def enforce_csrf():
    if request.method != "POST" or request.endpoint == "api_push":
        return None
    if "user_id" not in session and request.endpoint not in {
        "setup", "login", "register", "accept_invite"
    }:
        return None
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("_csrf_token", "")
    if not expected or not secrets.compare_digest(expected, supplied):
        abort(400, "invalid CSRF token")

@app.after_request
def secure_response(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; frame-ancestors 'none'",
    )
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.endpoint == "static":
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    if request.endpoint in {
        "admin_invites", "accept_invite", "create_magisk_module",
        "download_magisk_module", "create_windows_package", "download_windows_package",
        "create_generic_device",
    }:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"

    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
    compressible = response.mimetype in {
        "text/html", "text/css", "text/javascript", "application/javascript",
        "application/json", "application/manifest+json",
    }
    if (
        accepts_gzip and compressible and request.method != "HEAD"
        and response.status_code == 200 and not response.headers.get("Content-Encoding")
    ):
        response.direct_passthrough = False
        payload = response.get_data()
        if len(payload) >= 1024:
            compressed = gzip.compress(payload, compresslevel=6)
            if len(compressed) < len(payload):
                response.set_data(compressed)
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Content-Length"] = str(len(compressed))
                response.headers.pop("ETag", None)
                response.headers.pop("Accept-Ranges", None)
                response.vary.add("Accept-Encoding")
    return response

def shell_quote(value):
    """Quote one value for a config file sourced by Android's /system/bin/sh."""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"

def get_public_base_url():
    # ProxyFix has already applied X-Forwarded-Proto/Host from the reverse proxy.
    # host_url preserves an explicitly used port and naturally follows future migrations.
    return request.host_url.rstrip("/")

def validate_device_name(raw_name):
    name = re.sub(r"[\x00-\x1f\x7f]", "", (raw_name or "").strip())
    if not name or len(name) > 80:
        return None
    return name

def build_magisk_config(server_url, token, device_name, provision_id):
    return "\n".join([
        "# Auto-generated by Clipboard Sync. Do not share this file or module ZIP.",
        f"SERVER_URL={shell_quote(server_url)}",
        f"DEVICE_TOKEN={shell_quote(token)}",
        f"DEVICE_NAME={shell_quote(device_name)}",
        f"PROVISION_ID={shell_quote(provision_id)}",
        "SHOW_TOAST=1",
        "",
    ])

def build_magisk_zip(server_url, token, device_name, provision_id):
    if not MAGISK_TEMPLATE_DIR.is_dir():
        raise RuntimeError(f"Magisk template missing: {MAGISK_TEMPLATE_DIR}")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sorted(MAGISK_TEMPLATE_DIR.rglob("*")):
            if not source.is_file() or "__pycache__" in source.parts:
                continue
            relative = source.relative_to(MAGISK_TEMPLATE_DIR).as_posix()
            if relative == "config.conf":
                archive.writestr(relative, build_magisk_config(server_url, token, device_name, provision_id))
            else:
                archive.write(source, relative)
    output.seek(0)
    return output

def build_windows_zip(server_url, token, device_id, device_name):
    executable_name = f"clipboard-sync-windows-v{WINDOWS_VERSION}.exe"
    required_files = [
        WINDOWS_TEMPLATE_DIR / "template" / executable_name,
        WINDOWS_TEMPLATE_DIR / "安装并启动.cmd",
        WINDOWS_TEMPLATE_DIR / "卸载.cmd",
        WINDOWS_TEMPLATE_DIR / "使用说明.txt",
    ]
    if any(not path.is_file() for path in required_files):
        missing = [str(path) for path in required_files if not path.is_file()]
        raise RuntimeError(f"Windows template missing: {missing}")

    config = {
        "server_url": server_url,
        "device_token": token,
        "device_id": device_id,
        "device_name": device_name,
        "show_notifications": True,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.write(required_files[0], executable_name)
        for path in required_files[1:]:
            archive.write(path, path.name)
        archive.writestr("config.json", json.dumps(config, ensure_ascii=False, indent=2))
    output.seek(0)
    return output

@contextmanager
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def find_device_by_token(token, touch=True):
    if not token:
        return None
    with get_db() as conn:
        device = conn.execute("""
            SELECT d.id, d.user_id, d.name, d.sync_mode, d.platform,
                   d.client_version, u.username
            FROM devices d JOIN users u ON d.user_id = u.id
            WHERE d.token_hash=? AND COALESCE(u.disabled, 0)=0
        """, (token_digest(token),)).fetchone()
        if device and touch:
            client_version = request.headers.get("X-Client-Version", "").strip()[:40]
            if client_version:
                conn.execute(
                    "UPDATE devices SET last_seen_at=?, client_version=? WHERE id=?",
                    (int(time.time()), client_version, device['id']),
                )
            else:
                conn.execute(
                    "UPDATE devices SET last_seen_at=? WHERE id=?",
                    (int(time.time()), device['id']),
                )
            conn.commit()
        return device

def mark_device_synced(device_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE devices SET last_seen_at=?, last_sync_at=? WHERE id=?",
            (int(time.time()), int(time.time()), device_id),
        )
        conn.commit()

def password_matches(stored, supplied):
    if not stored:
        return False
    if stored.startswith(("scrypt:", "pbkdf2:")):
        return check_password_hash(stored, supplied)
    return secrets.compare_digest(stored, supplied)

@socketio.on('connect')
def socket_connect(auth):
    token = (auth or {}).get('token', '') if isinstance(auth, dict) else ''
    device = find_device_by_token(token)
    if device:
        with _socket_lock:
            _socket_devices[request.sid] = device['id']
        join_room(f"device_{device['id']}")
        return True
    return False

@socketio.on('disconnect')
def socket_disconnect():
    with _socket_lock:
        device_id = _socket_devices.pop(request.sid, None)
    if device_id:
        with get_db() as conn:
            conn.execute(
                "UPDATE devices SET last_seen_at=? WHERE id=?",
                (int(time.time()), device_id),
            )
            conn.commit()

def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT, question TEXT, answer TEXT, is_admin INTEGER DEFAULT 0, disabled INTEGER DEFAULT 0, last_login_at INTEGER)')
        c.execute('CREATE TABLE IF NOT EXISTS clips (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, device TEXT, content TEXT, created_at TEXT, created_ts INTEGER, is_favorite INTEGER DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS codes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, device TEXT, content TEXT, pure_code TEXT, created_at TEXT, created_ts INTEGER, is_favorite INTEGER DEFAULT 0)')
        c.execute("CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, token TEXT, token_hash TEXT UNIQUE, platform TEXT DEFAULT 'generic', sync_mode TEXT DEFAULT 'both', last_seen_at INTEGER, last_sync_at INTEGER, client_version TEXT)")
        c.execute('''CREATE TABLE IF NOT EXISTS sync_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source_device_id INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            content TEXT NOT NULL,
            pure_code TEXT,
            created_at TEXT NOT NULL,
            created_ts INTEGER,
            event_id TEXT
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_sync_events_user_id_id ON sync_events(user_id, id)')
        c.execute('''CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            original_name TEXT,
            file_size INTEGER,
            created_at TEXT,
            created_ts INTEGER
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT UNIQUE NOT NULL,
            created_by INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )''')
        c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('public_registration','0')")

        # 兼容旧表：如果旧表没有 is_favorite 列则添加
        try:
            c.execute("ALTER TABLE clips ADD COLUMN is_favorite INTEGER DEFAULT 0")
        except: pass
        try:
            c.execute("ALTER TABLE codes ADD COLUMN is_favorite INTEGER DEFAULT 0")
        except: pass
        try:
            c.execute("ALTER TABLE devices ADD COLUMN platform TEXT DEFAULT 'generic'")
        except: pass
        try:
            c.execute("ALTER TABLE devices ADD COLUMN token_hash TEXT")
        except: pass
        try:
            c.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        except: pass
        for statement in [
            "ALTER TABLE users ADD COLUMN disabled INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN last_login_at INTEGER",
            "ALTER TABLE devices ADD COLUMN sync_mode TEXT DEFAULT 'both'",
            "ALTER TABLE devices ADD COLUMN last_seen_at INTEGER",
            "ALTER TABLE devices ADD COLUMN last_sync_at INTEGER",
            "ALTER TABLE devices ADD COLUMN client_version TEXT",
            "ALTER TABLE clips ADD COLUMN created_ts INTEGER",
            "ALTER TABLE codes ADD COLUMN created_ts INTEGER",
            "ALTER TABLE files ADD COLUMN created_ts INTEGER",
            "ALTER TABLE sync_events ADD COLUMN created_ts INTEGER",
            "ALTER TABLE sync_events ADD COLUMN event_id TEXT",
        ]:
            try:
                c.execute(statement)
            except sqlite3.OperationalError:
                pass
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone() and not c.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone():
            c.execute("UPDATE users SET is_admin=1 WHERE id=(SELECT MIN(id) FROM users)")
        for row in c.execute("SELECT id, password FROM users").fetchall():
            if row[1] and not row[1].startswith(("scrypt:", "pbkdf2:")):
                c.execute(
                    "UPDATE users SET password=? WHERE id=?",
                    (generate_password_hash(row[1]), row[0]),
                )
        # Password recovery is intentionally removed; discard legacy answers.
        c.execute("UPDATE users SET question=NULL, answer=NULL WHERE question IS NOT NULL OR answer IS NOT NULL")
        for row in c.execute("SELECT id, token, token_hash FROM devices").fetchall():
            if row[1]:
                digest = row[2] or token_digest(row[1])
                c.execute(
                    "UPDATE devices SET token_hash=?, token=NULL WHERE id=?",
                    (digest, row[0]),
                )
        c.execute("UPDATE devices SET sync_mode='both' WHERE sync_mode NOT IN ('both','send_only','receive_only','paused') OR sync_mode IS NULL")
        current_year = datetime.datetime.now().year
        current_time = int(time.time())
        for table in ('clips', 'codes', 'files', 'sync_events'):
            for row in c.execute(
                f"SELECT id, created_at FROM {table} WHERE created_ts IS NULL"
            ).fetchall():
                timestamp = current_time
                try:
                    parsed = datetime.datetime.strptime(
                        f"{current_year}-{row[1]}", "%Y-%m-%d %H:%M:%S"
                    )
                    if parsed.timestamp() > current_time + 86400:
                        parsed = parsed.replace(year=current_year - 1)
                    timestamp = int(parsed.timestamp())
                except (TypeError, ValueError):
                    pass
                c.execute(f"UPDATE {table} SET created_ts=? WHERE id=?", (timestamp, row[0]))
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_token_hash ON devices(token_hash)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_events_device_event ON sync_events(source_device_id,event_id) WHERE event_id IS NOT NULL")
        c.execute("CREATE INDEX IF NOT EXISTS idx_clips_user_created ON clips(user_id,created_ts DESC,id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_codes_user_created ON codes(user_id,created_ts DESC,id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_clips_user_id_desc ON clips(user_id,id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_codes_user_id_desc ON codes(user_id,id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_clips_user_favorite_id ON clips(user_id,is_favorite,id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_codes_user_favorite_id ON codes(user_id,is_favorite,id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_files_user_id_desc ON files(user_id,id DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id,id)")
        conn.commit()

init_db()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        with get_db() as conn:
            user = conn.execute(
                "SELECT disabled FROM users WHERE id=?",
                (session['user_id'],),
            ).fetchone()
        if not user or user['disabled']:
            session.clear()
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        with get_db() as conn:
            admin = conn.execute("SELECT is_admin FROM users WHERE id=?", (session['user_id'],)).fetchone()
        if not admin or not admin['is_admin']:
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

def has_users():
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

def public_registration_enabled():
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='public_registration'"
        ).fetchone()
    return bool(row and row['value'] == '1')

@app.route('/healthz')
def healthz():
    with get_db() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "version": APP_VERSION}

@app.route('/service-worker')
def service_worker():
    response = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache, max-age=0"
    response.headers["Expires"] = "0"
    return response

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if has_users():
        return redirect('/login')
    if request.method == 'POST':
        remote = client_ip()
        if rate_limited("setup", remote, 10, 300):
            abort(429)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
            flash("用户名需为 3-40 位字母、数字、点、横线或下划线")
        elif len(password) < 10:
            flash("密码至少需要 10 个字符")
        else:
            try:
                with get_db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                        return redirect('/login')
                    conn.execute(
                        "INSERT INTO users (username,password,question,answer,is_admin) VALUES (?,?,NULL,NULL,1)",
                        (username, generate_password_hash(password)),
                    )
                    conn.commit()
                return redirect('/login')
            except sqlite3.IntegrityError:
                flash("初始化未完成，请重试")
    return render_template('setup.html')

# --- 路由：账号系统 ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if not has_users():
        return redirect('/setup')
    if request.method == 'POST':
        remote = client_ip()
        if rate_limited("login", remote, 10, 300):
            abort(429)
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=?", (request.form['username'],)).fetchone()
            if user and not user['disabled'] and password_matches(user['password'], request.form['password']):
                if not user['password'].startswith(("scrypt:", "pbkdf2:")):
                    conn.execute(
                        "UPDATE users SET password=? WHERE id=?",
                        (generate_password_hash(request.form['password']), user['id']),
                    )
                    conn.commit()
                conn.execute(
                    "UPDATE users SET last_login_at=? WHERE id=?",
                    (int(time.time()), user['id']),
                )
                conn.commit()
                session.clear()
                session['user_id'], session['username'] = user['id'], user['username']
                session['is_admin'] = bool(user['is_admin'])
                return redirect('/')
        flash("账号或密码错误")
    return render_template("login.html", public_registration=public_registration_enabled())

@app.route('/register', methods=['GET', 'POST'])
def register():
    if not has_users():
        return redirect('/setup')
    if not public_registration_enabled():
        abort(404)
    if request.method == 'POST':
        remote = client_ip()
        if rate_limited("register", remote, 5, 300):
            abort(429)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
            flash("用户名需为 3-40 位字母、数字、点、横线或下划线")
        elif len(password) < 10:
            flash("密码至少需要 10 个字符")
        else:
            try:
                with get_db() as conn:
                    conn.execute(
                        "INSERT INTO users (username,password,question,answer,is_admin) VALUES (?,?,NULL,NULL,0)",
                        (username, generate_password_hash(password)),
                    )
                    conn.commit()
                return redirect('/login')
            except sqlite3.IntegrityError:
                flash("用户名已存在")
    return render_template('register.html')

@app.route('/reset_pwd', methods=['GET', 'POST'])
def reset_pwd():
    abort(404)

@app.route('/admin/invites', methods=['GET', 'POST'])
@admin_required
def admin_invites():
    new_invite_url = None
    if request.method == 'POST':
        action = request.form.get('action', 'create_invite')
        if action == 'toggle_registration':
            enabled = '1' if request.form.get('enabled') == '1' else '0'
            with get_db() as conn:
                conn.execute(
                    "UPDATE settings SET value=? WHERE key='public_registration'",
                    (enabled,),
                )
                conn.commit()
            flash("开放注册已开启" if enabled == '1' else "开放注册已关闭")
            return redirect('/admin/invites')
        if action != 'create_invite':
            abort(400)
        raw_token = secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = now + 7 * 24 * 60 * 60
        with get_db() as conn:
            conn.execute(
                "INSERT INTO invites (token_hash,created_by,created_at,expires_at) VALUES (?,?,?,?)",
                (token_digest(raw_token), session['user_id'], now, expires_at),
            )
            conn.commit()
        new_invite_url = request.host_url.rstrip('/') + '/invite/' + raw_token
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,created_at,expires_at,used_at FROM invites WHERE created_by=? ORDER BY id DESC LIMIT 30",
            (session['user_id'],),
        ).fetchall()
    return render_template(
        'invites.html', rows=rows, new_invite_url=new_invite_url,
        now=int(time.time()), public_registration=public_registration_enabled(),
    )

@app.route('/invite/<raw_token>', methods=['GET', 'POST'])
def accept_invite(raw_token):
    if len(raw_token) > 100:
        abort(404)
    digest = token_digest(raw_token)
    with get_db() as conn:
        invite = conn.execute(
            "SELECT id,expires_at,used_at FROM invites WHERE token_hash=?",
            (digest,),
        ).fetchone()
    if not invite or invite['used_at'] or invite['expires_at'] < int(time.time()):
        abort(404)
    if request.method == 'POST':
        remote = client_ip()
        if rate_limited("invite", remote, 10, 300):
            abort(429)
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
            flash("用户名格式不正确")
        elif len(password) < 10:
            flash("密码至少需要 10 个字符")
        else:
            try:
                with get_db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    current = conn.execute(
                        "SELECT id FROM invites WHERE id=? AND used_at IS NULL AND expires_at>=?",
                        (invite['id'], int(time.time())),
                    ).fetchone()
                    if not current:
                        abort(404)
                    conn.execute(
                        "INSERT INTO users (username,password,question,answer,is_admin) VALUES (?,?,NULL,NULL,0)",
                        (username, generate_password_hash(password)),
                    )
                    conn.execute("UPDATE invites SET used_at=? WHERE id=?", (int(time.time()), invite['id']))
                    conn.commit()
                return redirect('/login')
            except sqlite3.IntegrityError:
                flash("用户名已存在")
    return render_template('invite.html')

@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirmation = request.form.get('confirm_password', '')
        with get_db() as conn:
            user = conn.execute(
                "SELECT password FROM users WHERE id=?",
                (session['user_id'],),
            ).fetchone()
            if not user or not password_matches(user['password'], current_password):
                flash("当前密码不正确")
            elif len(new_password) < 10:
                flash("新密码至少需要 10 个字符")
            elif new_password != confirmation:
                flash("两次输入的新密码不一致")
            else:
                conn.execute(
                    "UPDATE users SET password=? WHERE id=?",
                    (generate_password_hash(new_password), session['user_id']),
                )
                conn.commit()
                flash("密码修改成功")
                return redirect('/account')
    return render_template('account.html')

@app.route('/admin/users')
@admin_required
def admin_users():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT u.id,u.username,u.is_admin,u.disabled,u.last_login_at,
                   COUNT(DISTINCT d.id) AS device_count,
                   COUNT(DISTINCT c.id) AS clip_count
            FROM users u
            LEFT JOIN devices d ON d.user_id=u.id
            LEFT JOIN clips c ON c.user_id=u.id
            GROUP BY u.id
            ORDER BY u.id
        """).fetchall()
    return render_template('users.html', rows=rows)

@app.route('/admin/users/<int:user_id>/action', methods=['POST'])
@admin_required
def admin_user_action(user_id):
    action = request.form.get('action', '')
    with get_db() as conn:
        target = conn.execute(
            "SELECT id,username,is_admin,disabled FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not target:
            abort(404)
        if action == 'toggle_disabled':
            if target['id'] == session['user_id'] or target['is_admin']:
                flash("不能停用当前管理员账号")
            else:
                disabled = 0 if target['disabled'] else 1
                conn.execute("UPDATE users SET disabled=? WHERE id=?", (disabled, user_id))
                conn.commit()
                flash("账号已停用" if disabled else "账号已启用")
        elif action == 'reset_password':
            new_password = request.form.get('new_password', '')
            if len(new_password) < 10:
                flash("临时密码至少需要 10 个字符")
            else:
                conn.execute(
                    "UPDATE users SET password=? WHERE id=?",
                    (generate_password_hash(new_password), user_id),
                )
                conn.commit()
                flash(f"已重置 {target['username']} 的密码")
        else:
            abort(400)
    return redirect('/admin/users')

@app.route('/logout', methods=['POST'])
def logout(): session.clear(); return redirect('/login')

@app.route('/')
@login_required
def index(): return render_template("index.html")

def history_page(table, endpoint, include_pure_code=False):
    query = request.args.get('q', '').strip()[:200]
    device = request.args.get('device', '').strip()[:80]
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    try:
        page = max(1, int(request.args.get('page', '1')))
    except ValueError:
        page = 1
    try:
        requested_size = int(request.args.get('per_page', '50'))
    except ValueError:
        requested_size = 50
    per_page = requested_size if requested_size in {20, 50, 100} else 50

    clauses = ["user_id=?"]
    params = [session['user_id']]
    if query:
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        pattern = f"%{escaped}%"
        search_columns = ["content", "device"]
        if include_pure_code:
            search_columns.append("pure_code")
        clauses.append("(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in search_columns) + ")")
        params.extend([pattern] * len(search_columns))
    if device:
        clauses.append("device=?")
        params.append(device)

    def parse_date(value, next_day=False):
        if not value:
            return None
        try:
            parsed = datetime.datetime.strptime(value, "%Y-%m-%d")
            if next_day:
                parsed += datetime.timedelta(days=1)
            return int(parsed.timestamp())
        except ValueError:
            return None

    start_ts = parse_date(date_from)
    end_ts = parse_date(date_to, next_day=True)
    if start_ts is not None:
        clauses.append("created_ts>=?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("created_ts<?")
        params.append(end_ts)
    where = " AND ".join(clauses)

    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", params
        ).fetchone()[0]
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
        device_options = [row[0] for row in conn.execute(
            f"SELECT DISTINCT device FROM {table} WHERE user_id=? AND device IS NOT NULL AND device<>'' ORDER BY device",
            (session['user_id'],),
        ).fetchall()]

    def page_url(number):
        values = {
            'q': query, 'device': device, 'date_from': date_from,
            'date_to': date_to, 'per_page': per_page, 'page': number,
        }
        return url_for(endpoint, **{key: value for key, value in values.items() if value not in {'', None}})

    first = max(1, page - 2)
    last = min(pages, page + 2)
    pagination = {
        'page': page, 'pages': pages, 'total': total, 'per_page': per_page,
        'prev_url': page_url(page - 1) if page > 1 else None,
        'next_url': page_url(page + 1) if page < pages else None,
        'links': [(number, page_url(number)) for number in range(first, last + 1)],
    }
    filters = {
        'q': query, 'device': device, 'date_from': date_from, 'date_to': date_to,
    }
    return rows, device_options, filters, pagination

@app.route('/clips')
@login_required
def clips():
    rows, devices, filters, pagination = history_page('clips', 'clips')
    return render_template(
        "clips.html", rows=rows, devices=devices,
        filters=filters, pagination=pagination,
    )
@app.route('/api/clips')
@login_required
def api_clips():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM clips WHERE user_id=? ORDER BY id DESC LIMIT 100", (session['user_id'],)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/codes')
@login_required
def api_codes():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM codes WHERE user_id=? ORDER BY id DESC LIMIT 100", (session['user_id'],)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/devices')
@login_required
def api_devices():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,name,platform,sync_mode,last_seen_at,last_sync_at,client_version FROM devices WHERE user_id=?",
            (session['user_id'],),
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/files')
@login_required
def api_files_json():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM files WHERE user_id=? ORDER BY id DESC", (session['user_id'],)).fetchall()
    return jsonify([dict(r) for r in rows])



@app.route('/codes')
@login_required
def codes():
    rows, devices, filters, pagination = history_page('codes', 'codes', include_pure_code=True)
    return render_template(
        "codes.html", rows=rows, devices=devices,
        filters=filters, pagination=pagination,
    )

@app.route('/devices')
@login_required
def devices():
    return render_device_page()

def load_device_rows():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,name,platform,sync_mode,last_seen_at,last_sync_at,client_version,token_hash FROM devices WHERE user_id=?",
            (session['user_id'],),
        ).fetchall()
    with _socket_lock:
        socket_device_ids = set(_socket_devices.values())
    now = int(time.time())
    device_rows = []
    for row in rows:
        item = dict(row)
        item['online'] = row['id'] in socket_device_ids or (
            row['last_seen_at'] and now - row['last_seen_at'] <= 90
        )
        item['token_active'] = bool(row['token_hash'])
        device_rows.append(item)
    return device_rows

def render_device_page(new_generic_device=None):
    return render_template(
        "devices.html", rows=load_device_rows(), user_id=session['user_id'],
        new_generic_device=new_generic_device,
    )

@app.route('/devices/generic-token', methods=['POST'])
@login_required
def create_generic_device():
    device_name = validate_device_name(request.form.get('name'))
    sync_mode = request.form.get('sync_mode', 'both')
    if not device_name:
        flash("设备名称不能为空，且不能超过80个字符")
        return redirect('/devices')
    if sync_mode not in {'both', 'send_only', 'receive_only'}:
        abort(400)

    token = secrets.token_hex(32)
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO devices (user_id,name,token,token_hash,platform,sync_mode) VALUES (?,?,NULL,?,?,?)",
            (session['user_id'], device_name, token_digest(token), 'generic', sync_mode),
        )
        device_id = cursor.lastrowid
        conn.commit()

    return render_device_page({
        'id': device_id,
        'name': device_name,
        'token': token,
        'server_url': get_public_base_url(),
    })

@app.route('/devices/<int:device_id>/sync-mode', methods=['POST'])
@login_required
def update_device_sync_mode(device_id):
    mode = request.form.get('sync_mode', '')
    if mode not in {'both', 'send_only', 'receive_only', 'paused'}:
        abort(400)
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE devices SET sync_mode=? WHERE id=? AND user_id=?",
            (mode, device_id, session['user_id']),
        )
        conn.commit()
    if not cursor.rowcount:
        abort(404)
    flash("设备同步模式已更新")
    return redirect('/devices')

@app.route('/delete/<target>/<int:id>', methods=['POST'])
@login_required
def delete_single(target, id):
    table = {"clip":"clips", "code":"codes", "device":"devices"}.get(target)
    if not table:
        abort(404)
    with get_db() as conn:
        conn.execute(f"DELETE FROM {table} WHERE id=? AND user_id=?", (id, session['user_id']))
        conn.commit()
    return redirect(url_for(f'{target}s'))

@app.route('/api/bulk_delete', methods=['POST'])
@login_required
def bulk_delete():
    target = request.form.get("target")
    action = request.form.get("action")
    ids = request.form.getlist("item_ids")
    table = {"clip": "clips", "code": "codes"}.get(target)
    if table:
        with get_db() as conn:
            if action == "clear_all":
                conn.execute(f"DELETE FROM {table} WHERE user_id=?", (session['user_id'],))
            elif ids:
                placeholders = ','.join(['?'] * len(ids))
                conn.execute(f"DELETE FROM {table} WHERE user_id=? AND id IN ({placeholders})", [session['user_id']] + ids)
            conn.commit()
    return redirect(url_for(f'{target}s'))

@app.route('/devices/magisk-module', methods=['POST'])
@login_required
def create_magisk_module():
    device_name = validate_device_name(request.form.get('name'))
    if not device_name:
        flash("设备名称不能为空，且不能超过80个字符")
        return redirect('/devices')

    token = secrets.token_hex(32)
    provision_id = secrets.token_hex(16)
    try:
        module_zip = build_magisk_zip(get_public_base_url(), token, device_name, provision_id)
    except RuntimeError:
        app.logger.exception("Unable to build personalized Magisk module")
        flash("服务器缺少 Magisk 模块模板，请联系管理员")
        return redirect('/devices')

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO devices (user_id, name, token, token_hash, platform) VALUES (?,?,NULL,?,?)",
            (session['user_id'], device_name, token_digest(token), 'android_magisk'),
        )
        device_id = cursor.lastrowid
        conn.commit()

    return send_personalized_magisk_zip(module_zip, device_id)

@app.route('/devices/<int:device_id>/magisk-module', methods=['POST'])
@login_required
def download_magisk_module(device_id):
    with get_db() as conn:
        device = conn.execute(
            "SELECT id, name FROM devices WHERE id=? AND user_id=?",
            (device_id, session['user_id']),
        ).fetchone()
    if not device:
        flash("设备不存在")
        return redirect('/devices')

    provision_id = secrets.token_hex(16)
    token = secrets.token_hex(32)
    try:
        module_zip = build_magisk_zip(
            get_public_base_url(), token, device['name'], provision_id
        )
    except RuntimeError:
        app.logger.exception("Unable to build personalized Magisk module")
        flash("服务器缺少 Magisk 模块模板，请联系管理员")
        return redirect('/devices')
    with get_db() as conn:
        conn.execute("UPDATE devices SET token=NULL, token_hash=? WHERE id=? AND user_id=?", (token_digest(token), device['id'], session['user_id']))
        conn.commit()
    return send_personalized_magisk_zip(module_zip, device['id'])

def send_personalized_magisk_zip(module_zip, device_id):
    response = send_file(
        module_zip,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"clipboard-sync-v{MAGISK_VERSION}-magisk-device-{device_id}.zip",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

@app.route('/devices/windows-package', methods=['POST'])
@login_required
def create_windows_package():
    device_name = validate_device_name(request.form.get('name'))
    if not device_name:
        flash("设备名称不能为空，且不能超过80个字符")
        return redirect('/devices')

    token = secrets.token_hex(32)
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO devices (user_id, name, token, token_hash, platform) VALUES (?,?,NULL,?,?)",
            (session['user_id'], device_name, token_digest(token), 'windows'),
        )
        device_id = cursor.lastrowid
        conn.commit()

    try:
        package_zip = build_windows_zip(
            get_public_base_url(), token, device_id, device_name
        )
    except RuntimeError:
        app.logger.exception("Unable to build personalized Windows package")
        with get_db() as conn:
            conn.execute("DELETE FROM devices WHERE id=? AND user_id=?", (device_id, session['user_id']))
            conn.commit()
        flash("服务器缺少 Windows 客户端模板，请联系管理员")
        return redirect('/devices')
    return send_personalized_windows_zip(package_zip, device_id)

@app.route('/devices/<int:device_id>/windows-package', methods=['POST'])
@login_required
def download_windows_package(device_id):
    with get_db() as conn:
        device = conn.execute(
            "SELECT id, name FROM devices WHERE id=? AND user_id=?",
            (device_id, session['user_id']),
        ).fetchone()
    if not device:
        flash("设备不存在")
        return redirect('/devices')
    token = secrets.token_hex(32)
    try:
        package_zip = build_windows_zip(
            get_public_base_url(), token, device['id'], device['name']
        )
    except RuntimeError:
        app.logger.exception("Unable to build personalized Windows package")
        flash("服务器缺少 Windows 客户端模板，请联系管理员")
        return redirect('/devices')
    with get_db() as conn:
        conn.execute("UPDATE devices SET token=NULL, token_hash=? WHERE id=? AND user_id=?", (token_digest(token), device['id'], session['user_id']))
        conn.commit()
    return send_personalized_windows_zip(package_zip, device['id'])

def send_personalized_windows_zip(package_zip, device_id):
    response = send_file(
        package_zip,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"clipboard-sync-v{WINDOWS_VERSION}-windows-device-{device_id}.zip",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

# --- 收藏功能 ---

@app.route('/api/favorites')
@login_required
def api_favorites():
    with get_db() as conn:
        clips = [dict(r) for r in conn.execute("SELECT *, 'clip' as item_type FROM clips WHERE user_id=? AND is_favorite=1 ORDER BY id DESC", (session['user_id'],)).fetchall()]
        codes = [dict(r) for r in conn.execute("SELECT *, 'code' as item_type FROM codes WHERE user_id=? AND is_favorite=1 ORDER BY id DESC", (session['user_id'],)).fetchall()]
    return jsonify({"clips": clips, "codes": codes})

@app.route('/favorites')
@login_required
def favorites():
    with get_db() as conn:
        clips = conn.execute("SELECT *, 'clip' as item_type FROM clips WHERE user_id=? AND is_favorite=1 ORDER BY id DESC", (session['user_id'],)).fetchall()
        codes = conn.execute("SELECT *, 'code' as item_type FROM codes WHERE user_id=? AND is_favorite=1 ORDER BY id DESC", (session['user_id'],)).fetchall()
    return render_template("favorites.html", clips=clips, codes=codes)

@app.route('/api/toggle_favorite', methods=['POST'])
@login_required
def toggle_favorite():
    data = request.get_json()
    target = data.get("target")  # "clip" or "code"
    item_id = data.get("id")
    table = {"clip": "clips", "code": "codes"}.get(target)
    if not table or not item_id:
        return jsonify({"error": "invalid params"}), 400
    with get_db() as conn:
        row = conn.execute(f"SELECT is_favorite FROM {table} WHERE id=? AND user_id=?", (item_id, session['user_id'])).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        new_val = 0 if row['is_favorite'] else 1
        conn.execute(f"UPDATE {table} SET is_favorite=? WHERE id=?", (new_val, item_id))
        conn.commit()
    return jsonify({"is_favorite": new_val})

# --- 文件上传功能 ---
@app.route('/files')
@login_required
def file_list():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM files WHERE user_id=? ORDER BY id DESC", (session['user_id'],)).fetchall()
    return render_template("files.html", rows=rows)

@app.route('/api/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        flash("请选择文件")
        return redirect('/files')
    f = request.files['file']
    if f.filename == '':
        flash("请选择文件")
        return redirect('/files')

    # 生成唯一文件名
    ext = os.path.splitext(f.filename)[1].lower()[:12]
    if ext and not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
        ext = ""
    unique_name = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, unique_name)
    f.save(filepath)
    file_size = os.path.getsize(filepath)

    created_ts = int(time.time())
    now = datetime.datetime.fromtimestamp(created_ts).strftime("%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO files (user_id, filename, original_name, file_size, created_at, created_ts) VALUES (?,?,?,?,?,?)",
            (session['user_id'], unique_name, f.filename, file_size, now, created_ts)
        )
        conn.commit()
    flash(f"文件 {f.filename} 上传成功")
    return redirect('/files')

@app.route('/api/delete_file', methods=['POST'])
@login_required
def delete_file():
    file_id = request.form.get("file_id")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=? AND user_id=?", (file_id, session['user_id'])).fetchone()
        if row:
            filepath = os.path.join(UPLOAD_FOLDER, row['filename'])
            if os.path.exists(filepath):
                os.remove(filepath)
            conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            conn.commit()
    return redirect('/files')

@app.route('/uploads/<filename>')
@login_required
def serve_file(filename):
    with get_db() as conn:
        owned = conn.execute(
            "SELECT 1 FROM files WHERE filename=? AND user_id=?",
            (filename, session['user_id']),
        ).fetchone()
    if not owned:
        abort(404)
    response = send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

# --- API: 设备推送 ---
@app.route('/api/push', methods=['POST'])
def api_push():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    remote = client_ip()
    if rate_limited("api_push", token_digest(token) if token else remote, 180, 60):
        abort(429)
    device = find_device_by_token(token)
    if not device:
        return {"error":"unauthorized"}, 403
    if device['sync_mode'] not in {'both', 'send_only'}:
        return {"status":"ignored", "reason":"sending_disabled"}, 200
    body = request.get_json(silent=True) or {}
    content = body.get("content", "")
    if not isinstance(content, str) or not content:
        return {"status":"no_content"}
    if len(content) > 1_000_000:
        return {"error":"clipboard_too_large"}, 413
    event_id = str(body.get('event_id') or secrets.token_hex(16)).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,100}", event_id):
        return {"error":"invalid_event_id"}, 400

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM sync_events WHERE source_device_id=? AND event_id=?",
            (device['id'], event_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE devices SET last_seen_at=?,last_sync_at=? WHERE id=?",
                (int(time.time()), int(time.time()), device['id']),
            )
            conn.commit()
            return {
                "status":"ok", "event_id":event_id,
                "revision":existing['id'], "duplicate":True,
            }

        last_item = conn.execute(
            "SELECT content FROM sync_events WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (device['user_id'],),
        ).fetchone()
        if last_item and content.strip() == last_item['content'].strip():
            return {"status":"ignored", "reason":"duplicate"}, 200

        pure = None
        is_long = len(content) > 120 or len(content.split('\n')) > 3
        is_code = any(x in content for x in ["import ", "={", "def ", "class ", "ro.", "persist.", "#!/"])
        if not (is_long or is_code):
            if any(k in content.lower() for k in ["验证码", "code", "校验码"]):
                m = re.search(r'\b\d{2,3}\s?\d{2,3}\b', content)
                if m: pure = m.group().replace(" ", "")

        created_ts = int(time.time())
        now = datetime.datetime.fromtimestamp(created_ts).strftime("%m-%d %H:%M:%S")
        if pure:
            conn.execute(
                "INSERT INTO codes (user_id,device,content,pure_code,created_at,created_ts,is_favorite) VALUES (?,?,?,?,?,?,0)",
                (device['user_id'], device['name'], content, pure, now, created_ts),
            )
            payload = {'type':'code','content':content,'pure_code':pure,'device':device['name'],'device_id':device['id'],'event_id':event_id}
        else:
            conn.execute(
                "INSERT INTO clips (user_id,device,content,created_at,created_ts,is_favorite) VALUES (?,?,?,?,?,0)",
                (device['user_id'], device['name'], content, now, created_ts),
            )
            payload = {'type':'clip','content':content,'device':device['name'],'device_id':device['id'],'event_id':event_id}
        event_cursor = conn.execute(
            "INSERT INTO sync_events (user_id,source_device_id,item_type,content,pure_code,created_at,created_ts,event_id) VALUES (?,?,?,?,?,?,?,?)",
            (device['user_id'], device['id'], payload['type'], content, pure, now, created_ts, event_id),
        )
        payload['revision'] = event_cursor.lastrowid
        conn.execute(
            "UPDATE devices SET last_seen_at=?,last_sync_at=? WHERE id=?",
            (created_ts, created_ts, device['id']),
        )
        recipients = [row['id'] for row in conn.execute(
            "SELECT id FROM devices WHERE user_id=? AND id<>? AND sync_mode IN ('both','receive_only')",
            (device['user_id'], device['id']),
        ).fetchall()]
        conn.commit()
    for recipient_id in recipients:
        socketio.emit('clipboard_update', payload, to=f'device_{recipient_id}')
    return {"status":"ok", "event_id":event_id, "revision":payload['revision']}

@app.route('/api/latest')
def api_latest():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    device = find_device_by_token(token)
    if not device:
        return {"error": "unauthorized"}, 403
    if device['sync_mode'] not in {'both', 'receive_only'}:
        return {"status": "disabled", "revision": 0}
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM sync_events WHERE user_id=?",
            (device['user_id'],),
        ).fetchone()[0]
        row = conn.execute("""
            SELECT e.id, e.item_type, d.name AS device, e.source_device_id AS device_id,
                   e.content, e.pure_code, e.created_at
            FROM sync_events e LEFT JOIN devices d ON d.id=e.source_device_id
            WHERE e.user_id=? AND e.source_device_id<>?
            ORDER BY e.id DESC LIMIT 1
        """, (device['user_id'], device['id'])).fetchone()
    if not row:
        return {"status": "empty", "revision": cursor}
    mark_device_synced(device['id'])
    return {
        "status": "ok",
        "revision": cursor,
        "type": row['item_type'],
        "device": row['device'],
        "device_id": row['device_id'],
        "content": row['content'],
        "pure_code": row['pure_code'],
        "created_at": row['created_at'],
    }

@app.route('/api/ack', methods=['POST'])
def api_ack():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    device = find_device_by_token(token)
    if not device:
        return {"error": "unauthorized"}, 403
    mark_device_synced(device['id'])
    return {"status": "ok"}

@app.route('/api/poll')
def api_poll():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    device = find_device_by_token(token)
    if not device:
        return {"error": "unauthorized"}, 403
    try:
        after = max(0, int(request.args.get('after', 0)))
        timeout = min(30, max(0, int(request.args.get('timeout', 25))))
    except ValueError:
        return {"error": "invalid cursor"}, 400
    if device['sync_mode'] not in {'both', 'receive_only'}:
        if timeout:
            socketio.sleep(min(timeout, 5))
        return {"status": "disabled", "revision": after}
    deadline = time.monotonic() + timeout
    cursor = after
    while True:
        with get_db() as conn:
            cursor = conn.execute(
                "SELECT COALESCE(MAX(id),?) FROM sync_events WHERE user_id=?",
                (after, device['user_id']),
            ).fetchone()[0]
            row = conn.execute("""
                SELECT e.id, e.item_type, d.name AS device, e.source_device_id AS device_id,
                       e.content, e.pure_code, e.created_at
                FROM sync_events e LEFT JOIN devices d ON d.id=e.source_device_id
                WHERE e.user_id=? AND e.source_device_id<>? AND e.id>?
                ORDER BY e.id DESC LIMIT 1
            """, (device['user_id'], device['id'], after)).fetchone()
        if row:
            mark_device_synced(device['id'])
            return {
                "status": "ok",
                "revision": cursor,
                "type": row['item_type'],
                "device": row['device'],
                "device_id": row['device_id'],
                "content": row['content'],
                "pure_code": row['pure_code'],
                "created_at": row['created_at'],
            }
        if time.monotonic() >= deadline:
            return {"status": "timeout", "revision": cursor}
        socketio.sleep(0.5)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
