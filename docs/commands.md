# Telegram commands

## Single mode

| Command    | Description                              |
|------------|------------------------------------------|
| `/status`  | Google country via WARP (every WARP)     |
| `/check`   | Run a check right now (every WARP)       |
| `/restart [warp]` | Restart every WARP, or the named one |
| `/help`    | Command list                             |

## Fleet mode

| Command              | Description                              |
|----------------------|------------------------------------------|
| `/nodes`             | List every node with status              |
| `/status`            | Same as `/nodes`                         |
| `/status <node>`     | Details for one node                     |
| `/restart <node>`    | Queue a restart for every WARP of the node |
| `/restart <node> <warp>` | Restart one WARP of the node (name as in `/nodes`) |
| `/restart all`       | Queue a restart for every WARP of every node |
| `/logs <node>`       | Last command output for one node         |
| `/help`              | Command list                             |

### Status icons

| Icon | Meaning                                     |
|------|---------------------------------------------|
| 🟢   | alive, Google country not RU                |
| 🟡   | alive, Google country = RU (flagged)        |
| 🔴   | agent up, WARP SOCKS5 not answering         |
| ⚫   | no heartbeat within `STALE_AFTER` seconds   |