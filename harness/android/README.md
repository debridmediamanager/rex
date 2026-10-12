# The Android instrument — Plex for Android in an emulator, pinned to one server

Cycle 70's instrument. Plex for Android (2026.9.0 then; **2026.19.1**, versionCode 971050475,
since cycle 94) is a **React Native app compiled to Hermes
bytecode**, a codebase separate from Plex Web, so it builds its own requests. This directory drives
the real app in a local arm64 emulator through mitmproxy, pinned to exactly one server (rex or a
writable reference copy), and records every exchange the way `harness/scripts/tap.py` does for the
web client.

Nothing here holds a token. State and captures live in `$DROID_WORK` (default
`~/rex-android`); tokens come from `$DROID_TOKENS` (default `$DROID_WORK/tokens.env`, mode 600,
`ACCOUNT_TOKEN=` plus whatever server tokens you pin to).

## Setup (once)

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
sdkmanager "system-images;android-33;google_apis;arm64-v8a" "system-images;android-33;android-tv;arm64-v8a"
# sdkmanager's root is /opt/homebrew/share/android-commandlinetools; the emulator also needs
# platform-tools and platforms there, or it reports "Broken AVD system path":
R=/opt/homebrew/share/android-commandlinetools
ln -s ~/Library/Android/sdk/platform-tools $R/platform-tools; ln -s ~/Library/Android/sdk/platforms $R/platforms
echo no | avdmanager create avd -n c70phone -k "system-images;android-33;google_apis;arm64-v8a" -d pixel_6
```

The APK: pull the four splits of `com.plexapp.android` off a phone that has it (`dumpsys package
com.plexapp.android | grep codePath`, then `adb pull` `base.apk` and the `split_config.*.apk`s —
`pm path` fails on Android 17) and `adb install-multiple` them.

The proxy's CA must be a SYSTEM CA: boot with `-writable-system`, then `adb root`, `avbctl
disable-verification`, reboot, `adb remount`, push `~/.mitmproxy/mitmproxy-ca-cert.pem` as
`/system/etc/security/cacerts/<subject_hash_old>.0`, reboot. It survives later boots.

`$DROID_WORK/resources.json` is the account's own `GET https://clients.plex.tv/api/v2/resources`
(JSON); `droid.py` uses its `plexlab` entry as the template for the one server it advertises.

## Running a walk

```bash
./emu.sh c70phone 5580                      # boots through the proxy on 8899; animations off
./fresh.sh emulator-5580 <label> <name> <machineIdentifier> http://<ip>:<port> <token> <version>
python3 walk.py emulator-5580 <label> "<movie library>" "<movie title>" "<show library>" "<show title>" [steps]
python3 capdiff.py $DROID_WORK/N/cap/<ref>.jsonl <ref port> $DROID_WORK/N/cap/<sim>.jsonl <sim port>
```

`fresh.sh` restarts the proxy under launchd for `<label>` (one capture file per label), clears the
app and signs it in. `walk.py` steps: home, recommended, browse, third, movie, play, show, search;
each writes a UI dump, a screenshot and the capture-line range it produced.

## What `droid.py` does, and why

* **Rewrites plex.tv's resource list to ONE server.** The account owns production servers; a client
  that sees them talks to them (the same trap as `isolate.py`). The entry carries the pinned server's
  `accessToken` and one plain `http://IP:port` connection.
* **Answers plex.tv writes locally** (`{}`) so the account is never mutated — except creating a PIN
  or an anonymous user, which touch nothing of the account. Sign-in completes by injecting
  `authToken` into the PIN poll; the app never links a device.
* **Answers the account's `favoriteLibraries` setting** as the pinned server's sections, which is what
  makes Home draw that server's shelves, without writing the account.
* **Refuses production hosts outright** and any `*.plex.direct` name.
* **Re-resolves an IPv6 CONNECT over IPv4**: the emulator resolves names itself and CONNECTs to an
  AAAA literal this Mac cannot route ("No route to host" on every plex.tv call).
* `rewrite` (from `$DROID_WORK/N/rewrite.txt`, `a=>b;c=>d`) substitutes strings in the pinned server's
  answers — how cycle 70 proved a minted guid was the cause, by swapping it alone.

## Traps

* **The app caches per-server state.** Switching the proxy's target without clearing the app keeps
  drawing the old server. `fresh.sh` clears it every time.
* **uiautomator cannot dump a screen that never idles** (skeleton spinners); `emu.sh` turns
  animations off, and the walk falls back to coordinates for the library picker, a sheet the dump
  does not contain.
* **Background waiters that `pgrep -f` their own pattern never exit** — they match themselves. Wait
  on a PID.
* **A mitmdump started from an agent tool call can die between calls**; run it under launchd
  (`restart_mitm.sh`). A dead proxy leaves the emulator with no route at all.
* **After a cold boot the emulator can lose its default route** ("Network is unreachable");
  `adb emu kill` and boot again.
* **Keep HTTP/2 off at the proxy** (`--set http2=false`, in `mitm.sh`). With it on, a pip-installed
  mitmproxy 12.2.3 stalled the app after sign-in (cycle 73): `/api/v2/user` answered, then no
  further plex.tv request went out and the app reported "The operation was aborted" to Sentry.
* **The Homebrew cask's `mitmdump` can hang in the dynamic loader** before `main` (0 % CPU,
  `sample` shows only `_dyld_start`). Point `MITMDUMP` at a venv's instead:
  `uv venv ~/.cache/mitmvenv && uv pip install --python ~/.cache/mitmvenv/bin/python mitmproxy`.
* **Never pass a path containing `claude` to a long-lived process** on the driver's Mac: an hourly
  launchd job there `kill -9`s every tty-less process whose command line matches it. Symlink the
  work directory (`ln -s <scratch> ~/pxc70`) and use `DROID_WORK=~/pxc70`.
* **2026.19.1 opens the Favorite Libraries sheet straight from the Libraries tab** while no
  library is selected (cycle 94); 2026.9.0 drew a zero state whose "<lib> library on <server>"
  button opened it. `walk.py` accepts either.
* **After sign-in, 2026.19.1 can sit on `screen-loading`** with no further PMS request until the
  app is force-stopped and started again: `signin.py` then reports `state None` although the
  sign-in itself succeeded, and `am force-stop` + `am start` reaches Home (cycle 94, three times).
* **A pinned connection on a Tailscale address (`100.x`) does not go through the proxy**: the
  app reached plexscratch at `100.101.10.1:32410` directly, so nothing of it is in the tap log and
  `rewrite.txt` cannot touch it. Read the copy's own `Logs/Plex Media Server.log` (`Request:`
  lines) for the exact requests, or pin a `10.0.2.2` address that a local relay forwards.
* **Pinned to an unclaimed copy, a first sign-in lands on "Review Your Favorite Libraries"**
  with nothing favourited; Continue reaches a Home with no server shelves. The Server screen
  (Profile, `menu-user-servers`) still works: its Dashboard hides the Bandwidth, CPU and RAM
  graphs because the copy 404s `/statistics/*`, and Top Played retries the 404ing
  `/library/all/top` in a loop.

* **A cold start of 2026.19.1 restores the last screen** (the Server dashboard, say), which has no
  tab bar; `walk.py`'s `reset()` backs out to Home first, and its library step taps the sheet's
  `<lib> on <server>` row by label. The overflow button is `action-button-overflow`, not "More".
* **2026.19.1 opens `/:/websockets/notifications` only for an account with the
  `WebsocketsForPms` feature** (uuid `0668d10e-...`, from `/api/v2/features`); the test account
  lacks it. `featinject.py` adds it to the answer in the proxy (nothing is written to plex.tv);
  load it BEFORE `tap.py` so the tap records the edited body. The socket carries the ACCOUNT
  token, not the server's: a rex started with `--token` 401s it. `wstap.py` logs every frame
  and the close (`wslog=`); mitmproxy does not count ping/pong frames as messages.
* **Pin a copy on Tailscale through a local relay** when the proxy has to see it: droid only maps
  `10.0.2.2:32588` to loopback, and a `100.x` connection bypasses the proxy entirely; with a
  `10.0.2.2:<port>` relay the favourites hook cannot reach the target and the sheet shows the
  ACCOUNT's real favourites -- never pick one of those rows.

## Request census

`probes/apkcensus.py decompile <apk or split dir> <out>` pulls the Hermes bundle out of
the base APK and decompiles it; `census` and `diff` list every request template with its base
(PMS helper, plex.tv-family client, player-direct, in-app route) and call site.
`handover/android-census-2026.19.1.md` is the 2026.9.0 -> 2026.19.1 diff.

