# Remote access — putting Ollsoft Company OS behind a front door

The hub binds to **127.0.0.1 only**. It speaks plain HTTP, has no TLS, and no
protection against someone who can reach the port. That is deliberate: the
platform assumes something in front of it is handling transport security and the
first identity gate. `POST /login` **is** throttled (8 failures per 5 min locks
that key for 15 min, counted independently per account and per source) — that is
a backstop against guessing, not a substitute for gate 1.

This document covers the three sane ways to reach it from somewhere else, in
increasing order of effort.

---

## The two-gate model

Whatever you put in front, think of access as two independent gates:

1. **Can you reach the app?** — the front door: SSH, a tunnel, or a reverse proxy
   with its own authentication.
2. **Can you use the app?** — the platform's own PAM login, against a real Linux
   account on the box.

They are not the same question, and keeping them separate is what makes external
collaborators safe: someone can hold an account without being reachable, or be
reachable without holding an account. A guest needs **both**.

Never satisfy gate 1 by exposing port 8300 directly. There is no TLS, and the
login throttle is a backstop, not a front door — an exposed port is still a
password-guessing surface against real Unix accounts, in cleartext.

---

## Option A — SSH tunnel (recommended to start)

Zero infrastructure, zero attack surface, works today:

```bash
ssh -L 8300:127.0.0.1:8300 you@your-box
```

Then browse to `http://127.0.0.1:8300` on your laptop. Gate 1 is SSH itself —
which you are already securing — and gate 2 is the platform login.

Good for: individuals, small teams who all have SSH access, and anyone evaluating
the platform. Bad for: non-technical colleagues, phones, external guests.

---

## Option B — a zero-trust tunnel (Cloudflare Tunnel + Access, Tailscale, etc.)

The pattern that scales to non-technical users without opening a port:

```
browser ─► provider edge ─► identity gate ─► tunnel ─► daemon on your box ─► 127.0.0.1:8300
```

The daemon dials **outbound**, so there is no inbound port and no origin IP to
attack. The provider's identity gate (email one-time-PIN, SSO, whatever you
configure) is gate 1; the platform's PAM login remains gate 2.

Rough shape with Cloudflare, which is what this project was developed against:

1. Install `cloudflared` on the box and authenticate it to your account.
2. Create a tunnel whose ingress rule points at `http://127.0.0.1:8300`, with a
   catch-all `http_status:404` after it.
3. Add a proxied DNS CNAME for your chosen hostname pointing at
   `<tunnel-id>.cfargotunnel.com`.
4. Create an Access application for that hostname, and attach a policy that
   allows your staff email domain plus any individually listed guests.
5. Run `cloudflared` as an enabled systemd service.

Verify the whole chain — an unauthenticated request should be redirected to the
identity provider by the edge, never reach your origin:

```bash
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -I https://your-hostname
```

**Keep the run-token and any API tokens out of the repository and out of your
documentation.** Store the tunnel credential in the systemd unit or a
root-only file. Identifiers such as account and zone IDs are not secrets, but
they are still yours — keep your own deployment runbook private rather than in a
public fork.

Onboarding someone then means two steps, in this order: add their email to the
Access policy, then create their platform account in the admin UI. Offboarding is
the same in reverse — remove the account first, then the email.

---

## Option C — your own reverse proxy

If you already run nginx, Caddy or Traefik with an identity layer (OIDC,
mTLS, an authenticating proxy), point it at `127.0.0.1:8300`. Requirements:

- **Terminate TLS.** The session cookie is not `Secure`-only in transit otherwise.
- **Forward WebSockets.** The editor, presence, and terminal all need `Upgrade`
  and `Connection` headers passed through — `/ws/doc/*` and `/pty` will silently
  fail otherwise, and the symptom is "the editor loads but never syncs".
- **Do not buffer.** Response buffering breaks the terminal and live presence.
- **Rate-limit `POST /login` at the proxy too.** The platform throttles per
  account and per source (see the top of this file), but it identifies the source
  from `CF-Connecting-IP`/`X-Forwarded-For` — so your proxy must set one, or every
  remote attempt shares the single account counter.
- **Preserve the path.** The app is served from the root; it does not support
  being mounted under a subpath.

Minimal nginx location block:

```nginx
location / {
    proxy_pass         http://127.0.0.1:8300;
    proxy_http_version 1.1;
    proxy_set_header   Upgrade $http_upgrade;
    proxy_set_header   Connection $connection_upgrade;
    proxy_set_header   Host $host;
    proxy_buffering    off;
    proxy_read_timeout 3600s;
}
```

(`$connection_upgrade` comes from the usual `map $http_upgrade` block.
`proxy_buffering off` is not optional: `/api/events` is a server-sent event
stream, and a buffering proxy would deliver it only when it ends — never.)

---

## Before you expose it to anything

- Read **[SECURITY.md](SECURITY.md)** — the threat model and the residual risks.
- Change `--port` or firewall the box so 8300 is not reachable from the LAN
  either. `127.0.0.1` binding protects you from the network, not from other
  local users.
- Delete `/root/ollsoft-company-os-admin.txt` and `/root/ollsoft-company-os-demo.txt` once
  passwords have been changed.
- Remove the demo company if this is a real deployment:
  `sudo bash scripts/seed-demo.sh --undo`. It drops the demo accounts, the
  restricted project, the seeded documents and the test credentials file.
  Anything you wrote yourself is left alone — and that is now enforced rather
  than intended: teardown reads a manifest of what the seeder actually created,
  refuses to run at all when there is no manifest, and never touches a path it
  did not record. A file the seeder skipped because you already had one is
  therefore never a candidate.
- Decide whether you want `KB_PROTECTED_USERS` to cover more than the founding
  admin — it is the only thing stopping one admin from deleting another.
