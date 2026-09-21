# warp-healthcheck

Telegram bot and healthcheck that restarts a WARP container when Google
starts reporting the exit node as RU.

## Why

This project exists because of Gemini. When many users share a single
foreign VPN exit IP and hammer Gemini, Google eventually flags that IP
and starts geo-locating it as Russian. Once that happens, Gemini is
effectively unusable from that node.

Putting WARP in front of the AI tools helps — until Gemini flags the WARP
exit IP too and starts reporting RU again. Restarting the WARP container
gives you a fresh exit IP from Cloudflare's pool, and Gemini works again.

Doing that by hand is annoying. This bot watches the Google-reported
country through WARP and restarts the container automatically when it
flips to RU. It also gives you a Telegram interface to check status and
trigger a restart manually.

## Features

- Long-polling Telegram bot, only one authorized user is answered.
- Every `CHECK_INTERVAL` seconds, asks Google (through the WARP SOCKS5
  proxy) which country it thinks the exit node is in.
- If that country is `RU`, restarts the WARP docker-compose service.
- Restart cooldown to avoid hammering WARP.
- Every Telegram message is prefixed with `NODE_NAME`, so you can run
  the bot on several nodes and tell them apart in one chat.
- systemd-ready, sandboxed unit.

## Requirements

- Ubuntu 24.04 (or any systemd-based distro).
- `curl`, `jq`, `docker` with the Compose plugin.
- A running WARP container from
  [cmj2002/warp-docker](https://github.com/cmj2002/warp-docker)
  exposing SOCKS5 on `127.0.0.1:1080`.

## Install

```bash
git clone https://github.com/fwlhh/healthcheck
cd healthcheck
sudo make install
```

Then edit `/etc/warp-bot.env` and set `TG_TOKEN`, `TG_CHAT_ID`,
`NODE_NAME`. See [docs/install.md](docs/install.md) for the full
walkthrough, including how to create a bot and get your chat id.

Start the service:

```bash
sudo systemctl enable --now warp-bot
sudo journalctl -u warp-bot -f
```

## Commands

- `/status`  — current Google country via WARP
- `/check`   — run a check right now
- `/restart` — restart WARP manually
- `/help`    — this message

## Docs

- [Install](docs/install.md)
- [Configuration](docs/configuration.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture](docs/architecture.md)

## License

GPL 3.0