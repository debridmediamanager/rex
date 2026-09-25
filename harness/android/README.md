# The Android instrument — Plex for Android in an emulator, pinned to one server

Cycle 70's instrument. Plex for Android 2026.9.0 is a **React Native app compiled to Hermes
bytecode**, a codebase separate from Plex Web, so it builds its own requests. This directory drives
the real app in a local arm64 emulator through mitmproxy, pinned to exactly one server (plexsim or a
writable reference copy), and records every exchange the way `harness/scripts/tap.py` does for the
web client.

Nothing here holds a token. State and captures live in `$DROID_WORK` (default
`~/plexsim-android`); tokens come from `$DROID_TOKENS` (default `$DROID_WORK/tokens.env`, mode 600,
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
