// Starts Mac's Python app and stays as its parent, so macOS credits permissions (mic, screen, accessibility) to Mac.app.
// Stop signals are passed on to Python, and the launcher exits the way Python did, so launchd restarts only crashes.
import Foundation

let home = FileManager.default.homeDirectoryForCurrentUser.path
// build_app.sh writes the project folder into the app. Apps built before it did use the folder they were made for.
let saved = Bundle.main.path(forResource: "project_path", ofType: nil)
    .flatMap { try? String(contentsOfFile: $0, encoding: .utf8) }?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
let project = saved.isEmpty ? home + "/My Projects/companion" : saved
let task = Process()
task.executableURL = URL(fileURLWithPath: project + "/.venv/bin/python")
task.arguments = [project + "/companion.py"]
task.currentDirectoryURL = URL(fileURLWithPath: project)
// launchd's PATH is bare; add where claude and Homebrew tools live.
var env = ProcessInfo.processInfo.environment
env["PATH"] = [home + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin",
               env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"].joined(separator: ":")
task.environment = env
try task.run()

// Python started with default signal handling; now catch launchctl stop, logout and Ctrl+C and forward them.
var sources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .global())
    source.setEventHandler { kill(task.processIdentifier, sig) }
    source.resume()
    sources.append(source)
}
task.waitUntilExit()
// Killed by a signal: exit 128 + signal like a shell does (nonzero, so launchd counts it as a crash).
exit(task.terminationReason == .uncaughtSignal ? 128 + task.terminationStatus : task.terminationStatus)
