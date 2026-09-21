# Troubleshooting

## Google reports RU even though the node is in Germany

Cloudflare WARP exit IPs are shared. Heavy Gemini use on a single IP gets
it flagged, and Google starts geo-locating it as Russian. Cloudflare has
also acknowledged some WARP ranges are mis-geolocated by Google, IPv6 in
particular.

Symptoms:

- `https://cloudflare.com/cdn-cgi/trace` says `loc=DE`
- `https://api.country.is` says `DE`
- Google says `RU`

That is expected. A WARP container restart pulls a fresh IP and usually
clears it.

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

### If Google still says RU after restart

1. Wait. The bot will retry after the cooldown.
2. Force a fresh WARP registration:

   ```bash
   docker exec warp warp-cli registration delete
   docker exec warp warp-cli registration new
   docker exec warp warp-cli connect
   ```

3. Disable IPv6 in WARP. IPv4 ranges are geo-located more accurately
   by Google:

   ```yaml
   sysctls:
     - net.ipv6.conf.all.disable_ipv6=1
     - net.ipv4.conf.all.src_valid_mark=1
   ```

   Then `docker compose up -d`.

## Could not detect Google country

Google returned a consent redirect or an empty body.

```bash
curl -sSL --max-time 15 --socks5-hostname 127.0.0.1:1080 \
  -A 'Mozilla/5.0' -o /tmp/g.html \
  -w 'HTTP %{http_code} | size %{size_download} | redirect %{redirect_url}\n' \
  'https://play.google.com/'
```

- `HTTP 302` → `play.google.com/store` is normal, `-L` follows it.
- `HTTP 302` → `consent.google.com` means a consent cookie is required.
- `HTTP 302` → `google.com/sorry/index` means a captcha.

## WARP SOCKS5 is unreachable

```bash
docker ps --filter name=warp
ss -tlnp | grep 1080
docker logs --tail 50 warp
```

Port mapping must be `127.0.0.1:1080:1080`.

## Fleet: node shows ⚫ stale

The agent cannot reach the coordinator.

```bash
sudo systemctl status warp-agent
sudo journalctl -u warp-agent -n 50 --no-pager
curl -v http://<coordinator>:8080/commands \
  -H "X-Node: <name>" -H "X-Token: <token>"
```

Common causes: wrong `COORDINATOR_URL`, wrong `NODE_TOKEN`, firewall
blocking 8080, coordinator down.

## Fleet: `add-node` prints a token but the agent gets 401

The token is shown once. If it was lost, re-run `add-node` with the
same name — it rotates the token — and update the agent.

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