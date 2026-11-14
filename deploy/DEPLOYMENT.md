# ArenaGame Single-VPS Deployment

This release runs ArenaGame as one Python container:

- `dashboard.py` HTTP UI / API on `http://SERVER_IP:4399`
- `tactic.py` Arena Hero client (same container, background process)

It is intentionally a single replica. Runtime files (`map_memory.json`,
`tactic_config.json`, logs) live on a Docker volume.

## Security Notice

This deployment uses unencrypted HTTP. **Access is gated by a shared token**:
non-loopback requests to pages and API routes require `DASHBOARD_TOKEN` (the
container healthcheck probes `/api/health` over loopback). Without a valid
token you cannot even view the dashboard, let alone change tactic config /
production queue.

- Token lives in `deploy/.env.deploy` locally and in the remote `/srv/arena-game/.env`
  (`DASHBOARD_TOKEN`). Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Log in by opening the dashboard and entering the token, or pass it on
  non-loopback calls as `Authorization: Bearer <token>` or `?token=<token>`.
- Restrict TCP `4399` to trusted source IPs / a VPN in the cloud
  firewall/security group as well. Keep `ARENA_HERO_API_KEY` and
  `DASHBOARD_TOKEN` only in server-side `.env`.

### SSH host keys are pinned

The deploy channel ships the live game API key and the dashboard token, so the
server's SSH host key is now verified instead of blindly accepted:

- `deploy/.ssh_known_hosts` (gitignored, host-specific) holds the pinned keys.
  Every deploy starts with `RejectPolicy`: an unknown key is **refused**, and the
  script prints its SHA256 fingerprint plus what to do.
- First deploy from a new machine: verify the fingerprint out-of-band
  (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the VPS), then run once
  with `DEPLOY_TRUST_NEW_HOST=1`. The key is printed, appended to
  `deploy/.ssh_known_hosts` and the connection is retried.
- A *changed* key is never auto-accepted, even with `DEPLOY_TRUST_NEW_HOST=1`:
  a mismatch prints both fingerprints and stops. A rebuilt server means removing
  the stale line from `deploy/.ssh_known_hosts` by hand.
- Key auth is preferred: set `DEPLOY_KEY_PATH` (+ `DEPLOY_KEY_PASSPHRASE` if the
  key is encrypted). Password auth via `DEPLOY_PASSWORD` keeps working.
- `DEPLOY_USER` should be a dedicated non-root account with docker group access;
  when it is unset the scripts fall back to `root` and print a warning.
- Tokens are masked (`abc...yz`) in everything the deploy prints, including the
  smoke-test curl commands and the remote `.env` summary.

## Prerequisites

- A Linux VPS with Docker Engine and the Docker Compose plugin
- SSH access for the deployment account
- Outbound network access from the VPS to Arena Hero
- Firewall/security group exposing SSH and TCP `4399` only to intended clients

## Deploy From Local Machine

1. Copy credentials template:

```bash
cp deploy/.env.deploy.example deploy/.env.deploy
```

2. Fill in `deploy/.env.deploy`:

```env
DEPLOY_HOST=your-server-ip
DEPLOY_PORT=22
DEPLOY_USER=deploy                 # non-root account with docker group access
DEPLOY_REMOTE_BASE=/srv/arena-game

# Pick one auth method:
DEPLOY_KEY_PATH=C:/Users/you/.ssh/arenagame_deploy
# DEPLOY_KEY_PASSPHRASE=...
# DEPLOY_PASSWORD=your-password-here

ARENA_HERO_API_KEY=ah_live_your_key
DASHBOARD_TOKEN=your-dashboard-token
```

3. Install the dev/deploy dependencies, then deploy:

```bash
pip install -r requirements.txt -r requirements-dev.txt   # paramiko lives here
python deploy/deploy.py
```

On the first run against a new server the host key is unknown: the script stops,
prints the SHA256 fingerprint and the one-line instruction. Verify it, then
re-run with `DEPLOY_TRUST_NEW_HOST=1` (PowerShell: `$env:DEPLOY_TRUST_NEW_HOST="1"`).

The script will:

1. Print the **upload plan** (file count + MB) before connecting. Only the
   manifest in `deploy/deploy.py` is uploaded — the working tree is not walked,
   so a stray `_remote_*.json` dump can never be pushed again.
2. SFTP those files to `DEPLOY_REMOTE_BASE`
3. Write remote `.env` with `ARENA_HERO_API_KEY` + `DASHBOARD_TOKEN` (masked in
   the console output)
4. Run `docker compose up --build --detach`
5. Smoke-check `http://127.0.0.1:4399/`, `/api/health` and `/api/state`:
   both JSON endpoints must answer `{"ok": true}`, not merely HTTP 200. A
   `/api/health` **503** right after a deploy means "container up, bot not ticking
   yet" and is reported as a warning instead of failing the deploy.

## Manual First Launch On The VPS

```bash
cd /srv/arena-game
cp .env.example .env
nano .env   # set ARENA_HERO_API_KEY and DASHBOARD_TOKEN
chmod 600 .env
docker compose up --build --detach
docker compose ps
docker compose logs --follow app
```

Verify:

```bash
# From a remote client (non-loopback) you must pass the token:
curl -i http://SERVER_IP:4399/ -H "Authorization: Bearer YOUR_TOKEN"
curl -i http://SERVER_IP:4399/api/state -H "Authorization: Bearer YOUR_TOKEN"
```

`docker compose ps` should show `0.0.0.0:4399->4399/tcp`.

## Runtime Data And Restarts

Persistent volume: `arena-game-runtime` mounted at `/app/runtime`.

On start, the entrypoint links known runtime files into `/app` so dashboard and
tactic share config, map memory, queue DB, and logs across restarts.

### Process supervision (`docker-entrypoint.py`)

A container being `healthy` no longer implies the bot is running — the
entrypoint now owns liveness:

| Env var | Default | Behaviour |
|---|---|---|
| `TACTIC_MAX_CRASHES` | `10` | more than this many non-zero tactic exits within 10 minutes makes the entrypoint exit `1`, so `restart: unless-stopped` restarts the **container** instead of looping forever |
| `TACTIC_STALL_SECONDS` | `180` | tactic alive but `tactic_log.jsonl` (or its `.1` rotation) not written for this long → SIGTERM, then SIGKILL, then the normal restart+backoff path. Never trips during the first 120 s of a start or when `ARENA_HERO_API_KEY` is unset |
| `PLAY_LOG_MAX_MB` | `20` | `tactic_play.log` is rotated before it is opened; 2 backups (`.1`, `.2`) are kept |

Restarts get exponential backoff 2 s → 4 → 8 → 16 → 30 s (cap). The dashboard
process is monitored too: if it exits, the container exits with its code.

Container stdout is capped by compose (`logging: json-file`, 10 MB × 3) on top
of the `tactic_play.log` rotation. Logs are plain stdout — there is no
`LOG_LEVEL` setting to configure.

Back up before upgrades:

```bash
mkdir -p backups
docker run --rm \
  -v arena-game-runtime:/data:ro \
  -v "$PWD/backups":/backup \
  alpine:3.21 sh -c 'tar czf /backup/arena-game-runtime-$(date +%F-%H%M%S).tgz -C /data .'
```

Restore only while stopped:

```bash
docker compose down
docker run --rm \
  -v arena-game-runtime:/data \
  -v "$PWD/backups":/backup:ro \
  alpine:3.21 sh -c 'rm -rf /data/* && tar xzf /backup/FILE.tgz -C /data'
docker compose up --detach
```

Never run `docker compose down --volumes` unless intentionally deleting runtime data.

## Updating And Rollback

Local one-shot update:

```bash
python deploy/deploy.py
```

Or on the VPS:

```bash
cd /srv/arena-game
# refresh files, then:
docker compose up --build --detach
docker compose ps
docker compose logs --tail=100 app
```

A replacement container briefly interrupts the dashboard and reconnects the
tactic WebSocket session.

## Smoke Test

After every deployment (the deploy script does steps 1–2 automatically):

1. `http://SERVER_IP:4399/` shows the login page when no token is given, and
   the dashboard after entering the token
2. `/api/health` and `/api/state` answer **`{"ok": true}`** with a valid token.
   `/api/state` is a compact tick probe; `?full=1` renders the full panel
   payload (the whole remembered map) and is deliberately not the probe target.
   A `/api/health` **503** means the container is up but the newest tick is stale
   (tactic still starting, or the bot is not ticking) — the deploy script reports
   it as a WARNING and leaves the deploy marked successful.
3. `/api/config` responds (production targets live in the config form)
4. `docker compose logs app` shows tactic connecting when API key is set
5. New ticks appear in the dashboard after the tactic joins a game
6. `docker compose ps` reports `healthy` after the 60 s start period; if it stays
   `unhealthy`, check `docker compose logs app` for `[entrypoint] STALL` /
   restart lines.

## Ports

| Service    | Port |
|-----------|------|
| Dashboard | 4399 |
