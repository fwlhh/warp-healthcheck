# Configuration

All variables are read from the process environment. In the systemd unit
they come from `/etc/warp-bot.env`. The file uses plain `KEY=value` lines,
no `export`.

## Required

| Variable     | Description                                                          | Example         |
|--------------|----------------------------------------------------------------------|-----------------|
| `TG_TOKEN`   | Telegram bot token from @BotFather.                                  | `123456:AAH...` |
| `TG_CHAT_ID` | Numeric Telegram user id that is allowed to talk to the bot.         | `987654321`     |
| `NODE_NAME`  | Human-readable name of this node, prefixed to every outgoing message. Must match `[a-zA-Z0-9._-]+`. | `classic-copper` |

## Optional

| Variable           | Default                | Description                                                            |
|--------------------|------------------------|------------------------------------------------------------------------|
| `WARP_PROXY`       | `127.0.0.1:1080`       | SOCKS5 endpoint of the WARP container.                                 |
| `COMPOSE_DIR`      | `/opt/warp`            | Directory that contains the WARP `docker-compose.yml`.                 |
| `COMPOSE_FILE`     | `docker-compose.yml`   | Name of the compose file.                                              |
| `COMPOSE_SERVICE`  | `warp`                 | Compose service name to restart. Empty means restart all services.     |
| `CHECK_INTERVAL`   | `300`                  | Seconds between automatic Google-country checks.                       |
| `RESTART_COOLDOWN` | `300`                  | Minimum seconds between two WARP restarts.                             |
| `POLL_TIMEOUT`     | `25`                   | Telegram long-polling timeout in seconds.                              |
| `CURL_TIMEOUT`     | `10`                   | Timeout for the Google country probe, in seconds.                      |
| `STATE_DIR`        | `/var/lib/warp-bot`    | Directory for the restart timestamp and Telegram update offset.        |
| `USER_AGENT`       | Chrome 135 on Windows  | User-Agent used when talking to Google.                                |

## Example `/etc/warp-bot.env`

```bash
TG_TOKEN=123456:AAH...
TG_CHAT_ID=987654321
NODE_NAME=NODE_GE01

# Optional overrides
# WARP_PROXY=127.0.0.1:1080
# COMPOSE_DIR=/opt/warp
# COMPOSE_FILE=docker-compose.yml
# COMPOSE_SERVICE=warp
# CHECK_INTERVAL=300
# RESTART_COOLDOWN=300
# POLL_TIMEOUT=25
# CURL_TIMEOUT=10
# STATE_DIR=/var/lib/warp-bot
```

## Multiple nodes

You can run the bot on several servers. Each node must use its own bot
token, because Telegram delivers each update to exactly one long-polling
client. Two processes polling with the same token will fight over updates
and answer randomly.

Give each node a distinct `NODE_NAME`. All messages from all bots land in
the same chat if you use the same `TG_CHAT_ID`, and the prefix tells you
which node sent what.