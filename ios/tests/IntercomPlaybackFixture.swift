import Foundation

final class RecordingIntercomOutput: IntercomPCMOutput {
    private(set) var starts = 0
    private(set) var stops = 0
    private(set) var frames: [IntercomPCMFrame] = []

    func start() { starts += 1 }
    func enqueue(_ frame: IntercomPCMFrame) { frames.append(frame) }
    func stop() { stops += 1 }
}

@main
struct IntercomPlaybackFixture {
    static func main() {
        let base = URL(string: "ws://172.20.10.3:8443/phone?depth=1")!
        let receiver = IntercomPCMFrame.receiverURL(from: base)
        precondition(receiver?.absoluteString == "ws://172.20.10.3:8443/ws/intercom?role=phone",
                     "The playback socket must use the Jetson intercom path and preserve its origin")

        var bytes = Data(repeating: 0, count: IntercomPCMFrame.byteCount)
        bytes[0] = 0x34; bytes[1] = 0x12
        bytes[2] = 0x00; bytes[3] = 0x80
        guard let frame = IntercomPCMFrame(data: bytes) else {
            fatalError("A complete 20 ms PCM frame must be accepted")
        }
        precondition(frame.samples.count == 320 && frame.samples[0] == 0x1234 && frame.samples[1] == Int16.min,
                     "PCM16 input must decode as little-endian mono samples")
        precondition(IntercomPCMFrame(data: Data(repeating: 0, count: 639)) == nil,
                     "Short audio messages must be rejected")
        precondition(IntercomPCMFrame(data: Data(repeating: 0, count: 641)) == nil,
                     "Oversized audio messages must be rejected")

        guard IntercomWireMessage.parse(text: #"{"type":"talk","active":true}"#) == .talk(active: true),
              IntercomWireMessage.parse(text: #"{"type":"talk","active":false}"#) == .talk(active: false),
              IntercomWireMessage.parse(text: #"{"type":"talk","active":1}"#) == nil,
              IntercomWireMessage.parse(text: #"{"type":"other"}"#) == nil else {
            fatalError("Only explicit talk-state controls must be accepted")
        }

        let output = RecordingIntercomOutput()
        let playback = IntercomPlaybackCore(output: output)
        playback.receive(.audio(bytes))
        precondition(output.frames.isEmpty && output.starts == 0,
                     "Audio received while talk is off must never reach the speaker")
        playback.receive(.talk(active: true))
        playback.receive(.audio(bytes))
        playback.receive(.audio(Data(repeating: 0, count: 639)))
        precondition(output.starts == 1 && output.frames.count == 1,
                     "Only a valid frame received during an active talk session must play")
        playback.receive(.talk(active: false))
        playback.receive(.audio(bytes))
        precondition(output.stops == 1 && output.frames.count == 1,
                     "Turning talk off must flush playback and reject subsequent frames")
        playback.receive(.talk(active: true))
        playback.disconnected()
        precondition(output.starts == 2 && output.stops == 2,
                     "A receiver disconnect must stop an active speaker session")

        var window = IntercomScheduledWindow(capacity: 4)
        let oldGeneration = window.generation
        for _ in 0..<4 { precondition(!window.schedule(), "Four 20 ms frames fit in the 80 ms playback window") }
        precondition(window.pending == 4, "Scheduled playback must remain bounded")
        precondition(window.schedule(), "A fifth pending frame must flush stale audio before scheduling live audio")
        precondition(window.pending == 1 && window.generation == oldGeneration + 1,
                     "Overflow must keep only the newest frame and invalidate stale completions")
        precondition(!window.complete(generation: oldGeneration) && window.pending == 1,
                     "A completion from a flushed queue must not corrupt current queue accounting")
        precondition(window.complete(generation: window.generation) && window.pending == 0,
                     "The current playback completion must release its queue slot")
        print("PASS intercom playback protocol and bounded speaker queue")
    }
}
