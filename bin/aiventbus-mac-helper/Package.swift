// swift-tools-version:5.9
//
// aiventbus-mac-helper — Swift sidecar that surfaces macOS desktop events
// as newline-delimited JSON on stdout. Consumed by
// aiventbus/producers/desktop_events/backends/mac_helper.py.
//
// Build: swift build -c release
// Install: aibus install --build-helper (copies the built binary to
// ~/.local/bin/aiventbus-mac-helper).

import PackageDescription

let package = Package(
    name: "aiventbus-mac-helper",
    platforms: [.macOS(.v12)],
    targets: [
        .executableTarget(
            name: "aiventbus-mac-helper",
            path: "Sources/aiventbus-mac-helper"
        ),
    ]
)
