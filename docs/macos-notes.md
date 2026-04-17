# Running aiventbus on macOS

aiventbus runs on macOS 12 (Monterey) or newer. Most of the daemon is OS-agnostic; a thin platform layer (`aiventbus.platform`) and a small Swift sidecar handle the Apple-specific pieces. This doc covers the parts that differ from Linux.

## Install

```bash
git clone <repo-url> && cd aiventbus
pip install -e .
```

That's enough to run the daemon interactively (`python -m aiventbus`). If you want the daemon to autostart and survive reboots:

```bash
aibus install                 # writes ~/Library/LaunchAgents/com.aiventbus.daemon.plist and loads it
aibus install --build-helper  # also builds bin/aiventbus-mac-helper and installs it to ~/.local/bin
```

`aibus install` generates the LaunchAgent plist from the live `sys.executable` and resolved config/DB/log paths. The resolved paths are pinned into `EnvironmentVariables` so the launchd-managed daemon resolves identically to an interactive CLI invocation — no split-brain between a daemon running from launchd and a CLI run from your repo checkout.

`aibus install --build-helper` requires the Xcode command-line tools (`xcode-select --install`). It runs `swift build -c release` in `bin/aiventbus-mac-helper` and copies the resulting binary to `~/.local/bin/aiventbus-mac-helper`. `--dev --build-helper` symlinks instead of copying, so local rebuilds are picked up without rerunning the installer.

`aibus uninstall` removes the LaunchAgent and the installed helper. `aibus uninstall --purge` also deletes `~/Library/Application Support/aiventbus/` (config + DB) and `~/Library/Logs/aiventbus/`.

## Directory layout

All per-user state lives under standard macOS directories:

| What | Path |
|---|---|
| Config | `~/Library/Application Support/aiventbus/config.yaml` |
| Database | `~/Library/Application Support/aiventbus/aiventbus.db` |
| Logs | `~/Library/Logs/aiventbus/{stdout,stderr}.log` |
| Swift helper | `~/.local/bin/aiventbus-mac-helper` |
| LaunchAgent | `~/Library/LaunchAgents/com.aiventbus.daemon.plist` |

Override any path with `--config`/`--db` on the command line or the `AIVENTBUS_CONFIG`/`AIVENTBUS_DB`/`AIVENTBUS_MAC_HELPER` environment variables. CWD fallbacks (`./config.yaml`, `./aiventbus.db`) are opt-in via `--dev` or `$AIVENTBUS_DEV=1` so a launchd-managed daemon never silently picks up files from an unrelated working directory.

## Capability coverage

The Producers tab in the dashboard (and `GET /api/v1/producers`) reports per-capability availability with a concrete reason when something isn't wired up. Summary for macOS:

| Capability | Backend | Notes |
|---|---|---|
| `clipboard` | `pbpaste` | Always available on macOS. |
| `system_log` | `log stream --style=ndjson --predicate …` | Default predicate surfaces errors, faults, and auth subsystems. Override with `producers.log_stream_predicate` in config. |
| `session_state` | Swift helper → `NSDistributedNotificationCenter` (`com.apple.screenIsLocked` / `…Unlocked`) | Requires the Swift helper. |
| `app_lifecycle` | Swift helper → `NSWorkspace.notificationCenter` | Requires the Swift helper. Emits `app.launched` / `app.terminated` / `app.activated` with bundle ID, PID, and name. |
| `notifications_received` | **Unavailable** | Modern macOS requires a system extension to capture other apps' Notification Center messages. Out of scope. |
| `notifications_outbound` | `osascript -e 'display notification …'` | Used by the `notify` executor action. |
| `file_watch` | `watchfiles` / FSEvents | Portable. |
| `terminal_history` | zsh `extended_history` / bash history | Portable. Shell preexec hook via `aibus shell-hook --install` is recommended for real-time capture. |
| `webhook`, `cron` | HTTP / APScheduler | Portable. |

## The Swift helper

`bin/aiventbus-mac-helper` is a ~140-line Swift binary that subscribes to a handful of Apple APIs and emits newline-delimited JSON on stdout:

```json
{"v":1,"type":"helper.ready","ts":"2026-04-17T14:32:17Z","payload":{"version":1}}
{"v":1,"type":"session.locked","ts":"…","payload":{}}
{"v":1,"type":"app.launched","ts":"…","payload":{"bundle_id":"com.apple.Safari","pid":1234,"name":"Safari"}}
```

The Python side (`aiventbus/producers/desktop_events/backends/mac_helper.py`) spawns the helper, validates `v == 1`, maps event types onto bus topics, and restarts the helper with exponential backoff (cap 30s) on crash. Unknown types are logged and dropped so adding a new event type on the Swift side doesn't break old consumers.

**Runtime lookup is deterministic on purpose.** `mac_helper_path()` checks `$AIVENTBUS_MAC_HELPER` first (what the generated LaunchAgent plist pins), then `~/.local/bin/aiventbus-mac-helper`. No `$PATH` scan, no repo-relative fallback — a daemon started by launchd in `/` with a minimal environment resolves to the same binary as an interactive CLI.

If you move or rebuild the helper manually without running `aibus install`, the daemon fails fast with a clear error rather than picking up a surprising binary.

## Widget (Tauri)

The desktop widget builds to `.app` and `.dmg`:

```bash
cd widget
cargo tauri build
```

Artifacts land under `widget/src-tauri/target/release/bundle/{macos,dmg}/`.

**Builds are unsigned for now.** The first time you run a build, macOS Gatekeeper will refuse to open the `.app`. Either right-click → Open (and click Open again in the warning dialog), or `xattr -d com.apple.quarantine /path/to/aiventbus-widget.app`. Proper signing + notarization is an explicit deferred item — it needs an Apple Developer account ($99/year) and a CI pipeline.

Global hotkey (`Ctrl+Space` to focus the chat input) requires macOS Accessibility permission. The first time you launch the widget with the hotkey enabled, System Settings → Privacy & Security → Accessibility will prompt; toggle on `aiventbus-widget`.

## Known limitations

- **Notification-content capture isn't supported.** You can send notifications via `osascript`, but reading what other apps are showing in Notification Center requires a macOS system extension. The `desktop_events` producer reports `notifications_received: unavailable` with that reason.
- **App lifecycle only reports NSWorkspace events.** Apps launched via subprocess / launchd without an NSApplication (CLI tools, background daemons) don't show up. This matches how Spotlight and Activity Monitor see the world, not `ps`.
- **Unified logging is a firehose.** The default predicate (errors + faults + auth subsystems) keeps noise manageable. If you broaden the predicate via `log_stream_predicate` in `config.yaml`, expect a meaningful fraction of CPU to go toward parsing and filtering.
- **Widget builds are unsigned.** See the Gatekeeper workaround above.

## Troubleshooting

**`desktop_events` says "macOS helper not installed"** — Run `aibus install --build-helper`. Make sure `xcode-select -p` reports a valid path first.

**`aibus install --build-helper` fails with `swift: command not found`** — Install the Xcode command-line tools: `xcode-select --install`.

**Daemon started by launchd uses a different DB than my CLI** — Check `GET /api/v1/system/status` in both contexts. The `config_source` block reports which path was resolved and why. If they differ, the likely cause is that one context has `$AIVENTBUS_DEV=1` set (pulling in `./aiventbus.db`) while the other uses the platform default. Re-run `aibus install` to pin the production paths into the LaunchAgent.

**Notifications don't fire** — The `notify` executor action shells out to `osascript`. If that's broken, the first failing notification attempt surfaces the error in the approvals history. On a locked-down machine, check `System Settings → Notifications → Script Editor` and verify notifications from `osascript` are allowed.

**`log stream` consumes lots of CPU** — The default predicate is selective but the firehose is big. Narrow `log_stream_predicate` to the subsystems you actually care about, e.g. `'subsystem == "com.apple.xpc.launchd"'`.
