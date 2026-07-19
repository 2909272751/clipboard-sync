import importlib.util
import io
import json
import hashlib
import os
import re
import sqlite3
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = REPO_ROOT / "server" / "app" / "app.py"
MAGISK_TEMPLATE = REPO_ROOT / "clients" / "magisk"
WINDOWS_TEMPLATE = REPO_ROOT / "clients" / "windows"
ANDROID_BRIDGE_SOURCE = REPO_ROOT / "android-bridge" / "src" / "com" / "clipsync" / "bridge" / "Main.java"
WINDOWS_CLIENT_SOURCE = WINDOWS_TEMPLATE / "clipboard_sync_windows.pyw"
APP_CSS = REPO_ROOT / "server" / "app" / "static" / "app.css"
TEMPLATE_DIR = REPO_ROOT / "server" / "app" / "templates"


class PersonalizedMagiskModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "db.sqlite"
        uploads = Path(cls.temp_dir.name) / "uploads"
        uploads.mkdir()

        os.environ["DB_PATH"] = str(cls.db_path)
        os.environ["UPLOAD_FOLDER"] = str(uploads)
        os.environ["MAGISK_TEMPLATE_DIR"] = str(MAGISK_TEMPLATE)
        os.environ["WINDOWS_TEMPLATE_DIR"] = str(WINDOWS_TEMPLATE)
        os.environ["SECRET_KEY"] = "test-only-secret-key-that-is-long-enough-for-tests"

        spec = importlib.util.spec_from_file_location("clipboard_sync_app", APP_FILE)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.module.app.template_folder = str(APP_FILE.parent / "templates")
        cls.module.app.config.update(TESTING=True)
        setup_client = cls.module.app.test_client()
        setup_client.get("/setup")
        with setup_client.session_transaction() as state:
            setup_csrf = state["_csrf_token"]
        response = setup_client.post(
            "/setup",
            data={
                "username": "test-user",
                "password": "test-password-123",
                "csrf_token": setup_csrf,
            },
        )
        if response.status_code != 302:
            raise RuntimeError("test administrator setup failed")

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        self.module._rate_events.clear()
        self.client = self.module.app.test_client()
        self.base_url = "https://sync.example.test"
        self.client.get("/login", base_url=self.base_url)
        with self.client.session_transaction(base_url=self.base_url) as state:
            login_csrf = state["_csrf_token"]
        response = self.client.post(
            "/login",
            data={
                "username": "test-user",
                "password": "test-password-123",
                "csrf_token": login_csrf,
            },
            base_url=self.base_url,
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction(base_url=self.base_url) as state:
            state["_csrf_token"] = "test-csrf-token"
            self.csrf = state["_csrf_token"]

    def secure_post(self, path, data=None, **kwargs):
        payload = dict(data or {})
        payload["csrf_token"] = self.csrf
        return self.client.post(path, data=payload, **kwargs)

    def test_create_download_contains_unique_device_credentials(self):
        response = self.secure_post(
            "/devices/magisk-module",
            data={"name": "朋友的手机"},
            base_url=self.base_url,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/zip", response.content_type)
        self.assertIn("clipboard-sync-v1.3.0-magisk-device-", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])

        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            names = set(archive.namelist())
            self.assertIn("module.prop", names)
            self.assertIn("service.sh", names)
            self.assertIn("action.sh", names)
            self.assertIn("framework/clipbridge.jar", names)
            self.assertIn("system/bin/clipboard-syncd", names)
            self.assertNotIn("magisk-clipboard-sync/module.prop", names)
            config = archive.read("config.conf").decode("utf-8")
            module_prop = archive.read("module.prop").decode("utf-8")

        self.assertIn("version=1.3.0", module_prop)
        self.assertNotIn("POLL_SECONDS", config)
        self.assertIn("SHOW_TOAST=1", config)
        self.assertIn("SERVER_URL='https://sync.example.test'", config)
        self.assertIn("DEVICE_NAME='朋友的手机'", config)
        self.assertRegex(config, r"PROVISION_ID='[0-9a-f]{32}'")
        token = re.search(r"DEVICE_TOKEN='([0-9a-f]{64})'", config).group(1)

        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT name, token, token_hash FROM devices WHERE name=? ORDER BY id DESC LIMIT 1",
                ("朋友的手机",),
            ).fetchone()
        self.assertEqual(row, ("朋友的手机", None, hashlib.sha256(token.encode()).hexdigest()))

    def test_download_requires_login(self):
        anonymous = self.module.app.test_client()
        response = anonymous.post(
            "/devices/magisk-module",
            data={"name": "unauthorized"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_setup_closes_after_first_admin_and_registration_defaults_closed(self):
        self.assertEqual(self.client.get("/setup").status_code, 302)
        self.assertEqual(self.client.get("/register").status_code, 404)
        self.assertEqual(self.client.get("/reset_pwd").status_code, 404)

    def test_admin_can_toggle_public_registration(self):
        enabled = self.secure_post(
            "/admin/invites",
            data={"action": "toggle_registration", "enabled": "1"},
            base_url=self.base_url,
        )
        self.assertEqual(enabled.status_code, 302)

        anonymous = self.module.app.test_client()
        page = anonymous.get("/register", base_url=self.base_url)
        self.assertEqual(page.status_code, 200)
        with anonymous.session_transaction(base_url=self.base_url) as state:
            register_csrf = state["_csrf_token"]
        username = f"public-user-{time.time_ns()}"
        created = anonymous.post(
            "/register",
            data={
                "username": username,
                "password": "public-password-123",
                "csrf_token": register_csrf,
            },
            base_url=self.base_url,
        )
        self.assertEqual(created.status_code, 302)

        disabled = self.secure_post(
            "/admin/invites",
            data={"action": "toggle_registration", "enabled": "0"},
            base_url=self.base_url,
        )
        self.assertEqual(disabled.status_code, 302)
        self.assertEqual(anonymous.get("/register", base_url=self.base_url).status_code, 404)

    def test_one_time_invitation_is_hashed_and_single_use(self):
        response = self.secure_post(
            "/admin/invites",
            data={"action": "create_invite"},
            base_url=self.base_url,
        )
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        match = re.search(r"https://sync\.example\.test/invite/([A-Za-z0-9_-]+)", page)
        self.assertIsNotNone(match)
        raw_token = match.group(1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute(
                "SELECT token_hash FROM invites ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(stored, hashlib.sha256(raw_token.encode()).hexdigest())
        self.assertNotEqual(stored, raw_token)

        invited = self.module.app.test_client()
        invite_path = f"/invite/{raw_token}"
        self.assertEqual(invited.get(invite_path, base_url=self.base_url).status_code, 200)
        with invited.session_transaction(base_url=self.base_url) as state:
            invite_csrf = state["_csrf_token"]
        created = invited.post(
            invite_path,
            data={
                "username": f"invited-{time.time_ns()}",
                "password": "invited-password-123",
                "csrf_token": invite_csrf,
            },
            base_url=self.base_url,
        )
        self.assertEqual(created.status_code, 302)
        self.assertEqual(invited.get(invite_path, base_url=self.base_url).status_code, 404)

    def test_browser_posts_require_csrf(self):
        response = self.client.post(
            "/devices/windows-package",
            data={"name": "missing-csrf"},
            base_url=self.base_url,
        )
        self.assertEqual(response.status_code, 400)

    def test_create_windows_download_contains_executable_and_config(self):
        response = self.secure_post(
            "/devices/windows-package",
            data={"name": "朋友的电脑"},
            base_url=self.base_url,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("clipboard-sync-v1.3.0-windows-device-", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            names = set(archive.namelist())
            self.assertIn("clipboard-sync-windows-v1.3.0.exe", names)
            self.assertIn("config.json", names)
            self.assertIn("安装并启动.cmd", names)
            self.assertIn("卸载.cmd", names)
            config = json.loads(archive.read("config.json").decode("utf-8"))

        self.assertEqual(config["server_url"], "https://sync.example.test")
        self.assertEqual(config["device_name"], "朋友的电脑")
        self.assertTrue(config["show_notifications"])
        self.assertRegex(config["device_token"], r"^[0-9a-f]{64}$")
        self.assertIsInstance(config["device_id"], int)

        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT name, token, token_hash, platform FROM devices WHERE id=?",
                (config["device_id"],),
            ).fetchone()
        self.assertEqual(
            row,
            (
                "朋友的电脑",
                None,
                hashlib.sha256(config["device_token"].encode()).hexdigest(),
                "windows",
            ),
        )

    def test_device_name_is_safely_shell_quoted(self):
        config = self.module.build_magisk_config(
            "https://sync.example.test",
            "a" * 64,
            "friend's phone; reboot",
            "b" * 32,
        )
        self.assertIn("DEVICE_NAME='friend'\"'\"'s phone; reboot'", config)

    def test_public_tree_has_no_runtime_or_personalized_data(self):
        forbidden_suffixes = {".sqlite", ".db", ".zip", ".jpg", ".jpeg"}
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "build" in path.parts:
                continue
            self.assertNotIn(path.suffix.lower(), forbidden_suffixes, str(path))
        config = (MAGISK_TEMPLATE / "config.conf").read_text(encoding="utf-8")
        self.assertRegex(config, r'(?m)^SERVER_URL=""$')
        self.assertRegex(config, r'(?m)^DEVICE_TOKEN=""$')

    def test_legacy_credentials_are_migrated_and_recovery_answers_removed(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.execute(
                "INSERT INTO users (username,password,question,answer,is_admin) VALUES (?,?,?,?,0)",
                (f"legacy-{time.time_ns()}", "legacy-password-123", "old-question", "old-answer"),
            )
            user_id = cursor.lastrowid
            raw_token = "3" * 64
            conn.execute(
                "INSERT INTO devices (user_id,name,token,token_hash,platform) VALUES (?,?,?,NULL,?)",
                (user_id, "legacy-device", raw_token, "generic"),
            )
            conn.commit()

        self.module.init_db()
        with closing(sqlite3.connect(self.db_path)) as conn:
            user = conn.execute(
                "SELECT password,question,answer FROM users WHERE id=?", (user_id,)
            ).fetchone()
            device = conn.execute(
                "SELECT token,token_hash FROM devices WHERE user_id=?", (user_id,)
            ).fetchone()
        self.assertTrue(user[0].startswith(("scrypt:", "pbkdf2:")))
        self.assertTrue(self.module.check_password_hash(user[0], "legacy-password-123"))
        self.assertEqual(user[1:], (None, None))
        self.assertEqual(device, (None, hashlib.sha256(raw_token.encode()).hexdigest()))

    def test_android_bridge_persists_latest_upload_and_previews_toasts(self):
        source = ANDROID_BRIDGE_SOURCE.read_text(encoding="utf-8")
        self.assertIn('new File(config.stateDirectory, "pending-upload.json")', source)
        self.assertIn("queueLatestUpload(text, digest)", source)
        self.assertIn("persistPendingLocked()", source)
        self.assertIn("pendingLock.wait(retrySeconds * 1000L)", source)
        self.assertIn('if ("ok".equals(status))', source)
        self.assertIn('showToast("已上传：" + toastPreview(text))', source)
        self.assertIn('showToast("已接收（" + toastPreview(device)', source)

    def test_magisk_service_enforces_one_supervisor(self):
        service = (MAGISK_TEMPLATE / "service.sh").read_text(encoding="utf-8")
        daemon = (MAGISK_TEMPLATE / "system" / "bin" / "clipboard-syncd").read_text(
            encoding="utf-8"
        )
        self.assertIn("kill -9", service)
        self.assertIn("pkill -9 -f 'clipboard-syncd'", service)
        self.assertIn("</dev/null", service)
        self.assertIn("supervisor already running as pid", daemon)

    def test_windows_client_notifies_only_confirmed_syncs(self):
        source = WINDOWS_CLIENT_SOURCE.read_text(encoding="utf-8")
        self.assertIn("class WindowsNotifier", source)
        self.assertIn('self.state_path = app_dir() / "client-state.json"', source)
        self.assertIn("self.replace_pending(text)", source)
        self.assertIn("def recovery_worker(self):", source)
        self.assertIn('f"{self.config[\'server_url\']}/api/poll"', source)
        self.assertIn('status == "ok"', source)
        self.assertIn('status == "ignored"', source)
        self.assertIn('"Clipboard Sync 已上传"', source)
        self.assertIn('"Clipboard Sync 已接收"', source)
        self.assertIn("notification_preview(text)", source)

    def test_download_address_follows_current_host_and_port(self):
        with self.module.app.test_request_context(
            "/devices", base_url="http://192.0.2.10:8088"
        ):
            self.assertEqual(
                self.module.get_public_base_url(), "http://192.0.2.10:8088"
            )

    def test_authenticated_pages_share_the_responsive_shell(self):
        self.assertEqual(self.client.get("/healthz").get_json()["version"], "1.3.1")
        for path in (
            "/", "/clips", "/codes", "/favorites", "/files", "/devices",
            "/account", "/admin/invites", "/admin/users",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, base_url=self.base_url)
                self.assertEqual(response.status_code, 200)
                page = response.get_data(as_text=True)
                self.assertIn('viewport-fit=cover', page)
                self.assertIn('/static/app.css', page)
                self.assertIn('id="main-content"', page)
                self.assertIn('class="mobile-topbar"', page)
                self.assertNotIn('user-scalable=no', page)

    def test_design_system_covers_viewports_motion_and_long_content(self):
        css = APP_CSS.read_text(encoding="utf-8")
        for marker in (
            "@media (max-width: 1180px)",
            "@media (max-width: 920px)",
            "@media (max-width: 700px)",
            "@media (max-width: 480px)",
            "@media (prefers-reduced-motion: reduce)",
            "@view-transition",
            "animation: page-enter",
            "overflow-wrap: anywhere",
            "min-width: 0",
        ):
            self.assertIn(marker, css)
        for template in TEMPLATE_DIR.glob("*.html"):
            self.assertNotIn('style=', template.read_text(encoding="utf-8"), template.name)

    def test_clipboard_page_searches_content_and_device(self):
        marker = f"search-marker-{time.time_ns()}"
        with closing(sqlite3.connect(self.db_path)) as conn:
            user_id = conn.execute(
                "SELECT id FROM users WHERE username=?", ("test-user",)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO clips (user_id,device,content,created_at,is_favorite) VALUES (?,?,?,?,0)",
                (user_id, "search-phone", marker, "01-01 00:00:00"),
            )
            conn.execute(
                "INSERT INTO clips (user_id,device,content,created_at,is_favorite) VALUES (?,?,?,?,0)",
                (user_id, "other-device", "must-not-match", "01-01 00:00:01"),
            )
            conn.commit()

        response = self.client.get(f"/clips?q={marker}", base_url=self.base_url)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn(marker, page)
        self.assertNotIn("must-not-match", page)

    def test_authenticated_socket_receives_clipboard_update(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            user_id = conn.execute(
                "SELECT id FROM users WHERE username=?", ("test-user",)
            ).fetchone()[0]
            phone_token = "1" * 64
            windows_token = "2" * 64
            conn.execute(
                "INSERT OR IGNORE INTO devices (user_id,name,token,token_hash,platform) VALUES (?,?,NULL,?,?)",
                (user_id, "socket-phone", hashlib.sha256(phone_token.encode()).hexdigest(), "android_magisk"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO devices (user_id,name,token,token_hash,platform) VALUES (?,?,NULL,?,?)",
                (user_id, "socket-windows", hashlib.sha256(windows_token.encode()).hexdigest(), "windows"),
            )
            conn.commit()

        socket_client = self.module.socketio.test_client(
            self.module.app,
            flask_test_client=self.client,
            auth={"token": windows_token},
        )
        self.assertTrue(socket_client.is_connected())
        content = f"socket-test-{time.time_ns()}"
        response = self.client.post(
            "/api/push",
            json={"content": content, "event_id": "test-event-id"},
            headers={"Authorization": f"Bearer {phone_token}"},
        )
        self.assertEqual(response.status_code, 200)
        received = socket_client.get_received()
        updates = [item for item in received if item["name"] == "clipboard_update"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["args"][0]["content"], content)
        self.assertEqual(updates[0]["args"][0]["event_id"], "test-event-id")
        socket_client.disconnect()

    def test_push_event_id_is_idempotent_and_records_client_status(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            user_id = conn.execute(
                "SELECT id FROM users WHERE username=?", ("test-user",)
            ).fetchone()[0]
            token = secrets_token = hashlib.sha256(str(time.time_ns()).encode()).hexdigest()
            cursor = conn.execute(
                "INSERT INTO devices (user_id,name,token_hash,platform) VALUES (?,?,?,?)",
                (user_id, "idempotent-device", hashlib.sha256(token.encode()).hexdigest(), "windows"),
            )
            device_id = cursor.lastrowid
            conn.commit()

        event_id = f"event-{time.time_ns()}"
        content = f"idempotent-content-{time.time_ns()}"
        headers = {
            "Authorization": f"Bearer {secrets_token}",
            "X-Client-Version": "1.3.0",
        }
        first = self.client.post("/api/push", json={"content": content, "event_id": event_id}, headers=headers)
        second = self.client.post("/api/push", json={"content": content, "event_id": event_id}, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.get_json()["duplicate"], True)
        self.assertEqual(first.get_json()["revision"], second.get_json()["revision"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM sync_events WHERE source_device_id=? AND event_id=?", (device_id, event_id)).fetchone()[0],
                1,
            )
            status = conn.execute(
                "SELECT client_version,last_seen_at,last_sync_at FROM devices WHERE id=?", (device_id,)
            ).fetchone()
        self.assertEqual(status[0], "1.3.0")
        self.assertTrue(status[1])
        self.assertTrue(status[2])

    def test_device_sync_modes_are_enforced(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username='test-user'").fetchone()[0]
            blocked_token = hashlib.sha256(f"blocked-{time.time_ns()}".encode()).hexdigest()
            receive_token = hashlib.sha256(f"receive-{time.time_ns()}".encode()).hexdigest()
            conn.execute(
                "INSERT INTO devices (user_id,name,token_hash,platform,sync_mode) VALUES (?,?,?,?,?)",
                (user_id, "receive-only", hashlib.sha256(blocked_token.encode()).hexdigest(), "generic", "receive_only"),
            )
            conn.execute(
                "INSERT INTO devices (user_id,name,token_hash,platform,sync_mode) VALUES (?,?,?,?,?)",
                (user_id, "send-only", hashlib.sha256(receive_token.encode()).hexdigest(), "generic", "send_only"),
            )
            conn.commit()

        blocked = self.client.post(
            "/api/push",
            json={"content": f"blocked-{time.time_ns()}", "event_id": f"blocked-{time.time_ns()}"},
            headers={"Authorization": f"Bearer {blocked_token}"},
        )
        self.assertEqual(blocked.get_json(), {"status": "ignored", "reason": "sending_disabled"})
        disabled_receive = self.client.get(
            "/api/latest", headers={"Authorization": f"Bearer {receive_token}"}
        )
        self.assertEqual(disabled_receive.get_json()["status"], "disabled")

    def test_poll_recovers_only_the_latest_missed_clipboard(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username='test-user'").fetchone()[0]
            sender = hashlib.sha256(f"sender-{time.time_ns()}".encode()).hexdigest()
            receiver = hashlib.sha256(f"receiver-{time.time_ns()}".encode()).hexdigest()
            conn.execute("INSERT INTO devices (user_id,name,token_hash,platform) VALUES (?,?,?,?)", (user_id, "recovery-sender", hashlib.sha256(sender.encode()).hexdigest(), "generic"))
            conn.execute("INSERT INTO devices (user_id,name,token_hash,platform) VALUES (?,?,?,?)", (user_id, "recovery-receiver", hashlib.sha256(receiver.encode()).hexdigest(), "windows"))
            conn.commit()
        first = f"missed-first-{time.time_ns()}"
        latest = f"missed-latest-{time.time_ns()}"
        for content in (first, latest):
            response = self.client.post(
                "/api/push",
                json={"content": content, "event_id": f"recover-{time.time_ns()}"},
                headers={"Authorization": f"Bearer {sender}"},
            )
            self.assertEqual(response.get_json()["status"], "ok")
        recovered = self.client.get(
            "/api/poll?after=0&timeout=0",
            headers={"Authorization": f"Bearer {receiver}", "X-Client-Version": "1.3.0"},
        ).get_json()
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["content"], latest)

    def test_history_pagination_filters_without_deleting_records(self):
        marker = f"pagination-{time.time_ns()}"
        created_ts = int(time.time())
        with closing(sqlite3.connect(self.db_path)) as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username='test-user'").fetchone()[0]
            conn.executemany(
                "INSERT INTO clips (user_id,device,content,created_at,created_ts,is_favorite) VALUES (?,?,?,?,?,0)",
                [(user_id, "pagination-device", f"{marker}-{index:02d}", "07-19 12:00:00", created_ts) for index in range(55)],
            )
            conn.commit()
        response = self.client.get(
            f"/clips?q={marker}&device=pagination-device&per_page=20&page=2",
            base_url=self.base_url,
        )
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("共 55 条", page)
        self.assertIn("当前第 2/3 页", page)
        with closing(sqlite3.connect(self.db_path)) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM clips WHERE user_id=? AND content LIKE ?",
                (user_id, f"{marker}%"),
            ).fetchone()[0]
        self.assertEqual(remaining, 55)

    def test_admin_can_disable_user_without_deleting_history(self):
        username = f"managed-{time.time_ns()}"
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.execute(
                "INSERT INTO users (username,password,is_admin) VALUES (?,?,0)",
                (username, self.module.generate_password_hash("managed-password-123")),
            )
            user_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO clips (user_id,device,content,created_at,created_ts) VALUES (?,?,?,?,?)",
                (user_id, "managed-device", "preserve-me", "07-19 12:00:00", int(time.time())),
            )
            conn.commit()
        response = self.secure_post(
            f"/admin/users/{user_id}/action",
            data={"action": "toggle_disabled"},
            base_url=self.base_url,
        )
        self.assertEqual(response.status_code, 302)
        with closing(sqlite3.connect(self.db_path)) as conn:
            state = conn.execute("SELECT disabled FROM users WHERE id=?", (user_id,)).fetchone()[0]
            history = conn.execute("SELECT COUNT(*) FROM clips WHERE user_id=?", (user_id,)).fetchone()[0]
        self.assertEqual(state, 1)
        self.assertEqual(history, 1)

    def test_user_can_change_own_password(self):
        username = f"password-user-{time.time_ns()}"
        old_password = "old-password-123"
        new_password = "new-password-456"
        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.execute(
                "INSERT INTO users (username,password,is_admin) VALUES (?,?,0)",
                (username, self.module.generate_password_hash(old_password)),
            )
            user_id = cursor.lastrowid
            conn.commit()

        user_client = self.module.app.test_client()
        user_client.get("/login", base_url=self.base_url)
        with user_client.session_transaction(base_url=self.base_url) as state:
            login_csrf = state["_csrf_token"]
        login = user_client.post(
            "/login",
            data={"username": username, "password": old_password, "csrf_token": login_csrf},
            base_url=self.base_url,
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(user_client.get("/account", base_url=self.base_url).status_code, 200)
        with user_client.session_transaction(base_url=self.base_url) as state:
            account_csrf = state["_csrf_token"]
        changed = user_client.post(
            "/account",
            data={
                "current_password": old_password,
                "new_password": new_password,
                "confirm_password": new_password,
                "csrf_token": account_csrf,
            },
            base_url=self.base_url,
        )
        self.assertEqual(changed.status_code, 302)
        with closing(sqlite3.connect(self.db_path)) as conn:
            stored = conn.execute("SELECT password FROM users WHERE id=?", (user_id,)).fetchone()[0]
        self.assertTrue(self.module.check_password_hash(stored, new_password))
        self.assertFalse(self.module.check_password_hash(stored, old_password))


if __name__ == "__main__":
    unittest.main()
