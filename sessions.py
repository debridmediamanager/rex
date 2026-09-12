#!/usr/bin/env python3
"""Playback sessions and the byte counters behind the Dashboard.

Measured against a real PMS, and the first fact is the surprising one: **a
session is created by `/:/timeline`, not by anyone reading bytes.** A bare
`GET /:/timeline?...&state=playing` from curl -- no play queue, no decision, no
part read -- puts a full row in `/status/sessions` immediately, and a part read
on its own puts nothing there. `state=stopped` removes the row at once, and with
no further timelines a row disappeared between **60 and 80 seconds** after the
last one.

plexsim answered `/status/sessions` with `{size: 0}` for its whole life, so Now
Playing and the Dashboard were permanently empty and the Terminate button had
nothing to act on, while the timeline it was already receiving carried everything
needed to fill them.

The blocks, all measured:

    sessionKey   a server counter, as a STRING
    viewOffset   the last timeline's `time`
    Player       an echo of the client's own X-Plex-* values, plus the peer
                 address, the last `state`, and local/relayed/secure
    Session      `id` is the client's X-Plex-Session-Identifier (the machine
                 identifier when a client sends none, as Plex for Apple TV does)
    User         the account

No `TranscodeSession` is ever emitted: the client renders "Direct Play" precisely
when a `Session` is present and a `TranscodeSession` is not, which is the truth
for a server with no encoder.
"""

import time

# Measured: a row survived 60 s past its last timeline and was gone by 80 s.
SESSION_TTL = 70

# Statistics bucket sizes, decoded from a real PMS's own rows: 6 is per second,
# then hour, day, ISO week, month, year. The client sends `timespan=6` for the
# live Dashboard graph and coarser ones for the history.
TIMESPANS = {6: 1, 5: 60, 4: 3600, 3: 86400, 2: 604800, 1: 2592000, 0: 31536000}

# `Player` is an echo. Header (or query parameter -- Plex Web sends every
# X-Plex-* value in the query string) -> the field it lands in.
PLAYER_FROM = {
    "X-Plex-Client-Identifier": "machineIdentifier",
    "X-Plex-Device": "device",
    "X-Plex-Device-Name": "title",
    "X-Plex-Product": "product",
    "X-Plex-Version": "version",
    "X-Plex-Platform": "platform",
    "X-Plex-Platform-Version": "platformVersion",
    "X-Plex-Model": "model",
    "X-Plex-Playback-Id": "playbackId",
    "X-Plex-Playback-Session-Id": "playbackSessionId",
}


def _private(address):
    if not address:
        return True
    return (address.startswith("127.") or address.startswith("10.")
            or address.startswith("192.168.") or address.startswith("100.")
            or address.startswith("172.") or address == "::1")


class Sessions:
    """Live playback sessions, and the bytes this server has served."""

    def __init__(self, owner="plexsim", ttl=SESSION_TTL):
        self.owner = owner
        self.ttl = ttl
        self._sessions = {}
        self._next_key = 1
        self._terminated = {}
        # (device, lan) -> {second: bytes}. A real PMS counts every response's
        # bytes, not just media, and rolls the per-second rows into the coarser
        # buckets on read.
        self._bytes = {}
        self._devices = {}
        self._next_device = 1
        self._plays = []

    # -- identity --------------------------------------------------------

    def device_id(self, identifier):
        """A stable small integer per client identifier, as a PMS assigns."""
        identifier = identifier or ""
        if identifier not in self._devices:
            self._next_device += 1
            self._devices[identifier] = self._next_device
        return self._devices[identifier]

    def devices(self):
        return [{"id": key, "name": "", "platform": "", "clientIdentifier": name,
                 "createdAt": 0} for name, key in sorted(self._devices.items(),
                                                         key=lambda kv: kv[1])]

    # -- sessions --------------------------------------------------------

    def touch(self, row, query, headers, address, state, offset, duration=None):
        """Create or update the session a timeline describes. Returns it."""
        def value(name):
            return query.get(name) or (headers.get(name) if headers else None)

        identifier = value("X-Plex-Client-Identifier") or ""
        session_id = value("X-Plex-Session-Identifier") or identifier or "plexsim"
        entry = self._sessions.get(session_id)
        if entry is None:
            self._next_key += 1
            entry = {"sessionKey": str(self._next_key), "id": session_id}
            self._sessions[session_id] = entry

        player = {"address": address or "", "state": state or "playing",
                  "local": _private(address), "relayed": False,
                  "secure": False, "userID": 1}
        for name, field in PLAYER_FROM.items():
            found = value(name)
            if found:
                player[field] = found
        if not _private(address):
            player["remotePublicAddress"] = address

        media = (row.get("Media") or [{}])[0]
        bitrate = media.get("bitrate") or 0
        entry.update({
            "ratingKey": str(row.get("ratingKey")),
            "row": row,
            "viewOffset": int(offset or 0),
            "duration": duration or row.get("duration"),
            "state": state or "playing",
            "seen": time.time(),
            "Player": player,
            # Measured as ~2.1x the media bitrate in kbps (exact on two of three
            # samples). The formula is not pinned, so it is derived rather than
            # asserted, and it is the one field here that is an approximation.
            "Session": {"id": session_id, "bandwidth": int(round(bitrate * 2.1)),
                        "location": "lan" if player["local"] else "wan"},
            "User": {"id": "1", "thumb": "", "title": self.owner},
        })
        self.record_bytes(identifier, player["local"], 0)
        if state == "stopped":
            self._sessions.pop(session_id, None)
        return entry

    def live(self):
        """Sessions that have not expired, oldest first."""
        cutoff = time.time() - self.ttl
        for key, entry in list(self._sessions.items()):
            if entry["seen"] < cutoff:
                del self._sessions[key]
        return sorted(self._sessions.values(), key=lambda e: int(e["sessionKey"]))

    def by_session_id(self, session_id):
        return self._sessions.get(session_id)

    def terminate(self, session_id, reason):
        """The Stop button. Measured: the row STAYS and the kill is delivered
        through the session's next timeline as a terminationCode, with the next
        part read answering 503."""
        if session_id not in self._sessions:
            return False
        self._terminated[session_id] = (
            2006,
            '"%s" asked to stop your playback from this server: %s'
            % (self.owner, reason or ""))
        return True

    def termination(self, session_id):
        return self._terminated.get(session_id)

    def is_terminated(self, session_id):
        return session_id in self._terminated

    def clear_termination(self, session_id):
        self._terminated.pop(session_id, None)

    # -- statistics ------------------------------------------------------

    def record_bytes(self, identifier, lan, count):
        """Count a response against a (device, lan) bucket for this second."""
        if count < 0:
            return
        key = (self.device_id(identifier), bool(lan))
        second = int(time.time())
        buckets = self._bytes.setdefault(key, {})
        buckets[second] = buckets.get(second, 0) + count

    def record_play(self, row, seconds):
        self._plays.append({"at": int(time.time()), "type": row.get("type"),
                            "seconds": int(seconds or 0)})

    def bandwidth(self, timespan=6, since=None):
        """Rows for `/statistics/bandwidth`, rolled into the asked-for bucket."""
        width = TIMESPANS.get(int(timespan), 1)
        rows = {}
        for (device, lan), buckets in self._bytes.items():
            for second, count in buckets.items():
                if not count:
                    continue
                at = second - (second % width)
                if since is not None and at < since:
                    continue
                key = (device, lan, at)
                rows[key] = rows.get(key, 0) + count
        return [{"accountID": 1, "deviceID": device, "timespan": int(timespan),
                 "at": at, "lan": lan, "bytes": count}
                for (device, lan, at), count in sorted(rows.items(),
                                                       key=lambda kv: kv[0][2])]

    def media_statistics(self, timespan=4):
        """Rows for `/statistics/media`: a count and a duration per bucket."""
        width = TIMESPANS.get(int(timespan), 3600)
        kinds = {"movie": 1, "show": 2, "season": 3, "episode": 4}
        rows = {}
        for play in self._plays:
            at = play["at"] - (play["at"] % width)
            key = (at, kinds.get(play["type"], 1))
            count, seconds = rows.get(key, (0, 0))
            rows[key] = (count + 1, seconds + play["seconds"])
        return [{"accountID": 1, "deviceID": 1, "timespan": int(timespan),
                 "at": at, "metadataType": kind, "count": count, "duration": seconds}
                for (at, kind), (count, seconds) in sorted(rows.items())]
