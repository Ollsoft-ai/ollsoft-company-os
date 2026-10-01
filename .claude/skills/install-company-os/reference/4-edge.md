# Phase 4 — Put it on a domain, behind Cloudflare

## Ask

"Do you want Company OS on your own domain, reachable from anywhere? I recommend **Cloudflare Tunnel + Access** (free plan): the server opens no web port at all, and Cloudflare checks who someone is (an email code, or Google/Microsoft login) before they even see the sign-in page."

| Answer | Path |
|---|---|
| **Yes, Cloudflare** (recommend) | A — this file |
| A domain, but no Cloudflare | B — nginx + Let's Encrypt |
| Not now | C — SSH tunnel only; rerun this phase later |

For A, collect: **the domain** (is it on Cloudflare yet?), **the hostname** (default `os.<domain>`), **who may sign in** (an email domain like `@acme.com`, and/or individual addresses), and **dashboard or token** — they click with your step-by-step, or give you a scoped API token and you do it.

## A0. Prerequisites the human does in the dashboard either way

1. **Domain on Cloudflare:** dash.cloudflare.com → Add a domain → Free → change the nameservers at their registrar → wait until it says **Active**.
2. **Zero Trust, once per account:** one.dash.cloudflare.com → pick a **team name** → **Free** plan (it may ask for a payment method; free stays free).

## A1. cloudflared on the server

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
sudo tee /etc/systemd/system/cloudflared.service >/dev/null <<'EOF'
[Unit]
Description=Cloudflare Tunnel client
After=network-online.target
Wants=network-online.target

[Service]
TimeoutStartSec=15
Type=notify
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run --token-file /etc/cloudflared/token
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF
```

- **Token in a 0600 file, never `cloudflared service install <token>`:** that bakes the token into a world-readable unit, and every Company OS user could read it and run a rogue connector for the hostname.
- Updates come from apt via unattended-upgrades (phase 2 allowed the `cloudflared` origin).
- Once `/etc/cloudflared/token` exists: `sudo systemctl daemon-reload && sudo systemctl enable --now cloudflared`.

## A2. Dashboard path — dictate these, one screen at a time

Access first, so the hostname never answers without it.

1. **Access app:** Zero Trust → Access → Applications → Add → **Self-hosted**. Name `Company OS`, session 24 h, public hostname `os.<domain>`. Policy `Staff`: **Allow**, include *Emails ending in* `@<domain>` and/or *Emails* `<list>`. Login method: One-time PIN (Google/Microsoft can be added later). Save, then copy the **Application Audience (AUD) tag**.
2. **Tunnel:** Zero Trust → Networks → Tunnels → Create → **Cloudflared** → name `company-os`. The install page shows a command with a long token (`eyJ…`). **Do not run that command**; copy only the token. Key drop `cf-tunnel`, then:
   `ssh companyos 'sudo install -D -m 600 /root/.cos-cf-tunnel.key /etc/cloudflared/token && sudo shred -u /root/.cos-cf-tunnel.key'` and start the service (A1). The dashboard shows the connector **Healthy**.
3. **Route:** the tunnel → **Published application routes** → Add: subdomain `os`, the domain, type **HTTP**, URL `127.0.0.1:8300`. (Not "Hostname routes" — that is WARP for employees.) Cloudflare creates the DNS record.
4. **JWT check at the origin:** same route → Additional application settings → Access → **Protect with Access** on, team name, the AUD tag. Now even a deleted Access app cannot expose the origin.

Dashboard labels drift; if a name differs, describe what you need and let the human find it.

## A3. Token path — you do it over the API

The human creates **My Profile → API Tokens → Create Token → Custom**, expiring tomorrow:

| Scope | Permission |
|---|---|
| Account | Cloudflare Tunnel — Edit |
| Account | Access: Apps and Policies — Edit |
| Account | Access: Organizations, Identity Providers, and Groups — Read |
| Zone (only this domain) | DNS — Edit; Zone — Read |

Keep it in a local shell variable only — never on the server, never in the state file. **Cloudflare answers HTTP 200 with `success:false` on errors: check `.success` and `.errors` after every call.** Needs `jq` locally (`brew install jq` · `sudo apt install jq` · `winget install jqlang.jq`).

```bash
DOMAIN=acme.com HOST=os.acme.com      # from the answers
cf() { curl -sS -m 30 -X "$1" "https://api.cloudflare.com/client/v4/$2" \
       -H "Authorization: Bearer $CF_TOKEN" -H 'Content-Type: application/json' ${3:+-d "$3"}; }
ACC=$(cf GET accounts | jq -r '.result[0].id')                 # ask which, if several
ZONE=$(cf GET "zones?name=$DOMAIN" | jq -r '.result[0].id')
TEAM=$(cf GET accounts/$ACC/access/organizations | jq -r '.result.auth_domain' | cut -d. -f1)
# 1. Access first
POL=$(cf POST accounts/$ACC/access/policies '{"name":"Company OS staff","decision":"allow","include":[{"email_domain":{"domain":"acme.com"}},{"email":{"email":"guest@example.com"}}]}' | jq -r .result.id)
AUD=$(cf POST accounts/$ACC/access/apps "{\"name\":\"Company OS\",\"type\":\"self_hosted\",\"domain\":\"$HOST\",\"session_duration\":\"24h\",\"policies\":[{\"id\":\"$POL\",\"precedence\":1}]}" | jq -r .result.aud)
# 2. Tunnel, route with JWT check, DNS
TUN=$(cf POST accounts/$ACC/cfd_tunnel '{"name":"company-os","config_src":"cloudflare"}' | jq -r .result.id)
cf PUT accounts/$ACC/cfd_tunnel/$TUN/configurations "{\"config\":{\"ingress\":[{\"hostname\":\"$HOST\",\"service\":\"http://127.0.0.1:8300\",\"originRequest\":{\"access\":{\"required\":true,\"teamName\":\"$TEAM\",\"audTag\":[\"$AUD\"]}}},{\"service\":\"http_status:404\"}]}}"
cf POST zones/$ZONE/dns_records "{\"type\":\"CNAME\",\"name\":\"$HOST\",\"content\":\"$TUN.cfargotunnel.com\",\"proxied\":true}"
# 3. Token straight to the server, never printed
cf GET accounts/$ACC/cfd_tunnel/$TUN/token | jq -r .result \
  | ssh companyos "sudo sh -c 'umask 077; mkdir -p /etc/cloudflared; cat > /etc/cloudflared/token'"
```

Then start the service (A1). Record `ACC`, `ZONE`, `TUN`, `POL` in the state file — they are IDs, not secrets; phase 7 adds people to `POL` and phase 8 adds the share route to `TUN`.

## A4. Verify

- `ssh companyos systemctl is-active cloudflared` → `active`.
- `curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -I https://$HOST` → `302` to `https://<team>.cloudflareaccess.com/…`. **A 200 here means Access is not in front: stop and fix.**
- The human opens `https://<host>`, gets the email code, then the Company OS sign-in.
- **Onboarding order from now on:** add the email to the Access policy, then create the account. Offboarding: reverse.

## B. Own domain, no Cloudflare — nginx + Let's Encrypt

1. Human adds a DNS **A record** `os.<domain>` → server IP (DNS-only if the zone is on Cloudflare). Wait until `dig +short` returns the IP.
2. `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo apt-get install -y nginx certbot python3-certbot-nginx`
3. `/etc/nginx/sites-available/company-os`, symlinked into `sites-enabled`, default site removed:

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
limit_req_zone $binary_remote_addr zone=kblogin:10m rate=10r/m;
server {
  listen 80;
  server_name os.example.com;
  location = /login { limit_req zone=kblogin burst=5 nodelay; include /etc/nginx/company-os-proxy.conf; }
  location /        { include /etc/nginx/company-os-proxy.conf; }
}
```

`/etc/nginx/company-os-proxy.conf`:

```nginx
proxy_pass         http://127.0.0.1:8300;
proxy_http_version 1.1;
proxy_set_header   Upgrade $http_upgrade;
proxy_set_header   Connection $connection_upgrade;
proxy_set_header   Host $host;
proxy_set_header   X-Forwarded-For $remote_addr;   # overwrite, never append: the hub throttles logins by it
proxy_buffering    off;                            # /api/events is SSE; buffering kills it
proxy_read_timeout 3600s;
```

4. `sudo nginx -t && sudo systemctl reload nginx && sudo certbot --nginx -d os.<domain> --redirect -m <email> --agree-tos -n`
5. Verify: `curl -sI https://os.<domain>` → `302` to `/login`; the editor syncs (WebSockets) and a web terminal opens.

Tell them plainly: **without Cloudflare Access the Company OS password is the only gate on the open internet.** fail2ban covers SSH, not the web login; the platform and nginx throttle logins.

## C. SSH tunnel only

Each person runs `ssh -N -L 8300:127.0.0.1:8300 companyos` and opens http://localhost:8300. Fine for one or two technical people; anyone else needs A.
