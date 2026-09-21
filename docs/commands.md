# Telegram commands

## Single mode

| Command    | Description                              |
|------------|------------------------------------------|
| `/status`  | Google country via WARP                  |
| `/check`   | Run a check right now                    |
| `/restart` | Restart WARP manually                    |
| `/help`    | Command list                             |

## Fleet mode

| Command              | Description                              |
|----------------------|------------------------------------------|
| `/nodes`             | List every node with status              |
| `/status`            | Same as `/nodes`                         |
| `/status <node>`     | Details for one node                     |
| `/restart <node>`    | Queue a restart for one node             |
| `/restart all`       | Queue a restart for every node           |
| `/logs <node>`       | Last command output for one node         |
| `/help`              | Command list                             |

### Status icons

| Icon | Meaning                                     |
|------|---------------------------------------------|
| 🟢   | alive, Google country not RU                |
| 🟡   | alive, Google country = RU (flagged)        |
| 🔴   | agent up, WARP SOCKS5 not answering         |
| ⚫   | no heartbeat within `STALE_AFTER` seconds   |