import Foundation
import Network

final class Locked<Value> {
    private let lock = NSLock()
    private var value: Value
    init(_ value: Value) { self.value = value }
    func get() -> Value { lock.lock(); defer { lock.unlock() }; return value }
    func update(_ operation: (inout Value) -> Void) { lock.lock(); defer { lock.unlock() }; operation(&value) }
}

func require(_ condition: Bool, _ message: String) {
    if !condition { fatalError(message) }
}

func until(_ message: String, timeout: Double = 8, _ condition: () -> Bool) {
    let deadline = Date().addingTimeInterval(timeout)
    while !condition() && Date() < deadline { Thread.sleep(forTimeInterval: 0.01) }
    require(condition(), message)
}

final class EchoAfterFIN {
    let queue = DispatchQueue(label: "bridge-test-upstream")
    let port = Locked<UInt16>(0)
    let received = Locked<Int>(0)
    let listener: NWListener
    var connections: [NWConnection] = []
    var paused: Bool

    init(paused: Bool = false) throws {
        self.paused = paused
        let p = NWParameters.tcp
        p.requiredLocalEndpoint = .hostPort(host: .ipv4(IPv4Address("127.0.0.1")!), port: .any)
        listener = try NWListener(using: p, on: .any)
        listener.stateUpdateHandler = { [weak self] state in
            if case .ready = state { self?.port.update { $0 = self?.listener.port?.rawValue ?? 0 } }
        }
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { return }
            self.connections.append(connection)
            connection.stateUpdateHandler = { [weak self] state in
                if case .ready = state, let self, !self.paused { self.read(connection, data: Data()) }
            }
            connection.start(queue: self.queue)
        }
        listener.start(queue: queue)
    }

    func read(_ connection: NWConnection, data: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self] bytes, _, eof, error in
            guard let self, error == nil else { return }
            var all = data
            if let bytes { all.append(bytes); self.received.update { $0 += bytes.count } }
            if eof {
                // The server must receive the complete request FIN before it
                // responds; an eager full cancel in the bridge breaks this.
                var response = Data("RESP\0".utf8); response.append(all)
                connection.send(content: response, completion: .contentProcessed { error in
                    guard error == nil else { return }
                    connection.send(content: nil, contentContext: .finalMessage, isComplete: true, completion: .contentProcessed { _ in })
                })
            } else { self.read(connection, data: all) }
        }
    }

    func resume() {
        queue.async { self.paused = false; for connection in self.connections { self.read(connection, data: Data()) } }
    }

    func stop() { queue.sync { listener.cancel(); for c in connections { c.cancel() }; connections.removeAll() } }
}

final class Client {
    let queue = DispatchQueue(label: "bridge-test-client")
    let ready = Locked(false), ended = Locked(false), bytes = Locked(Data())
    let connection: NWConnection
    init(port: UInt16) {
        connection = NWConnection(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!, using: .tcp)
        connection.stateUpdateHandler = { [weak self] state in
            if case .ready = state { self?.ready.update { $0 = true }; self?.read() }
            if case .failed = state { self?.ended.update { $0 = true } }
        }
        connection.start(queue: queue)
    }
    func read() {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 8192) { [weak self] data, _, eof, error in
            guard let self else { return }
            if let data { self.bytes.update { $0.append(data) } }
            if eof || error != nil { self.ended.update { $0 = true } }
            else { self.read() }
        }
    }
    func send(_ data: Data, halfClose: Bool) {
        connection.send(content: data, completion: .contentProcessed { [weak self] error in
            if error != nil { self?.ended.update { $0 = true }; return }
            if halfClose { self?.connection.send(content: nil, contentContext: .finalMessage, isComplete: true, completion: .contentProcessed { _ in }) }
        })
    }
    func stop() { connection.cancel() }
}

@main
struct PhoneBridgeFixture {
    static func config(_ upstream: UInt16) -> PhoneBridgeConfig {
        PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                          localPort: .any, upstreamPort: NWEndpoint.Port(rawValue: upstream)!)
    }
    static func main() throws {
        let op = try PhoneBridgeConfig.make(role: .operator, localTailIP: "ignored", roverPeerIP: "100.80.2.3", jetsonUSBIP: "ignored")
        require(String(describing: op.local) == "127.0.0.1" && op.localPort.rawValue == 8081 && op.upstreamPort.rawValue == 8081, "operator binding/ports")
        let rover = try PhoneBridgeConfig.make(role: .rover, localTailIP: "100.127.1.2", roverPeerIP: "ignored", jetsonUSBIP: "172.20.10.3")
        require(rover.upstreamPort.rawValue == 8443, "rover fixed upstream port")
        for bad in ["1.2.3.4", "100.128.1.1", "127.0.0.1", "hostname", "100.080.2.3"] {
            do { _ = try PhoneBridgeConfig.make(role: .operator, localTailIP: "", roverPeerIP: bad, jetsonUSBIP: ""); fatalError("unsafe upstream accepted") }
            catch { }
        }
        do { _ = try PhoneBridgeConfig.make(role: .rover, localTailIP: "100.80.2.3", roverPeerIP: "", jetsonUSBIP: "172.20.10.16"); fatalError("outside USB range accepted") }
        catch { }

        let upstream = try EchoAfterFIN(paused: true)
        defer { upstream.stop() }
        until("upstream listener not ready") { upstream.port.get() != 0 }
        let server = PhoneBridgeServer(maxPairs: 1, connectTimeout: 1, sendTimeout: 4)
        let state = Locked(PhoneBridgeSnapshot())
        server.onSnapshot = { snapshot in state.update { $0 = snapshot } }
        server.start(config(upstream.port.get()))
        until("bridge listener not ready") { state.get().ready }
        let first = Client(port: state.get().port)
        defer { first.stop(); server.stop() }
        until("client did not connect") { first.ready.get() && state.get().active == 1 }
        let excess = Client(port: state.get().port)
        defer { excess.stop() }
        until("connection limit not enforced") { state.get().rejected == 1 }
        var payload = Data()
        for i in 0..<262144 { payload.append(UInt8(truncatingIfNeeded: i * 19)) }
        first.send(payload, halfClose: true)
        Thread.sleep(forTimeInterval: 0.3)
        require(upstream.received.get() == 0, "paused upstream unexpectedly read")
        require(state.get().peakBufferedBytes <= 32768, "application buffering exceeded one16KiB per lane")
        upstream.resume()
        until("half-close response did not drain") { first.ended.get() }
        var expected = Data("RESP\0".utf8); expected.append(payload)
        require(first.bytes.get() == expected, "opaque bytes corrupted: got=\(first.bytes.get().count) expected=\(expected.count) upstream=\(upstream.received.get()) status=\(state.get().status) forwarded=\(state.get().forwardedBytes) errors=\(state.get().errors)")
        until("completed pair not released") { state.get().active == 0 }
        require(state.get().errors == 0, "healthy half-close raised error")
        require(state.get().forwardedBytes == UInt64(payload.count + expected.count), "byte accounting wrong")

        // Exercise small FIN payloads and multiple buffer boundaries, including
        // a large response that must drain after the peer's terminal state.
        for count in [3, 16393, 1024 * 1024] {
            let client = Client(port: state.get().port)
            until("repeat client not ready") { client.ready.get() && state.get().active == 1 }
            let request = Data(repeating: UInt8(count % 251), count: count)
            client.send(request, halfClose: true)
            until("repeat half-close failed") { client.ended.get() }
            var response = Data("RESP\0".utf8); response.append(request)
            require(client.bytes.get() == response, "repeat response truncated at \(count) bytes")
            until("repeat pair not drained") { state.get().active == 0 }
            require(state.get().errors == 0, "repeat pair errored")
            client.stop()
        }

        // Stall a fresh upstream, put a large write through the real bridge,
        // then stop. Pending reads/writes must not keep the pair alive.
        let stalled = try EchoAfterFIN(paused: true)
        defer { stalled.stop() }
        until("stalled listener not ready") { stalled.port.get() != 0 }
        server.start(config(stalled.port.get()))
        until("second bridge not ready") { state.get().ready && state.get().accepted == 0 }
        let pending = Client(port: state.get().port)
        defer { pending.stop() }
        until("pending client not connected") { pending.ready.get() && state.get().active == 1 }
        pending.send(Data(repeating: 17, count: 16 * 1024 * 1024), halfClose: false)
        Thread.sleep(forTimeInterval: 0.5)
        require(state.get().peakBufferedBytes <= 32768, "slow receiver caused unbounded app buffering")
        require(state.get().forwardedBytes < 16 * 1024 * 1024, "backpressure did not stop forwarding to unread peer")
        server.stop()
        until("stop left a pair alive") { !state.get().running && state.get().active == 0 && state.get().bufferedBytes == 0 }
        until("stop did not close client") { pending.ended.get() }
        print("PASS: fixed IP policy, opaque integrity, delayed-reader backpressure, half-close reverse drain, limit, stop")
    }
}
