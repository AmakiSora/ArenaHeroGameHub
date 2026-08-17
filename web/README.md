# Arena Web Console (/arena)

Real-time manual control console for the ArenaGame bot account, adapted from
[arena-hero-web](https://github.com/arena-hero/arena-hero-web) (Apache-2.0).

## How it is deployed

The dashboard (`../dashboard.py`) serves the Vite build output under `/arena`
and reverse-proxies the game API, so this app is **same-origin and
credential-free** in the browser:

| Browser request | Dashboard behavior |
|---|---|
| `GET /arena/*` | Static files from `web/dist`, SPA fallback to `index.html` |
| `GET /api/v1/leaderboard`, `/api/v1/me`, `/api/v1/me/stats` | Forwarded to `api.arenahero.io` with `Authorization: Bearer $ARENA_HERO_API_KEY` |
| `POST /api/v1/game/commands` | Forwarded with the key; `Idempotency-Key` passed through |
| `WS /api/v1/game/ws` | Handshake terminates in the dashboard (browsers cannot send `Authorization` on upgrades); the upstream socket carries the key, frames piped byte-for-byte |

Manual plans (`MANUAL` source) are stored separately from the bot's `AGENT`
plans, so using this console does not cancel the tactic process.

The original login/registration/OAuth flow was removed; `AuthContext` now
provides a static proxy identity. All dashboard routes still require the
existing `DASHBOARD_TOKEN`.

## Local development

```
npm install
npm run dev        # http://localhost:3000/arena, proxies /api to localhost:4399
```

Start the dashboard first (`python dashboard.py`) so the dev proxy has a
backend. Production bundle: `npm run build` (output in `dist/`, also built
inside the Docker image).

## Tests and lint

```
npm run lint
npm run test
```
