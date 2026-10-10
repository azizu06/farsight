import Foundation
import Network

@main
struct PhoneBridgeFixture {
    static func config(_ upstream: UInt16) -> PhoneBridgeConfig {
        PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                          localPort: .any, upstreamPort: NWEndpoint.Port(rawValue: upstream)!)
    }
    static func main() throws {
        let op = try PhoneBridgeConfig.make(role: .operator, localTailIP: "ignored", roverPeerIP: "100.80.2.3", jetsonUSBIP: "ignored")
        require(String(describing: op.local) == "127.0.0.1" && op.localPort.rawValue == 8081 && op.upstreamPort.rawValue == 8081, "operator binding/ports")
        let rover = try PhoneBridgeConfig.make(role: .rover, localTailIP: "100.127.1.2", roverPeerIP: "ignored", jetsonUSBIP: "172.20.10.3")
        require(rover.upstreamPort.rawValue == 8443, "rover fixed upstream port")
        for bad in ["1.2.3.4", "100.128.1.1", "127.0.0.1", "hostname", "100.080.2.3"] {
            do { _ = try PhoneBridgeConfig.make(role: .operator, localTailIP: "", roverPeerIP: bad, jetsonUSBIP: ""); fatalError("unsafe upstream accepted") }
            catch { }
        }
        do { _ = try PhoneBridgeConfig.make(role: .rover, localTailIP: "100.80.2.3", roverPeerIP: "", jetsonUSBIP: "172.20.10.16"); fatalError("outside USB range accepted") }
        catch { }

        let upstream = try EchoAfterFIN(paused: true)
        defer { upstream.stop() }
        until("upstream listener not ready") { upstream.port.get() != 0 }
        let server = PhoneBridgeServer(maxPairs: 1, connectTimeout: 1, sendTimeout: 4)
        let state = Locked(PhoneBridgeSnapshot())
        server.onSnapshot = { snapshot in state.update { $0 = snapshot } }
        server.start(config(upstream.port.get()))
        until("bridge listener not ready") { state.get().ready }
        let first = Client(port: state.get().port)
        defer { first.stop(); server.stop() }
        until("client did not connect") { first.ready.get() && state.get().active == 1 }
        let excess = Client(port: state.get().port)
        defer { excess.stop() }
        until("connection limit not enforced") { state.get().rejected == 1 }
        var payload = Data()
        for i in 0..<262144 { payload.append(UInt8(truncatingIfNeeded: i * 19)) }
        first.send(payload, halfClose: true)
        Thread.sleep(forTimeInterval: 0.3)
        require(upstream.received.get() == 0, "paused upstream unexpectedly read")
        require(state.get().peakBufferedBytes <= 32768, "application buffering exceeded one16KiB per lane")
        upstream.resume()
        until("half-close response did not drain") { first.ended.get() }
        var expected = Data("RESP\0".utf8); expected.append(payload)
        require(first.bytes.get() == expected, "opaque bytes corrupted: got=\(first.bytes.get().count) expected=\(expected.count) upstream=\(upstream.received.get()) status=\(state.get().status) forwarded=\(state.get().forwardedBytes) errors=\(state.get().errors)")
        until("completed pair not released") { state.get().active == 0 }
        require(state.get().errors == 0, "healthy half-close raised error")
        require(state.get().forwardedBytes == UInt64(payload.count + expected.count), "byte accounting wrong")

        // Exercise small FIN payloads and multiple buffer boundaries, including
        // a large response that must drain after the peer's terminal state.
        for count in [3, 16393, 1024 * 1024] {
            let client = Client(port: state.get().port)
            until("repeat client not ready") { client.ready.get() && state.get().active == 1 }
            let request = Data(repeating: UInt8(count % 251), count: count)
            client.send(request, halfClose: true)
            until("repeat half-close failed") { client.ended.get() }
            var response = Data("RESP\0".utf8); response.append(request)
            require(client.bytes.get() == response, "repeat response truncated at \(count) bytes")
            until("repeat pair not drained") { state.get().active == 0 }
            require(state.get().errors == 0, "repeat pair errored")
            client.stop()
        }

        // Stall a fresh upstream, put a large write through the real bridge,
        // then stop. Pending reads/writes must not keep the pair alive.
        let stalled = try EchoAfterFIN(paused: true)
        defer { stalled.stop() }
        until("stalled listener not ready") { stalled.port.get() != 0 }
        server.start(config(stalled.port.get()))
        until("second bridge not ready") { state.get().ready && state.get().accepted == 0 }
        let pending = Client(port: state.get().port)
        defer { pending.stop() }
        until("pending client not connected") { pending.ready.get() && state.get().active == 1 }
        pending.send(Data(repeating: 17, count: 16 * 1024 * 1024), halfClose: false)
        Thread.sleep(forTimeInterval: 0.5)
        require(state.get().peakBufferedBytes <= 32768, "slow receiver caused unbounded app buffering")
        require(state.get().forwardedBytes < 16 * 1024 * 1024, "backpressure did not stop forwarding to unread peer")
        server.stop()
        until("stop left a pair alive") { !state.get().running && state.get().active == 0 && state.get().bufferedBytes == 0 }
        until("stop did not close client") { pending.ended.get() }
        print("PASS: fixed IP policy, opaque integrity, delayed-reader backpressure, half-close reverse drain, limit, stop")
    }
}
