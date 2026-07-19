# Clipboard Sync

Self-hosted clipboard synchronization for Windows and rooted Android. The server generates personalized Windows and Magisk packages with the current address and a one-time device credential already included.

## Quick start

Requirements: Docker Engine and Docker Compose v2.

```bash
git clone https://github.com/2909272751/clipboard-sync.git
cd clipboard-sync
chmod +x install.sh update.sh
./install.sh
```

Open `http://SERVER_IP:5000/setup`, create the first administrator, then use the **Devices** page:

- Android: download the personalized Magisk ZIP, flash it without extracting, and reboot.
- Windows: download the personalized ZIP, extract it, and run `安装并启动.cmd`.

No APK or Python is required on client devices. Windows starts automatically after sign-in. Android uses clipboard callbacks and stops remote polling while the screen is off.

The first account is the administrator. Registration is closed by default. From **Registration management**, the administrator can either temporarily enable public registration or generate a one-time invitation link that expires after seven days.

## Reliability and management

- Windows and Magisk clients keep the latest unsent clipboard item on disk. A temporary network outage or client restart does not lose it; a newer local copy replaces the older pending item.
- Windows records its receive cursor and automatically fetches the latest missed item after sleep, network recovery, or a realtime reconnect.
- The **Devices** page shows online state, client version, last activity, last successful sync, and whether the token is active.
- The **Devices** page can create a generic one-time-visible token for scripts and third-party clients; only its SHA-256 digest is stored.
- Each device can use bidirectional, send-only, receive-only, or paused mode without rebuilding its package.
- Users can change their own password. Administrators can disable ordinary accounts or assign a temporary password; disabling an account preserves its devices, files, and history.
- Clipboard and code history supports content, device, date, and page-size filters. The database retains the complete history unless the user explicitly deletes it.
- The web interface uses a responsive sidebar/mobile drawer, adaptive cards and forms, accessible focus states, and reduced-motion-aware transitions across desktop, tablet, and phone layouts.
- Versioned long-lived static caching, gzip responses, faster GPU-friendly transitions, and deferred rendering keep repeat visits and long history pages responsive on slower networks and phones.

## Automatic HTTPS

Point a domain's A/AAAA record to the server, allow ports 80 and 443, then run:

```bash
DOMAIN=sync.example.com ./install.sh
```

Caddy obtains and renews the certificate automatically. HTTPS is required for Internet-facing deployments because clipboard contents and device credentials are otherwise visible in transit.

## Updates and backups

```bash
git pull --ff-only
./update.sh
```

Persistent state is stored in `./data` by default (or `DATA_DIR` if configured). Back up that directory before an upgrade. `.env`, databases, uploads, logs, personalized packages, and build output are excluded from Git.

## Security defaults

- The first administrator can only be created while the database is empty. Public registration defaults to off; administrators can toggle it or create a hashed, one-time invitation from **Registration management**.
- Passwords are hashed and device tokens are stored only as SHA-256 digests.
- Re-downloading a device package rotates its token and invalidates the previous installation.
- Browser mutations require CSRF tokens; login and push endpoints are rate limited.
- Uploaded files are size limited, downloaded as attachments, and checked against the signed-in owner.
- Socket.IO is same-origin by default. Set `ALLOWED_ORIGINS` only when a separate trusted frontend is required.
- Forwarded client addresses are ignored by default. Set `TRUST_PROXY_HOPS` only when the app is behind that exact number of trusted reverse proxies; the bundled HTTPS setup configures it automatically.

Personalized ZIP files contain active device credentials. Never publish or forward them.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r server/app/requirements.txt
python -m unittest discover -s tests -v
```

Windows executables and Magisk ZIPs use versioned filenames. Release artifacts are generated from tagged source and are not committed except for the single Windows runtime embedded in the server image.

## License

MIT. See [LICENSE](LICENSE). Security reports should follow [SECURITY.md](SECURITY.md).
