import Foundation
import Network
import CryptoKit

/// Geometry-only listener. Callbacks run on `queue`; supply a serial source queue
/// or immutable synchronized snapshots. Never synchronously dispatch back to it.
final class PhoneGeometryServer {
    var onState: ((String, UInt16) -> Void)?
    var onStateTagged: ((UUID, String, UInt16) -> Void)?
    private var listenerContext: UUID?
    private let queue: DispatchQueue
    private let status: () -> [String: Any]
    private let subscribe: () -> [Data]
    private let rate: Double
    private let timeout: Double
    private let maxPending: Int
    private let maxClients: Int
    private var listener: NWListener?
    private var generation = UUID()
    private var clients: [UUID: GeometryClient] = [:]
    private var nextAdmission = 0.0

    init(status: @escaping () -> [String: Any], subscribe: @escaping () -> [Data],
         queue: DispatchQueue = DispatchQueue(label: "farsight.geometry"),
         bytesPerSecond: Double = 102400, writeTimeout: Double = 8,
         maxPendingBytes: Int = 8 * 1024 * 1024, maxClients: Int = 2) {
        precondition(bytesPerSecond.isFinite && bytesPerSecond >= 0)
        precondition(writeTimeout.isFinite && writeTimeout > 0)
        precondition(maxPendingBytes >= 124 && maxClients > 0 && maxClients <= 2)
        self.status = status; self.subscribe = subscribe; self.queue = queue
        rate = bytesPerSecond; timeout = writeTimeout
        maxPending = maxPendingBytes; self.maxClients = maxClients
    }

    deinit {
        listener?.stateUpdateHandler = nil; listener?.newConnectionHandler = nil
        listener?.cancel()
        for client in clients.values { client.connection.cancel() }
    }

    func start(local: IPv4Address, port: NWEndpoint.Port = 8083, context: UUID? = nil) {
        queue.async { [weak self] in self?.startOnQueue(local, port, context) }
    }
    func stop() { queue.async { [weak self] in self?.stopOnQueue() } }
    func publish(_ packet: Data) {
        queue.async { [weak self] in
            guard let self, self.listener != nil else { return }
            guard GeometryRecord(packet) != nil else {
                for client in Array(self.clients.values) where client.upgraded { self.close(client) }
                return
            }
            for client in Array(self.clients.values) where client.upgraded {
                self.enqueue(packet, client, coalesce: true)
            }
        }
    }

    private func emit(_ state: String, _ port: UInt16) {
        onState?(state, port)
        if let context = listenerContext { onStateTagged?(context, state, port) }
    }
    private func stopOnQueue() {
        generation = UUID()
        listener?.stateUpdateHandler = nil; listener?.newConnectionHandler = nil
        listener?.cancel(); listener = nil
        for client in clients.values { client.connection.cancel() }
        clients.removeAll(); nextAdmission = 0
        emit("stopped", 0)
        listenerContext = nil
    }
    private func startOnQueue(_ local: IPv4Address, _ port: NWEndpoint.Port, _ context: UUID?) {
        stopOnQueue()
        listenerContext = context
        let token = generation
        do {
            let tcp = NWProtocolTCP.Options(); tcp.noDelay = true
            let parameters = NWParameters(tls: nil, tcp: tcp)
            parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(local), port: port)
            let current = try NWListener(using: parameters)
            listener = current
            current.stateUpdateHandler = { [weak self] state in
                guard let self, self.generation == token else { return }
                if case .ready = state { self.emit("ready", current.port?.rawValue ?? 0) }
                if case .failed(let error) = state {
                    self.emit("failed: \(error)", 0); self.stopOnQueue()
                }
            }
            current.newConnectionHandler = { [weak self] connection in
                guard let self, self.generation == token, self.clients.count < self.maxClients else {
                    connection.cancel(); return
                }
                let client = GeometryClient(connection, token)
                self.clients[client.id] = client
                connection.stateUpdateHandler = { [weak self, weak client] state in
                    guard let self, let client, self.live(client) else { return }
                    if case .ready = state { self.receive(client) }
                    if case .failed = state { self.close(client) }
                    if case .cancelled = state { self.close(client) }
                }
                connection.start(queue: self.queue)
                self.queue.asyncAfter(deadline: .now() + 3) { [weak self, weak client] in
                    guard let self, let client, self.live(client), !client.upgraded else { return }
                    self.close(client)
                }
            }
            current.start(queue: queue)
        } catch { emit("failed: \(error)", 0); stopOnQueue() }
    }
    private func live(_ client: GeometryClient) -> Bool {
        generation == client.generation && clients[client.id] === client
    }
    private func close(_ client: GeometryClient) {
        guard clients.removeValue(forKey: client.id) != nil else { return }
        client.connection.stateUpdateHandler = nil
        client.connection.cancel(); client.pending.removeAll(); client.controls.removeAll()
    }
    private func receive(_ client: GeometryClient) {
        guard live(client) else { return }
        client.connection.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self, weak client] data, _, done, error in
            guard let self, let client, self.live(client) else { return }
            if let data { client.incoming.append(data) }
            if client.upgraded { self.parseControls(client) } else { self.parseRequest(client) }
            guard self.live(client) else { return }
            if done || error != nil { self.close(client) } else { self.receive(client) }
        }
    }
    private func parseRequest(_ client: GeometryClient) {
        guard client.incoming.count <= 4096 else { close(client); return }
        guard let end = client.incoming.range(of: Data("\r\n\r\n".utf8)) else { return }
        let request = client.incoming.subdata(in: 0..<end.upperBound)
        guard let text = String(data: request, encoding: .utf8) else { close(client); return }
        let lines = text.components(separatedBy: "\r\n")
        let first = lines[0].split(separator: " ")
        guard first.count == 3, first[0] == "GET", first[2] == "HTTP/1.1" else { close(client); return }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() where !line.isEmpty {
            guard let colon = line.firstIndex(of: ":") else { close(client); return }
            let key = line[..<colon].lowercased()
            guard headers[key] == nil else { close(client); return }
            headers[key] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        client.incoming.removeSubrange(0..<end.upperBound)
        if first[1] == "/geometry/status" {
            let snapshot = status()
            guard JSONSerialization.isValidJSONObject(snapshot),
                  let body = try? JSONSerialization.data(withJSONObject: snapshot), body.count <= 4096 else {
                sendHTTP(client, code: "503 Service Unavailable", body: Data("{}".utf8)); return
            }
            sendHTTP(client, code: "200 OK", body: body); return
        }
        guard first[1] == "/geometry/stream", headers["upgrade"]?.lowercased() == "websocket",
              headers["connection"]?.lowercased().split(separator: ",").contains(where: { $0.trimmingCharacters(in: .whitespaces) == "upgrade" }) == true,
              headers["sec-websocket-version"] == "13", let key = headers["sec-websocket-key"],
              Data(base64Encoded: key)?.count == 16 else {
            sendHTTP(client, code: "400 Bad Request", body: Data("{}".utf8)); return
        }
        let accept = Data(Insecure.SHA1.hash(data: Data((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").utf8))).base64EncodedString()
        let response = Data("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: \(accept)\r\n\r\n".utf8)
        client.writing = true
        send(client, response) { [weak self, weak client] in
            guard let self, let client, self.live(client) else { return }
            client.writing = false; client.upgraded = true
            let initial = self.subscribe()
            // Full snapshots stay ordered; no per-anchor coalescing within them.
            guard initial.reduce(0, { $0 + $1.count }) <= self.maxPending,
                  initial.allSatisfy({ GeometryRecord($0) != nil }) else { self.close(client); return }
            client.pending = initial; client.pendingBytes = initial.reduce(0, { $0 + $1.count })
            client.inSnapshot = initial.last.map { $0[1] != 3 } ?? false
            self.parseControls(client); self.drain(client)
        }
    }
    private func sendHTTP(_ client: GeometryClient, code: String, body: Data) {
        client.writing = true
        var output = Data("HTTP/1.1 \(code)\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n".utf8)
        output.append(body)
        send(client, output) { [weak self, weak client] in
            if let self, let client { self.close(client) }
        }
    }
    private func parseControls(_ client: GeometryClient) {
        while live(client) && client.incoming.count >= 2 {
            let data = client.incoming
            let opcode = data[0] & 15, length = Int(data[1] & 127)
            guard data[0] & 0xF0 == 0x80, [8, 9, 10].contains(opcode),
                  data[1] & 0x80 != 0, length <= 125 else { close(client); return }
            guard data.count >= 6 + length else { return }
            var payload = Data(count: length)
            for i in 0..<length { payload[i] = data[6+i] ^ data[2+i%4] }
            client.incoming.removeSubrange(0..<(6+length))
            if opcode == 10 { continue }
            guard opcode != 8 || length != 1 else { close(client); return }
            guard client.controls.count < 4 else { close(client); return }
            client.controls.append(Self.frame(payload, opcode: opcode == 9 ? 10 : 8))
            if opcode == 8 { client.closing = true }
            drain(client)
        }
    }
    private func enqueue(_ packet: Data, _ client: GeometryClient, coalesce: Bool) {
        guard live(client), !client.closing, let record = GeometryRecord(packet) else { return }
        if record.op == 2 {
            client.pending.removeAll(); client.pendingBytes = 0; client.inSnapshot = true
        } else if coalesce && !client.inSnapshot && (record.op == 0 || record.op == 1) {
            // Only updates after the last barrier may replace an unsent anchor.
            let barrier = client.pending.lastIndex(where: { $0[1] >= 2 }).map { $0 + 1 } ?? 0
            if let old = client.pending.indices.dropFirst(barrier).first(where: {
                guard let prior = GeometryRecord(client.pending[$0]) else { return false }
                return prior.identity == record.identity && prior.anchor == record.anchor
            }) {
                guard GeometryRecord(client.pending[old])!.sequence < record.sequence else { return }
                client.pendingBytes -= client.pending[old].count; client.pending.remove(at: old)
            }
        }
        guard client.pendingBytes + packet.count <= maxPending else { close(client); return }
        client.pending.append(packet); client.pendingBytes += packet.count
        if record.op == 3 { client.inSnapshot = false }
        if record.op < 2 && !client.inSnapshot {
            let barrier = client.pending.lastIndex(where: { $0[1] >= 2 }).map { $0 + 1 } ?? 0
            client.pending.replaceSubrange(barrier..<client.pending.count,
                with: client.pending[barrier...].sorted { GeometryRecord($0)!.sequence < GeometryRecord($1)!.sequence })
        }
        drain(client)
    }
    private func drain(_ client: GeometryClient) {
        guard live(client), !client.writing else { return }
        let output: Data
        if !client.controls.isEmpty { output = client.controls.removeFirst() }
        else if !client.pending.isEmpty {
            let packet = client.pending.removeFirst(); client.pendingBytes -= packet.count
            output = Self.frame(packet, opcode: 2)
        } else { return }
        client.writing = true
        send(client, output) { [weak self, weak client] in
            guard let self, let client, self.live(client) else { return }
            client.writing = false
            if output.first == 0x88 { self.close(client) } else { self.drain(client) }
        }
    }
    private func send(_ client: GeometryClient, _ data: Data, done: @escaping () -> Void) {
        let ticket = UUID(); client.writeTicket = ticket
        queue.asyncAfter(deadline: .now() + timeout) { [weak self, weak client] in
            guard let self, let client, self.live(client), client.writeTicket == ticket else { return }
            self.close(client)
        }
        writeChunk(client, data, offset: 0, ticket: ticket, done: done)
    }
    private func writeChunk(_ client: GeometryClient, _ data: Data, offset: Int, ticket: UUID, done: @escaping () -> Void) {
        guard live(client), client.writeTicket == ticket else { return }
        let chunkLimit = rate == 0 ? 16384 : max(1, min(16384, Int(min(16384, rate * 0.025))))
        let end = min(data.count, offset + chunkLimit)
        let chunk = data.subdata(in: offset..<end)
        let now = ProcessInfo.processInfo.systemUptime
        let admission = rate == 0 ? now : max(now, nextAdmission)
        if rate > 0 { nextAdmission = admission + Double(chunk.count) / rate }
        queue.asyncAfter(deadline: .now() + max(0, admission-now)) { [weak self, weak client] in
            guard let self, let client, self.live(client), client.writeTicket == ticket else { return }
            client.connection.send(content: chunk, completion: .contentProcessed { [weak self, weak client] error in
                guard let self, let client, self.live(client), client.writeTicket == ticket else { return }
                if error != nil { self.close(client); return }
                if end < data.count { self.writeChunk(client, data, offset: end, ticket: ticket, done: done) }
                else { client.writeTicket = nil; done() }
            })
        }
    }
    private static func frame(_ payload: Data, opcode: UInt8) -> Data {
        var output = Data([0x80 | opcode])
        if payload.count < 126 { output.append(UInt8(payload.count)) }
        else if payload.count <= 65535 {
            output.append(126); output.append(UInt8(payload.count >> 8)); output.append(UInt8(payload.count & 255))
        } else {
            output.append(127)
            for shift in stride(from: 56, through: 0, by: -8) { output.append(UInt8((UInt64(payload.count) >> shift) & 255)) }
        }
        output.append(payload); return output
    }
}

private final class GeometryClient {
    let id = UUID(), generation: UUID, connection: NWConnection
    var incoming = Data(), pending: [Data] = [], controls: [Data] = []
    var pendingBytes = 0, upgraded = false, writing = false, closing = false, inSnapshot = false
    var writeTicket: UUID?
    init(_ connection: NWConnection, _ generation: UUID) { self.connection = connection; self.generation = generation }
}
private struct GeometryRecord {
    let op: UInt8, sequence: UInt64, identity: Data, anchor: Data
    init?(_ packet: Data) {
        guard packet.count >= 124, packet.count <= 256 * 1024, packet[0] == 1, packet[1] <= 3 else { return nil }
        op = packet[1]
        sequence = (0..<8).reduce(UInt64(0)) { $0 | UInt64(packet[4+$1]) << (8*$1) }
        guard sequence > 0 else { return nil }
        identity = packet.subdata(in: 12..<36); anchor = packet.subdata(in: 36..<52)
    }
}
