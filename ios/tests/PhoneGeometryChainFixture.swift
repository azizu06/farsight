import Foundation
import Network

private func pumpUntil(_ message: String, timeout: Double = 6, _ predicate: () -> Bool) {
    let deadline = Date().addingTimeInterval(timeout)
    while !predicate() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    require(predicate(), message)
}
private func little(_ data: Data, _ offset: Int, _ count: Int) -> UInt64 {
    (0..<count).reduce(UInt64(0)) { $0 | UInt64(data[offset+$1]) << (8*$1) }
}
private func websocketRecords(_ data: Data) -> [Data]? {
    guard let header = data.range(of: Data("\r\n\r\n".utf8)) else { return nil }
    guard String(data: data.subdata(in: 0..<header.upperBound), encoding: .utf8)?.hasPrefix("HTTP/1.1 101") == true else {
        fatalError("bridge did not forward websocket handshake")
    }
    var position = header.upperBound, packets: [Data] = []
    while packets.count < 3 {
        guard data.count >= position + 2 else { return nil }
        require(data[position] == 0x82 && data[position+1] & 0x80 == 0, "binary WS frame changed")
        var length = Int(data[position+1] & 127); position += 2
        if length == 126 {
            guard data.count >= position + 2 else { return nil }
            length = Int(data[position]) << 8 | Int(data[position+1]); position += 2
        } else if length == 127 {
            guard data.count >= position + 8 else { return nil }
            length = (0..<8).reduce(0) { $0 << 8 | Int(data[position+$1]) }; position += 8
        }
        require(length <= GeometryWire.maxPacketBytes, "unbounded WS record")
        guard data.count >= position + length else { return nil }
        packets.append(data.subdata(in: position..<(position+length))); position += length
    }
    return packets
}
private func subscriber(_ port: UInt16) -> Client {
    let client = Client(port: port)
    until("subscriber socket ready") { client.ready.get() }
    let request = "GET /geometry/stream HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    client.send(Data(request.utf8), halfClose: false)
    until("snapshot did not traverse geometry bridge") { websocketRecords(client.bytes.get()) != nil }
    return client
}

@main struct PhoneGeometryChainFixture {
    static func main() throws {
        let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let session = UUID(uuidString: "123e4567-e89b-12d3-a456-426614174000")!
        let anchor = UUID(uuidString: "223e4567-e89b-12d3-a456-426614174000")!
        let feed = PhoneGeometryFeed()
        let sourcePort = Locked<UInt16>(0)
        let geometry = PhoneGeometryServer(status: { feed.status() }, subscribe: { feed.snapshot() }, bytesPerSecond: 0)
        geometry.onState = { state, port in if state == "ready" { sourcePort.update { $0 = port } } }
        feed.onRecord = { _, packet in geometry.publish(packet) }
        feed.begin(session: session, epoch: 7)
        var transform = GeometryWire.identity; transform[12] = 1; transform[13] = 2; transform[14] = 3
        feed.replace(session: session, anchor: anchor, transform: transform,
                     vertices: [0,0,0,1,0,0,0,1,0], indices: [0,1,2])
        until("fake anchor did not enter compressed cache") { feed.status()["anchors"] as? Int == 1 }
        geometry.start(local: IPv4Address("127.0.0.1")!, port: .any)
        until("geometry source ready") { sourcePort.get() > 0 }
        defer { geometry.stop(); feed.stop() }
        let echo = try EchoAfterFIN(responsePrefix: "CONTROL\0")
        until("control fixture ready") { echo.port.get() > 0 }
        defer { echo.stop() }
        let bridge = PhoneBridge(geometryConfiguration: { _ in
            PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                              localPort: .any, upstreamPort: NWEndpoint.Port(rawValue: sourcePort.get())!)
        }, configuration: { _, _, _, _, _ in
            PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                              localPort: .any, upstreamPort: NWEndpoint.Port(rawValue: echo.port.get())!)
        })
        func startOperator() {
            bridge.start(role: .operator, localTailIP: "", roverPeerIP: "", jetsonUSBIP: "", geometry: true)
            pumpUntil("operator geometry and control listeners ready") { bridge.listeningPort > 0 && bridge.geometryListeningPort > 0 }
            require(bridge.listeningPort != bridge.geometryListeningPort, "geometry shares the control listener")
        }
        func verifySnapshot(_ client: Client, name: String) throws -> UInt64 {
            let packets = websocketRecords(client.bytes.get())!
            require(packets.map { $0[1] } == [2,0,3], "snapshot barriers changed")
            require(packets[0].count == 124 && packets[2].count == 124, "barrier packet length changed")
            let sequences = packets.map { little($0,4,8) }
            require(sequences[0] < sequences[1] && sequences[1] < sequences[2], "snapshot sequence not increasing")
            for packet in packets {
                require(little(packet,12,8) == 7, "capture epoch changed")
                require(packet.subdata(in: 20..<36) == withUnsafeBytes(of: session.uuid) { Data($0) }, "capture session changed")
            }
            require(little(packets[1],2,2) == 2, "native Brotli flag missing")
            require(little(packets[1],116,4) == 3 && little(packets[1],120,4) == 3, "triangle counts changed")
            for (index, packet) in packets.enumerated() {
                try packet.write(to: directory.appendingPathComponent("\(name)-\(index).bin"))
            }
            return sequences[2]
        }
        startOperator()
        let statusClient = Client(port: bridge.geometryListeningPort)
        until("status client ready") { statusClient.ready.get() }
        statusClient.send(Data("GET /geometry/status HTTP/1.1\r\nHost: localhost\r\n\r\n".utf8), halfClose: false)
        until("status reply not forwarded") { statusClient.ended.get() }
        let statusBytes = statusClient.bytes.get()
        let statusEnd = statusBytes.range(of: Data("\r\n\r\n".utf8))!.upperBound
        let status = try JSONSerialization.jsonObject(with: statusBytes.subdata(in: statusEnd..<statusBytes.count)) as! [String: Any]
        require(status["source"] as? String == "IPHONE ARKIT MESH" && status["anchors"] as? Int == 1, "status provenance changed")
        try statusBytes.subdata(in: statusEnd..<statusBytes.count).write(to: directory.appendingPathComponent("status.json"))
        statusClient.stop() // A completed HTTP client must release its TCP half too.
        let first = subscriber(bridge.geometryListeningPort)
        let firstLast = try verifySnapshot(first, name: "first")
        let second = subscriber(bridge.geometryListeningPort)
        let secondLast = try verifySnapshot(second, name: "second")
        require(little(websocketRecords(second.bytes.get())![0],4,8) > firstLast, "new subscriber replayed old snapshot sequence")
        let control = Client(port: bridge.listeningPort)
        until("control client ready") { control.ready.get() }
        control.send(Data("independent request".utf8), halfClose: true)
        until("control after-FIN response lost") { control.ended.get() }
        require(control.bytes.get() == Data("CONTROL\0independent request".utf8), "geometry corrupted control transport")
        let idle = Client(port: bridge.listeningPort)
        until("idle control ready") { idle.ready.get() }
        bridge.stop()
        pumpUntil("manual Stop did not close both lanes") { first.ended.get() && second.ended.get() && idle.ended.get() }
        bridge.recoverOperatorOnActive(role: .operator)
        RunLoop.main.run(until: Date().addingTimeInterval(1.2))
        require(bridge.listeningPort == 0 && bridge.geometryListeningPort == 0 && !bridge.operatorRecoveryWanted, "manual Stop revived transport")
        startOperator()
        let beforeBackground = subscriber(bridge.geometryListeningPort)
        let beforeLast = try verifySnapshot(beforeBackground, name: "before-background")
        require(beforeLast > secondLast, "restart did not issue fresh snapshot")
        let backgroundControl = Client(port: bridge.listeningPort)
        until("background control ready") { backgroundControl.ready.get() }
        bridge.suspendForBackground()
        pumpUntil("background did not close both lanes") { beforeBackground.ended.get() && backgroundControl.ended.get() }
        RunLoop.main.run(until: Date().addingTimeInterval(1.2))
        require(bridge.listeningPort == 0 && bridge.geometryListeningPort == 0, "background revived transport")
        bridge.recoverOperatorOnActive(role: .operator)
        pumpUntil("active operator did not restore both listeners") { bridge.listeningPort > 0 && bridge.geometryListeningPort > 0 }
        let recovered = subscriber(bridge.geometryListeningPort)
        let recoveredLast = try verifySnapshot(recovered, name: "recovered")
        require(little(websocketRecords(recovered.bytes.get())![0],4,8) > beforeLast && recoveredLast > beforeLast, "recovery reused old snapshot")
        require(feed.status()["session_id"] as? String == session.uuidString, "bridge recovery changed capture intent")
        bridge.stop(); pumpUntil("final geometry teardown") { recovered.ended.get() }
        print("PASS joined native geometry chain: Brotli snapshots/status, independent control FIN, Stop/background/active recovery")
    }
}
