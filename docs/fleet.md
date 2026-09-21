# Fleet mode

A central coordinator with the Telegram bot and a small agent on every
node. Nodes only need outbound HTTP to the coordinator.

## Topology

```
Telegram  ◄──►  [coordinator]  ◄──HTTP──  [agent@node-1]
                    :8080                 [agent@node-2]
                                          ...
```

Agents heartbeat every 60s and poll for commands every 5s. The
coordinator holds the Telegram bot, an SQLite database, and the routing
logic. No inbound access to nodes is required.

## Network requirements

The coordinator's HTTP port (default 8080) **must not be exposed to the
public internet**. Tokens travel over HTTP in cleartext. Run everything
over WireGuard, Tailscale, or an SSH tunnel.

## Coordinator setup

On one server:

```bash
git clone https://github.com/fwlhh/warp-healthcheck
cd warp-healthcheck
sudo make install-coordinator
sudo nano /etc/warp-coordinator.env     # TG_TOKEN, TG_CHAT_ID
sudo systemctl enable --now warp-coordinator
```

Register nodes:

```bash
sudo warp-coordinator add-node NODE_GE01
# prints NODE_NAME=... NODE_TOKEN=...
sudo warp-coordinator add-node de-frankfurt-01
sudo warp-coordinator list-nodes
```

## Agent setup

On every node, install once:

```bash
git clone https://github.com/fwlhh/warp-healthcheck
cd warp-healthcheck
sudo make install-agent
sudo nano /etc/warp-agent.env
```

Minimum in `/etc/warp-agent.env`:

```
COORDINATOR_URL=http://10.0.0.1:8080
NODE_NAME=NODE_GE01
NODE_TOKEN=<from add-node>
```

Start:

```bash
sudo systemctl enable --now warp-agent
sudo journalctl -u warp-agent -f
```

## Telegram commands

See [commands.md](commands.md#fleet-mode).

## Coordinator CLI

```bash
warp-coordinator run                # start the server (systemd does this)
warp-coordinator add-node <name>    # print a fresh token for a node
warp-coordinator remove-node <name> # forget a node and its commands
warp-coordinator list-nodes         # local view without Telegram
```

## Rotating a node token

```bash
sudo warp-coordinator add-node NODE_GE01   # same name, new token
```

Then update `/etc/warp-agent.env` on that node and restart the agent.

## Removing a node

```bash
sudo warp-coordinator remove-node NODE_GE01
```

Then on the node: `sudo make uninstall-agent`.