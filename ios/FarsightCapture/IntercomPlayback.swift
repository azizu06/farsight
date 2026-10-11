import Foundation
import CoreFoundation

struct IntercomPCMFrame: Equatable {
    static let sampleRate = 16_000
    static let sampleCount = 320
    static let byteCount = sampleCount * MemoryLayout<Int16>.size

    let samples: [Int16]

    init?(data: Data) {
        guard data.count == Self.byteCount else { return nil }
        var samples = [Int16]()
        samples.reserveCapacity(Self.sampleCount)
        for offset in stride(from: 0, to: data.count, by: 2) {
            let value = UInt16(data[offset]) | (UInt16(data[offset + 1]) << 8)
            samples.append(Int16(bitPattern: value))
        }
        self.samples = samples
    }

    static func receiverURL(from captureURL: URL) -> URL? {
        guard var components = URLComponents(url: captureURL, resolvingAgainstBaseURL: false),
              components.scheme == "ws" || components.scheme == "wss",
              components.host != nil else { return nil }
        components.path = "/ws/intercom"
        components.queryItems = [URLQueryItem(name: "role", value: "phone")]
        components.fragment = nil
        return components.url
    }
}

enum IntercomWireMessage: Equatable {
    case talk(active: Bool)
    case audio(Data)

    static func parse(text: String) -> IntercomWireMessage? {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["type"] as? String == "talk",
              let active = object["active"] as? NSNumber,
              CFGetTypeID(active) == CFBooleanGetTypeID() else { return nil }
        return .talk(active: active.boolValue)
    }
}

protocol IntercomPCMOutput: AnyObject {
    func start()
    func enqueue(_ frame: IntercomPCMFrame)
    func stop()
}

final class IntercomPlaybackCore {
    private let output: IntercomPCMOutput
    private(set) var isTalking = false

    init(output: IntercomPCMOutput) { self.output = output }

    func receive(_ message: IntercomWireMessage) {
        switch message {
        case .talk(let active):
            guard active != isTalking else { return }
            isTalking = active
            if active { output.start() } else { output.stop() }
        case .audio(let data):
            guard isTalking, let frame = IntercomPCMFrame(data: data) else { return }
            output.enqueue(frame)
        }
    }

    func disconnected() {
        isTalking = false
        output.stop()
    }
}

struct IntercomScheduledWindow {
    let capacity: Int
    private(set) var pending = 0
    private(set) var generation = 0

    init(capacity: Int) {
        precondition(capacity > 0)
        self.capacity = capacity
    }

    mutating func schedule() -> Bool {
        let overflow = pending >= capacity
        if overflow {
            pending = 0
            generation += 1
        }
        pending += 1
        return overflow
    }

    @discardableResult
    mutating func complete(generation completedGeneration: Int) -> Bool {
        guard completedGeneration == generation, pending > 0 else { return false }
        pending -= 1
        return true
    }

    mutating func reset() {
        pending = 0
        generation += 1
    }
}

#if canImport(AVFoundation)
import AVFoundation

final class IntercomPlayback {
    private let queue = DispatchQueue(label: "farsight.intercom.playback", qos: .userInitiated)
    private var captureURL: URL?
    private var generation = UUID()
    private var retryWork: DispatchWorkItem?
    private var retryDelay: TimeInterval = 0.25
    private var session: URLSession?
    private var socket: URLSessionWebSocketTask?
    private lazy var output = IPhoneIntercomPCMOutput(queue: queue)
    private lazy var core = IntercomPlaybackCore(output: output)

    func beginAttempt(from captureURL: URL) {
        queue.async { [weak self] in
            guard let self else { return }
            self.closeSocket()
            self.core.disconnected()
            self.captureURL = captureURL
            self.generation = UUID()
            self.retryDelay = 0.25
            self.connect(generation: self.generation)
        }
    }

    func disconnect() {
        queue.async { [weak self] in
            guard let self else { return }
            self.generation = UUID()
            self.captureURL = nil
            self.retryWork?.cancel()
            self.retryWork = nil
            self.closeSocket()
            self.core.disconnected()
        }
    }

    private func connect(generation: UUID) {
        guard self.generation == generation, let captureURL,
              let receiverURL = IntercomPCMFrame.receiverURL(from: captureURL) else { return }
        let network = URLSession(configuration: .ephemeral)
        let task = network.webSocketTask(with: receiverURL)
        task.maximumMessageSize = 4096
        session = network
        socket = task
        task.resume()
        receive(task, generation: generation)
    }

    private func receive(_ task: URLSessionWebSocketTask, generation: UUID) {
        task.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard self.generation == generation, self.socket === task else { return }
                switch result {
                case .failure:
                    self.retry(generation: generation)
                case .success(.data(let data)):
                    self.retryDelay = 0.25
                    self.core.receive(.audio(data))
                    self.receive(task, generation: generation)
                case .success(.string(let text)):
                    self.retryDelay = 0.25
                    if let message = IntercomWireMessage.parse(text: text) {
                        self.core.receive(message)
                    }
                    self.receive(task, generation: generation)
                @unknown default:
                    self.retry(generation: generation)
                }
            }
        }
    }

    private func retry(generation: UUID) {
        guard self.generation == generation else { return }
        closeSocket()
        core.disconnected()
        let delay = retryDelay
        retryDelay = min(2, retryDelay * 2)
        let work = DispatchWorkItem { [weak self] in self?.connect(generation: generation) }
        retryWork = work
        queue.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func closeSocket() {
        retryWork?.cancel()
        retryWork = nil
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        session?.invalidateAndCancel()
        session = nil
    }
}

private final class IPhoneIntercomPCMOutput: IntercomPCMOutput {
    private let queue: DispatchQueue
    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var format: AVAudioFormat?
    private var window = IntercomScheduledWindow(capacity: 4)

    init(queue: DispatchQueue) { self.queue = queue }

    func start() {
        guard engine == nil else { return }
        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.playback, mode: .voicePrompt)
            try audioSession.setActive(true)
            let engine = AVAudioEngine()
            let player = AVAudioPlayerNode()
            let format = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                       sampleRate: Double(IntercomPCMFrame.sampleRate),
                                       channels: 1, interleaved: false)!
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: format)
            try engine.start()
            player.play()
            self.engine = engine
            self.player = player
            self.format = format
        } catch {
            stop()
        }
    }

    func enqueue(_ frame: IntercomPCMFrame) {
        guard engine != nil, let player, let format,
              let buffer = AVAudioPCMBuffer(pcmFormat: format,
                                            frameCapacity: AVAudioFrameCount(frame.samples.count)),
              let samples = buffer.int16ChannelData?.pointee else { return }
        buffer.frameLength = AVAudioFrameCount(frame.samples.count)
        for index in frame.samples.indices { samples[index] = frame.samples[index] }
        if window.schedule() {
            player.stop()
            player.reset()
            player.play()
        }
        let scheduledGeneration = window.generation
        player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let self else { return }
            self.queue.async { self.window.complete(generation: scheduledGeneration) }
        }
        _ = engine
    }

    func stop() {
        player?.stop()
        player?.reset()
        engine?.stop()
        window.reset()
        player = nil
        format = nil
        engine = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}
#endif
