# Install

Two modes, pick one:

- **[Single mode](single.md)** — one bash script with a Telegram bot, one node.
  Best for 1–3 nodes.
- **[Fleet mode](fleet.md)** — Python coordinator + agent, one bot for any
  number of nodes. Best for 4+ nodes.

Both modes require:

- Ubuntu 24.04 or another systemd-based distribution.
- `curl`, `jq`, `docker` with the Compose plugin.
- A WARP container from
  [cmj2002/warp-docker](https://github.com/cmj2002/warp-docker)
  exposing SOCKS5 on `127.0.0.1:1080`.

See [configuration.md](configuration.md) for every environment variable
and [troubleshooting.md](troubleshooting.md) when something goes wrong.