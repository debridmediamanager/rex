# plexscratch42 — a writable reference pinned to 1.42.2

`plexscratch` is a copy of the lab's config and is therefore **1.43.4**. plexsim reports
**1.42.2.10156**, and four HANDOVER 3.3 entries (cycles 21, 25, 27, 28) all said the same
thing: *a writable 1.42.2 copy would settle this.* This is that copy.

It is not a copy at all — it is a **fresh install** of `plexinc/pms-docker:1.42.2.10156-f737b826c`,
the exact build zen runs, so there is no identity to strip and it has never held a plex.tv
token. It runs on **kids** at `/home/ben/plexscratch42`, answering at
`http://100.101.10.1:32411`.

```bash
cd /home/ben/plexscratch42
docker compose up -d plex          # then wait for /identity to lose startState
docker compose up -d loopback relay
cat token.txt                      # the .LocalAdminToken clients send
docker compose down                # and `rm -rf config` to start over
```

## What it can and cannot show

- **It can** answer every question about a 1.42.2 server's *grammar*: which edit-field names
  it accepts, what a `Player.profile` resolves to, what shape an On Deck row is, what
  `/filters` and the `Meta` descriptor advertise, which settings exist. Cycle 30 used it to
  close three 3.3 entries in an afternoon, and to **correct cycle 28's own On Deck patch**,
  which had been built from zen's single on-deck row.
- **It cannot** answer anything that needs real metadata. It is isolated from plex.tv by
  construction (`internal: true`, no DNS), so every item is UNMATCHED: `local://` guids,
  filename titles, no `Guid`, no `Rating`, no people tags. A row-shape question whose answer
  depends on those blocks needs zen.
- **It cannot** answer anything that needs a CLAIMED server either — cycle 25's
  scrobble-versus-timeline history pair still waits on one.

## The traps, which are plexscratch's own

- **An unclaimed PMS admits only loopback, and only with a local `Host`.** The `loopback`
  service shares PMS's network namespace and relays `0.0.0.0:32499 -> 127.0.0.1:32400`, so
  every request arrives as loopback; the `relay` publishes that on one address. Clients send
  the server's own `.LocalAdminToken` (`token.txt`).
- **Start order is load-bearing**: `loopback` shares `plex`'s namespace, so `plex` must be up
  first and `loopback` must be recreated with it.
- **Section creation answers 400 while `/identity` still reports `startState`.** Wait for it
  to clear rather than concluding the parameters are wrong.
- **Its media is the lab's, mounted READ-ONLY** (`/home/ben/plexscratch/media-long`), so an
  item delete can never remove a file. Its show library needs a `/library/sections/2/refresh`
  after creation — the first scan found nothing.
- Its own subnets (`172.31.19.0/24`, `172.31.20.0/24`), container names and port are all
  distinct from plexscratch's, so the two never touch.

## Proving the isolation

```bash
docker exec plexscratch42-plex sh -c 'timeout 6 wget -q -O- https://plex.tv >/dev/null && echo REACHABLE || echo isolated'
docker exec plexscratch42-plex sh -c 'getent hosts plex.tv >/dev/null && echo dns || echo "no dns"'
curl -s http://100.101.10.1:32411/identity   # claimed="0", version 1.42.2.10156
```
