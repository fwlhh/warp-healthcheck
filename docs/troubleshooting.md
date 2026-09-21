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
usually clears it — which is exactly what the bot does.

### Confirm the split brain

```bash
echo '--- Cloudflare trace ---'
curl -s --max-time 10 --socks5-hostname 127.0.0.1:1080 \
  https://cloudflare.com/cdn-cgi/trace | grep -E '^(ip|loc|warp)='

echo '--- country.is ---'
curl -s --max-time 10 --socks5-hostname 127.0.0.1:1080 \
  https://api.country.is

echo '--- Google play.google.com ---'
curl -sSL --max-time 15 --socks5-hostname 127.0.0.1:1080 \
  -A 'Mozilla/5.0' 'https://play.google.com/' \
  | grep -oP '"countryCode":"\K[A-Z]{2}' | head -1
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

## Could not detect Google country through WARP

Google returned something the parser did not understand — usually a
consent redirect or an empty body.

Test manually:

```bash
curl -sSL --max-time 15 --socks5-hostname 127.0.0.1:1080 \
  -A 'Mozilla/5.0' -o /tmp/g.html \
  -w 'HTTP %{http_code} | size %{size_download} | redirect %{redirect_url}\n' \
  'https://play.google.com/'
```

- `HTTP 302` with `redirect https://play.google.com/store` is normal.
  `-L` follows it.
- `HTTP 302` with `consent.google.com` means Google wants a consent
  cookie. The bot does not send one. If this becomes frequent, open an
  issue — adding a `SOCS` cookie is the fix.
- `HTTP 302` with `google.com/sorry/index` means Google served a captcha.
  Nothing the bot can do; it will retry after the interval.

## WARP SOCKS5 is unreachable

The WARP container is not answering on `127.0.0.1:1080`.

```bash
docker ps --filter name=warp
ss -tlnp | grep 1080
docker logs --tail 50 warp
```

If the container is up but the port is not listening, check the compose
file: the port mapping must be `127.0.0.1:1080:1080` (or `1080:1080`).

## The bot does not reply at all

1. Check the service is running:

   ```bash
   sudo systemctl status warp-bot
   sudo journalctl -u warp-bot -n 50 --no-pager
   ```

2. If the log says `FATAL: TG_TOKEN is not configured`, the env file was
   not picked up. Verify:

   ```bash
   sudo systemctl show warp-bot --property=Environment
   ```

   You should see `TG_TOKEN=...`, `TG_CHAT_ID=...`, `NODE_NAME=...`.

3. If the service is up but the bot is silent, make sure you sent
   `/start` to the bot from the account whose numeric id is in
   `TG_CHAT_ID`. Telegram does not allow a bot to message a user who has
   never initiated a conversation.

## Another user messages the bot

Every incoming update is checked against `TG_CHAT_ID`. Messages from any
other user are discarded before any command handler runs. The bot does
not reply to them, does not log them, and does not acknowledge them. This
is by design.

## Two nodes, one bot token

Do not do that. Telegram delivers each update to exactly one long-polling
client. Two processes on two servers with the same token will split
updates unpredictably. Create a separate bot for each node.