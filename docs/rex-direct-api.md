# The rex-direct API: a provider gives rex an HTTPS name

rex-direct is rex's own plex.direct. A **provider** points a DNS name of its choosing at the
public IP rex calls from and hands rex a certificate for that name; rex serves the
certificate and announces `https://<name>:<port>` to plex.tv, so the Plex apps reach it over
HTTPS from anywhere without the operator owning a domain. DMM runs one, and rex uses it by
default with no key. Anyone can run their own: this is the contract. It is rex's own API,
not an emulated Plex route (`rust/src/runtime/rexdirect.rs`, `tests/test_rex_direct.py`).

**The provider chooses the name and the certificate rex serves and announces** as one of the
server's addresses, so it decides where Plex apps signed in to your account connect, and it
receives the server's token. Point rex only at a provider you trust.

## Configuring rex

From the environment only, never the command line:

| Variable | Meaning |
|---|---|
| `REX_DIRECT_URL` | The provider's register URL. Default `https://debridmediamanager.com/api/rex-direct/register`; `off` turns rex-direct off. It must be `https://`; plain `http://` is accepted only to this machine (127.0.0.0/8, `::1`, `localhost`), and any other URL is a WARN at start and rex-direct stays off (below) |
| `REX_DIRECT_KEY` | Optional. Sent as `Authorization: Bearer <key>` for a provider that hands out keys; DMM's ignores it |
| `REX_DIRECT_PORT` | The outside port routers forward to rex, when it is not rex's `--port`: the port the announced URL names |

## When rex calls

- **Only once claimed**, because the provider knows the caller by its claim. A claimed start
  registers before the port opens, so its first TLS client already gets the certificate; the
  start waits at most the request's 10 s timeout for it. An unclaimed start registers
  nothing and logs `rex-direct: registers with <url> once claimed`; the moment
  `POST /myplex/claim` succeeds rex registers in the background, then makes the claim's device
  PUT with the rex-direct URL in it. No restart either way.
- **Every 6 hours** after an answer: the dynamic DNS, and how a renewed certificate arrives.
- **10 minutes** after a failure, and **`retryAfter`** seconds after a 202.

Each request times out after 10 s. A redirect is not followed: it is answered as any other
status.

## The request

```
POST <REX_DIRECT_URL>
Content-Type: application/json
User-Agent: rex/0.1
X-Plex-Token: <the claim's server token>
X-Plex-Client-Identifier: <machineIdentifier>
Authorization: Bearer <REX_DIRECT_KEY>          (only when REX_DIRECT_KEY is set)

{"machineIdentifier": "<machineIdentifier>", "lanIp": "192.168.1.20"}
```

`X-Plex-Token` is the server token plex.tv issued at the claim; rex holds no account token.
The body's `machineIdentifier` and `X-Plex-Client-Identifier` are the same
value: the identity rex announces to plex.tv on. rex logs neither the token nor the key.

| Field | Meaning |
|---|---|
| `machineIdentifier` | Always sent |
| `lanIp` | rex's address on its LAN, the one it announces as `http://<lanIp>:<port>`: the first RFC 1918 IPv4 among `--announce-url`'s plain `http://` entries (how a setup names it when rex cannot read it itself: Docker without host networking, a VPN), else the bound address, or under `--host 0.0.0.0` the interface its default route leaves from, read at each request, unless that interface is a container network's (a veth: Docker, Podman or LXC without host networking), which nothing outside the container reaches. Sent only when it is RFC 1918 IPv4 (10/8, 172.16/12, 192.168/16); IPv6, a tunnel's (Tailscale's 100.64/10), a public address or loopback leaves the key out. So the provider can give rex a name on its LAN as well; a provider that does not know the key ignores it |

## Verifying the caller

The token proves which account owns the server. Ask plex.tv with the request's own headers:

```
GET https://plex.tv/api/v2/resources
Accept: application/json
X-Plex-Token: <token>
X-Plex-Client-Identifier: <machineIdentifier>
```

It answers a JSON array of the account's devices. The caller is genuine when an entry has
`clientIdentifier` equal to the `machineIdentifier`, `owned` true and `provides` containing
`server`. Measured 2026-10-19 against a claimed rex: the server token lists every device on
the account, including rows that are not servers (`provides` `controller`), so match on all
three fields; a bad token is 401. Point the name at the request's source address (DMM's
provider writes an IPv4 A record).

`lanIp` is the caller's word alone: nothing can check it, and a record holding it points a
public name wherever the caller says. Accept it only when it is RFC 1918 IPv4, so the worst a
caller can do is name an address on a private network; refuse anything else (DMM answers 400
`Invalid lanIp`, which rex treats as any other failure).

## Answers

### 200: the name and its certificate

```json
{"hostname": "a1b2c3d4e5f6.rex-direct.debridmediamanager.com", "ip": "203.0.113.7",
 "lanHostname": "lan.a1b2c3d4e5f6.rex-direct.debridmediamanager.com", "lanIp": "192.168.1.20",
 "certificate": "-----BEGIN CERTIFICATE-----\n...", "privateKey": "-----BEGIN EC PRIVATE KEY-----\n..."}
```

| Field | Meaning |
|---|---|
| `hostname` | The DNS name: letters, digits, `.` and `-`, with at least one dot. Anything else is refused |
| `ip` | The address the name points at. rex only logs it |
| `lanHostname` | Optional: a name pointing at the request's `lanIp`, one label under `hostname` (DMM: `lan.<hostname>`). The same rule as `hostname`; a `lanHostname` that breaks it refuses the whole answer. Absent or `null`, rex does as it did before LAN names |
| `lanIp` | Optional: the address `lanHostname` points at. rex only logs it |
| `certificate` | The PEM chain for `hostname`: a certificate for that name itself, or a wildcard over its parent; with a `lanHostname`, one that also covers it (DMM: `*.<hostname>`, beside `hostname` itself) |
| `privateKey` | Its PEM key: PKCS#8, RSA or SEC1 EC (`BEGIN EC PRIVATE KEY`) |

Other fields are ignored. With the answer rex:

- **serves the certificate by SNI** on its port, beside plain HTTP: to a client asking for
  `hostname`, or for one more label under `hostname`'s parent (what a wildcard covers), and
  with a `lanHostname` for one more label under `hostname` itself (where `lanHostname` lives).
  `--tls-cert`'s certificate answers every other name and a client sending none; without
  `--tls-cert` the rex-direct certificate answers every name.
- **announces** `https://<hostname>:<REX_DIRECT_PORT or --port>` after `--announce-url`'s
  addresses, on the claim's heartbeat, and logs `rex-direct: https://<hostname>:<port> -> <ip>`.
  A `lanHostname` is announced right after it as `https://<lanHostname>:<--port>`: a client
  on the LAN reaches rex directly, not through the router's forward `REX_DIRECT_PORT` names.
  rex logs it as `rex-direct: https://<lanHostname>:<port> -> <lanIp> (LAN)`. rex's plain
  `http://<lanIp>:<port>` connection is still announced last: plex.tv turns it into a
  `plex.direct` name rex holds no certificate for, which is why the LAN name exists.
- **keeps the answer** as sent in `rex-direct.json` beside the catalog, mode 600 since it
  holds the private key, and serves it at a start the provider does not answer.

The same answer again changes nothing. A new certificate is swapped in without a restart; a
new `hostname` or `lanHostname` is announced instead of the old one, and the kept answer
announces both again at a start the provider does not answer.

### 202: still issuing the certificate

```json
{"retryAfter": 60, "error": "issuing the certificate"}
```

Not a failure. rex logs an INFO with the reason, keeps serving what it has (at a start, the
answer kept in `rex-direct.json`, if any) and asks again after `retryAfter` seconds, held
between 15 and 600; 60 when it is missing or not a whole number. At a start it holds the
port no longer than the request took.

### Anything else

```json
{"error": "the token does not own this server"}
```

A WARN with the status and the reason, `rex-direct: registration failed: HTTP 401 (the token
does not own this server); retrying in 10 min`, and rex runs on with what it has. An
unreachable provider and a 200 that is not the answer above are the same: never fatal.

## Why https

The answer carries the certificate's private key and the request carries the claim's server
token, so rex sends neither over plain HTTP to another machine. Loopback is allowed for a
provider on the same host (`tests/test_rex_direct.py` runs a plain-HTTP stub on 127.0.0.1).
