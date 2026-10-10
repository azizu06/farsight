import Foundation
import Network

func recoverUntil(_ message: String, _ predicate: () -> Bool) {
    let deadline = Date().addingTimeInterval(6)
    while !predicate() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    require(predicate(), message)
}
func settleRecovery() { RunLoop.main.run(until: Date().addingTimeInterval(1.2)) }

@main struct PhoneBridgeRecoveryFixture {
    static func main() throws {
        let upstream = try EchoAfterFIN(responsePrefix: "FRESH\0")
        defer { upstream.stop() }
        until("upstream ready") { upstream.port.get() != 0 }
        for dual in [false, true] {
            let occupied = try EchoAfterFIN()
            until("occupied listener ready") { occupied.port.get() != 0 }
            let blocked = NWEndpoint.Port(rawValue: occupied.port.get())!
            let bridge = PhoneBridge(configuration: { _, _, _, _, _ in
                var config = PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                    localPort: dual ? .any : blocked, upstreamPort: NWEndpoint.Port(rawValue: upstream.port.get())!,
                    fallbackUpstream: dual ? IPv4Address("127.0.0.1") : nil,
                    fallbackPort: dual ? NWEndpoint.Port(rawValue: upstream.port.get())! : nil)
                config.dualSecondaryPort = blocked
                return config
            })
            bridge.start(role: .operator, localTailIP: "", roverPeerIP: "", jetsonUSBIP: "",
                         fallbackPeerIP: dual ? "" : nil, dualRoutes: dual)
            recoverUntil("bind failure did not schedule retry") { !bridge.running && bridge.status.contains("Re-arming") }
            require(bridge.operatorRecoveryWanted && bridge.listeningPort == 0 && bridge.secondaryListeningPort == 0,
                    "partial listeners survived failure")
            occupied.stop()
            recoverUntil("listener did not recover automatically") {
                bridge.listeningPort != 0 && (!dual || bridge.secondaryListeningPort != 0)
            }
            let client = Client(port: dual ? bridge.secondaryListeningPort : bridge.listeningPort)
            defer { client.stop() }
            until("fresh client ready") { client.ready.get() }
            client.send(Data("new connection".utf8), halfClose: true)
            until("fresh response drained") { client.ended.get() }
            require(client.bytes.get() == Data("FRESH\0new connection".utf8), "recovery corrupted stream")
            bridge.stop(); settleRecovery(); bridge.recoverOperatorOnActive(role: .operator)
            require(!bridge.running && !bridge.operatorRecoveryWanted && bridge.listeningPort == 0,
                    "manual stop or active transition revived listeners")
        }
        let occupied = try EchoAfterFIN()
        until("background occupied port") { occupied.port.get() != 0 }
        let port = NWEndpoint.Port(rawValue: occupied.port.get())!
        let background = PhoneBridge(configuration: { _, _, _, _, _ in
            PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                              localPort: port, upstreamPort: NWEndpoint.Port(rawValue: upstream.port.get())!)
        })
        background.start(role: .operator, localTailIP: "", roverPeerIP: "", jetsonUSBIP: "")
        recoverUntil("background retry not scheduled") { background.status.contains("Re-arming") }
        background.suspendForBackground(); occupied.stop(); settleRecovery()
        require(!background.running && background.listeningPort == 0, "retry revived background app")
        background.recoverOperatorOnActive(role: .operator)
        recoverUntil("foreground did not recover explicitly started listener") { background.listeningPort != 0 }
        background.stop()
        print("PASS listener recovery: single/dual fresh streams, atomic bind failure, Stop and background")
    }
}
