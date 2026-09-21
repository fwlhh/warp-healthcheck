# Single mode

One bash script, one node, one Telegram bot. No Python, no central server.

## Prerequisites

- Ubuntu 24.04 or another systemd-based distribution.
- `curl`, `jq`, `docker` with the Compose plugin.
- A WARP container exposing SOCKS5 on `127.0.0.1:1080`.

Recommended image: [cmj2002/warp-docker](https://github.com/cmj2002/warp-docker).

## Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather), send `/newbot`.
2. Copy the token.
3. Open [@userinfobot](https://t.me/userinfobot), send `/start`,
   copy your numeric id.
4. Send `/start` to your new bot from your account. Without this the bot
   cannot message you.

## Install

```bash
git clone https://github.com/fwlhh/warp-healthcheck
cd warp-healthcheck
sudo make install-single
```

Files placed:

- `/usr/local/bin/warp-bot.sh`
- `/etc/systemd/system/warp-bot.service`
- `/etc/warp-bot.env` (created from example, mode 600)

## Configure

```bash
sudo nano /etc/warp-bot.env
```

Set `TG_TOKEN`, `TG_CHAT_ID`, `NODE_NAME`. `NODE_NAME` must match
`[a-zA-Z0-9._-]+`.

## Start

```bash
sudo systemctl enable --now warp-bot
sudo journalctl -u warp-bot -f
```

Expected first line:

```
[2026-09-21T14:30:10+00:00] [NODE_GE01] bot started (node=NODE_GE01, chat_id=..., interval=300s)
```

## Update

```bash
git pull
sudo make install-single
sudo systemctl restart warp-bot
```

`/etc/warp-bot.env` is not touched.

## Uninstall

```bash
sudo make uninstall-single
```

## Telegram commands

See [commands.md](commands.md#single-mode).