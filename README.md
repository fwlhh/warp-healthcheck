# warp-healthcheck

Keep WARP exit nodes from being geo-located as RU by Google.

Two modes, one repository:

- **single** — one bash script with a Telegram bot, one node.
  Dependencies: `curl`, `jq`, `docker`. No Python.
- **fleet** — Python coordinator + agent, one Telegram bot for any
  number of nodes. Agents poll the coordinator, no inbound access to
  nodes is required.

## Which mode to pick

| Nodes | Mode   |
|-------|--------|
| 1–3   | single |
| 4+    | fleet  |

Single mode is a self-contained script: it long-polls Telegram itself,
probes Google through the local WARP SOCKS5, and restarts the WARP
container on RU. Everything runs on the node.

Fleet mode splits the same logic into two roles. Agents on each node do
the probing and restarting. A central coordinator holds the Telegram
bot, keeps state in SQLite, and routes commands. Nodes only need
outbound HTTP to the coordinator — nothing inbound.

## Why this exists

Gemini flags shared exit IPs as Russian after heavy use. Once that
happens, Gemini is unusable from that node. WARP in front of the AI
tools helps until Google flags the WARP exit range too. A WARP
container restart pulls a fresh IP from Cloudflare and clears the flag.

This project does that check-and-restart automatically.

## Quickstart — single

```bash
git clone https://github.com/fwlhh/warp-healthcheck
cd warp-healthcheck
sudo make install-single
sudo nano /etc/warp-bot.env          # TG_TOKEN, TG_CHAT_ID, NODE_NAME
sudo systemctl enable --now warp-bot
sudo journalctl -u warp-bot -f
```

## Quickstart — fleet

On the coordinator host (one server):

```bash
sudo make install-coordinator
sudo nano /etc/warp-coordinator.env  # TG_TOKEN, TG_CHAT_ID
sudo systemctl enable --now warp-coordinator
sudo warp-coordinator add-node NODE_GE01   # save the printed token
```

On every node (repeat for each):

```bash
git clone https://github.com/you/warp-healthcheck
cd warp-healthcheck
sudo make install-agent
sudo nano /etc/warp-agent.env        # URL, NODE_NAME, NODE_TOKEN
sudo systemctl enable --now warp-agent
```

In Telegram: `/nodes`, `/status NODE_GE01`, `/restart NODE_GE01`.

## Requirements

- Ubuntu 24.04 or another systemd distro.
- `curl`, `jq`, `docker` with the Compose plugin.
- Python 3.10+ (fleet mode only; Ubuntu 24.04 ships 3.12).
- WARP container from
  [cmj2002/warp-docker](https://github.com/cmj2002/warp-docker)
  exposing SOCKS5 on `127.0.0.1:1080`.

## Docs

- [Single mode](docs/single.md)
- [Fleet mode](docs/fleet.md)
- [Configuration](docs/configuration.md)
- [Telegram commands](docs/commands.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Architecture](docs/architecture.md)

## License

GPL 3.0