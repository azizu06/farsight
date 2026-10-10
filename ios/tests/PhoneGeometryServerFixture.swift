import Foundation
import Network

@main struct Fixture {
    static func main() throws {
        let args = CommandLine.arguments
        let rate = args.count > 1 ? Double(args[1])! : 0
        let bound = args.count > 2 ? Int(args[2])! : 8 * 1024 * 1024
        let timeout = args.count > 3 ? Double(args[3])! : 8
        let session = UUID(uuidString: "123e4567-e89b-12d3-a456-426614174000")!
        let anchor = UUID(uuidString: "223e4567-e89b-12d3-a456-426614174000")!
        func packet(_ sequence: UInt64, _ op: GeometryWire.Operation, large: Bool = false) -> Data {
            let xyz: [Float] = large ? (0..<3000).flatMap { i in [Float(i), Float(i%20), 0] } : [0,0,0,1,0,0,0,1,0]
            return try! GeometryWire.encode(session: session, epoch: 1, sequence: sequence, operation: op,
                anchor: op == .replace || op == .remove ? anchor : nil,
                vertices: op == .replace ? xyz : [], indices: op == .replace ? [0,1,2] : [], compress: false)
        }
        let queue = DispatchQueue(label: "geometry.fixture")
        let server = PhoneGeometryServer(status: {
            ["geometry": true, "protocol": 1, "source": "IPHONE ARKIT MESH", "ready": true,
             "session_id": session.uuidString, "map_epoch": 1, "anchors": 1,
             "vertices": 3, "triangles": 1, "dropped": 0]
        }, subscribe: { [packet(1,.reset), packet(2,.replace), packet(3,.snapshotEnd)] },
            queue: queue, bytesPerSecond: rate, writeTimeout: timeout, maxPendingBytes: bound)
        server.onState = { state, port in print("STATE \(state) \(port)"); fflush(stdout) }
        server.start(local: IPv4Address("127.0.0.1")!, port: .any)
        while let command = readLine() {
            switch command {
            case "large": server.publish(packet(4,.replace,large: true))
            case "updates":
                server.publish(packet(4,.replace,large: true))
                for seq in UInt64(5)...34 { server.publish(packet(seq,.replace)) }
                server.publish(packet(35,.remove))
            case "snapshot":
                server.publish(packet(100,.reset))
                server.publish(packet(101,.replace))
                server.publish(packet(102,.replace))
                server.publish(packet(103,.snapshotEnd))
            case "overflow":
                // Each reset is a barrier, not a droppable anchor update.
                for seq in UInt64(40)...200 { server.publish(packet(seq,.snapshotEnd)) }
            case "stop": server.stop()
            case "restart": server.start(local: IPv4Address("127.0.0.1")!, port: .any)
            case "quit": server.stop(); queue.sync {}; return
            default: break
            }
        }
        server.stop(); queue.sync {}
    }
}
