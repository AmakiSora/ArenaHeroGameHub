# ArenaGame Single-VPS Deployment

This release runs ArenaGame as one Python container:

- `dashboard.py` HTTP UI / API on `http://SERVER_IP:4399`
- `tactic.py` Arena Hero client (same container, background process)

It is intentionally a single replica. Runtime files (`map_memory.json`,
`tactic_config.json`, logs) live on a Docker volume.

## Security Notice

This deployment uses unencrypted HTTP. Anyone who can reach TCP port `4399`
can open the dashboard and change tactic config / production queue.

Restrict TCP `4399` to trusted source IPs, a VPN, or a private network in the
cloud firewall/security group. Do not expose this endpoint broadly on an
untrusted network. Keep `ARENA_HERO_API_KEY` only in server-side `.env`.

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
2. Write remote `.env` with `ARENA_HERO_API_KEY`
3. Run `docker compose up --build --detach`
4. Smoke-check `http://127.0.0.1:4399/` and `/api/state`

## Manual First Launch On The VPS

```bash
cd /srv/arena-game
cp .env.example .env
nano .env   # set ARENA_HERO_API_KEY
chmod 600 .env
docker compose up --build --detach
docker compose ps
docker compose logs --follow app
```

Verify:

```bash
curl -i http://SERVER_IP:4399/
curl -i http://SERVER_IP:4399/api/state
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

1. `http://SERVER_IP:4399/` loads the Chinese dashboard
2. `/api/state` returns JSON
3. `/api/config` responds (production targets live in the config form)
4. `docker compose logs app` shows tactic connecting when API key is set
5. New ticks appear in the dashboard after the tactic joins a game

## Ports

| Service    | Port |
|-----------|------|
| Dashboard | 4399 |
