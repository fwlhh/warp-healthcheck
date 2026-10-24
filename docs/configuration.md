# Configuration

## Single mode — `/etc/warp-bot.env`

| Variable           | Required | Default                | Description                                   |
|--------------------|----------|------------------------|-----------------------------------------------|
| `TG_TOKEN`         | yes      | —                      | Telegram bot token                            |
| `TG_CHAT_ID`       | yes      | —                      | Numeric id allowed to talk to the bot         |
| `NODE_NAME`        | yes      | —                      | Prefix for outgoing messages, `[a-zA-Z0-9._-]+` |
| `WARP_PROXY`       | no       | `127.0.0.1:1080`       | SOCKS5 endpoint                               |
| `COMPOSE_DIR`      | no       | `/opt/warp`            | Directory of the WARP compose file            |
| `COMPOSE_FILE`     | no       | `docker-compose.yml`   | Compose filename                              |
| `COMPOSE_SERVICE`  | no       | `warp`                 | Service name to restart                       |
| `WARP_NAME`        | no       | the service name       | How the first WARP is called in messages      |
| `WARPn_PROXY`, `WARPn_SERVICE` | for n ≥ 2 | —       | Another WARP (`WARP2_…`, `WARP3_…`, no limit): its SOCKS5 endpoint and compose service. Optional: `WARPn_NAME`, `WARPn_COMPOSE_DIR`, `WARPn_COMPOSE_FILE`. `127.0.0.1:1081:1080` (docker's ports form) is accepted |
| `BAD_COUNTRIES`    | no       | `RU`                   | Countries where Google refuses the service; Google seeing one of them restarts that WARP (space or comma separated; set but empty = never) |
| `CHECK_INTERVAL`   | no       | `300`                  | Seconds between Google checks                 |
| `RESTART_COOLDOWN` | no       | `60`                   | Minimum seconds between restarts              |
| `POLL_TIMEOUT`     | no       | `25`                   | Telegram long-polling timeout                 |
| `POLL_BACKOFF_MIN` | no       | `5`                    | First retry delay (s) after a failed Telegram poll |
| `POLL_BACKOFF_MAX` | no       | `60`                   | Cap for the doubling retry delay (s)          |
| `CURL_TIMEOUT`     | no       | `10`                   | Timeout for Google probe                      |
| `STATE_DIR`        | no       | `/var/lib/warp-bot`    | Where the restart timestamp lives             |

## Fleet — `/etc/warp-coordinator.env`

| Variable      | Required | Default                                  | Description                          |
|---------------|----------|------------------------------------------|--------------------------------------|
| `TG_TOKEN`    | yes      | —                                        | Telegram bot token                   |
| `TG_CHAT_ID`  | yes      | —                                        | Authorized user id                   |
| `HTTP_HOST`   | no       | `0.0.0.0`                                | Bind address for the HTTP API        |
| `HTTP_PORT`   | no       | `8080`                                   | Bind port                            |
| `HTTP_SOCKET_TIMEOUT` | no | `30`                                 | Seconds before an idle/stalled HTTP client is dropped |
| `DB_PATH`     | no       | `/var/lib/warp-coordinator/coordinator.db` | SQLite path                        |
| `STALE_AFTER` | no       | `180`                                    | Node considered stale after N seconds |
| `BAD_COUNTRIES` | no | `RU` | Countries shown with a 🟡 warning in `/nodes` and `/status`, and reported as "still XX" after a restart; use the agents' value |
| `COUNTRY_NAMES` | no | _(empty)_ | Extra names for country codes, e.g. `XK=Kosovo,AQ=Antarctica`. About 90 countries are built in; any other valid code still shows its flag and code |
| `RETRY_UNKNOWN_LIMIT` | no | `2`                                  | Extra restarts after one ends with unknown Google country; `0` disables |
| `RETRY_UNKNOWN_DELAY` | no | `90`                                 | Seconds before such a retry is delivered; keep above the agents' `RESTART_COOLDOWN` |
| `NODE_ALERTS` | no | `1` | Message when a node stops reporting or loses WARP, and when it recovers; `0` disables |
| `WARP_DOWN_ALERT_AFTER` | no | `120` | Seconds WARP must stay down before alerting (ordinary restarts do not page) |
| `MONITOR_INTERVAL` | no | `30` | Seconds between monitor passes |
| `DEADMAN_URL` | no | _(empty)_ | URL pinged every `DEADMAN_INTERVAL` s while the coordinator can still alert you (Telegram reachable, monitor running). Point it at a push monitor (healthchecks.io, Uptime Kuma) so *its* alarm tells you when the coordinator itself is gone |
| `DEADMAN_INTERVAL` | no | `60` | Seconds between pings |
| `COMMAND_TTL` | no | `600` | Seconds after which a command no node fetched is dropped (never below `RETRY_UNKNOWN_DELAY`+120) |
| `COMMAND_RETENTION_DAYS` | no | `30` | Finished commands older than this are deleted |

## Fleet — `/etc/warp-agent.env`

| Variable             | Required | Default              | Description                                 |
|----------------------|----------|----------------------|---------------------------------------------|
| `COORDINATOR_URL`    | yes      | —                    | Base URL, e.g. `http://10.0.0.1:8080`       |
| `NODE_NAME`          | yes      | —                    | Must match the name registered on coordinator |
| `NODE_TOKEN`         | yes      | —                    | Printed by `warp-coordinator add-node`      |
| `WARP_PROXY`         | no       | `127.0.0.1:1080`     | SOCKS5 endpoint                             |
| `COMPOSE_DIR`        | no       | `/opt/warp`          | WARP compose directory                      |
| `COMPOSE_FILE`       | no       | `docker-compose.yml` | WARP compose filename                       |
| `COMPOSE_SERVICE`    | no       | `warp`               | Service name                                |
| `WARP_NAME`          | no       | the service name     | How the first WARP is called in messages    |
| `WARPn_PROXY`, `WARPn_SERVICE` | for n ≥ 2 | —       | Another WARP on this node (`WARP2_…`, `WARP3_…`, no limit); see *Several WARPs on one node* |
| `WARPn_NAME`, `WARPn_COMPOSE_DIR`, `WARPn_COMPOSE_FILE` | no | service name, `COMPOSE_DIR`, `COMPOSE_FILE` | Optional parts of that group |
| `BAD_COUNTRIES`      | no       | `RU`                 | Countries where Google refuses the service; Google seeing one of them as the exit's country restarts that WARP (space or comma separated) |
| `HEARTBEAT_INTERVAL` | no       | `60`                 | Seconds between heartbeats                  |
| `POLL_INTERVAL`      | no       | `5`                  | Seconds between command polls               |
| `CHECK_INTERVAL`     | no       | `300`                | Seconds between Google checks               |
| `RESTART_COOLDOWN`   | no       | `60`                 | Minimum seconds between restarts, manual (`/restart`) and automatic alike |
| `CURL_TIMEOUT`       | no       | `30`                 | Google probe timeout                        |
| `STATE_DIR`          | no       | `/var/lib/warp-agent` | Where the restart timestamps live           |

### Several WARPs on one node

Each extra WARP is a container with its own SOCKS5 port, restarted through its
own docker compose service. Add one by appending three lines with the next
number to `/etc/warp-agent.env`; there is no limit, the agent discovers every
`WARPn_*` group:

```
WARP2_NAME=warp2
WARP2_PROXY=127.0.0.1:1081        # or 127.0.0.1:1081:1080, as in docker's "ports:"
WARP2_SERVICE=warp2               # the compose service to restart
```

The first WARP keeps using `WARP_PROXY` / `COMPOSE_SERVICE`, so a node with a
single WARP needs no change. Every WARP has its own check, country, cooldown
and alerts, and every message and log line names it (`NE01 / warp2`).

* `/restart NE01` restarts all WARPs of the node, `/restart NE01 warp2` one.
* `warp-agent status` (with the env file loaded: `set -a; . /etc/warp-agent.env;
  set +a; warp-agent status`) probes every WARP once and prints its proxy,
  service, state, Google country, exit IP and remaining cooldown.
* A misconfigured group (a service without a proxy, two WARPs with the same
  name, proxy or service) stops the agent at start with a clear message.
