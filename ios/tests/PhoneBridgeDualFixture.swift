import Foundation
import Network

func dualUntil(_ message: String, _ condition: () -> Bool) {
    let deadline = Date().addingTimeInterval(5)
    while !condition() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    require(condition(), message)
}

@main
struct PhoneBridgeDualFixture {
    static func main() throws {
        let policy = try PhoneBridgeConfig.make(role: .operator, localTailIP: "",
                                               roverPeerIP: "100.97.94.113", jetsonUSBIP: "",
                                               fallbackPeerIP: "100.103.169.40", dualRoutes: true)
        require(policy.localPort.rawValue == 8081 && policy.dualSecondaryPort.rawValue == 8082 &&
                policy.upstreamPort.rawValue == 8081 && policy.fallbackPort?.rawValue == 8081, "dual fixed port contract")
        require(policy.dualRoutes, "dual option not recorded")
        for peer in [nil, "1.2.3.4", "100.97.94.113"] as [String?] {
            do {
                _ = try PhoneBridgeConfig.make(role: .operator, localTailIP: "", roverPeerIP: "100.97.94.113",
                                               jetsonUSBIP: "", fallbackPeerIP: peer, dualRoutes: true)
                fatalError("invalid second address accepted")
            } catch { }
        }

        let primary = try EchoAfterFIN(responsePrefix: "PRIMARY\0")
        let fallback = try EchoAfterFIN(responsePrefix: "FALLBACK\0")
        defer { primary.stop(); fallback.stop() }
        until("upstreams not ready") { primary.port.get() != 0 && fallback.port.get() != 0 }
        let makeConfig: (PhoneBridgeRole, String, String, String, String?) throws -> PhoneBridgeConfig = { _, _, _, _, _ in
            var config = PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                                           localPort: .any, upstreamPort: NWEndpoint.Port(rawValue: primary.port.get())!,
                                           fallbackUpstream: IPv4Address("127.0.0.1"),
                                           fallbackPort: NWEndpoint.Port(rawValue: fallback.port.get())!)
            config.dualSecondaryPort = .any // Actual two sockets; production policy is8081/8082 above.
            return config
        }
        let bridge = PhoneBridge(configuration: makeConfig)
        bridge.start(role: .operator, localTailIP: "", roverPeerIP: "", jetsonUSBIP: "", fallbackPeerIP: "", dualRoutes: true)
        dualUntil("both listeners not ready") { bridge.listeningPort != 0 && bridge.secondaryListeningPort != 0 }
        require(bridge.listeningPort != bridge.secondaryListeningPort && bridge.dualRoutesActive, "one listener substituted for two")
        let p = Client(port: bridge.listeningPort), f = Client(port: bridge.secondaryListeningPort)
        defer { p.stop(); f.stop(); bridge.stop() }
        dualUntil("dual clients") { p.ready.get() && f.ready.get() }
        p.send(Data("primary only".utf8), halfClose: true)
        f.send(Data("fallback only".utf8), halfClose: true)
        dualUntil("dual drain") { p.ended.get() && f.ended.get() }
        require(p.bytes.get() == Data("PRIMARY\0primary only".utf8), "primary socket routed to fallback")
        require(f.bytes.get() == Data("FALLBACK\0fallback only".utf8), "fallback socket routed to primary")
        require(primary.received.get() == 12 && fallback.received.get() == 13, "route bytes duplicated")

        let heldA = Client(port: bridge.listeningPort), heldB = Client(port: bridge.secondaryListeningPort)
        defer { heldA.stop(); heldB.stop() }
        dualUntil("held clients") { heldA.ready.get() && heldB.ready.get() }
        bridge.suspendForBackground()
        require(!bridge.running && bridge.operatorRecoveryWanted && bridge.listeningPort == 0 && bridge.secondaryListeningPort == 0,
                "background did not suspend both")
        dualUntil("background sockets remained") { heldA.ended.get() && heldB.ended.get() }
        bridge.recoverOperatorOnActive(role: .operator)
        dualUntil("foreground did not rearm both") { bridge.listeningPort != 0 && bridge.secondaryListeningPort != 0 }
        let fresh = Client(port: bridge.secondaryListeningPort)
        defer { fresh.stop() }
        dualUntil("fresh dual client") { fresh.ready.get() }
        fresh.send(Data("fresh after foreground".utf8), halfClose: true)
        dualUntil("fresh dual drain") { fresh.ended.get() }
        require(fresh.bytes.get() == Data("FALLBACK\0fresh after foreground".utf8), "foreground resumed stale stream")
        primary.stopAccepting()
        let down = Client(port: bridge.listeningPort)
        defer { down.stop() }
        dualUntil("down route client") { down.ready.get() }
        down.send(Data("never move routes".utf8), halfClose: true)
        dualUntil("lost primary client not closed") { down.ended.get() }
        require(bridge.running && bridge.secondaryListeningPort != 0, "upstream loss canceled sibling listener")
        let stillGood = Client(port: bridge.secondaryListeningPort)
        defer { stillGood.stop() }
        dualUntil("sibling client") { stillGood.ready.get() }
        stillGood.send(Data("sibling still healthy".utf8), halfClose: true)
        dualUntil("sibling response") { stillGood.ended.get() }
        require(stillGood.bytes.get() == Data("FALLBACK\0sibling still healthy".utf8), "sibling failed after primary loss")

        bridge.stop(); bridge.recoverOperatorOnActive(role: .operator)
        require(!bridge.running && !bridge.operatorRecoveryWanted && bridge.listeningPort == 0 && bridge.secondaryListeningPort == 0,
                "manual stop left dual intent")

        let occupied = try EchoAfterFIN()
        defer { occupied.stop() }
        until("occupied port") { occupied.port.get() != 0 }
        let firstCore = PhoneBridgeServer(), secondCore = PhoneBridgeServer()
        let firstState = Locked<PhoneBridgeSnapshot?>(nil), secondState = Locked<PhoneBridgeSnapshot?>(nil)
        let failed = PhoneBridge(server: firstCore, secondaryServer: secondCore, configuration: { role, a, b, c, d in
            var config = try makeConfig(role, a, b, c, d)
            config.dualSecondaryPort = NWEndpoint.Port(rawValue: occupied.port.get())!
            return config
        })
        // Inspect actual transport state after the automatic failure, without
        // issuing another stop that could conceal incomplete teardown.
        failed.start(role: .operator, localTailIP: "", roverPeerIP: "", jetsonUSBIP: "", fallbackPeerIP: "", dualRoutes: true)
        dualUntil("port conflict not reported") { !failed.running && failed.status.contains("Both stopped") }
        require(failed.listeningPort == 0 && failed.secondaryListeningPort == 0, "partial listener retained after conflict")
        firstCore.readSnapshot { value in firstState.update { $0 = value } }
        secondCore.readSnapshot { value in secondState.update { $0 = value } }
        until("snapshot unavailable") { firstState.get() != nil && secondState.get() != nil }
        require(firstState.get()?.running == false && secondState.get()?.running == false,
                "partial listener remained bound after conflict")
        failed.stop()

        let earlyA = PhoneBridgeServer(), earlyB = PhoneBridgeServer()
        let earlyStateA = Locked<PhoneBridgeSnapshot?>(nil), earlyStateB = Locked<PhoneBridgeSnapshot?>(nil)
        let earlyFailure = PhoneBridge(server: earlyA, secondaryServer: earlyB, configuration: { role, a, b, c, d in
            var config = try makeConfig(role, a, b, c, d)
            config = PhoneBridgeConfig(local: config.local, upstream: config.upstream,
                                       localPort: NWEndpoint.Port(rawValue: occupied.port.get())!,
                                       upstreamPort: config.upstreamPort, fallbackUpstream: config.fallbackUpstream,
                                       fallbackPort: config.fallbackPort, dualSecondaryPort: .any)
            return config
        })
        earlyFailure.start(role: .operator, localTailIP: "", roverPeerIP: "", jetsonUSBIP: "", fallbackPeerIP: "", dualRoutes: true)
        dualUntil("early primary conflict not atomic") { !earlyFailure.running && earlyFailure.status.contains("Both stopped") }
        earlyA.readSnapshot { value in earlyStateA.update { $0 = value } }
        earlyB.readSnapshot { value in earlyStateB.update { $0 = value } }
        until("early snapshots unavailable") { earlyStateA.get() != nil && earlyStateB.get() != nil }
        require(earlyStateA.get()?.running == false && earlyStateB.get()?.running == false,
                "late sibling start survived primary bind failure")
        earlyFailure.stop()

        let invalid = PhoneBridge()
        invalid.start(role: .operator, localTailIP: "", roverPeerIP: "100.97.94.113", jetsonUSBIP: "",
                      fallbackPeerIP: "not an IP", dualRoutes: true)
        require(!invalid.running && !invalid.operatorRecoveryWanted, "validation started a partial listener")
        print("PASS: dual port policy, exact pinned routing, isolated upstream loss, both suspend/rearm/stop, atomic conflict, prevalidation")
    }
}
