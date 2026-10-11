import Foundation
import CoreFoundation

struct IntercomPCMFrame: Equatable {
    static let sampleRate = 16_000
    static let sampleCount = 320
    static let byteCount = sampleCount * MemoryLayout<Int16>.size

    let samples: [Int16]

    init(samples: [Int16]) {
        precondition(samples.count == Self.sampleCount)
        self.samples = samples
    }

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

    var data: Data {
        var bytes = Data(capacity: Self.byteCount)
        for sample in samples {
            let littleEndian = UInt16(bitPattern: sample).littleEndian
            bytes.append(UInt8(truncatingIfNeeded: littleEndian))
            bytes.append(UInt8(truncatingIfNeeded: littleEndian >> 8))
        }
        return bytes
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
    case peer(connected: Bool)
    case audio(Data)

    static func parse(text: String) -> IntercomWireMessage? {
        guard let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = object["type"] as? String else { return nil }
        switch type {
        case "talk":
            guard let active = object["active"] as? NSNumber,
                  CFGetTypeID(active) == CFBooleanGetTypeID() else { return nil }
            return .talk(active: active.boolValue)
        case "peer":
            guard let connected = object["connected"] as? NSNumber,
                  CFGetTypeID(connected) == CFBooleanGetTypeID() else { return nil }
            return .peer(connected: connected.boolValue)
        default: return nil
        }
    }
}

struct IntercomFrameAccumulator {
    private var pending: [Int16] = []

    mutating func append(_ samples: [Int16]) -> [Data] {
        pending.append(contentsOf: samples)
        var frames: [Data] = []
        while pending.count >= IntercomPCMFrame.sampleCount {
            frames.append(IntercomPCMFrame(samples: Array(pending.prefix(IntercomPCMFrame.sampleCount))).data)
            pending.removeFirst(IntercomPCMFrame.sampleCount)
        }
        return frames
    }

    mutating func reset() { pending.removeAll(keepingCapacity: true) }
}

protocol IntercomPCMOutput: AnyObject {
    func start(onCapture: @escaping (Data) -> Void, onUnavailable: @escaping () -> Void)
    func enqueue(_ frame: IntercomPCMFrame)
    func stop()
}

final class IntercomPlaybackCore {
    private let output: IntercomPCMOutput
    private(set) var isTalking = false
    private(set) var peerConnected = false
    private let sendAudio: (Data) -> Void
    private let onUnavailable: () -> Void

    init(output: IntercomPCMOutput, sendAudio: @escaping (Data) -> Void = { _ in },
         onUnavailable: @escaping () -> Void = {}) {
        self.output = output
        self.sendAudio = sendAudio
        self.onUnavailable = onUnavailable
    }

    func receive(_ message: IntercomWireMessage) {
        switch message {
        case .peer(let connected):
            peerConnected = connected
            if !connected { stopTalking() }
        case .talk(let active):
            guard active != isTalking else { return }
            isTalking = active
            if active {
                output.start(onCapture: { [weak self] data in
                    guard let self, data.count == IntercomPCMFrame.byteCount else { return }
                    self.sendAudio(data)
                }, onUnavailable: onUnavailable)
            } else { output.stop() }
        case .audio(let data):
            guard isTalking, let frame = IntercomPCMFrame(data: data) else { return }
            output.enqueue(frame)
        }
    }

    func disconnected() {
        peerConnected = false
        stopTalking()
    }

    private func stopTalking() {
        guard isTalking else { return }
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
    private var pingWork: DispatchWorkItem?
    private var retryDelay: TimeInterval = 0.25
    private var session: URLSession?
    private var socket: URLSessionWebSocketTask?
    private var audioSendsInFlight = 0
    var onStatus: ((String) -> Void)?
    private lazy var output = IPhoneIntercomPCMOutput(queue: queue) { [weak self] message in
        self?.report(message)
    }
    private lazy var core = IntercomPlaybackCore(output: output, sendAudio: { [weak self] data in
        self?.sendCapturedAudio(data)
    }, onUnavailable: { [weak self] in
        self?.report("iPhone microphone unavailable · check iOS permission")
    })

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
        var request = URLRequest(url: receiverURL)
        request.timeoutInterval = 5
        let task = network.webSocketTask(with: request)
        task.maximumMessageSize = 4096
        session = network
        socket = task
        task.resume()
        receive(task, generation: generation)
        schedulePing(task, generation: generation)
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
                        switch message {
                        case .talk(let active): self.report(active ? "Talk on · requesting iPhone microphone" : "Talk off")
                        case .peer(let connected): self.report(connected ? "Quest connected · Talk is off" : "Quest disconnected")
                        case .audio: break
                        }
                    }
                    self.receive(task, generation: generation)
                @unknown default:
                    self.retry(generation: generation)
                }
            }
        }
    }

    private func schedulePing(_ task: URLSessionWebSocketTask, generation: UUID) {
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.generation == generation, self.socket === task else { return }
            task.sendPing { error in
                self.queue.async {
                    guard self.generation == generation, self.socket === task else { return }
                    if error != nil {
                        self.retry(generation: generation)
                    } else {
                        self.schedulePing(task, generation: generation)
                    }
                }
            }
        }
        pingWork = work
        queue.asyncAfter(deadline: .now() + 10, execute: work)
    }

    private func retry(generation: UUID) {
        guard self.generation == generation else { return }
        closeSocket()
        core.disconnected()
        audioSendsInFlight = 0
        report("Intercom disconnected · retrying")
        let delay = retryDelay
        retryDelay = min(2, retryDelay * 2)
        let work = DispatchWorkItem { [weak self] in self?.connect(generation: generation) }
        retryWork = work
        queue.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func closeSocket() {
        retryWork?.cancel()
        retryWork = nil
        pingWork?.cancel()
        pingWork = nil
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
        session?.invalidateAndCancel()
        session = nil
    }

    private func sendCapturedAudio(_ data: Data) {
        queue.async { [weak self] in
            guard let self, self.core.isTalking, self.audioSendsInFlight < 4,
                  let socket = self.socket, socket.state == .running else { return }
            let generation = self.generation
            self.audioSendsInFlight += 1
            socket.send(.data(data)) { [weak self] error in
                guard let self else { return }
                self.queue.async {
                    guard self.generation == generation, self.socket === socket else { return }
                    self.audioSendsInFlight = max(0, self.audioSendsInFlight - 1)
                    if error != nil { self.retry(generation: generation) }
                }
            }
        }
    }

    private func report(_ message: String) {
        DispatchQueue.main.async { [weak self] in self?.onStatus?(message) }
    }
}

private final class IPhoneIntercomPCMOutput: IntercomPCMOutput {
    private let queue: DispatchQueue
    private let report: (String) -> Void
    private let lock = NSLock()
    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var outputFormat: AVAudioFormat?
    private var converter: AVAudioConverter?
    private var capture: ((Data) -> Void)?
    private var attempt = UUID()
    private var accumulator = IntercomFrameAccumulator()
    private var window = IntercomScheduledWindow(capacity: 4)

    init(queue: DispatchQueue, report: @escaping (String) -> Void) {
        self.queue = queue
        self.report = report
    }

    func start(onCapture: @escaping (Data) -> Void, onUnavailable: @escaping () -> Void) {
        guard engine == nil else { return }
        let id = UUID()
        attempt = id
        capture = onCapture
        accumulator.reset()
        let permission = AVAudioApplication.shared.recordPermission
        do {
            try configureSpeaker(id: id)
        } catch {
            stop()
            onUnavailable()
            return
        }
        switch permission {
        case .denied:
            onUnavailable()
        case .undetermined:
            AVAudioApplication.requestRecordPermission { [weak self] granted in
                guard let self else { return }
                self.queue.async {
                    guard self.attempt == id else { return }
                    if granted { self.configureMicrophone(id: id, onUnavailable: onUnavailable) }
                    else { onUnavailable() }
                }
            }
        case .granted:
            configureMicrophone(id: id, onUnavailable: onUnavailable)
        @unknown default:
            onUnavailable()
        }
    }

    private func configureSpeaker(id: UUID) throws {
        guard attempt == id, engine == nil else { return }
        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.playAndRecord, mode: .voiceChat,
                                     options: [.defaultToSpeaker])
        try audioSession.setActive(true)
        try audioSession.overrideOutputAudioPort(.speaker)
        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()
        let input = engine.inputNode
        try input.setVoiceProcessingEnabled(true)
        let inputFormat = input.outputFormat(forBus: 0)
        let outputFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                         sampleRate: Double(IntercomPCMFrame.sampleRate),
                                         channels: 1, interleaved: false)!
        let mutedMic = AVAudioMixerNode()
        engine.attach(player)
        engine.attach(mutedMic)
        engine.connect(player, to: engine.mainMixerNode, format: outputFormat)
        engine.connect(input, to: mutedMic, format: inputFormat)
        engine.connect(mutedMic, to: engine.mainMixerNode, format: inputFormat)
        mutedMic.outputVolume = 0
        self.engine = engine
        self.player = player
        self.outputFormat = outputFormat
        try engine.start()
        player.play()
        report("iPhone speaker ready · requesting microphone")
    }

    private func configureMicrophone(id: UUID, onUnavailable: @escaping () -> Void) {
        guard attempt == id, let engine else { return }
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        let outputFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                         sampleRate: Double(IntercomPCMFrame.sampleRate),
                                         channels: 1, interleaved: false)!
        guard let converter = AVAudioConverter(from: inputFormat, to: outputFormat) else {
            onUnavailable(); return
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { [weak self] buffer, _ in
            self?.capture(buffer, converter: converter, outputFormat: outputFormat)
        }
        self.converter = converter
        report("Two-way audio active")
    }

    private func capture(_ buffer: AVAudioPCMBuffer, converter: AVAudioConverter, outputFormat: AVAudioFormat) {
        guard let converted = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: 1024) else { return }
        var conversionError: NSError?
        var supplied = false
        let status = converter.convert(to: converted, error: &conversionError) { _, inputStatus in
            if supplied { inputStatus.pointee = .noDataNow; return nil }
            supplied = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, conversionError == nil, let samples = converted.int16ChannelData?.pointee else { return }
        let count = Int(converted.frameLength)
        let frames: [Data]
        let send: ((Data) -> Void)?
        lock.lock()
        frames = accumulator.append(Array(UnsafeBufferPointer(start: samples, count: count)))
        send = capture
        lock.unlock()
        for frame in frames { send?(frame) }
    }

    func enqueue(_ frame: IntercomPCMFrame) {
        guard engine != nil, let player, let outputFormat,
              let buffer = AVAudioPCMBuffer(pcmFormat: outputFormat,
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
    }

    func stop() {
        attempt = UUID()
        lock.lock()
        capture = nil
        accumulator.reset()
        lock.unlock()
        engine?.inputNode.removeTap(onBus: 0)
        player?.stop()
        player?.reset()
        engine?.stop()
        window.reset()
        player = nil
        outputFormat = nil
        converter = nil
        engine = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}
#endif
