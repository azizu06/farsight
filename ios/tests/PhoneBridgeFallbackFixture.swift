import Darwin
import Foundation
import Network

func unusedPort() -> UInt16 {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    require(fd >= 0, "socket")
    defer { close(fd) }
    var address = sockaddr_in()
    address.sin_family = sa_family_t(AF_INET); address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    address.sin_addr.s_addr = inet_addr("127.0.0.1")
    let bound = withUnsafePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
    }
    require(bound == 0, "bind")
    var length = socklen_t(MemoryLayout<sockaddr_in>.size)
    _ = withUnsafeMutablePointer(to: &address) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &length) }
    }
    return UInt16(bigEndian: address.sin_port)
}

final class ResetAfterPrefix {
    private let fd: Int32
    let port: UInt16
    let bytes = Locked(Data()), accepted = Locked(0)
    private let queue = DispatchQueue(label: "bridge-reset-fixture")
    init() {
        let socketFD = socket(AF_INET, SOCK_STREAM, 0)
        fd = socketFD
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET); address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        require(withUnsafePointer(to: &address) { p in p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(socketFD, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } } == 0, "reset bind")
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &address) { p in p.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(socketFD, $0, &length) } }
        port = UInt16(bigEndian: address.sin_port)
        require(listen(fd, 4) == 0, "reset listen")
        queue.async { [self] in
            let peer = accept(fd, nil, nil)
            guard peer >= 0 else { return }
            accepted.update { $0 += 1 }
            var input = [UInt8](repeating: 0, count: 4096)
            let count = read(peer, &input, input.count)
            if count > 0 { bytes.update { $0.append(contentsOf: input.prefix(count)) } }
            var reset = linger(l_onoff: 1, l_linger: 0)
            _ = withUnsafePointer(to: &reset) { setsockopt(peer, SOL_SOCKET, SO_LINGER, $0, socklen_t(MemoryLayout<linger>.size)) }
            close(peer) // Explicit TCP RST, not a normal half-close.
        }
    }
    func stop() { shutdown(fd, SHUT_RDWR); close(fd) }
}

func mainUntil(_ message: String, _ condition: () -> Bool) {
    let deadline = Date().addingTimeInterval(5)
    while !condition() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    require(condition(), message)
}

@main
struct PhoneBridgeFallbackFixture {
    static func config(primary: UInt16, fallback: UInt16? = nil) -> PhoneBridgeConfig {
        PhoneBridgeConfig(local: IPv4Address("127.0.0.1")!, upstream: IPv4Address("127.0.0.1")!,
                          localPort: .any, upstreamPort: NWEndpoint.Port(rawValue: primary)!,
                          fallbackUpstream: fallback == nil ? nil : IPv4Address("127.0.0.1"),
                          fallbackPort: fallback.flatMap { NWEndpoint.Port(rawValue: $0) })
    }
    static func exchange(_ server: PhoneBridgeServer, state: Locked<PhoneBridgeSnapshot>, body: String) {
        let client = Client(port: state.get().port)
        defer { client.stop() }
        until("exchange ready") { client.ready.get() }
        client.send(Data(body.utf8), halfClose: true)
        until("exchange ended") { client.ended.get() }
        require(client.bytes.get() == Data(("RESP\0" + body).utf8), "wrong request/response bytes")
        until("exchange released") { state.get().active == 0 }
    }
    static func main() throws {
        let good = try EchoAfterFIN()
        defer { good.stop() }
        until("good ready") { good.port.get() != 0 }
        let state = Locked(PhoneBridgeSnapshot())
        let server = PhoneBridgeServer(connectTimeout: 1, sendTimeout: 0.3)
        defer { server.stop() }
        server.onSnapshot = { snapshot in state.update { $0 = snapshot } }
        server.start(config(primary: unusedPort(), fallback: good.port.get()))
        until("refusal bridge ready") { state.get().ready }
        exchange(server, state: state, body: "POST body once")
        require(state.get().lastChosen.hasPrefix("fallback"), "refused primary did not select fallback")
        require(good.received.get() == "POST body once".utf8.count, "request body duplicated")

        // Both candidates use real sockets. Only primary READY delivery is
        // delayed; this deterministically covers the slow-connect selection race.
        let slow = try EchoAfterFIN()
        defer { slow.stop() }
        until("slow ready") { slow.port.get() != 0 }
        let slowState = Locked(PhoneBridgeSnapshot())
        let race = PhoneBridgeServer(connectTimeout: 1, sendTimeout: 1,
                                     candidateReadyDelay: { $0 == .primary ? 0.5 : 0 })
        defer { race.stop() }
        race.onSnapshot = { snapshot in slowState.update { $0 = snapshot } }
        race.start(config(primary: slow.port.get(), fallback: good.port.get()))
        until("race listener") { slowState.get().ready }
        exchange(race, state: slowState, body: "slow primary loser")
        require(slow.received.get() == 0, "losing primary received application bytes")
        require(slowState.get().lastChosen.hasPrefix("fallback"), "slow primary won incorrectly")
        let countBefore = slow.queue.sync { slow.connections.count }
        exchange(race, state: slowState, body: "cooldown chooses healthy fallback")
        require(slow.queue.sync { slow.connections.count } == countBefore, "bad primary retried during cooldown")

        // Keep a selected primary TCP session alive while its listening port
        // goes away. New sessions use fallback; the existing one stays primary.
        let primary = try EchoAfterFIN()
        defer { primary.stop() }
        until("primary ready") { primary.port.get() != 0 }
        server.start(config(primary: primary.port.get(), fallback: good.port.get()))
        until("mixed listener") { state.get().ready && state.get().accepted == 0 }
        let old = Client(port: state.get().port)
        defer { old.stop() }
        until("primary chosen") { old.ready.get() && state.get().primaryActive == 1 }
        Thread.sleep(forTimeInterval: 0.4)
        require(!old.ended.get(), "normal idle mistaken for a stall")
        primary.stopAccepting()
        let newer = Client(port: state.get().port)
        defer { newer.stop() }
        until("mixed paths not visible") { newer.ready.get() && state.get().primaryActive == 1 && state.get().fallbackActive == 1 }
        old.send(Data("existing primary".utf8), halfClose: true)
        newer.send(Data("new fallback".utf8), halfClose: true)
        until("mixed drain") { old.ended.get() && newer.ended.get() }
        require(old.bytes.get() == Data("RESP\0existing primary".utf8), "existing stream migrated")
        require(newer.bytes.get() == Data("RESP\0new fallback".utf8), "fallback wrong response")

        let reset = ResetAfterPrefix()
        defer { reset.stop() }
        let fallbackBefore = good.received.get()
        server.start(config(primary: reset.port, fallback: good.port.get()))
        until("reset listener") { state.get().ready && state.get().accepted == 0 }
        let failed = Client(port: state.get().port)
        defer { failed.stop() }
        until("reset client") { failed.ready.get() && state.get().primaryActive == 1 }
        failed.send(Data("POST non-idempotent body".utf8), halfClose: false)
        until("midstream reset did not close client") { failed.ended.get() && state.get().active == 0 }
        require(reset.bytes.get().count > 0 && good.received.get() == fallbackBefore, "midstream body replayed onto fallback")
        exchange(server, state: state, body: "explicitly new connection")
        require(state.get().lastChosen.hasPrefix("fallback"), "next connection did not avoid failed primary")

        let blocked = try EchoAfterFIN(paused: true)
        defer { blocked.stop() }
        until("blocked primary ready") { blocked.port.get() != 0 }
        server.start(config(primary: blocked.port.get(), fallback: good.port.get()))
        until("stall listener") { state.get().ready && state.get().accepted == 0 }
        let stalledClient = Client(port: state.get().port)
        defer { stalledClient.stop() }
        until("stall primary chosen") { stalledClient.ready.get() && state.get().primaryActive == 1 }
        let fallbackBeforeStall = good.received.get()
        stalledClient.send(Data(repeating: 35, count: 16 * 1024 * 1024), halfClose: false)
        until("pending-write stall did not close client") { stalledClient.ended.get() && state.get().active == 0 && state.get().errors > 0 }
        require(good.received.get() == fallbackBeforeStall, "stalled body replayed to fallback")
        exchange(server, state: state, body: "new after write stall")
        require(state.get().lastChosen.hasPrefix("fallback"), "write-stall path reused immediately")

        server.start(config(primary: unusedPort(), fallback: unusedPort()))
        until("both-lost listener") { state.get().ready && state.get().accepted == 0 }
        let lost = Client(port: state.get().port)
        defer { lost.stop() }
        until("lost ready") { lost.ready.get() }
        lost.send(Data("must not forward".utf8), halfClose: false)
        until("both lost remained open") { lost.ended.get() && state.get().active == 0 && state.get().errors > 0 }
        require(state.get().forwardedBytes == 0, "unselected upstream received bytes")
        server.stop()
        until("stop state") { !state.get().running && state.get().active == 0 }

        let timeoutState = Locked(PhoneBridgeSnapshot())
        let timeoutServer = PhoneBridgeServer(connectTimeout: 0.2, sendTimeout: 1, candidateReadyDelay: { _ in 1 })
        defer { timeoutServer.stop() }
        timeoutServer.onSnapshot = { snapshot in timeoutState.update { $0 = snapshot } }
        timeoutServer.start(config(primary: slow.port.get(), fallback: good.port.get()))
        until("deadline listener") { timeoutState.get().ready }
        let timed = Client(port: timeoutState.get().port)
        defer { timed.stop() }
        until("deadline client") { timed.ready.get() }
        timed.send(Data("do not duplicate on timeout".utf8), halfClose: false)
        until("selection deadline not enforced") { timed.ended.get() && timeoutState.get().active == 0 && timeoutState.get().errors > 0 }
        require(timeoutState.get().forwardedBytes == 0, "deadline forwarded before winner")
        timeoutServer.stop()

        var intent = BridgeForegroundIntent()
        require(!intent.shouldRearmOnActive(role: .operator), "cold launch auto-start")
        intent.explicitStart(role: .rover)
        require(!intent.shouldRearmOnActive(role: .rover), "rover auto-restart")
        intent.explicitStart(role: .operator)
        require(intent.shouldRearmOnActive(role: .operator), "operator intent lost")
        intent.manualStopOrRoleChange()
        require(!intent.shouldRearmOnActive(role: .operator), "manual stop retained intent")

        // Exercise the actual ObservableObject recovery methods and real
        // listener sockets, using injected loopback config, not device routing.
        let wrapper = PhoneBridge(configuration: { _, _, _, _, _ in config(primary: good.port.get()) })
        wrapper.recoverOperatorOnActive(role: .operator)
        require(!wrapper.running, "wrapper cold launch started")
        wrapper.start(role: .operator, localTailIP: "", roverPeerIP: "", jetsonUSBIP: "")
        mainUntil("wrapper listener") { wrapper.listeningPort != 0 }
        let preBackground = Client(port: wrapper.listeningPort)
        mainUntil("pre-background client") { preBackground.ready.get() }
        wrapper.suspendForBackground()
        require(wrapper.operatorRecoveryWanted && !wrapper.running, "background forgot intent or kept listener")
        mainUntil("old socket remained open") { preBackground.ended.get() }
        wrapper.recoverOperatorOnActive(role: .operator)
        mainUntil("foreground listener did not rearm") { wrapper.listeningPort != 0 }
        let recovered = Client(port: wrapper.listeningPort)
        mainUntil("fresh recovered client") { recovered.ready.get() }
        recovered.send(Data("fresh foreground session".utf8), halfClose: true)
        mainUntil("fresh response") { recovered.ended.get() }
        require(recovered.bytes.get() == Data("RESP\0fresh foreground session".utf8), "recovery did not create fresh stream")
        wrapper.stop(); wrapper.recoverOperatorOnActive(role: .operator)
        require(!wrapper.running && !wrapper.operatorRecoveryWanted, "manual Stop automatically rearmed")
        preBackground.stop(); recovered.stop()
        print("PASS: refusal, delayed readiness, no loser bytes/replay, cooldown, mixed paths, both lost, normal idle, foreground recovery, manual Stop")
    }
}
