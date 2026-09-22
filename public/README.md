# kb-share — public links, in a box

The container in here is the only part of Company OS on the open internet.
It is built to be boring: no database, no session key, no `/srv/kb`, no shell,
no network route back to the host, a read-only root filesystem and every
capability dropped. It serves what the platform bind-mounts in front of it.

```
/data/<id>/        the shared file or folder (bind mount; ro unless the share can be edited)
/conf/<id>.json    what may be done with it (mode, expiry, password hash, token hash)
/assets/           the platform's built frontend, read-only — so a shared document
                   opens in the REAL editor rather than a textarea
```

The first two are written by the hub, as root, on the host; `/assets` is the
deployed bundle every browser on the app already downloads, mounted rather
than copied so a frontend deploy updates this page too. The container cannot create
a share, widen one, or see a path that was not mounted for it. Revoking is an
unmount on the host, so a leaked link stops working within a second.

Build and run are handled by `scripts/install-public-share.sh` and the
`kb-share.service` unit; see [docs/public-sharing.md](../docs/public-sharing.md)
for the whole design, the threat model and the Cloudflare side.
