# Install

## Prerequisites

- Ubuntu 24.04 or another systemd-based distribution.
- `curl`, `jq`, `docker` with the Compose plugin.
- A working WARP container exposing SOCKS5 on `127.0.0.1:1080`.

If you do not have WARP yet, set it up first. The recommended image is
[cmj2002/warp-docker](https://github.com/cmj2002/warp-docker). A minimal
`docker-compose.yml` looks like this:

```yaml
services:
  warp:
    image: caomingjun/warp
    container_name: warp
    restart: always
    device_cgroup_rules:
      - 'c 10:200 rwm'
    ports:
      - "127.0.0.1:1080:1080"
    environment:
      - WARP_SLEEP=2
    cap_add:
      - MKNOD
      - AUDIT_WRITE
      - NET_ADMIN
    sysctls:
      - net.ipv6.conf.all.disable_ipv6=0
      - net.ipv4.conf.all.src_valid_mark=1
    volumes:
      - ./data:/var/lib/cloudflare-warp
    healthcheck:
      test: ["CMD", "curl", "-f", "--socks5-hostname", "127.0.0.1:1080", "https://cloudflare.com/cdn-cgi/trace"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
```

Bring it up with `docker compose up -d` in the directory that contains
the file. The default path expected by the bot is `/opt/warp`, but you
can override it with `COMPOSE_DIR` and `COMPOSE_FILE`.

## Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`, pick a name and a username.
3. Copy the token — you will need it as `TG_TOKEN`.
4. Open [@userinfobot](https://t.me/userinfobot) and send `/start`.
   It replies with your numeric user id — that is `TG_CHAT_ID`.
5. **Send `/start` to your new bot from your account.** Telegram does
   not let a bot message a user who has never talked to it.

## Install the bot

```bash
git clone https://github.com/fwlhh/warp-healthcheck
cd warp-healthcheck
sudo make install
```

`make install` copies:

- `bin/warp-bot.sh` to `/usr/local/bin/warp-bot.sh`
- `etc/systemd/warp-bot.service` to `/etc/systemd/system/`
- `etc/warp-bot.env.example` to `/etc/warp-bot.env` (only if the latter
  does not exist)

It does **not** start the service. You are expected to edit the config
first.

## Configure

Open `/etc/warp-bot.env` and set at least:

```
TG_TOKEN=123456:AAH...
TG_CHAT_ID=987654321
NODE_NAME=NODE_GE01
```

`NODE_NAME` is free-form but must match `[a-zA-Z0-9._-]+`. It is
prefixed to every outgoing Telegram message.

See [configuration.md](configuration.md) for every variable.

## Start

```bash
sudo systemctl enable --now warp-bot
sudo systemctl status warp-bot
sudo journalctl -u warp-bot -f
```

On startup you should see a line like:

```
[2026-09-15T14:30:10+00:00] [NODE_GE01] bot started (node=NODE_GE01, chat_id=987654321, interval=300s)
```

Send `/help` to your bot in Telegram. If it replies with the command
list, everything is wired up.

## Update

```bash
cd warp-healthcheck
git pull
sudo make install
sudo systemctl restart warp-bot
```

`make install` does not overwrite `/etc/warp-bot.env`, so your tokens
survive the update.

## Uninstall

```bash
sudo make uninstall
```

The script stops and disables the service, removes the unit and the
script, and asks whether to delete the config and state directory.