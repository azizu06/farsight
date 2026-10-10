import Foundation

/// Compression/cache work stays off the camera queue. Pending changes coalesce
/// by anchor; both pending raw input and retained compressed output are bounded.
final class PhoneGeometryFeed {
    private struct Change {
        let token: UUID
        let session: UUID
        let anchor: UUID
        let transform: [Float]
        let vertices: [Float]
        let indices: [UInt32]
        let remove: Bool
        var bytes: Int { (vertices.count + indices.count) * 4 + 64 }
    }
    private struct Stored { let packet: Data; let vertices: Int; let triangles: Int }
    private let queue = DispatchQueue(label: "farsight.geometry.encode", qos: .utility)
    private let lock = NSLock()
    private var wantedEpoch: UInt64 = 1
    private var token = UUID(), wanted: UUID?, pending: [UUID: Change] = [:]
    private var pendingBytes = 0, scheduled = false
    private var session: UUID?, epoch: UInt64 = 1, sequence: UInt64 = 0
    private var cache: [UUID: Stored] = [:], order: [UUID] = []
    private var vertices = 0, triangles = 0, retainedBytes = 0, dropped = 0
    var onRecord: ((UUID, Data) -> Void)?
    private let byteBudget = 8 * 1024 * 1024
    private let vertexBudget = 300_000, triangleBudget = 100_000, anchorBudget = 256

    func begin(session: UUID, epoch: UInt64 = 1) {
        lock.lock()
        if wanted == session && wantedEpoch == epoch { lock.unlock(); return }
        wantedEpoch = epoch
        let id = UUID(); token = id; wanted = session; pending.removeAll(); pendingBytes = 0; scheduled = false
        lock.unlock()
        queue.async { [weak self] in
            guard let self, self.isCurrent(id) else { return }
            self.session = session; self.epoch = epoch; self.sequence = 0
            self.cache.removeAll(); self.order.removeAll()
            self.vertices = 0; self.triangles = 0; self.retainedBytes = 0; self.dropped = 0
        }
    }
    func stop() {
        lock.lock(); let id = UUID(); token = id; wanted = nil; pending.removeAll(); pendingBytes = 0; scheduled = false; lock.unlock()
        queue.async { [weak self] in
            guard let self, self.matches(id) else { return }
            self.session = nil; self.cache.removeAll(); self.order.removeAll()
            self.vertices = 0; self.triangles = 0; self.retainedBytes = 0
        }
    }
    func replace(session: UUID, anchor: UUID, transform: [Float], vertices: [Float], indices: [UInt32]) {
        submit(session: session, anchor: anchor, transform: transform, vertices: vertices, indices: indices, remove: false)
    }
    func reject(session: UUID) {
        lock.lock(); let id = token, valid = wanted == session; lock.unlock()
        if valid { queue.async { [weak self] in if let self, self.isCurrent(id) { self.dropped += 1 } } }
    }
    func remove(session: UUID, anchor: UUID) {
        submit(session: session, anchor: anchor, transform: GeometryWire.identity, vertices: [], indices: [], remove: true)
    }
    private func submit(session: UUID, anchor: UUID, transform: [Float], vertices: [Float], indices: [UInt32], remove: Bool) {
        lock.lock()
        guard wanted == session else { lock.unlock(); return }
        let change = Change(token: token, session: session, anchor: anchor, transform: transform,
                            vertices: vertices, indices: indices, remove: remove)
        let oldBytes = pending[anchor]?.bytes ?? 0
        guard change.bytes <= byteBudget, pendingBytes - oldBytes + change.bytes <= byteBudget,
              pending[anchor] != nil || pending.count < anchorBudget else {
            let id = token; lock.unlock()
            queue.async { [weak self] in if let self, self.isCurrent(id) { self.dropped += 1 } }; return
        }
        pendingBytes += change.bytes - oldBytes; pending[anchor] = change
        let start = !scheduled; scheduled = true; let id = token
        lock.unlock()
        if start { queue.async { [weak self] in self?.drain(id) } }
    }
    private func matches(_ id: UUID) -> Bool { lock.lock(); defer { lock.unlock() }; return token == id }
    private func isCurrent(_ id: UUID) -> Bool { lock.lock(); defer { lock.unlock() }; return token == id && wanted != nil }
    private func drain(_ id: UUID) {
        while true {
            lock.lock()
            guard token == id else { lock.unlock(); return }
            guard let entry = pending.first else { scheduled = false; lock.unlock(); return }
            pending.removeValue(forKey: entry.key); pendingBytes -= entry.value.bytes
            lock.unlock()
            let change = entry.value
            guard isCurrent(id), session == change.session else { continue }
            do {
                sequence += 1
                let packet = try GeometryWire.encode(session: change.session, epoch: epoch, sequence: sequence,
                    operation: change.remove ? .remove : .replace, anchor: change.anchor,
                    transform: change.transform, vertices: change.vertices, indices: change.indices)
                guard isCurrent(id) else { continue }
                discard(change.anchor)
                if !change.remove {
                    let count = change.vertices.count / 3, faces = change.indices.count / 3
                    cache[change.anchor] = Stored(packet: packet, vertices: count, triangles: faces)
                    order.append(change.anchor); vertices += count; triangles += faces; retainedBytes += packet.count
                }
                onRecord?(change.session, packet)
                while vertices > vertexBudget || triangles > triangleBudget || retainedBytes > byteBudget || cache.count > anchorBudget {
                    guard let oldest = order.first else { break }
                    discard(oldest); dropped += 1; sequence += 1
                    let remove = try GeometryWire.encode(session: change.session, epoch: epoch, sequence: sequence,
                        operation: .remove, anchor: oldest)
                    onRecord?(change.session, remove)
                }
            } catch { if isCurrent(id) { dropped += 1 } }
        }
    }
    private func discard(_ anchor: UUID) {
        guard let old = cache.removeValue(forKey: anchor) else { return }
        vertices -= old.vertices; triangles -= old.triangles; retainedBytes -= old.packet.count
        order.removeAll { $0 == anchor }
    }
    func snapshot() -> [Data] {
        queue.sync {
            lock.lock(); let active = wanted; lock.unlock()
            guard let session, active == session else { return [] }
            do {
                sequence += 1
                var result = [try GeometryWire.encode(session: session, epoch: epoch, sequence: sequence, operation: .reset)]
                for anchor in order {
                    guard let stored = cache[anchor] else { continue }
                    sequence += 1
                    var packet = stored.packet
                    var little = sequence.littleEndian
                    withUnsafeBytes(of: &little) { packet.replaceSubrange(4..<12, with: $0) }
                    result.append(packet)
                }
                sequence += 1
                result.append(try GeometryWire.encode(session: session, epoch: epoch, sequence: sequence, operation: .snapshotEnd))
                return result
            } catch { return [] }
        }
    }
    func status() -> [String: Any] {
        queue.sync {
            lock.lock(); let active = wanted; lock.unlock()
            let current = active != nil && active == session
            return ["geometry": true, "protocol": 1, "source": "IPHONE ARKIT MESH", "ready": current,
                    "session_id": current ? session!.uuidString as Any : NSNull(),
                    "map_epoch": current ? epoch as Any : NSNull(), "anchors": current ? cache.count : 0,
                    "vertices": current ? vertices : 0, "triangles": current ? triangles : 0, "dropped": dropped]
        }
    }
}
