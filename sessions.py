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

import re
import time


_NUMERIC_PLATFORM = re.compile(
    r"^[ \t]*[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[ \t]*$")


def _stored_platform(value):
    """Return the typed platform value PMS stores for a numeric spelling."""
    text = str(value)
    if not _NUMERIC_PLATFORM.fullmatch(text):
        return value
    text = text.strip()
    try:
        return float(text) if "." in text or "e" in text.lower() else int(text, 10)
    except (OverflowError, ValueError):
        return value

# Cycle 26, re-measured on the writable copy on `long` media over TEN runs, polling once
# a second and with the row identified by its own client identifier: the row went at
# +184.0 at the earliest and +210.4 at the latest, and was still there at +209.4 in one
# of them. That is not one constant with a fine sweep -- a session alive at +209.4 in one
# run and gone at +184.0 in another -- so it is a cutoff plus a reaper TICK. The bracket
# the ten samples give is a cutoff at or below 184 s with a tick of at least 26 s; 180
# and 30 are the round pair inside it, and the boundary is not bisected. Two durations
# (10 s and 30 min) and two rows behaved the same, so it is not a function of either.
#
# Cycle 9's 60-80 s came from the lab's ten-second clips before `long` mode existed, and
# is the shape §3.3 warns about for everything measured there. The consequence of the old
# value is a client that stops beating -- several TV apps stop while paused -- vanishing
# from Now Playing after seventy seconds where a real PMS holds it for three minutes.
#
# Cycle 41 made a dense cohort on both disposable versions: fifteen sessions opened two
# seconds apart and polled once a second. Rows disappeared in two global clusters 29.94 s
# and 29.97 s apart. At a tick an age of 180.90/181.05 s was removed while the adjacent
# 178.95/178.99 s row survived. The rule is therefore a process-wide 30-second reaper
# applying `seen < tick - 180`, not a per-row timer. A row remains visible for 180-210 s.
SESSION_TTL = 180
SESSION_REAP_INTERVAL = 30

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
        # The real scheduler is process-global. Advancing this phase only when the
        # surface is observed is equivalent for an in-memory server: skipped ticks have
        # no externally visible effect until a client reads the session list.
        self._last_reap = time.time()
        self._sessions = {}
        self._next_key = 1
        self._terminated = {}
        # (device, lan) -> {second: bytes}. A real PMS counts every response's
        # bytes, not just media, and rolls the per-second rows into the coarser
        # buckets on read.
        self._bytes = {}
        # Id 1 is the row with NO client identifier -- measured: the reference's
        # device 1 has an empty name, platform and clientIdentifier and was created
        # when the server started. Every client after it counts up from 2.
        started = int(time.time())
        self._devices = {"": 1}
        self._device_seen = {"": started}
        self._device_names = {}
        self._device_platforms = {}
        self._next_device = 1
        self._plays = []

    # -- identity --------------------------------------------------------

    def device_id(self, identifier, name=None, platform=None):
        """A stable small integer per client identifier, as a PMS assigns.

        `name` and `platform` are the client's X-Plex-Device-Name and
        X-Plex-Platform, recorded whenever a request carries them.
        """
        identifier = identifier or ""
        if identifier not in self._devices:
            self._next_device += 1
            self._devices[identifier] = self._next_device
            self._device_seen[identifier] = int(time.time())
        # Cycle 49, measured on both writable copies: a request carrying a device name or
        # a platform and NO client identifier writes them onto the ANONYMOUS row -- the
        # one with `clientIdentifier=""` -- exactly as an identified one writes its own.
        # plexsim skipped it, so that row stayed nameless whatever a client sent. (This
        # is the same write that corrupted both references: a numeric `X-Plex-Platform`
        # stores as a number there and makes `/devices` a 500. Preserve that typed state;
        # the two affected list endpoints reproduce the measured refusal while the
        # direct device row remains readable.)
        if name:
            self._device_names[identifier] = name
        if platform:
            self._device_platforms[identifier] = _stored_platform(platform)
        return self._devices[identifier]

    def devices(self):
        """The Device rows `/devices` lists and the Dashboard's graphs key by.

        Re-measured, and overturning the note that stood here: a real PMS fills
        `name` from X-Plex-Device-Name and `platform` from X-Plex-Platform, each
        independently (a client that sent only a platform has `name: ""`), and
        leaves them empty only for a client that sent neither -- which is all the
        earlier probe, sending no X-Plex headers, could see. `/devices` lists
        EVERY device the server has seen, 40 on the reference, where plexsim
        listed one row describing itself.
        """
        return [self._device_row(identifier, key)
                for identifier, key in sorted(self._devices.items(), key=lambda kv: kv[1])]

    def has_numeric_platform(self):
        """Whether a listed Device would trip PMS's string-only serialiser."""
        return any(isinstance(value, (int, float)) and not isinstance(value, bool)
                   for value in self._device_platforms.values())

    def device(self, device_id):
        for identifier, key in self._devices.items():
            if str(key) == str(device_id):
                return self._device_row(identifier, key)
        return None

    def _device_row(self, identifier, key):
        return {"id": key, "name": self._device_names.get(identifier, ""),
                "platform": self._device_platforms.get(identifier, ""),
                "clientIdentifier": identifier,
                "createdAt": self._device_seen.get(identifier, 0)}

    # -- sessions --------------------------------------------------------

    def touch(self, row, query, headers, address, state, offset, duration=None,
              profile=None):
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
        # The client profile the server RESOLVED for this session. Cycle 10 recorded it
        # as the constant "Web" from a copy that had only ever been driven by Plex Web;
        # cycle 27 swept all 78 profile names through it and it is nothing of the kind.
        # It is the resolved profile's own NAME -- `Roku` reports `Roku-7.x`, `Safari`
        # and `Opera` report `Web`, `Plexamp` reports itself -- and the attribute is
        # OMITTED entirely when nothing the client sent names a profile (`webOS`,
        # `Linux`, `Darwin`, `chrome`). The resolver lives beside the profile list in
        # server.py, because the decision's own 400 asks the same question of the same
        # three headers.
        if profile:
            player["profile"] = profile
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
        self.record_bytes(identifier, player["local"], 0,
                          name=value("X-Plex-Device-Name"),
                          platform=value("X-Plex-Platform"))
        if state == "stopped":
            self._sessions.pop(session_id, None)
        return entry

    # What a session row does NOT carry, measured on a writable copy of the reference
    # against the same item's detail row: no `Guid` and no `Image`, where plexsim copied the
    # detail row wholesale -- three `<Image>` children a real PMS never sends there.
    SESSION_ROW_DROPS = ("Guid", "Image")

    def live(self):
        """Sessions that have not expired, oldest first."""
        now = time.time()
        elapsed = now - self._last_reap
        if elapsed >= SESSION_REAP_INTERVAL:
            ticks = int(elapsed // SESSION_REAP_INTERVAL)
            self._last_reap += ticks * SESSION_REAP_INTERVAL
            cutoff = self._last_reap - self.ttl
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

    def record_bytes(self, identifier, lan, count, name=None, platform=None):
        """Count a response against a (device, lan) bucket for this second."""
        if count < 0:
            return
        key = (self.device_id(identifier, name, platform), bool(lan))
        second = int(time.time())
        buckets = self._bytes.setdefault(key, {})
        buckets[second] = buckets.get(second, 0) + count

    def record_play(self, row, seconds, device_id=1):
        self._plays.append({"at": int(time.time()), "type": row.get("type"),
                            "seconds": int(seconds or 0), "device": device_id})

    # Measured: with NO `timespan` a real PMS answers every rollup at once --
    # year, month, week, day and hour rows (0 to 4) in one list, ordered by span.
    ROLLUPS = (0, 1, 2, 3, 4)

    def bandwidth(self, timespan=6, since=None):
        """Rows for `/statistics/bandwidth`, rolled into the asked-for bucket."""
        if timespan is None:
            return [row for span in self.ROLLUPS for row in self.bandwidth(span, since)]
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
        """Rows for `/statistics/media`: a count and a duration per bucket.

        Keyed by the device that PLAYED, which is the client that sent the
        timeline. plexsim wrote device 1 on every row, an id that no Device row in
        the same container named.
        """
        if timespan is None:
            return [row for span in self.ROLLUPS for row in self.media_statistics(span)]
        width = TIMESPANS.get(int(timespan), 3600)
        kinds = {"movie": 1, "show": 2, "season": 3, "episode": 4}
        rows = {}
        for play in self._plays:
            at = play["at"] - (play["at"] % width)
            key = (at, kinds.get(play["type"], 1), play.get("device", 1))
            count, seconds = rows.get(key, (0, 0))
            rows[key] = (count + 1, seconds + play["seconds"])
        return [{"accountID": 1, "deviceID": device, "timespan": int(timespan),
                 "at": at, "metadataType": kind, "count": count, "duration": seconds}
                for (at, kind, device), (count, seconds) in sorted(rows.items())]
