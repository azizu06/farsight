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
    var fallbackUpstream: IPv4Address? = nil
    var fallbackPort: NWEndpoint.Port? = nil

    static func make(role: PhoneBridgeRole, localTailIP: String, roverPeerIP: String,
                     jetsonUSBIP: String, fallbackPeerIP: String? = nil) throws -> PhoneBridgeConfig {
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
            let primary = try tail(roverPeerIP)
            let fallback = try fallbackPeerIP.map { try tail($0) }
            guard fallback != primary else { throw BridgeError("Fallback must differ from primary") }
            return PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: primary,
                                     localPort: 8081, upstreamPort: 8081,
                                     fallbackUpstream: fallback, fallbackPort: fallback == nil ? nil : 8081)
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
    options.enableKeepalive = true
    options.keepaliveIdle = 15
    options.keepaliveInterval = 5
    options.keepaliveCount = 3
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
    var primaryActive = 0
    var fallbackActive = 0
    var lastChosen = "none"
    var status = "Bridge off"
}

/// Serialized transport core. Tests use loopback configs; the UI can only create
/// the two validated fixed-upstream configs above. No HTTP/control interpretation.
final class PhoneBridgeServer {
    private let queue = DispatchQueue(label: "farsight.phone-bridge", qos: .userInitiated)
    private var listener: NWListener?
    private var pairs: [UUID: BridgePair] = [:]
    private var snapshot = PhoneBridgeSnapshot()
    private var activePaths: [UUID: BridgePath] = [:]
    private var badUntil: [BridgePath: Double] = [:]
    private var generation = UUID()
    private let maxPairs: Int
    private let connectTimeout: Double
    private let sendTimeout: Double
    private let candidateReadyDelay: (BridgePath) -> Double
    var onSnapshot: ((PhoneBridgeSnapshot) -> Void)?

    func setSnapshotHandler(_ handler: @escaping (PhoneBridgeSnapshot) -> Void) {
        queue.async { [weak self] in self?.onSnapshot = handler }
    }

    init(maxPairs: Int = 16, connectTimeout: Double = 3, sendTimeout: Double = 3,
         candidateReadyDelay: @escaping (BridgePath) -> Double = { _ in 0 }) {
        self.maxPairs = maxPairs; self.connectTimeout = connectTimeout; self.sendTimeout = sendTimeout
        self.candidateReadyDelay = candidateReadyDelay
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
        let old = pairs; pairs.removeAll(); activePaths.removeAll()
        for pair in old.values { pair.cancel() }
        snapshot.running = false; snapshot.ready = false; snapshot.active = 0; snapshot.primaryActive = 0; snapshot.fallbackActive = 0; snapshot.bufferedBytes = 0
        snapshot.status = reason; emit()
    }

    private func countPaths() {
        snapshot.primaryActive = activePaths.values.filter { $0 == .primary }.count
        snapshot.fallbackActive = activePaths.values.filter { $0 == .fallback }.count
    }

    private func startOnQueue(_ config: PhoneBridgeConfig) {
        stopOnQueue("Bridge starting")
        snapshot = PhoneBridgeSnapshot(); snapshot.running = true; snapshot.status = "Binding local listener"
        badUntil.removeAll()
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
                let primary = BridgeTarget(path: .primary, host: config.upstream, port: config.upstreamPort)
                let fallback = config.fallbackUpstream.map { BridgeTarget(path: .fallback, host: $0, port: config.fallbackPort ?? config.upstreamPort) }
                let preferFallback = fallback != nil && (self.badUntil[.primary] ?? 0) > ProcessInfo.processInfo.systemUptime &&
                    (self.badUntil[.fallback] ?? 0) <= ProcessInfo.processInfo.systemUptime
                let targets = preferFallback ? [fallback!, primary] : [primary] + (fallback.map { [$0] } ?? [])
                let pair = BridgePair(client: connection, targets: targets, queue: self.queue,
                                      connectTimeout: self.connectTimeout, sendTimeout: self.sendTimeout,
                                      candidateReadyDelay: self.candidateReadyDelay,
                                      onBad: { [weak self] path in
                    guard let self, self.generation == token else { return }
                    self.badUntil[path] = ProcessInfo.processInfo.systemUptime + 5
                }, onSelected: { [weak self] target in
                    guard let self, self.generation == token else { return }
                    self.activePaths[id] = target.path; self.badUntil[target.path] = nil
                    self.snapshot.lastChosen = "\(target.path.rawValue) \(target.host):\(target.port.rawValue)"
                    self.snapshot.status = "Selected \(self.snapshot.lastChosen); existing sessions do not migrate"
                    self.countPaths(); self.emit()
                }, onBytes: { [weak self] count in
                    guard let self, self.generation == token else { return }
                    self.snapshot.forwardedBytes += UInt64(count); self.emit()
                }, onBuffer: { [weak self] delta in
                    guard let self, self.generation == token else { return }
                    self.snapshot.bufferedBytes += delta
                    self.snapshot.peakBufferedBytes = max(self.snapshot.peakBufferedBytes, self.snapshot.bufferedBytes)
                    self.emit()
                }, onClose: { [weak self] error in
                    guard let self, self.generation == token else { return }
                    self.pairs.removeValue(forKey: id); self.activePaths.removeValue(forKey: id)
                    self.snapshot.active = self.pairs.count; self.countPaths()
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

enum BridgePath: String { case primary, fallback }

private struct BridgeTarget {
    let path: BridgePath
    let host: IPv4Address
    let port: NWEndpoint.Port
}

/// Selects a ready transport before the client is read. Candidates never get
/// request bytes, so a refused/slow connection cannot duplicate a POST or drive.
private final class BridgeUpstreamRace {
    private let targets: [BridgeTarget], queue: DispatchQueue, deadline: Double
    private let onBad: (BridgePath) -> Void
    private let completion: (BridgeTarget?, NWConnection?, String?) -> Void
    private let readyDelay: (BridgePath) -> Double
    private var connections: [Int: NWConnection] = [:]
    private var failures = Set<Int>()
    private var deadlineTimer: DispatchSourceTimer?, staggerTimer: DispatchSourceTimer?
    private var finished = false

    init(targets: [BridgeTarget], queue: DispatchQueue, deadline: Double,
         onBad: @escaping (BridgePath) -> Void,
         readyDelay: @escaping (BridgePath) -> Double,
         completion: @escaping (BridgeTarget?, NWConnection?, String?) -> Void) {
        self.targets = targets; self.queue = queue; self.deadline = deadline
        self.onBad = onBad; self.completion = completion
        self.readyDelay = readyDelay
    }

    private func timer(_ seconds: Double, _ action: @escaping () -> Void) -> DispatchSourceTimer {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + seconds); timer.setEventHandler(handler: action); timer.resume()
        return timer
    }

    func start() {
        deadlineTimer = timer(deadline) { [weak self] in
            guard let self, !self.finished else { return }
            for target in self.targets { self.onBad(target.path) }
            self.fail("Upstream selection deadline exceeded")
        }
        attempt(0)
        if targets.count > 1 {
            staggerTimer = timer(0.15) { [weak self] in self?.attempt(1) }
        }
    }

    private func attempt(_ index: Int) {
        guard !finished, index < targets.count, connections[index] == nil, !failures.contains(index) else { return }
        let target = targets[index]
        let connection = NWConnection(host: .ipv4(target.host), port: target.port, using: bridgeTCPParameters())
        connections[index] = connection
        connection.stateUpdateHandler = { [weak self] state in
            guard let self, !self.finished else { return }
            switch state {
            case .ready:
                // Default zero. The portable fixture delays only readiness
                // delivery, while real loopback sockets prove loser byte counts.
                self.queue.asyncAfter(deadline: .now() + self.readyDelay(target.path)) { [weak self] in
                    self?.choose(index, connection: connection)
                }
            case .failed(let error):
                self.failures.insert(index); self.onBad(target.path)
                connection.stateUpdateHandler = nil; connection.cancel(); self.connections.removeValue(forKey: index)
                if index == 0 && self.targets.count > 1 { self.attempt(1) }
                if self.failures.count == self.targets.count { self.fail("Both upstream paths unavailable: \(error)") }
            default: break
            }
        }
        connection.start(queue: queue)
    }

    private func choose(_ index: Int, connection: NWConnection) {
        guard !finished, connections[index] === connection, !failures.contains(index) else { return }
        finished = true; deadlineTimer?.cancel(); staggerTimer?.cancel()
        if index != 0 { onBad(targets[0].path) }
        connections.removeValue(forKey: index); connection.stateUpdateHandler = nil
        for candidate in connections.values { candidate.stateUpdateHandler = nil; candidate.cancel() }
        connections.removeAll(); completion(targets[index], connection, nil)
    }

    private func fail(_ error: String) {
        guard !finished else { return }; cancel(); completion(nil, nil, error)
    }

    func cancel() {
        finished = true; deadlineTimer?.cancel(); staggerTimer?.cancel()
        for connection in connections.values { connection.stateUpdateHandler = nil; connection.cancel() }
        connections.removeAll()
    }
}

private final class BridgePair {
    private var connections: [NWConnection]
    private let targets: [BridgeTarget]
    private let candidateReadyDelay: (BridgePath) -> Double
    private let onBad: (BridgePath) -> Void, onSelected: (BridgeTarget) -> Void
    private var selector: BridgeUpstreamRace?
    private var selected: BridgeTarget?
    private let queue: DispatchQueue
    private let connectTimeout: Double, sendTimeout: Double
    private let onBytes: (Int) -> Void, onBuffer: (Int) -> Void, onClose: (String?) -> Void
    private var ready = [false, false], finished = [false, false]
    private var buffered = [0, 0]
    private var writes: [DispatchSourceTimer?] = [nil, nil]
    private var connectTimer: DispatchSourceTimer?
    private var terminalDrainTimer: DispatchSourceTimer?
    private var pumping = false, closed = false

    init(client: NWConnection, targets: [BridgeTarget], queue: DispatchQueue,
         connectTimeout: Double, sendTimeout: Double,
         candidateReadyDelay: @escaping (BridgePath) -> Double,
         onBad: @escaping (BridgePath) -> Void, onSelected: @escaping (BridgeTarget) -> Void,
         onBytes: @escaping (Int) -> Void, onBuffer: @escaping (Int) -> Void,
         onClose: @escaping (String?) -> Void) {
        connections = [client]; self.targets = targets; self.queue = queue
        self.onBad = onBad; self.onSelected = onSelected
        self.candidateReadyDelay = candidateReadyDelay
        self.connectTimeout = connectTimeout; self.sendTimeout = sendTimeout
        self.onBytes = onBytes; self.onBuffer = onBuffer; self.onClose = onClose
    }

    func start() {
        connectTimer = timer(seconds: connectTimeout) { [weak self] in
            guard let self else { return }
            if self.ready[0] && !self.ready[1] { for target in self.targets { self.onBad(target.path) } }
            self.finish("Connection deadline exceeded")
        }
        attachState(0)
        connections[0].start(queue: queue)
        let selector = BridgeUpstreamRace(targets: targets, queue: queue, deadline: connectTimeout,
                                          onBad: onBad, readyDelay: candidateReadyDelay) { [weak self] target, connection, error in
            guard let self, !self.closed else { connection?.cancel(); return }
            guard let target, let connection else { self.finish(error ?? "Both upstream paths unavailable"); return }
            self.selected = target; self.connections.append(connection)
            self.attachState(1); self.ready[1] = true
            self.onSelected(target); self.beginPump()
        }
        self.selector = selector; selector.start()
    }

    private func beginPump() {
        guard ready.allSatisfy({ $0 }), !pumping, !closed else { return }
        pumping = true; connectTimer?.cancel(); connectTimer = nil
        read(0); read(1)
    }

    private func attachState(_ index: Int) {
        connections[index].stateUpdateHandler = { [weak self] state in
            guard let self, !self.closed else { return }
            switch state {
            case .ready: self.ready[index] = true; self.beginPump()
            case .failed(let error):
                if self.pumping && self.finished[1 - index] && !self.finished[index] {
                    if self.terminalDrainTimer == nil {
                        self.terminalDrainTimer = self.timer(seconds: self.sendTimeout) { [weak self] in
                            self?.finish("Terminal receive drain deadline: \(error)", upstreamFailed: index == 1)
                        }
                    }
                } else { self.finish("\(index == 0 ? "Client" : "Upstream") state: \(error)", upstreamFailed: index == 1) }
            case .cancelled: self.finish("Connection cancelled", upstreamFailed: index == 1)
            default: break
            }
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
                self.writes[lane] = self.timer(seconds: self.sendTimeout) { [weak self] in self?.finish("Write backpressure deadline exceeded", upstreamFailed: lane == 0) }
                self.connections[1 - lane].send(content: data, completion: .contentProcessed { [weak self] error in
                    guard let self, !self.closed else { return }
                    self.writes[lane]?.cancel(); self.writes[lane] = nil
                    self.onBuffer(-self.buffered[lane]); self.buffered[lane] = 0
                    if let error { self.finish("Write: \(error)", upstreamFailed: lane == 0); return }
                    self.onBytes(data.count)
                    // A receive can carry final bytes together with an error.
                    // Preserve those bytes before closing, without fabricating EOF.
                    if let readError { self.finish("Read: \(readError)", upstreamFailed: lane == 1); return }
                    if eof { self.halfClose(lane) } else { self.read(lane) }
                })
            } else if let readError { self.finish("Read: \(readError)", upstreamFailed: lane == 1) }
            else if eof { self.halfClose(lane) }
            else { self.read(lane) }
        }
    }

    private func halfClose(_ lane: Int) {
        guard !closed, !finished[lane] else { return }
        writes[lane] = timer(seconds: sendTimeout) { [weak self] in self?.finish("FIN drain deadline exceeded", upstreamFailed: lane == 0) }
        connections[1 - lane].send(content: nil, contentContext: .finalMessage, isComplete: true,
                                   completion: .contentProcessed { [weak self] error in
            guard let self, !self.closed else { return }
            self.writes[lane]?.cancel(); self.writes[lane] = nil
            if let error { self.finish("FIN: \(error)", upstreamFailed: lane == 0); return }
            self.finished[lane] = true
            // A request FIN must not cancel the reverse response stream.
            if self.finished.allSatisfy({ $0 }) { self.finish(nil) }
        })
    }

    private func finish(_ error: String?, upstreamFailed: Bool = false) {
        guard !closed else { return }; closed = true
        if upstreamFailed, let selected { onBad(selected.path) }
        selector?.cancel(); selector = nil
        connectTimer?.cancel(); connectTimer = nil
        terminalDrainTimer?.cancel(); terminalDrainTimer = nil
        for lane in 0...1 {
            writes[lane]?.cancel(); writes[lane] = nil
            if buffered[lane] != 0 { onBuffer(-buffered[lane]); buffered[lane] = 0 }
            if lane < connections.count { connections[lane].stateUpdateHandler = nil; connections[lane].cancel() }
        }
        onClose(error)
    }
}

/// In-memory operator intent only. Suspension never grants camera or motion intent.
struct BridgeForegroundIntent {
    private(set) var operatorWanted = false
    mutating func explicitStart(role: PhoneBridgeRole) { operatorWanted = role == .operator }
    mutating func manualStopOrRoleChange() { operatorWanted = false }
    func shouldRearmOnActive(role: PhoneBridgeRole) -> Bool { operatorWanted && role == .operator }
}

final class PhoneBridge: ObservableObject {
    @Published private(set) var running = false
    @Published private(set) var status = "Bridge off · phone VPN/USB path unqualified"
    @Published private(set) var counters = "0 connections · 0 bytes"
    @Published private(set) var operatorRecoveryWanted = false
    @Published private(set) var listeningPort: UInt16 = 0
    private let server: PhoneBridgeServer
    private let configuration: (PhoneBridgeRole, String, String, String, String?) throws -> PhoneBridgeConfig
    private var generation = UUID()
    private let snapshotLock = NSLock()
    private var latestSnapshot: (UUID, PhoneBridgeSnapshot)?
    private var updateScheduled = false
    private var foregroundIntent = BridgeForegroundIntent()
    private var operatorConfig: PhoneBridgeConfig?

    init(server: PhoneBridgeServer = PhoneBridgeServer(),
         configuration: @escaping (PhoneBridgeRole, String, String, String, String?) throws -> PhoneBridgeConfig = {
        try PhoneBridgeConfig.make(role: $0, localTailIP: $1, roverPeerIP: $2, jetsonUSBIP: $3, fallbackPeerIP: $4)
    }) {
        self.server = server; self.configuration = configuration
    }

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
            self.listeningPort = snapshot.ready ? snapshot.port : 0
            self.counters = "\(snapshot.active)/16 active (primary \(snapshot.primaryActive), fallback \(snapshot.fallbackActive), connecting \(snapshot.active - snapshot.primaryActive - snapshot.fallbackActive)) · \(snapshot.errors) errors · \(snapshot.forwardedBytes) bytes · last \(snapshot.lastChosen)"
        }
    }

    func start(role: PhoneBridgeRole, localTailIP: String, roverPeerIP: String, jetsonUSBIP: String,
               fallbackPeerIP: String? = nil) {
        guard !running else { return }
        do {
            let config = try configuration(role, localTailIP, roverPeerIP, jetsonUSBIP, fallbackPeerIP)
            foregroundIntent.explicitStart(role: role)
            operatorRecoveryWanted = foregroundIntent.operatorWanted
            operatorConfig = role == .operator ? config : nil
            startConfigured(config)
        } catch { status = error.localizedDescription }
    }

    func stop() {
        foregroundIntent.manualStopOrRoleChange(); operatorRecoveryWanted = false; operatorConfig = nil
        closeTransport("Bridge stopped")
    }

    private func startConfigured(_ config: PhoneBridgeConfig) {
        let token = UUID(); generation = token; running = true; status = "Starting fixed-upstream bridge"
        server.setSnapshotHandler { [weak self] snapshot in self?.enqueue(snapshot, token: token) }
        server.start(config)
    }

    private func closeTransport(_ reason: String) {
        generation = UUID(); running = false; status = reason
        listeningPort = 0
        counters = "0/16 active · bridge stopped"; server.stop()
    }

    func suspendForBackground() {
        closeTransport(operatorRecoveryWanted ? "Operator suspended; foreground re-arms listener only" : "Bridge stopped on background")
    }

    func recoverOperatorOnActive(role: PhoneBridgeRole) {
        guard !running, foregroundIntent.shouldRearmOnActive(role: role), let operatorConfig else { return }
        startConfigured(operatorConfig)
    }
}
