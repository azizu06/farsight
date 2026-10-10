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
    let responsePrefix: String

    init(paused: Bool = false, responsePrefix: String = "RESP\0") throws {
        self.paused = paused
        self.responsePrefix = responsePrefix
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
                var response = Data(self.responsePrefix.utf8); response.append(all)
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
    func stopAccepting() { queue.sync { listener.cancel() } }
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
