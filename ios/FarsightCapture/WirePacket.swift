import Foundation

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
