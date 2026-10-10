import Foundation

@main
struct PacketFixture {
    static func main() throws {
        let args = CommandLine.arguments
        if args.count == 4 && args[1] == "endpoint" {
            guard let url = WirePacket.endpoint(args[2], depth: args[3] == "depth") else {
                exit(2)
            }
            print(url.absoluteString)
            return
        }
        guard args.count >= 5, args[1] == "packet" else { exit(2) }
        let json = try Data(contentsOf: URL(fileURLWithPath: args[2]))
        guard let header = try JSONSerialization.jsonObject(with: json) as? [String: Any] else { exit(2) }
        let payloads = try args.dropFirst(4).map { try Data(contentsOf: URL(fileURLWithPath: $0)) }
        let packet = try WirePacket.bundle(header: header, payloads: payloads)
        try packet.write(to: URL(fileURLWithPath: args[3]))
    }
}
