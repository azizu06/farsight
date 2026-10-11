import Foundation

final class RecordingIntercomOutput: IntercomPCMOutput {
    private(set) var starts = 0
    private(set) var stops = 0
    private(set) var frames: [IntercomPCMFrame] = []
    private var capture: ((Data) -> Void)?

    func start(onCapture: @escaping (Data) -> Void, onUnavailable: @escaping () -> Void) { starts += 1; capture = onCapture }
    func enqueue(_ frame: IntercomPCMFrame) { frames.append(frame) }
    func stop() { stops += 1; capture = nil }
    func emit(_ frame: Data) { capture?(frame) }
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
              IntercomWireMessage.parse(text: #"{"type":"peer","connected":true}"#) == .peer(connected: true),
              IntercomWireMessage.parse(text: #"{"type":"talk","active":1}"#) == nil,
              IntercomWireMessage.parse(text: #"{"type":"other"}"#) == nil else {
            fatalError("Only explicit talk-state controls must be accepted")
        }

        var accumulator = IntercomFrameAccumulator()
        precondition(accumulator.append(Array(repeating: 7, count: 100)).isEmpty,
                     "Partial microphone buffers must wait until one full 20 ms frame is available")
        let outgoing = accumulator.append(Array(repeating: -2, count: 220))
        precondition(outgoing.count == 1 && IntercomPCMFrame(data: outgoing[0])?.samples == Array(repeating: 7, count: 100) + Array(repeating: -2, count: 220),
                     "Microphone samples must be emitted as exact 320-sample PCM frames")

        let output = RecordingIntercomOutput()
        var sent: [Data] = []
        let playback = IntercomPlaybackCore(output: output, sendAudio: { sent.append($0) })
        playback.receive(.peer(connected: true))
        precondition(playback.peerConnected, "Phone receiver must observe its Quest peer")
        playback.receive(.audio(bytes))
        precondition(output.frames.isEmpty && output.starts == 0,
                     "Audio received while talk is off must never reach the speaker")
        output.emit(bytes)
        precondition(sent.isEmpty, "Phone microphone must stay gated while talk is off")
        playback.receive(.talk(active: true))
        playback.receive(.audio(bytes))
        playback.receive(.audio(Data(repeating: 0, count: 639)))
        output.emit(bytes)
        precondition(output.starts == 1 && output.frames.count == 1,
                     "Only a valid frame received during an active talk session must play")
        precondition(sent == [bytes], "The iPhone microphone must transmit while speaker playback is active")
        playback.receive(.talk(active: false))
        playback.receive(.audio(bytes))
        precondition(output.stops == 1 && output.frames.count == 1,
                     "Turning talk off must flush playback and reject subsequent frames")
        output.emit(bytes)
        precondition(sent == [bytes], "Turning talk off must stop iPhone microphone transmission")
        playback.receive(.talk(active: true))
        playback.receive(.peer(connected: false))
        precondition(!playback.peerConnected, "Disconnecting Quest must clear peer state")
        playback.disconnected()
        precondition(output.starts == 2 && output.stops == 2,
                     "A peer or receiver disconnect must stop both audio directions")

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
        print("PASS duplex intercom protocol and bounded audio queues")
    }
}
