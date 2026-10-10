import Compression
import Foundation

/// Matches farsight-geometry v1. One WS message contains one entire record.
/// Geometry has its own transport; these packets never enter the control lane.
enum GeometryWire {
    enum Operation: UInt8 { case replace = 0, remove = 1, reset = 2, snapshotEnd = 3 }
    enum Failure: Error { case invalidGeometry, compressionFailed, updateTooLarge }
    static let maxPacketBytes = 256 * 1024
    static let maxVertices = 64 * 1024
    static let maxIndices = 64 * 1024
    static let identity: [Float] = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]

    static func encode(session: UUID, epoch: UInt64, sequence: UInt64,
                       operation: Operation, anchor: UUID? = nil,
                       transform: [Float] = identity, vertices: [Float] = [],
                       indices: [UInt32] = [], compress: Bool = true) throws -> Data {
        guard epoch > 0, sequence > 0, transform.count == 16,
              transform.allSatisfy({ $0.isFinite }), vertices.count % 3 == 0,
              vertices.count / 3 <= maxVertices, indices.count <= maxIndices else {
            throw Failure.invalidGeometry
        }
        let count = vertices.count / 3
        if operation == .replace {
            guard anchor != nil, count > 0, !indices.isEmpty, indices.count % 3 == 0,
                  vertices.allSatisfy({ $0.isFinite }),
                  indices.allSatisfy({ Int($0) < count }) else { throw Failure.invalidGeometry }
        } else if !vertices.isEmpty || !indices.isEmpty || (operation == .remove && anchor == nil) {
            throw Failure.invalidGeometry
        }
        var raw = Data(capacity: vertices.count * 4 + indices.count * 4)
        for coordinate in vertices { append(coordinate.bitPattern, to: &raw) }
        for index in indices { append(index, to: &raw) }
        var flags: UInt16 = 0
        let payload: Data
        if compress && !raw.isEmpty {
            // Apple exposes a fixed level-2 Brotli encoder on iOS 15+.
            var encoded = Data(count: raw.count + 64 * 1024)
            let capacity = encoded.count
            let length = encoded.withUnsafeMutableBytes { destination in
                raw.withUnsafeBytes { source in
                    compression_encode_buffer(destination.bindMemory(to: UInt8.self).baseAddress!, capacity,
                                              source.bindMemory(to: UInt8.self).baseAddress!, raw.count,
                                              nil, COMPRESSION_BROTLI)
                }
            }
            guard length > 0 else { throw Failure.compressionFailed }
            encoded.count = length; payload = encoded; flags = 2
        } else { payload = raw }
        guard 124 + payload.count <= maxPacketBytes else { throw Failure.updateTooLarge }
        var packet = Data(capacity: 124 + payload.count)
        packet.append(1); packet.append(operation.rawValue); append(flags, to: &packet)
        append(sequence, to: &packet); append(epoch, to: &packet)
        append(session, to: &packet)
        append(anchor ?? UUID(uuid: (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)), to: &packet)
        for value in transform { append(value.bitPattern, to: &packet) }
        append(UInt32(count), to: &packet); append(UInt32(indices.count), to: &packet)
        packet.append(payload)
        return packet
    }

    private static func append<T: FixedWidthInteger>(_ value: T, to data: inout Data) {
        var little = value.littleEndian
        withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
    }
    private static func append(_ value: UUID, to data: inout Data) {
        var bytes = value.uuid
        withUnsafeBytes(of: &bytes) { data.append(contentsOf: $0) }
    }
}
