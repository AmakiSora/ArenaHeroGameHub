# ArenaGame Single-VPS Deployment

This release runs ArenaGame as one Python container:

- `dashboard.py` HTTP UI / API on `http://SERVER_IP:4399`
- `tactic.py` Arena Hero client (same container, background process)

It is intentionally a single replica. Runtime files (`map_memory.json`,
`tactic_config.json`, logs) live on a Docker volume.

## Security Notice

This deployment uses unencrypted HTTP. **Access is gated by a shared token**:
every page and API route requires `DASHBOARD_TOKEN` (except requests from
loopback, which the container healthcheck and deploy smoke-tests use). Without
a valid token you cannot even view the dashboard, let alone change tactic
config / production queue.

- Token lives in `deploy/.env.deploy` locally and in the remote `/srv/arena-game/.env`
  (`DASHBOARD_TOKEN`). Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Log in by opening the dashboard and entering the token, or pass it on
  non-loopback calls as `Authorization: Bearer <token>` or `?token=<token>`.
- Restrict TCP `4399` to trusted source IPs / a VPN in the cloud
  firewall/security group as well. Keep `ARENA_HERO_API_KEY` and
  `DASHBOARD_TOKEN` only in server-side `.env`.

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
DEPLOY_USER=root
DEPLOY_PASSWORD=your-password-here
DEPLOY_REMOTE_BASE=/srv/arena-game
ARENA_HERO_API_KEY=ah_live_your_key
```

3. Install Paramiko if needed, then deploy:

```bash
pip install paramiko
python deploy/deploy.py
```

The script will:

1. SFTP project files to `DEPLOY_REMOTE_BASE`
2. Write remote `.env` with `ARENA_HERO_API_KEY` + `DASHBOARD_TOKEN`
3. Run `docker compose up --build --detach`
4. Smoke-check `http://127.0.0.1:4399/` and `/api/state`

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

After every deployment:

1. `http://SERVER_IP:4399/` shows the login page when no token is given, and
   the dashboard after entering the token
2. `/api/state` returns JSON (with a valid token)
3. `/api/config` responds (production targets live in the config form)
4. `docker compose logs app` shows tactic connecting when API key is set
5. New ticks appear in the dashboard after the tactic joins a game

## Ports

| Service    | Port |
|-----------|------|
| Dashboard | 4399 |
