import Foundation
import CoreFoundation

enum WirePacket {
    static func bundle(header: [String: Any], payloads: [Data]) throws -> Data {
        let json = try JSONSerialization.data(withJSONObject: header, options: [.sortedKeys])
        guard json.count > 0, json.count <= 65_536 else {
            throw NSError(domain: "FarsightPacket", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Frame header is too large"])
        }
        var length = UInt32(json.count).littleEndian
        var result = withUnsafeBytes(of: &length) { Data($0) }
        result.append(json)
        for payload in payloads { result.append(payload) }
        let limit = header["type"] as? String == "rgb_frame" ? 2 : 8
        guard result.count <= limit * 1024 * 1024 else {
            throw NSError(domain: "FarsightPacket", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "Frame exceeds the server's \(limit) MiB limit"])
        }
        return result
    }

    static func endpoint(_ text: String, depth: Bool) -> URL? {
        guard var parts = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["ws", "wss"].contains(parts.scheme?.lowercased() ?? ""),
              parts.host != nil, parts.user == nil, parts.password == nil else { return nil }
        parts.path = depth ? "/phone" : "/phone/rgb"
        parts.query = nil
        parts.fragment = nil
        return parts.url
    }
}

/// Clock evidence is a runtime plausibility check, not an Apple ARKit clock-origin guarantee.
struct CapturePermitGate {
    struct Permit {
        let token: String
        let receivedAt: Double
        let validSeconds: Double
    }

    private(set) var sessionID = ""
    private(set) var mapEpoch = 1
    private(set) var permit: Permit?
    private(set) var plausibleSamples = 0
    private(set) var diagnostic = "Waiting for capture permit"
    private var previousCapture: Double?
    private var previousHost: Double?
    private var recentPermits: [Permit] = []
    private var firstReceipts: [String: Double] = [:]
    static let requiredSamples = 12
    static let maximumSensorAge = 0.150

    mutating func start(sessionID: String, mapEpoch: Int) {
        self = CapturePermitGate()
        self.sessionID = sessionID
        self.mapEpoch = mapEpoch
    }

    mutating func clear() { self = CapturePermitGate() }

    @discardableResult
    mutating func receive(_ message: [String: Any], at hostTime: Double) -> Bool {
        guard message["type"] as? String == "capture_permit",
              message["session_id"] as? String == sessionID, !sessionID.isEmpty,
              let epoch = Self.number(message["map_epoch"]), epoch == Double(mapEpoch),
              let token = message["token"] as? String, !token.isEmpty, token.utf8.count <= 256,
              let milliseconds = Self.number(message["valid_for_ms"]), milliseconds > 0, milliseconds <= 250,
              hostTime.isFinite, hostTime > 0 else { return false }
        // A duplicated token never renews its first receipt time.
        if firstReceipts[token] != nil { return true }
        guard permit == nil || hostTime >= permit!.receivedAt else { return false }
        guard firstReceipts.count < 65_536 else {
            diagnostic = "Capture permit history full; stop and start a new session"
            return false
        }
        permit = Permit(token: token, receivedAt: hostTime, validSeconds: milliseconds / 1000)
        firstReceipts[token] = hostTime
        recentPermits.removeAll { hostTime - $0.receivedAt >= $0.validSeconds }
        recentPermits.append(permit!)
        if recentPermits.count > 8 { recentPermits.removeFirst(recentPermits.count - 8) }
        diagnostic = "Capture permit received; checking sensor clock"
        return true
    }

    mutating func observe(captureTime: Double, hostTime: Double) -> Bool {
        let delta = hostTime - captureTime
        let advancing = previousCapture == nil ||
            (captureTime > previousCapture! && hostTime > previousHost!)
        previousCapture = captureTime
        previousHost = hostTime
        guard captureTime.isFinite, hostTime.isFinite, captureTime > 0, hostTime > 0,
              delta >= 0, delta <= Self.maximumSensorAge, advancing else {
            plausibleSamples = 0
            diagnostic = "Sensor clock unverified or frame older than 150 ms"
            return false
        }
        plausibleSamples = min(Self.requiredSamples, plausibleSamples + 1)
        diagnostic = plausibleSamples == Self.requiredSamples ?
            "Sensor/host clock plausible; physical qualification still required" :
            "Checking sensor clock \(plausibleSamples)/\(Self.requiredSamples)"
        return plausibleSamples == Self.requiredSamples
    }

    /// Qualification comes from documented clock conversion or separately approved
    /// runtime evidence; plausible numeric samples alone never establish it.
    func eligible(captureTime: Double, hostTime: Double, clockQualified: Bool) -> Permit? {
        guard clockQualified, plausibleSamples >= Self.requiredSamples,
              captureTime.isFinite, hostTime.isFinite else { return nil }
        // Camera callbacks can lag multiple permit publications. Choose the
        // newest live predecessor of the actual exposure, not a later token.
        return recentPermits.reversed().first {
            Self.validAtSend($0, captureTime: captureTime, hostTime: hostTime)
        }
    }

    /// Revalidate the selected immutable token after encoding/JSON framing.
    static func validAtSend(_ permit: Permit, captureTime: Double, hostTime: Double) -> Bool {
        captureTime.isFinite && hostTime.isFinite && captureTime > permit.receivedAt &&
        hostTime >= captureTime && hostTime - captureTime <= maximumSensorAge &&
        hostTime >= permit.receivedAt && hostTime - permit.receivedAt < permit.validSeconds
    }

    private static func number(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let result = number.doubleValue
        return result.isFinite ? result : nil
    }
}
