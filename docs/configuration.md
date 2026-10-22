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
| `CHECK_INTERVAL`   | no       | `300`                  | Seconds between Google checks                 |
| `RESTART_COOLDOWN` | no       | `300`                  | Minimum seconds between restarts              |
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
| `RETRY_UNKNOWN_LIMIT` | no | `2`                                  | Extra restarts after one ends with unknown Google country; `0` disables |
| `RETRY_UNKNOWN_DELAY` | no | `330`                                | Seconds before such a retry is delivered; keep above the agents' `RESTART_COOLDOWN` |
| `NODE_ALERTS` | no | `1` | Message when a node stops reporting or loses WARP, and when it recovers; `0` disables |
| `WARP_DOWN_ALERT_AFTER` | no | `120` | Seconds WARP must stay down before alerting (ordinary restarts do not page) |
| `MONITOR_INTERVAL` | no | `30` | Seconds between monitor passes |

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
| `HEARTBEAT_INTERVAL` | no       | `60`                 | Seconds between heartbeats                  |
| `POLL_INTERVAL`      | no       | `5`                  | Seconds between command polls               |
| `CHECK_INTERVAL`     | no       | `300`                | Seconds between Google checks               |
| `RESTART_COOLDOWN`   | no       | `300`                | Minimum seconds between restarts, manual (`/restart`) and automatic alike |
| `CURL_TIMEOUT`       | no       | `10`                 | Google probe timeout                        |
| `STATE_DIR`          | no       | `/var/lib/warp-agent` | Where the restart timestamp lives           |