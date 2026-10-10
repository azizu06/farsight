import Foundation
import Network

// Synthetic bounded anchor input; actual production native codec/listener and
// optional operator PhoneBridge hop. All addresses are injected loopback only.
@main struct NativeGeometrySourceFixture {
    static func main() throws {
        let throughOperator = CommandLine.arguments.contains("--through-operator")
        let session = UUID(uuidString: "123e4567-e89b-12d3-a456-426614174000")!
        let anchor = UUID(uuidString: "223e4567-e89b-12d3-a456-426614174000")!
        let feed = PhoneGeometryFeed()
        let server = PhoneGeometryServer(status: { feed.status() }, subscribe: { feed.snapshot() }, bytesPerSecond: 0)
        feed.onRecord = { [weak server] _, packet in server?.publish(packet) }
        feed.begin(session: session)
        var transform = GeometryWire.identity; transform[12] = 1; transform[13] = 2; transform[14] = 3
        feed.replace(session: session, anchor: anchor, transform: transform,
                     vertices: [0, 0, 0, 1, 0, 0, 0, 1, 0], indices: [0, 1, 2])
        var sourcePort: UInt16 = 0, dummyPort: UInt16 = 0
        server.onState = { state, port in
            if state == "ready" { DispatchQueue.main.async { sourcePort = port } }
        }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(IPv4Address("127.0.0.1")!), port: .any)
        let dummy = try NWListener(using: parameters)
        dummy.stateUpdateHandler = { state in
            if case .ready = state { DispatchQueue.main.async { dummyPort = dummy.port?.rawValue ?? 0 } }
        }
        // No control client is created; any unexpected dummy connection closes.
        dummy.newConnectionHandler = { $0.cancel() }
        dummy.start(queue: DispatchQueue(label: "geometry.fixture.dummy-control"))
        server.start(local: IPv4Address("127.0.0.1")!, port: .any)
        func wait(_ condition: () -> Bool) {
            let deadline = Date().addingTimeInterval(8)
            while !condition() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
            precondition(condition(), "native fixture listener did not become ready")
        }
        wait { sourcePort > 0 && dummyPort > 0 }
        var bridge: PhoneBridge?
        let outputPort: UInt16
        if throughOperator {
            let target = sourcePort, controlTarget = dummyPort
            let operatorBridge = PhoneBridge(geometryConfiguration: { _ in
                PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                                  localPort: .any, upstreamPort: NWEndpoint.Port(rawValue: target)!)
            }, configuration: { _, _, _, _, _ in
                PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                                  localPort: .any, upstreamPort: NWEndpoint.Port(rawValue: controlTarget)!)
            })
            bridge = operatorBridge
            operatorBridge.start(role: .operator, localTailIP: "", roverPeerIP: "", jetsonUSBIP: "", geometry: true)
            wait { operatorBridge.geometryListeningPort > 0 && operatorBridge.listeningPort > 0 }
            outputPort = operatorBridge.geometryListeningPort
            precondition(Set([sourcePort, dummyPort, outputPort, operatorBridge.listeningPort]).count == 4,
                         "fixture geometry/control listeners must use separate ephemeral ports")
        } else { outputPort = sourcePort }
        print("PORT=\(outputPort)")
        print("ROUTE=\(throughOperator ? "operator-forwarded" : "direct") SOURCE_PORT=\(sourcePort)")
        fflush(stdout)
        RunLoop.main.run(until: Date().addingTimeInterval(60))
        bridge?.stop(); server.stop(); feed.stop(); dummy.stateUpdateHandler = nil; dummy.cancel()
    }
}
