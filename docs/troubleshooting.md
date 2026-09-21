# Troubleshooting

## Google reports RU even though the node is in Germany

This is the problem the project was built to work around.

Cloudflare WARP exit IPs are shared. When many users on the same WARP
exit IP hammer Gemini, Google eventually flags the IP and starts
geo-locating it as Russian. Independently of that, Cloudflare itself has
acknowledged that some WARP address ranges are mis-geolocated by Google —
IPv6 addresses in particular are often reported as RU or IR.

So you end up with a split brain:

- `https://cloudflare.com/cdn-cgi/trace` says `loc=DE`
- `https://api.country.is` says `DE`
- `https://get.geojs.io/v1/ip/country.json` says `DE`
- Google says `RU`

That is not a bug in this bot. It is Google's view of the WARP range.
Restarting the WARP container pulls a fresh IP from Cloudflare's pool and
usually clears it — which is exactly what the bot does. After every
restart the bot re-probes Google and reports the new country together
with the fresh exit IP.

### Confirm the split brain

```bash
echo '--- Cloudflare trace (authoritative for WARP exit) ---'
curl -s --max-time 10 --socks5-hostname 127.0.0.1:1080 \
  https://cloudflare.com/cdn-cgi/trace | grep -E '^(ip|loc|warp)='

echo '--- country.is ---'
curl -s --max-time 10 --socks5-hostname 127.0.0.1:1080 \
  https://api.country.is

echo '--- Google via YouTube ---'
curl -sSL --max-time 15 --socks5-hostname 127.0.0.1:1080 \
  -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36' \
  -H 'Cookie: SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjUwNzMwLjA1X3AwGgJlbiACGgYIgPC_xAY' \
  'https://www.youtube.com' | grep -oP '"countryCode":"\K[A-Z]{2}' | head -1
```

If the first two agree on a European country and Google says `RU`, the
bot is behaving correctly: it will restart WARP.

### If Google still says RU after a restart

The new IP landed in another flagged range. Options:

1. **Wait.** The bot will try again after the cooldown.

2. **Force a new registration.** Inside the WARP container:

   ```bash
   docker exec warp warp-cli registration delete
   docker exec warp warp-cli registration new
   docker exec warp warp-cli connect
   ```

3. **Switch WARP to IPv4 only.** IPv4 WARP ranges are generally
   geo-located more accurately by Google. In `docker-compose.yml` set:

   ```yaml
   sysctls:
     - net.ipv6.conf.all.disable_ipv6=1
     - net.ipv4.conf.all.src_valid_mark=1
   ```

   then `docker compose up -d`.

## Google source list changed

`play.google.com` no longer returns `"countryCode"` in its HTML: the
country is loaded later via an authenticated XHR. The bot does not use
it. If you script your own check, use YouTube:

```bash
curl -sSL --max-time 15 --socks5-hostname 127.0.0.1:1080 \
  -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36' \
  -H 'Cookie: SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjUwNzMwLjA1X3AwGgJlbiACGgYIgPC_xAY' \
  'https://www.youtube.com' | grep -oP '"countryCode":"\K[A-Z]{2}' | head -1
```

The `SOCS` cookie is required to skip the consent interstitial in the
EU. Without it, YouTube returns a consent redirect and no
`countryCode`.

## Could not detect Google country

Google returned something the parser did not understand.

Test the three fallbacks manually:

```bash
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36'
COOKIE='SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjUwNzMwLjA1X3AwGgJlbiACGgYIgPC_xAY'

echo '--- YouTube ---'
curl -sSL --max-time 15 --socks5-hostname 127.0.0.1:1080 \
  -A "$UA" -H "Cookie: $COOKIE" 'https://www.youtube.com' \
  | grep -oP '"countryCode":"\K[A-Z]{2}' | head -1

echo '--- Google Search ---'
curl -sSL --max-time 15 --socks5-hostname 127.0.0.1:1080 \
  -A "$UA" -H "Cookie: $COOKIE" 'https://www.google.com/search?q=test' \
  | grep -oP '"gl":"\K[A-Z]{2}' | head -1

echo '--- accounts.google.com ---'
curl -sSL --max-time 15 --socks5-hostname 127.0.0.1:1080 \
  -A "$UA" -H "Cookie: $COOKIE" 'https://accounts.google.com/' \
  | grep -oP '"countryCode":"\K[A-Z]{2}' | head -1
```

If YouTube works — the bot works. If all three are empty, Google is
serving a captcha or an unsupported layout for your WARP range. Try:

```bash
curl -sSL --max-time 15 --socks5-hostname 127.0.0.1:1080 \
  -A "$UA" -o /tmp/y.html \
  -w 'HTTP %{http_code} | size %{size_download} | final %{url_effective}\n' \
  'https://www.youtube.com'
grep -o 'consent.google.com\|sorry/index' /tmp/y.html | head -1
```

- `consent.google.com` — cookie was not sent or was rejected.
- `sorry/index` — Google served a captcha. Nothing the bot can do; it
  will retry after the interval.

## WARP SOCKS5 is unreachable

```bash
docker ps --filter name=warp
ss -tlnp | grep 1080
docker logs --tail 50 warp
```

Port mapping must be `127.0.0.1:1080:1080` (or `1080:1080`).

## Fleet: node shows ⚫ stale

The agent cannot reach the coordinator.

```bash
sudo systemctl status warp-agent
sudo journalctl -u warp-agent -n 50 --no-pager
curl -v http://<coordinator>:<port>/commands \
  -H "X-Node: <name>" -H "X-Token: <token>"
```

Common causes: wrong `COORDINATOR_URL`, wrong `NODE_TOKEN`, firewall
blocking the port, coordinator down.

## Fleet: agent logs `401 unauthorized`

The coordinator did not recognise the `X-Node` / `X-Token` pair.

1. Check what names exist in the coordinator database:

   ```bash
   sudo warp-coordinator list-nodes
   ```

2. Compare with the agent's `NODE_NAME` in `/etc/warp-agent.env`. They
   must match byte-for-byte.

3. If the name is right but the token is wrong, rotate it:

   ```bash
   sudo warp-coordinator add-node <name>
   ```

   Update `NODE_TOKEN` in `/etc/warp-agent.env` on that node and restart
   the agent.

## Fleet: `/logs <node>` says `No results`

`/logs` shows the output of the last completed command. If the node has
never been restarted through the bot, there is nothing to show.

```
/restart <node>
```

Wait for the notification, then `/logs <node>` will show the output —
including the post-restart Google country and exit IP.

## Single mode: bot does not reply

```bash
sudo systemctl status warp-bot
sudo journalctl -u warp-bot -n 50 --no-pager
```

If you see `FATAL: TG_TOKEN is not configured`, the env file was not
picked up:

```bash
sudo systemctl show warp-bot --property=Environment
```

Also make sure you sent `/start` to the bot from the account whose id is
in `TG_CHAT_ID`.

## Telegram returns 409 Conflict

Two processes are polling with the same bot token. This usually means:

- A second instance of the coordinator is running.
- `warp-bot` (single mode) is still running on the same host.
- A webhook was set for the bot.

Find every process:

```bash
ps aux | grep -E 'warp-bot|warp-coordinator' | grep -v grep
```

Only one should be listed. Stop the extra:

```bash
sudo systemctl disable --now warp-bot
```

Check for a webhook:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getWebhookInfo" | jq '.result.url'
```

If not empty:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/deleteWebhook?drop_pending_updates=true"
```