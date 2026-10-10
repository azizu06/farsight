import Compression
import Foundation

/// Synthetic mesh inputs through the actual production feed. No ARKit/device API.
@main struct PhoneGeometryFeedFixture {
    static let sid = UUID(uuidString: "123e4567-e89b-12d3-a456-426614174000")!
    static let nextSID = UUID(uuidString: "123e4567-e89b-12d3-a456-426614174001")!
    static let a = UUID(uuidString: "223e4567-e89b-12d3-a456-426614174000")!
    static let b = UUID(uuidString: "223e4567-e89b-12d3-a456-426614174001")!
    static let triangle: [Float] = [0, 0, 0, 1, 0, 0, 0, 1, 0]
    static let faces: [UInt32] = [0, 1, 2]

    final class Records {
        let lock = NSLock()
        var items: [(UUID, Data)] = []
        var blockNext = false
        let entered = DispatchSemaphore(value: 0)
        let resume = DispatchSemaphore(value: 0)
        func receive(_ session: UUID, _ data: Data) {
            lock.lock(); items.append((session, data)); let block = blockNext; blockNext = false; lock.unlock()
            if block {
                entered.signal()
                precondition(resume.wait(timeout: .now() + 5) == .success, "fixture blocked too long")
            }
        }
        func pauseNext() { lock.lock(); blockNext = true; lock.unlock() }
        func copy() -> [(UUID, Data)] { lock.lock(); defer { lock.unlock() }; return items }
    }
    static func u32(_ packet: Data, _ offset: Int) -> UInt32 {
        packet.withUnsafeBytes { UInt32(littleEndian: $0.loadUnaligned(fromByteOffset: offset, as: UInt32.self)) }
    }
    static func u64(_ packet: Data, _ offset: Int) -> UInt64 {
        packet.withUnsafeBytes { UInt64(littleEndian: $0.loadUnaligned(fromByteOffset: offset, as: UInt64.self)) }
    }
    static func body(_ packet: Data) -> Data {
        let rawCount = Int(u32(packet, 116) * 3 + u32(packet, 120)) * 4
        if packet[2] == 0 { return packet.subdata(in: 124..<packet.count) }
        precondition(packet[2] == 2 && packet[3] == 0)
        var output = Data(count: rawCount)
        let length = output.withUnsafeMutableBytes { dst in
            packet.withUnsafeBytes { src in
                compression_decode_buffer(dst.bindMemory(to: UInt8.self).baseAddress!, rawCount,
                    src.bindMemory(to: UInt8.self).baseAddress!.advanced(by: 124), packet.count - 124,
                    nil, COMPRESSION_BROTLI)
            }
        }
        precondition(length == rawCount); return output
    }
    static func expectStatus(_ feed: PhoneGeometryFeed, anchors: Int, vertices: Int, triangles: Int, ready: Bool = true) {
        let status = feed.status()
        precondition(status["ready"] as? Bool == ready)
        precondition(status["anchors"] as? Int == anchors)
        precondition(status["vertices"] as? Int == vertices)
        precondition(status["triangles"] as? Int == triangles)
    }
    static func replace(_ feed: PhoneGeometryFeed, _ anchor: UUID = a, _ points: [Float] = triangle,
                        _ indices: [UInt32] = faces, session: UUID = sid) {
        feed.replace(session: session, anchor: anchor, transform: GeometryWire.identity, vertices: points, indices: indices)
    }
    static func snapshot(_ feed: PhoneGeometryFeed, count: Int) -> [Data] {
        let packets = feed.snapshot()
        precondition(packets.count == count + 2)
        precondition(packets.first![1] == 2 && packets.last![1] == 3)
        precondition(packets.first!.count == 124 && packets.last!.count == 124)
        precondition(packets.first![2] == 0 && packets.last![2] == 0)
        let seq = packets.map { u64($0, 4) }
        precondition(zip(seq, seq.dropFirst()).allSatisfy { $0 < $1 })
        return packets
    }
    static func main() throws {
        let mode = CommandLine.arguments[1]
        let directory = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
        let feed = PhoneGeometryFeed(), records = Records()
        feed.onRecord = { records.receive($0, $1) }
        feed.begin(session: sid)
        expectStatus(feed, anchors: 0, vertices: 0, triangles: 0)
        var result: [String: Any] = ["mode": mode, "software_only": true]
        if mode == "snapshot" {
            replace(feed); expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            let first = snapshot(feed, count: 1), second = snapshot(feed, count: 1)
            precondition(u64(second[0], 4) > u64(first.last!, 4))
            precondition(body(first[1]) == body(second[1]))
            for (index, packet) in (first + second).enumerated() {
                try packet.write(to: directory.appendingPathComponent("packet-\(index).bin"))
            }
            precondition(first[1].subdata(in: 124..<first[1].count) == second[1].subdata(in: 124..<second[1].count))
            result["snapshot_records"] = 6
        } else if mode == "coalescing" {
            records.pauseNext(); replace(feed)
            precondition(records.entered.wait(timeout: .now() + 3) == .success)
            for value in 0..<100 {
                var points = triangle; points[0] = Float(value)
                replace(feed, b, points)
            }
            records.resume.signal(); expectStatus(feed, anchors: 2, vertices: 6, triangles: 2)
            let packets = snapshot(feed, count: 2)
            precondition(records.copy().count == 2, "pending updates did not coalesce")
            let last = packets.first { $0.subdata(in: 36..<52) == records.copy().last!.1.subdata(in: 36..<52) }!
            precondition(Float(bitPattern: u32(body(last), 0)) == 99)
            replace(feed, a, triangle + triangle, [0, 1, 2, 3, 4, 5])
            expectStatus(feed, anchors: 2, vertices: 9, triangles: 3)
            feed.remove(session: sid, anchor: b); expectStatus(feed, anchors: 1, vertices: 6, triangles: 2)
            let removed = records.copy().last!.1
            precondition(removed[1] == 1 && removed.count == 124 && u32(removed, 116) == 0 && u32(removed, 120) == 0)
            try removed.write(to: directory.appendingPathComponent("remove.bin"))
            let seq = records.copy().map { u64($0.1, 4) }
            precondition(zip(seq, seq.dropFirst()).allSatisfy { $0 < $1 })
            result["submitted_updates"] = 100; result["coalesced_records"] = 2
        } else if mode == "lifecycle" {
            replace(feed); expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            let before = records.copy().count
            replace(feed, b, session: nextSID); feed.remove(session: nextSID, anchor: a)
            expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            precondition(records.copy().count == before)
            // Freeze an encoding callback, then supersede its pending changes.
            records.pauseNext(); replace(feed, b)
            precondition(records.entered.wait(timeout: .now() + 3) == .success)
            replace(feed, a, triangle + triangle, [0, 1, 2, 3, 4, 5])
            feed.stop(); feed.begin(session: nextSID)
            replace(feed, a, session: sid) // late old producer callback
            replace(feed, b, session: nextSID)
            records.resume.signal(); expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            precondition(feed.status()["session_id"] as? String == nextSID.uuidString)
            let fresh = snapshot(feed, count: 1)
            precondition(u64(fresh[0], 4) == 2 && u64(fresh[1], 4) == 3 && u64(fresh[2], 4) == 4)
            precondition(records.copy().last!.0 == nextSID && u64(records.copy().last!.1, 4) == 1)
            feed.stop(); expectStatus(feed, anchors: 0, vertices: 0, triangles: 0, ready: false)
            precondition(feed.snapshot().isEmpty && feed.status()["session_id"] is NSNull)
            let stoppedCount = records.copy().count
            replace(feed, session: nextSID); expectStatus(feed, anchors: 0, vertices: 0, triangles: 0, ready: false)
            precondition(records.copy().count == stoppedCount)
            feed.begin(session: sid, epoch: 7)
            let empty = snapshot(feed, count: 0)
            precondition(u64(empty[0], 4) == 1 && u64(empty[0], 12) == 7)
            result["stale_changes_ignored"] = true; result["new_session_sequence_reset"] = true
        } else if mode == "invalid" {
            replace(feed); expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            let before = records.copy().count
            feed.replace(session: sid, anchor: a, transform: [1], vertices: triangle, indices: faces)
            expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            replace(feed, a, [.nan, 0, 0], [0, 0, 0]); expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            replace(feed, a, triangle, [0, 1, 99]); expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            replace(feed, a, triangle, [0, 1]); expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            replace(feed, a, Array(repeating: 1, count: (GeometryWire.maxVertices + 1) * 3))
            expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            replace(feed, a, Array(repeating: 1, count: 2_200_002)) // pending bytes > 8 MiB
            expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            var seed: UInt32 = 13
            let noisy: [Float] = (0..<150_000).map { _ in
                seed = seed &* 1664525 &+ 1013904223
                return Float(bitPattern: 0x3f000000 | (seed & 0x007fffff))
            }
            replace(feed, a, noisy); expectStatus(feed, anchors: 1, vertices: 3, triangles: 1)
            precondition(feed.status()["dropped"] as? Int == 7)
            precondition(records.copy().count == before, "invalid updates changed live cache/output")
            let retained = snapshot(feed, count: 1)
            precondition(u32(retained[1], 116) == 3 && u32(retained[1], 120) == 3)
            result["atomic_rejections"] = 7
        } else if mode == "bounds" {
            // Six valid 60k-vertex anchors cross the retained 300k-vertex cap.
            let large = Array(repeating: Float(1), count: 180_000)
            for _ in 0..<6 {
                replace(feed, UUID(), large)
                _ = feed.status() // drain one anchor; do not let pending coalescing hide it
            }
            expectStatus(feed, anchors: 5, vertices: 300_000, triangles: 5)
            precondition(feed.status()["dropped"] as? Int == 1)
            precondition(records.copy().last!.1[1] == 1)
            _ = snapshot(feed, count: 5)
            feed.begin(session: nextSID)
            for _ in 0..<257 {
                replace(feed, UUID(), session: nextSID); _ = feed.status()
            }
            expectStatus(feed, anchors: 256, vertices: 768, triangles: 256)
            precondition(feed.status()["dropped"] as? Int == 1)
            precondition(records.copy().last!.1[1] == 1)
            _ = snapshot(feed, count: 256)
            feed.begin(session: sid)
            let manyFaces = Array(repeating: UInt32(0), count: 60_000)
            for _ in 0..<6 {
                replace(feed, UUID(), triangle, manyFaces); _ = feed.status()
            }
            expectStatus(feed, anchors: 5, vertices: 15, triangles: 100_000)
            precondition(feed.status()["dropped"] as? Int == 1)
            precondition(records.copy().last!.1[1] == 1)
            _ = snapshot(feed, count: 5)
            // Pin the encoding queue so nine distinct raw changes contend for
            // the 8 MiB pending budget. Eight fit; the ninth is rejected whole.
            feed.begin(session: nextSID)
            records.pauseNext(); replace(feed, session: nextSID)
            precondition(records.entered.wait(timeout: .now() + 3) == .success)
            let beforePending = records.copy().count
            for _ in 0..<9 {
                replace(feed, UUID(), large, manyFaces, session: nextSID)
            }
            records.resume.signal()
            expectStatus(feed, anchors: 5, vertices: 300_000, triangles: 100_000)
            // One admission drop, one seed eviction and three large evictions.
            precondition(feed.status()["dropped"] as? Int == 5)
            let pendingRecords = Array(records.copy().dropFirst(beforePending))
            precondition(pendingRecords.filter { $0.1[1] == 0 }.count == 8)
            precondition(pendingRecords.filter { $0.1[1] == 1 }.count == 4)
            let seq = pendingRecords.map { u64($0.1, 4) }
            precondition(zip(seq, seq.dropFirst()).allSatisfy { $0 < $1 })
            _ = snapshot(feed, count: 5)
            result["vertex_cap"] = 300_000; result["anchor_cap"] = 256
            result["triangle_cap"] = 100_000; result["pending_budget_rejections"] = 1
        } else { fatalError("unknown fixture mode") }
        feed.stop(); _ = feed.status()
        result["passed"] = true
        let output = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        print(String(decoding: output, as: UTF8.self))
    }
}
