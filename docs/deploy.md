# Deploying the hosted worker

The live companion at `https://nfl.austingreen.net`, private behind a Cloudflare Access login (decided 2026-10-13). The worker runs in Docker on a
Hetzner server (2026-10-15: Fly.io's shared CPU is throttled to 1/16 of a core, too slow for the simulator).
The architecture is in `hosting-plan.md`. This file lists the set-up steps in order. Steps 1–5 happen in the
Cloudflare and Hetzner dashboards; step 6 is the first deploy.

```
Hetzner server       ──put_object──▶  R2 bucket "nflsim"  ──custom domain──▶  nfl.austingreen.net
nflsim worker --publish r2                                     Cloudflare CDN + Access login
```

## 1. Move austingreen.net's DNS to Cloudflare

The domain stays registered at Squarespace; only the name servers change.

1. Create a free Cloudflare account and **Add a domain**: `austingreen.net`, Free plan. Cloudflare scans the
   existing records.
2. Compare the imported records with this list (taken from public DNS on 2026-10-13) and fix any differences:

   | Type | Name | Content | Proxy |
   |---|---|---|---|
   | A | `@` | 185.199.108.153, 185.199.109.153, 185.199.110.153, 185.199.111.153 (four records) | DNS only |
   | CNAME | `www` | `ausgreen.github.io` | DNS only |
   | MX | `@` | `aspmx.l.google.com` (1), `alt1.aspmx.l.google.com` (5), `alt2.aspmx.l.google.com` (5), `alt3.aspmx.l.google.com` (10), `alt4.aspmx.l.google.com` (10) | — |
   | TXT | `@` | `v=spf1 include:_spf.google.com ~all` | — |

   - Delete the A record `@ → 198.49.23.144` if it is imported. It's a Squarespace address left next to GitHub's,
     so some visitors to the bare domain miss your GitHub Pages site.
   - Keep the GitHub records **DNS only** (grey cloud), so GitHub Pages keeps renewing its HTTPS certificate.
   - The MX and TXT records are your Google Workspace email. If they're missing or wrong, mail stops arriving.
3. In Squarespace (Domains → austingreen.net → DNS): turn **DNSSEC off** if it's on, then set **custom name
   servers** to the two Cloudflare gives you.
4. Wait for Cloudflare to say the domain is **Active** (minutes to a few hours). Then check:
   `dig +short NS austingreen.net` shows Cloudflare, `https://www.austingreen.net` loads, and an email to the domain
   arrives.

## 2. R2 bucket on nfl.austingreen.net

1. **R2 Object Storage**: enable it. It asks for a payment method even on the free tier (10 GB storage, 1M writes
   and 10M reads a month; this site uses a fraction).
2. **Create bucket** `nflsim` (location: automatic).
3. Bucket → **Settings → Custom Domains → Connect domain**: `nfl.austingreen.net`. Leave the `r2.dev` public URL
   **disabled**: it would bypass the Access login.
4. **R2 → Manage API tokens → Create API token**: permission *Object Read & Write*, applied to bucket `nflsim`
   only. Copy the **Access Key ID**, the **Secret Access Key** (shown once) and your **Account ID** (R2 overview
   page). Keep them in a password manager; they go in `.env` (gitignored), never into the repo.

## 3. Cache rule

By default Cloudflare's CDN doesn't cache `.json` or `.html`, so every viewer request would reach R2. Make it follow
the worker's `Cache-Control` headers (3 s for game states, none for `status.json`, 5 min for the page):

**Caching → Cache Rules → Create rule**: hostname equals `nfl.austingreen.net` → *Eligible for cache*; Edge TTL:
*Use cache-control header if present, bypass cache if not*; Browser TTL: *Respect origin*.

## 4. Private access (Cloudflare Access)

1. **Zero Trust** (free up to 50 users): choose a team name when asked.
2. **Access → Applications → Add → Self-hosted**: name `NFL companion`, domain `nfl.austingreen.net`, session
   24 hours or longer.
3. Policy: **Allow**, include **Emails**: your address and anyone else you invite.
4. Login method: **One-time PIN** (a code by email; no other set-up).

Check in a private window that `https://nfl.austingreen.net` shows the Cloudflare login page. Before the first
deploy the site itself returns 404, because the bucket is empty.

## 5. Hetzner server

1. Create a Hetzner Cloud account and a project (`nflsim`).
2. **Add server**:
   - Location: any EU one (Nuremberg, Falkenstein or Helsinki); the US locations don't offer the cheap plans.
   - Image: **Ubuntu 24.04**.
   - Type: **Shared vCPU, x86, CX22** (2 vCPU, 4 GB; building the simulator set-up peaks at ~2.1 GB). Or the
     cheapest current x86 plan with 2 vCPU and 4 GB. About €4–6 a month; check current prices.
   - Networking: public IPv4 on (ESPN and R2 are reached over IPv4).
   - SSH key: add `~/.ssh/id_ed25519.pub`.
   - **Cloud config**: paste the contents of `deploy/cloud-init.yaml` (user `deploy` with your SSH key, Docker,
     firewall with SSH only, automatic security updates, root and password logins off).
   - Name: `nflsim`.
3. Optional: **Firewalls → Create firewall** with one inbound rule (TCP 22) and apply it to the server. The server
   also runs its own firewall (ufw) with the same rule.
4. Wait ~3 minutes for cloud-init, then check: `ssh deploy@<server-ip> docker version`.

## 6. Deploy

From the repo, with `.env` filled in:

```sh
deploy/push.sh <server-ip>
```

It copies the code and data (`rsync`, ~230 MB the first time), `.env` (first time only), and this laptop's caches
(`data/live/`, only those the server doesn't have), then builds the image on the server and starts the worker
(`docker compose up -d --build`; restarts automatically, also after a reboot). Check:

```sh
ssh deploy@<server-ip> 'cd /opt/nflsim && docker compose logs -f --tail 50'
```

and `https://nfl.austingreen.net/status.json` (after the login): `updated` is recent, `errors` is `{}`.

## Operating it

| What | How (`ssh deploy@<server-ip>`, then `cd /opt/nflsim`) |
|---|---|
| Logs | `docker compose logs -f --tail 100` |
| Restart | `docker compose restart` |
| Stop (off-season) | `docker compose stop`; back with `docker compose start` (or delete the server and recreate it) |
| New code or data | from the laptop: `deploy/push.sh <server-ip>` (the server's caches are kept; the set-up rebuilds if `sim/` or the pbp files changed) |
| Starter change | edit `data/curated/roster.csv`, `deploy/push.sh`, then delete the week's cached predictions (below), which picked the old starter |
| Give someone access | add their email to the Access policy |
| New R2 keys | edit `/opt/nflsim/.env` on the server, `docker compose up -d` |
| Kalshi's live prices | the private key at `/opt/nflsim/secrets/kalshi.pem` (mode 600; mounted read-only at `/run/secrets`, never synced by `push.sh`), and `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH=/run/secrets/kalshi.pem` in `/opt/nflsim/.env`; without them the worker polls Kalshi's REST endpoint |

Until the weekly refresh (phase 4) exists, the server's data is whatever was in `data/` at the last push. Lines and
starters for a new week come from `schedules.csv`, so run `uv run nflsim fetch --since 2026` and `deploy/push.sh`
early in each game week. The week's v3 predictions are cached once computed; to recompute after a late line move,
delete the week's file on the server: `rm /opt/nflsim/data/live/live_v3_2026_wNN.parquet` and `docker compose restart`.
