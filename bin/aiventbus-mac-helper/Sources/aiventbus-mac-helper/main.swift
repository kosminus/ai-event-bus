// aiventbus-mac-helper
//
// Subscribes to macOS desktop signals that are only reachable via native
// Apple APIs and emits them as newline-delimited JSON on stdout. The
// Python producer (aiventbus/producers/desktop_events/backends/mac_helper.py)
// spawns this binary, reads stdout line-by-line, and republishes each
// event on the bus.
//
// Wire format (version 1):
//
//   {"v":1,"type":"helper.ready","ts":"2026-04-17T14:32:17Z","payload":{}}
//   {"v":1,"type":"session.locked","ts":"...","payload":{}}
//   {"v":1,"type":"session.unlocked","ts":"...","payload":{}}
//   {"v":1,"type":"app.launched","ts":"...","payload":{"bundle_id":"com.apple.Safari","pid":1234,"name":"Safari"}}
//   {"v":1,"type":"app.terminated","ts":"...","payload":{"bundle_id":"...","pid":1234}}
//   {"v":1,"type":"app.activated","ts":"...","payload":{"bundle_id":"...","name":"..."}}
//
// Signals:
//   * NSDistributedNotificationCenter subscribes for
//       com.apple.screenIsLocked / com.apple.screenIsUnlocked
//       (these still work on modern macOS and don't require entitlements).
//   * NSWorkspace.shared.notificationCenter subscribes for
//       didLaunchApplicationNotification
//       didTerminateApplicationNotification
//       didActivateApplicationNotification.
//
// Lifecycle: stays alive on RunLoop.main until SIGTERM/SIGINT. Writes
// are line-buffered so the Python side sees events as they arrive.

import Foundation
import AppKit

// MARK: - Line-buffered stdout

// Without this, stdout may be block-buffered when redirected into a pipe
// (which is exactly how Python spawns us), so events wouldn't arrive at
// the producer until the buffer filled up.
setvbuf(stdout, nil, _IOLBF, 0)

// MARK: - Event emission

let isoFormatter: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()

func emit(type: String, payload: [String: Any] = [:]) {
    let event: [String: Any] = [
        "v": 1,
        "type": type,
        "ts": isoFormatter.string(from: Date()),
        "payload": payload,
    ]
    guard
        let data = try? JSONSerialization.data(withJSONObject: event, options: []),
        let line = String(data: data, encoding: .utf8)
    else { return }
    // Each event on its own line.
    print(line)
}

// MARK: - Session lock / unlock via NSDistributedNotificationCenter

let dnc = DistributedNotificationCenter.default()

dnc.addObserver(
    forName: Notification.Name("com.apple.screenIsLocked"),
    object: nil,
    queue: .main
) { _ in
    emit(type: "session.locked")
}

dnc.addObserver(
    forName: Notification.Name("com.apple.screenIsUnlocked"),
    object: nil,
    queue: .main
) { _ in
    emit(type: "session.unlocked")
}

// MARK: - App lifecycle via NSWorkspace

let wsCenter = NSWorkspace.shared.notificationCenter

func payloadFor(_ app: NSRunningApplication?) -> [String: Any] {
    var p: [String: Any] = [:]
    if let id = app?.bundleIdentifier { p["bundle_id"] = id }
    if let name = app?.localizedName { p["name"] = name }
    if let pid = app?.processIdentifier { p["pid"] = Int(pid) }
    return p
}

wsCenter.addObserver(
    forName: NSWorkspace.didLaunchApplicationNotification,
    object: nil,
    queue: .main
) { note in
    let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
    emit(type: "app.launched", payload: payloadFor(app))
}

wsCenter.addObserver(
    forName: NSWorkspace.didTerminateApplicationNotification,
    object: nil,
    queue: .main
) { note in
    let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
    emit(type: "app.terminated", payload: payloadFor(app))
}

wsCenter.addObserver(
    forName: NSWorkspace.didActivateApplicationNotification,
    object: nil,
    queue: .main
) { note in
    let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
    emit(type: "app.activated", payload: payloadFor(app))
}

// MARK: - Clean shutdown on SIGTERM / SIGINT

let sigtermSrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigtermSrc.setEventHandler {
    exit(0)
}
sigtermSrc.resume()
signal(SIGTERM, SIG_IGN)

let sigintSrc = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigintSrc.setEventHandler {
    exit(0)
}
sigintSrc.resume()
signal(SIGINT, SIG_IGN)

// MARK: - Ready + run

emit(type: "helper.ready", payload: ["version": 1])

// Block the main thread on a run loop so the observers stay alive.
RunLoop.main.run()
