import Foundation

@main
struct CapturePermitFixture {
    static func expect(_ value: Bool, _ message: String) {
        if !value { fatalError(message) }
    }

    static func warmed() -> CapturePermitGate {
        var gate = CapturePermitGate()
        gate.start(sessionID: "current", mapEpoch: 1)
        for i in 0..<12 {
            let capture = 1000 + Double(i) * 0.02
            _ = gate.observe(captureTime: capture, hostTime: capture + 0.02)
        }
        return gate
    }

    static func main() {
        let message: [String: Any] = ["type": "capture_permit", "session_id": "current",
                                      "map_epoch": 1, "token": "opaque", "valid_for_ms": 250]
        var gate = warmed()
        expect(gate.receive(message, at: 1000.25), "valid permit rejected")
        expect(gate.eligible(captureTime: 1000.26, hostTime: 1000.28, clockQualified: true) != nil,
               "fresh exposure after token must qualify")
        expect(gate.eligible(captureTime: 1000.26, hostTime: 1000.28, clockQualified: false) == nil,
               "plausibility alone must not qualify the clock")
        expect(gate.eligible(captureTime: 1000.24, hostTime: 1000.28, clockQualified: true) == nil,
               "queued exposure before token must be skipped")
        expect(gate.eligible(captureTime: 1000.25, hostTime: 1000.28, clockQualified: true) == nil,
               "equal capture/receipt timestamp must be skipped")
        expect(gate.eligible(captureTime: 1000.49, hostTime: 1000.5, clockQualified: true) == nil,
               "250ms expiry must fail closed")
        expect(gate.eligible(captureTime: 1000.26, hostTime: 1000.42, clockQualified: true) == nil,
               "sensor backlog over150ms must fail closed")
        expect(gate.eligible(captureTime: 1000.29, hostTime: 1000.28, clockQualified: true) == nil,
               "future sensor timestamp must fail closed")
        let selected = gate.permit!
        expect(CapturePermitGate.validAtSend(selected, captureTime: 1000.26, hostTime: 1000.28), "fresh send rejected")
        expect(!CapturePermitGate.validAtSend(selected, captureTime: 1000.26, hostTime: 1000.42), "encoding backlog accepted")
        expect(!CapturePermitGate.validAtSend(selected, captureTime: 1000.49, hostTime: 1000.5), "token expired during encoding accepted")
        expect(!CapturePermitGate.validAtSend(selected, captureTime: 1000.26, hostTime: 1000.2), "host clock rewind accepted")
        expect(gate.receive(message, at: 1000.49) && gate.permit!.receivedAt == 1000.25,
               "duplicate token must not renew receipt")
        var successor = message
        successor["token"] = "newer-than-exposure"
        expect(gate.receive(successor, at: 1000.30), "successor token rejected")
        expect(gate.eligible(captureTime: 1000.28, hostTime: 1000.32, clockQualified: true)?.token == "opaque",
               "latest post-exposure token must not hide a valid predecessor")
        expect(gate.eligible(captureTime: 1000.31, hostTime: 1000.33, clockQualified: true)?.token == "newer-than-exposure",
               "newest qualifying token must be selected")
        var wrong = message
        wrong["session_id"] = "old"
        expect(!gate.receive(wrong, at: 1000.27), "wrong session accepted")
        wrong = message; wrong["map_epoch"] = 2
        expect(!gate.receive(wrong, at: 1000.27), "wrong map epoch accepted")
        for bad: Any in [true, 251, -1, Double.infinity, "250"] {
            wrong = message; wrong["valid_for_ms"] = bad
            expect(!gate.receive(wrong, at: 1000.27), "malformed validity accepted")
        }
        wrong = message; wrong["map_epoch"] = true
        expect(!gate.receive(wrong, at: 1000.27), "boolean epoch accepted")
        var warming = CapturePermitGate()
        warming.start(sessionID: "current", mapEpoch: 1)
        expect(warming.receive(message, at: 1000.25), "warmup permit")
        _ = warming.observe(captureTime: 1000.26, hostTime: 1000.28)
        expect(warming.eligible(captureTime: 1000.26, hostTime: 1000.28, clockQualified: true) == nil,
               "one plausible observation is insufficient")
        expect(!gate.observe(captureTime: 1000.26, hostTime: 2000), "unrelated clock origin accepted")
        expect(gate.plausibleSamples == 0, "invalid sample must reset qualification")
        gate = warmed()
        expect(!gate.observe(captureTime: 1000.22, hostTime: 1000.25), "repeated capture must reset evidence")
        gate = warmed(); gate.clear()
        expect(gate.permit == nil && gate.plausibleSamples == 0, "stop must clear token and clock evidence")
        gate = warmed(); _ = gate.receive(message, at: 1000.25)
        gate.start(sessionID: "new", mapEpoch: 1)
        expect(gate.permit == nil && !gate.receive(message, at: 1000.27), "new session retained old permit")
        print("PASS: capture permit timing, identity, expiry, sensor backlog, clock qualification, reset")
    }
}
