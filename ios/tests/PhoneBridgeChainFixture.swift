import Foundation
import Network

// Software-only harness: the production bridge core, twice in series.
// Loopback addresses and ephemeral ports bypass the phone UI's fixed roles.
private final class SnapshotCell {
    private let lock = NSLock()
    private var value = PhoneBridgeSnapshot()
    func put(_ next: PhoneBridgeSnapshot) { lock.lock(); value = next; lock.unlock() }
    func get() -> PhoneBridgeSnapshot { lock.lock(); defer { lock.unlock() }; return value }
}

@main
struct PhoneBridgeChainFixture {
    static func wait(_ predicate: () -> Bool) throws {
        let deadline = Date().addingTimeInterval(5)
        while !predicate() && Date() < deadline { Thread.sleep(forTimeInterval: 0.005) }
        if !predicate() { throw BridgeError("Fixture operation deadline") }
    }

    static func config(_ port: UInt16) -> PhoneBridgeConfig {
        PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                          localPort: .any, upstreamPort: NWEndpoint.Port(rawValue: port)!)
    }

    static func dictionary(_ s: PhoneBridgeSnapshot) -> [String: Any] {
        ["running": s.running, "ready": s.ready, "port": Int(s.port), "active": s.active,
         "accepted": s.accepted, "rejected": s.rejected, "errors": s.errors,
         "forwarded_bytes": s.forwardedBytes, "buffered_bytes": s.bufferedBytes,
         "peak_buffered_bytes": s.peakBufferedBytes]
    }

    static func main() throws {
        guard CommandLine.arguments.count == 2, let port = UInt16(CommandLine.arguments[1]), port != 0 else {
            throw BridgeError("Pass a loopback backend port")
        }
        let rover = PhoneBridgeServer(), operatorBridge = PhoneBridgeServer()
        let roverState = SnapshotCell(), operatorState = SnapshotCell()
        rover.onSnapshot = { roverState.put($0) }
        operatorBridge.onSnapshot = { operatorState.put($0) }
        defer { operatorBridge.stop(); rover.stop() }
        func start() throws {
            // Drain the previous generation before reading the new ephemeral port.
            // A ready snapshot from the old listener must not satisfy this wait.
            operatorBridge.stop(); rover.stop()
            try wait { !operatorState.get().running && !roverState.get().running }
            rover.start(config(port))
            try wait { roverState.get().ready }
            operatorBridge.start(config(roverState.get().port))
            try wait { operatorState.get().ready }
        }
        func emit(_ action: String) throws {
            let data = try JSONSerialization.data(withJSONObject: ["action": action,
                "rover": dictionary(roverState.get()), "operator": dictionary(operatorState.get())], options: [.sortedKeys])
            FileHandle.standardOutput.write(data + Data([10]))
        }
        try start(); try emit("ready")
        while let action = readLine() {
            switch action {
            case "stop-rover":
                rover.stop(); try wait { !roverState.get().running && roverState.get().active == 0 }
            case "stop-operator":
                operatorBridge.stop(); try wait { !operatorState.get().running && operatorState.get().active == 0 }
            case "start": try start()
            case "snapshot": break
            case "quit":
                operatorBridge.stop(); rover.stop()
                try wait { !operatorState.get().running && !roverState.get().running }
                try emit(action); return
            default: throw BridgeError("Unknown fixture action")
            }
            try emit(action)
        }
    }
}
