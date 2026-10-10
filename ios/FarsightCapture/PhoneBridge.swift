import Combine
import Foundation
import Network

enum PhoneBridgeRole: String, CaseIterable, Identifiable {
    case off, rover, `operator`
    var id: String { rawValue }
    var title: String { self == .off ? "Off" : self == .rover ? "Rover phone" : "Operator phone" }
}

struct PhoneBridgeConfig {
    let local: IPv4Address
    let upstream: IPv4Address
    let localPort: NWEndpoint.Port
    let upstreamPort: NWEndpoint.Port

    static func make(role: PhoneBridgeRole, localTailIP: String, roverPeerIP: String,
                     jetsonUSBIP: String) throws -> PhoneBridgeConfig {
        func address(_ text: String) throws -> IPv4Address {
            let parts = text.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: ".", omittingEmptySubsequences: false)
            guard parts.count == 4, parts.allSatisfy({ UInt8($0).map { String($0) } == String($0) }),
                  let value = IPv4Address(parts.joined(separator: ".")) else { throw BridgeError("Use a numeric IPv4 address") }
            return value
        }
        func tail(_ text: String) throws -> IPv4Address {
            let value = try address(text), bytes = Array(value.rawValue)
            guard bytes[0] == 100 && (64...127).contains(bytes[1]) else { throw BridgeError("Tailscale IPv4 must be in 100.64.0.0/10") }
            return value
        }
        if role == .operator {
            return PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: try tail(roverPeerIP),
                                     localPort: 8081, upstreamPort: 8081)
        }
        guard role == .rover else { throw BridgeError("Choose a phone role before starting") }
        let local = try tail(localTailIP), upstream = try address(jetsonUSBIP), bytes = Array(upstream.rawValue)
        guard (bytes[0] == 172 && bytes[1] == 20 && bytes[2] == 10 && bytes[3] < 16) ||
              (bytes[0] == 192 && bytes[1] == 168 && bytes[2] == 55) else {
            throw BridgeError("Jetson USB address must be in 172.20.10.0/28 or 192.168.55.0/24")
        }
        return PhoneBridgeConfig(local: local, upstream: upstream, localPort: 8081, upstreamPort: 8443)
    }
}

struct BridgeError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

private func bridgeTCPParameters() -> NWParameters {
    let options = NWProtocolTCP.Options()
    options.noDelay = true
    return NWParameters(tls: nil, tcp: options)
}

struct PhoneBridgeSnapshot {
    var running = false
    var ready = false
    var port: UInt16 = 0
    var active = 0
    var accepted = 0
    var rejected = 0
    var errors = 0
    var forwardedBytes: UInt64 = 0
    var bufferedBytes = 0
    var peakBufferedBytes = 0
    var status = "Bridge off"
}

/// Serialized transport core. Tests use loopback configs; the UI can only create
/// the two validated fixed-upstream configs above. No HTTP/control interpretation.
final class PhoneBridgeServer {
    private let queue = DispatchQueue(label: "farsight.phone-bridge", qos: .userInitiated)
    private var listener: NWListener?
    private var pairs: [UUID: BridgePair] = [:]
    private var snapshot = PhoneBridgeSnapshot()
    private var generation = UUID()
    private let maxPairs: Int
    private let connectTimeout: Double
    private let sendTimeout: Double
    var onSnapshot: ((PhoneBridgeSnapshot) -> Void)?

    func setSnapshotHandler(_ handler: @escaping (PhoneBridgeSnapshot) -> Void) {
        queue.async { [weak self] in self?.onSnapshot = handler }
    }

    init(maxPairs: Int = 16, connectTimeout: Double = 3, sendTimeout: Double = 3) {
        self.maxPairs = maxPairs; self.connectTimeout = connectTimeout; self.sendTimeout = sendTimeout
    }

    deinit {
        listener?.cancel()
        for pair in pairs.values { pair.cancel() }
    }

    func start(_ config: PhoneBridgeConfig) {
        queue.async { [weak self] in self?.startOnQueue(config) }
    }

    func stop() { queue.async { [weak self] in self?.stopOnQueue("Bridge stopped") } }

    private func emit() { onSnapshot?(snapshot) }

    private func stopOnQueue(_ reason: String) {
        generation = UUID()
        listener?.stateUpdateHandler = nil; listener?.newConnectionHandler = nil
        listener?.cancel(); listener = nil
        let old = pairs; pairs.removeAll()
        for pair in old.values { pair.cancel() }
        snapshot.running = false; snapshot.ready = false; snapshot.active = 0; snapshot.bufferedBytes = 0
        snapshot.status = reason; emit()
    }

    private func startOnQueue(_ config: PhoneBridgeConfig) {
        stopOnQueue("Bridge starting")
        snapshot = PhoneBridgeSnapshot(); snapshot.running = true; snapshot.status = "Binding local listener"
        let token = generation
        do {
            let parameters = bridgeTCPParameters()
            // The bind address and port come only from requiredLocalEndpoint: also passing the
            // port to NWListener(using:on:) makes iOS fail the listener with POSIX(22) EINVAL.
            parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(config.local), port: config.localPort)
            let listener = try NWListener(using: parameters)
            self.listener = listener
            listener.stateUpdateHandler = { [weak self] state in
                guard let self, self.generation == token else { return }
                switch state {
                case .ready:
                    self.snapshot.ready = true; self.snapshot.port = listener.port?.rawValue ?? 0
                    self.snapshot.status = "Listener ready · phone VPN/USB path unqualified"; self.emit()
                case .failed(let error): self.snapshot.errors += 1; self.stopOnQueue("Listener failed: \(error)")
                case .waiting(let error): self.snapshot.status = "Listener waiting: \(error)"; self.emit()
                default: break
                }
            }
            listener.newConnectionHandler = { [weak self] connection in
                guard let self, self.generation == token else { connection.cancel(); return }
                guard self.pairs.count < self.maxPairs else {
                    connection.cancel(); self.snapshot.rejected += 1; self.emit(); return
                }
                let id = UUID()
                // No requiredInterfaceType or local binding on outbound connections:
                // system VPN routes select the path to the fixed upstream.
                let upstream = NWConnection(host: .ipv4(config.upstream), port: config.upstreamPort, using: bridgeTCPParameters())
                let pair = BridgePair(client: connection, upstream: upstream, queue: self.queue,
                                      connectTimeout: self.connectTimeout, sendTimeout: self.sendTimeout,
                                      onBytes: { [weak self] count in
                    guard let self, self.generation == token else { return }
                    self.snapshot.forwardedBytes += UInt64(count); self.emit()
                }, onBuffer: { [weak self] delta in
                    guard let self, self.generation == token else { return }
                    self.snapshot.bufferedBytes += delta
                    self.snapshot.peakBufferedBytes = max(self.snapshot.peakBufferedBytes, self.snapshot.bufferedBytes)
                    self.emit()
                }, onClose: { [weak self] error in
                    guard let self, self.generation == token else { return }
                    self.pairs.removeValue(forKey: id); self.snapshot.active = self.pairs.count
                    if let error { self.snapshot.errors += 1; self.snapshot.status = "Pair closed: \(error)" }
                    self.emit()
                })
                self.pairs[id] = pair; self.snapshot.accepted += 1; self.snapshot.active = self.pairs.count
                self.emit(); pair.start()
            }
            emit(); listener.start(queue: queue)
        } catch { snapshot.errors += 1; stopOnQueue("Listener failed: \(error.localizedDescription)") }
    }
}

private final class BridgePair {
    private let connections: [NWConnection]
    private let queue: DispatchQueue
    private let connectTimeout: Double, sendTimeout: Double
    private let onBytes: (Int) -> Void, onBuffer: (Int) -> Void, onClose: (String?) -> Void
    private var ready = [false, false], finished = [false, false]
    private var buffered = [0, 0]
    private var writes: [DispatchSourceTimer?] = [nil, nil]
    private var connectTimer: DispatchSourceTimer?
    private var terminalDrainTimer: DispatchSourceTimer?
    private var pumping = false, closed = false

    init(client: NWConnection, upstream: NWConnection, queue: DispatchQueue,
         connectTimeout: Double, sendTimeout: Double,
         onBytes: @escaping (Int) -> Void, onBuffer: @escaping (Int) -> Void,
         onClose: @escaping (String?) -> Void) {
        connections = [client, upstream]; self.queue = queue
        self.connectTimeout = connectTimeout; self.sendTimeout = sendTimeout
        self.onBytes = onBytes; self.onBuffer = onBuffer; self.onClose = onClose
    }

    func start() {
        connectTimer = timer(seconds: connectTimeout) { [weak self] in self?.finish("Upstream connection deadline exceeded") }
        for index in 0...1 {
            connections[index].stateUpdateHandler = { [weak self] state in
                guard let self, !self.closed else { return }
                switch state {
                case .ready:
                    self.ready[index] = true
                    if self.ready.allSatisfy({ $0 }) && !self.pumping {
                        self.pumping = true; self.connectTimer?.cancel(); self.connectTimer = nil
                        self.read(0); self.read(1)
                    }
                case .failed(let error):
                    if self.pumping && self.finished[1 - index] && !self.finished[index] {
                        // Network.framework may report a terminal state after
                        // local SHUT_WR while queued receive bytes remain. Do
                        // not turn that state into EOF or discard the response.
                        // The receive callback must confirm EOF/error; drain is bounded.
                        if self.terminalDrainTimer == nil {
                            self.terminalDrainTimer = self.timer(seconds: self.sendTimeout) { [weak self] in
                                self?.finish("Terminal receive drain deadline: \(error)")
                            }
                        }
                    } else { self.finish("\(index == 0 ? "Client" : "Upstream") state: \(error)") }
                case .cancelled: self.finish("Connection cancelled")
                default: break
                }
            }
            connections[index].start(queue: queue)
        }
    }

    func cancel() { finish(nil) }

    private func timer(seconds: Double, action: @escaping () -> Void) -> DispatchSourceTimer {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + seconds); timer.setEventHandler(handler: action); timer.resume()
        return timer
    }

    private func read(_ lane: Int) {
        guard !closed, !finished[lane] else { return }
        connections[lane].receive(minimumIncompleteLength: 1, maximumLength: 16 * 1024) { [weak self] data, _, eof, readError in
            guard let self, !self.closed else { return }
            if let data, !data.isEmpty {
                self.buffered[lane] = data.count; self.onBuffer(data.count)
                self.writes[lane] = self.timer(seconds: self.sendTimeout) { [weak self] in self?.finish("Write backpressure deadline exceeded") }
                self.connections[1 - lane].send(content: data, completion: .contentProcessed { [weak self] error in
                    guard let self, !self.closed else { return }
                    self.writes[lane]?.cancel(); self.writes[lane] = nil
                    self.onBuffer(-self.buffered[lane]); self.buffered[lane] = 0
                    if let error { self.finish("Write: \(error)"); return }
                    self.onBytes(data.count)
                    // A receive can carry final bytes together with an error.
                    // Preserve those bytes before closing, without fabricating EOF.
                    if let readError { self.finish("Read: \(readError)"); return }
                    if eof { self.halfClose(lane) } else { self.read(lane) }
                })
            } else if let readError { self.finish("Read: \(readError)") }
            else if eof { self.halfClose(lane) }
            else { self.read(lane) }
        }
    }

    private func halfClose(_ lane: Int) {
        guard !closed, !finished[lane] else { return }
        writes[lane] = timer(seconds: sendTimeout) { [weak self] in self?.finish("FIN drain deadline exceeded") }
        connections[1 - lane].send(content: nil, contentContext: .finalMessage, isComplete: true,
                                   completion: .contentProcessed { [weak self] error in
            guard let self, !self.closed else { return }
            self.writes[lane]?.cancel(); self.writes[lane] = nil
            if let error { self.finish("FIN: \(error)"); return }
            self.finished[lane] = true
            // A request FIN must not cancel the reverse response stream.
            if self.finished.allSatisfy({ $0 }) { self.finish(nil) }
        })
    }

    private func finish(_ error: String?) {
        guard !closed else { return }; closed = true
        connectTimer?.cancel(); connectTimer = nil
        terminalDrainTimer?.cancel(); terminalDrainTimer = nil
        for lane in 0...1 {
            writes[lane]?.cancel(); writes[lane] = nil
            if buffered[lane] != 0 { onBuffer(-buffered[lane]); buffered[lane] = 0 }
            connections[lane].stateUpdateHandler = nil; connections[lane].cancel()
        }
        onClose(error)
    }
}

final class PhoneBridge: ObservableObject {
    @Published private(set) var running = false
    @Published private(set) var status = "Bridge off · phone VPN/USB path unqualified"
    @Published private(set) var counters = "0 connections · 0 bytes"
    private let server = PhoneBridgeServer()
    private var generation = UUID()
    private let snapshotLock = NSLock()
    private var latestSnapshot: (UUID, PhoneBridgeSnapshot)?
    private var updateScheduled = false

    private func enqueue(_ snapshot: PhoneBridgeSnapshot, token: UUID) {
        snapshotLock.lock()
        latestSnapshot = (token, snapshot)
        let schedule = !updateScheduled
        updateScheduled = true
        snapshotLock.unlock()
        guard schedule else { return }
        // Counter refreshes replace one slot; bulk traffic cannot create an
        // unbounded queue of UI work while the main thread is busy.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            guard let self else { return }
            self.snapshotLock.lock()
            let latest = self.latestSnapshot
            self.latestSnapshot = nil; self.updateScheduled = false
            self.snapshotLock.unlock()
            guard let (token, snapshot) = latest, self.generation == token else { return }
            self.running = snapshot.running; self.status = snapshot.status
            self.counters = "\(snapshot.active)/16 active · \(snapshot.accepted) accepted · \(snapshot.rejected) rejected · \(snapshot.errors) errors · \(snapshot.forwardedBytes) bytes"
        }
    }

    func start(role: PhoneBridgeRole, localTailIP: String, roverPeerIP: String, jetsonUSBIP: String) {
        guard !running else { return }
        do {
            let config = try PhoneBridgeConfig.make(role: role, localTailIP: localTailIP,
                                                  roverPeerIP: roverPeerIP, jetsonUSBIP: jetsonUSBIP)
            let token = UUID(); generation = token; running = true; status = "Starting fixed-upstream bridge"
            server.setSnapshotHandler { [weak self] snapshot in self?.enqueue(snapshot, token: token) }
            server.start(config)
        } catch { status = error.localizedDescription }
    }

    func stop() {
        generation = UUID(); running = false; status = "Bridge stopped"
        counters = "0/16 active · bridge stopped"; server.stop()
    }
}
