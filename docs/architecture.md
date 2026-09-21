# Architecture

## Why two modes

1–3 nodes fit into a single bash script that talks to Telegram directly.
Beyond that, the operational cost of N independent bots grows faster
than N: N chats, N tokens, N places to look when something breaks.

Fleet mode splits the problem. Agents only do local work (probe Google,
restart WARP, report). The coordinator owns the Telegram bot and the
state. Adding a node is one CLI call and one systemd unit.

The two modes do not share code. Bash and Python do not share logic
without pain, and the logic is small enough that duplication is cheaper
than the abstraction.

## Single mode

```
Telegram ◄─long-poll─ warp-bot.sh ──socks5──► WARP ──► Google
                          │
                          └── docker compose restart
```

The script long-polls Telegram, checks Google every `CHECK_INTERVAL`
seconds through the WARP SOCKS5, and restarts the compose service on RU.
State lives in `/var/lib/warp-bot`.

## Fleet mode

```
Telegram ◄─long-poll─ coordinator ──HTTP──► agent@node-N ──► WARP
                          │                       │
                          │ SQLite                └── docker compose restart
                          └─ nodes, commands, results
```

Agents poll the coordinator. Nothing is pushed to nodes. The
coordinator keeps:

- `nodes` — name, hashed token, last heartbeat, last known Google country
- `commands` — queued restarts and their results
- `meta` — Telegram offset and misc key/values

## Why polling, not push

If the coordinator could push commands to agents, it would need an
inbound channel to every node: SSH, an open HTTP port, or a queue
system the agents subscribe to. That is more surface area, more
firewall rules, and one more failure mode per node.

Polling inverts that. Agents initiate every connection. The coordinator
never has to reach them. A node behind NAT, on a dynamic IP, with no
open ports, works fine.

The cost is latency: a queued command is picked up within
`POLL_INTERVAL` seconds (default 5). For a manual restart that is
imperceptible.

## Why SQLite

30 agents writing one row per minute is 30 writes per minute. SQLite
handles that trivially, and the file is easy to back up (`cp`) and
inspect (`sqlite3`).

If you outgrow SQLite — say, hundreds of nodes — swap it for Postgres.
The schema is small and the queries are simple.

## Why the agent re-probes Google locally

The coordinator does not need to know whether Google is reachable — it
has no WARP. Each agent probes Google through its own WARP SOCKS5 and
reports the country in the heartbeat. The coordinator just stores and
displays.

That means two nodes on different continents may report different
countries, and that is correct: their WARP exit IPs differ.

## Why the coordinator does not store tokens in cleartext

Well, it does — SQLite stores the token as issued. Rotating the token
is a matter of `add-node <name>` again. If you want hashed tokens,
hash on receipt and compare with `secrets.compare_digest` after
hashing the presented one. The current design trades that hardening
for simplicity; the tokens never leave the private network.

## Sandboxing

Both systemd units run with:

- `NoNewPrivileges=true`
- `ProtectSystem=full`
- `ProtectHome=true`
- `PrivateTmp=true`

The coordinator writes only to `/var/lib/warp-coordinator`. The agent
writes only to `/var/lib/warp-agent` and invokes `docker compose`.

If you later switch the agent to `docker exec warp warp-cli ...` and
find the sandbox blocks it, add `ProtectSystem=strict` with
`ReadWritePaths=` for the docker socket directory. The current design
does not need that.