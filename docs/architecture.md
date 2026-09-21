# Architecture

## One file, no framework

The bot is a single bash script. No Python, no Node, no framework. This is
deliberate: the whole thing has to be droppable onto a fresh Ubuntu VPS
with nothing but `curl`, `jq`, and `docker` installed, and it has to be
readable by someone who is not a bash expert at 3 AM when Gemini is
broken.

The trade-off is that testing requires a little discipline (see
`tests/`), but the operational surface stays tiny.

## Long polling, not webhooks

Telegram offers two ways to receive updates: webhooks and long polling.

Webhooks require a public HTTPS endpoint with a valid certificate. That
is a lot of moving parts for a bot that only talks to one person. Long
polling only needs outbound HTTPS to `api.telegram.org`, which the
server already has.

The cost is that `getUpdates` blocks for `POLL_TIMEOUT` seconds. The main
loop is:

```
while true; do
  handle_updates        # blocks up to POLL_TIMEOUT seconds
  scheduled_check       # returns immediately unless CHECK_INTERVAL passed
done
```

`scheduled_check` uses a monotonic wall-clock comparison
(`now - last_check_ts < CHECK_INTERVAL`). It does not use `sleep`,
because `sleep` would block `getUpdates` and make commands feel laggy.
Instead, the polling loop itself provides the pacing.

## Google country detection

There is no clean, free, unauthenticated Google endpoint that returns the
country of the caller. Every option is a scrape:

1. `https://www.google.com` — the HTML contains `xx_YY` language-locale
   pairs. This is the least reliable source and is used only as a
   last resort.
2. `https://play.google.com/` — the page contains
   `"countryCode":"XX"` in its JavaScript. This is the primary source.
3. `https://accounts.google.com/` — the login page also contains
   `"countryCode":"XX"`. This is the fallback if Play is unreachable.

The probe goes through `--socks5-hostname 127.0.0.1:1080`, so the request
actually leaves through WARP. If WARP is down, `warp_alive()` returns
non-zero first and the probe is skipped entirely.

## Restart, not reconnect

When RU is detected, the bot runs `docker compose restart <service>`.

The alternative would be to exec into the container and run
`warp-cli disconnect && warp-cli connect`. That reconnects the existing
WARP registration without pulling a fresh one. In practice it often
reuses the same exit range, which does not clear Google's flag.

A full container restart forces a new registration handshake with
Cloudflare and usually lands on a different exit IP. That is the whole
point: a new IP means a clean slate with Google.

The downside is that the SOCKS5 port is briefly unavailable while the
container restarts. On a node that only serves AI tools, that is
acceptable. If your node also serves live traffic, schedule the check
during low-traffic windows or set `COMPOSE_SERVICE` to a dedicated
AI-only WARP instance.

## Cooldown

`RESTART_COOLDOWN` defaults to 300 seconds, the same as
`CHECK_INTERVAL`. Without it, a detection of RU would trigger a restart,
the next check five minutes later could still see RU (Google caches
geo-location for a while), and the bot would restart again — an endless
loop of pointless restarts.

With the cooldown, the bot restarts at most once per window. If the
first restart does not help, the next attempt happens after the cooldown,
giving WARP time to settle.

`/restart` from Telegram also respects the cooldown. If you need to
force a restart during the cooldown, wait it out or temporarily set
`RESTART_COOLDOWN=0` and restart the service.

## Multi-node

The bot is designed to run on several servers. Each node needs its own
bot token, because Telegram does not fan out updates to multiple
long-polling clients. All nodes can share the same `TG_CHAT_ID`, so
every notification lands in one chat.

`NODE_NAME` is prepended to every outgoing message as
`[<code>name</code>]`. That makes a chat with ten nodes readable.

The state directory (`/var/lib/warp-bot`) is per-node. It holds the last
restart timestamp and the Telegram update offset. Do not share it between
nodes.

## systemd sandboxing

The unit runs the script with:

- `NoNewPrivileges=true`
- `ProtectSystem=full`
- `ProtectHome=true`
- `PrivateTmp=true`

The script needs to write only to `/var/lib/warp-bot` and to talk to
Docker. `ProtectSystem=full` makes everything outside `/var` read-only,
which is enough for `docker compose restart`. `PrivateTmp` gives the
service its own `/tmp`, so any temporary file it creates cannot collide
with the host or with other services.

If you later switch to `docker exec warp warp-cli ...` and find that the
sandbox blocks it, add `ProtectSystem=strict` with an explicit
`ReadWritePaths=` for the docker socket directory. The current setup
does not need that.