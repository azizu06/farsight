import Foundation
import Network

// Synthetic bounded anchor input; actual production native codec/listener.
@main struct NativeGeometrySourceFixture {
    static func main() {
        let session = UUID(uuidString: "123e4567-e89b-12d3-a456-426614174000")!
        let anchor = UUID(uuidString: "223e4567-e89b-12d3-a456-426614174000")!
        let feed = PhoneGeometryFeed()
        let server = PhoneGeometryServer(status: { feed.status() }, subscribe: { feed.snapshot() }, bytesPerSecond: 0)
        feed.onRecord = { _, packet in server.publish(packet) }
        feed.begin(session: session)
        var transform = GeometryWire.identity; transform[12] = 1; transform[13] = 2; transform[14] = 3
        feed.replace(session: session, anchor: anchor, transform: transform,
                     vertices: [0, 0, 0, 1, 0, 0, 0, 1, 0], indices: [0, 1, 2])
        server.onState = { state, port in
            if state == "ready" { print("PORT=\(port)"); fflush(stdout) }
        }
        server.start(local: IPv4Address("127.0.0.1")!, port: .any)
        RunLoop.main.run(until: Date().addingTimeInterval(60))
        server.stop()
    }
}
