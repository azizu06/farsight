import Foundation

@main struct GeometryWireFixture {
    static func main() throws {
        let session = UUID(uuidString: "123e4567-e89b-12d3-a456-426614174000")!
        let anchor = UUID(uuidString: "223e4567-e89b-12d3-a456-426614174000")!
        let vertices: [Float] = [0, 0, 0, 1, 0, 0, 0, 1, 0]
        let indices: [UInt32] = [0, 1, 2]
        var transform = GeometryWire.identity; transform[12] = 1; transform[13] = 2; transform[14] = 3
        let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        for compress in [false, true] {
            let record = try GeometryWire.encode(session: session, epoch: 2, sequence: 44,
                operation: .replace, anchor: anchor, transform: transform,
                vertices: vertices, indices: indices, compress: compress)
            try record.write(to: directory.appendingPathComponent(compress ? "br.bin" : "raw.bin"))
        }
        let reset = try GeometryWire.encode(session: session, epoch: 2, sequence: 45, operation: .reset)
        precondition(reset.count == 124)
        try reset.write(to: directory.appendingPathComponent("reset.bin"))
        for invalid in [[UInt32(3), 1, 2], [UInt32(0), 1]] {
            do {
                _ = try GeometryWire.encode(session: session, epoch: 2, sequence: 46, operation: .replace,
                    anchor: anchor, vertices: vertices, indices: invalid)
                fatalError("invalid topology encoded")
            } catch GeometryWire.Failure.invalidGeometry { }
        }
        do {
            _ = try GeometryWire.encode(session: session, epoch: 2, sequence: 46, operation: .replace,
                anchor: anchor, vertices: [.nan, 0, 0], indices: [0, 0, 0])
            fatalError("nonfinite vertex encoded")
        } catch GeometryWire.Failure.invalidGeometry { }
        do {
            _ = try GeometryWire.encode(session: session, epoch: 2, sequence: 46, operation: .replace,
                anchor: anchor, vertices: Array(repeating: 1, count: 90_000), indices: indices, compress: false)
            fatalError("oversize record encoded")
        } catch GeometryWire.Failure.updateTooLarge { }
        print("PASS native geometry wire: Brotli/raw, bounds and invalid topology")
    }
}
