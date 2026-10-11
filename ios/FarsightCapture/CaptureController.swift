import ARKit
import AVFoundation
import Combine
import CoreImage
import CoreMedia
import Foundation
import Network
import UIKit

// Camera/control transport is serialized on its capture queue; geometry
// compression and streaming use bounded independent workers. Published values
// are presentation only. There is never more than one camera frame send active.
final class CaptureController: NSObject, ObservableObject, ARSessionDelegate, AVCaptureVideoDataOutputSampleBufferDelegate {
    @Published var running = false
    @Published var status = "Ready. Connect the rover phone to the Jetson over USB."
    @Published var source = "Not streaming"
    @Published var tracking = "not_available"
    @Published var sent = 0
    @Published var skipped = 0
    @Published var preview: UIImage?
    @Published var resolvedEndpoint = ""
    @Published var geometryStatus = "Phone 3D disabled"
    @Published var clockDiagnostic = "Capture clock not checked"
    @Published var intercomStatus = "Talk off"

    let supportsMesh = ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)
    let supportsDepth = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
    private var capture = ARSession()
    private let rgbCapture = AVCaptureSession()
    private var rgbOutput: AVCaptureVideoDataOutput?
    private var permitGate = CapturePermitGate()
    private var capturePermitsNegotiated = false
    private var lastDiagnosticAt = -Double.infinity
    private var cameraObservers: [NSObjectProtocol] = []
    private let queue = DispatchQueue(label: "farsight.capture", qos: .userInitiated)
    private let context = CIContext(options: [.cacheIntermediates: false])
    private let network = URLSession(configuration: .ephemeral)
    private let intercomPlayback = IntercomPlayback()
    private var socket: URLSessionWebSocketTask?
    private var generation: UUID?
    private var uiGeneration: UUID?
    private var uiAttempt: UUID?
    private struct Request { let intent: UUID; let url: URL; let depth: Bool; let device: String; let meshHost: String? }
    private var request: Request?
    private var reconnect = CaptureReconnect()
    private let admission = CaptureIntentAdmission()
    private let geometryFeed = PhoneGeometryFeed()
    private var geometryReady = false
    private var geometryRetry: DispatchWorkItem?
    private lazy var geometryServer: PhoneGeometryServer = {
        let server = PhoneGeometryServer(status: { [weak self] in
            guard let self else { return ["geometry": true, "protocol": 1, "ready": false] }
            return self.queue.sync {
                var packet = self.geometryFeed.status()
                let allowed = self.request.map { self.admission.withIntent($0.intent, { true }) == true } ?? false
                packet["ready"] = allowed && self.generation != nil && self.geometryReady && self.withDepth
                return packet
            }
        }, subscribe: { [weak self] in self?.geometryFeed.snapshot() ?? [] })
        server.onStateTagged = { [weak self] id, state, port in
            self?.queue.async { [weak self] in
                guard let self, self.generation == id else { return }
                self.geometryReady = state == "ready"
                self.publish(id) { $0.geometryStatus = state == "ready" ? "Phone-built mesh · port \(port)" : "Phone mesh: \(state)" }
                if state.hasPrefix("failed"), let host = self.request?.meshHost, let local = IPv4Address(host) {
                    let work = DispatchWorkItem { [weak self] in
                        guard let self, self.generation == id, let request = self.request,
                              self.admission.withIntent(request.intent, { true }) == true else { return }
                        self.geometryServer.start(local: local, context: id)
                    }
                    self.geometryRetry = work; self.queue.asyncAfter(deadline: .now() + 1, execute: work)
                }
            }
        }
        return server
    }()
    private var permitWatchdog: DispatchWorkItem?
    private var retryWork: DispatchWorkItem?
    private var sessionID = ""
    private var withDepth = false
    private var helloSent = false
    private var inFlight = false
    private var cadence = CaptureCadence()
    private var frameID = 0
    private var sentCount = 0
    private var skippedCount = 0
    private var timeout: DispatchWorkItem?

    override init() {
        super.init()
        intercomPlayback.onStatus = { [weak self] value in self?.intercomStatus = value }
        capture.delegate = self
        capture.delegateQueue = queue
        geometryFeed.onRecord = { [weak self] session, packet in
            self?.queue.async { [weak self] in
                guard let self, self.sessionID == session.uuidString, self.generation != nil, let request = self.request,
                      self.admission.withIntent(request.intent, { true }) == true else { return }
                self.geometryServer.publish(packet)
            }
        }
    }

    private func observeRGBCamera(id: UUID) {
        for name in [Notification.Name.AVCaptureSessionWasInterrupted, .AVCaptureSessionRuntimeError] {
            cameraObservers.append(NotificationCenter.default.addObserver(forName: name, object: rgbCapture, queue: nil) { [weak self] note in
                let error = (note.userInfo?[AVCaptureSessionErrorKey] as? NSError)?.localizedDescription
                self?.queue.async { [weak self] in
                    guard let self, !self.withDepth, self.generation == id else { return }
                    self.fail(error.map { "RGB camera: \($0)" } ?? "RGB camera interrupted", id: id, retryable: false)
                }
            })
        }
    }

    deinit { for observer in cameraObservers { NotificationCenter.default.removeObserver(observer) } }

    func start(endpoint: String, rgbOnly: Bool, meshHost: String? = nil) {
        guard !running else { return }
        let depth = supportsDepth && !rgbOnly
        guard !depth || ARWorldTrackingConfiguration.isSupported else {
            status = "ARKit camera capture is unavailable on this device. Use a real iPhone."
            return
        }
        guard let url = WirePacket.endpoint(endpoint, depth: depth) else {
            status = "Enter ws://JETSON_USB_IP:8443 (or a trusted wss:// address)."
            return
        }
        let geometryHost = depth && supportsMesh ? meshHost?.trimmingCharacters(in: .whitespacesAndNewlines) : nil
        if let geometryHost, IPv4Address(geometryHost) == nil {
            status = "Enter the rover phone Tailscale IPv4 to share its mesh."; return
        }
        let id = UUID()
        admission.start(id)
        uiGeneration = id
        uiAttempt = nil
        running = true
        status = "Requesting camera permission…"
        resolvedEndpoint = url.absoluteString
        source = depth ? "IPHONE LIDAR" : "PHONE RGB — no measured depth"
        sent = 0
        skipped = 0
        UIApplication.shared.isIdleTimerDisabled = true
        let deviceName = UIDevice.current.model
        AVCaptureDevice.requestAccess(for: .video) { [weak self] allowed in
            guard let self else { return }
            DispatchQueue.main.async {
                guard self.uiGeneration == id else { return }
                if !allowed {
                    self.stop()
                    self.status = "Camera permission denied. Enable it in Settings → Farsight Capture."
                    return
                }
                self.status = "Connecting to \(url.host ?? "Jetson")…"
                self.queue.async {
                    let request = Request(intent: id, url: url, depth: depth, device: deviceName, meshHost: geometryHost)
                    self.request = request; self.reconnect.start(id); self.connect(request)
                }
            }
        }
    }

    func stop() {
        admission.stop()
        uiGeneration = nil; uiAttempt = nil
        running = false
        geometryStatus = "Phone mesh stopped"
        status = "Stopped. Rover video freshness will expire and motors will stop."
        UIApplication.shared.isIdleTimerDisabled = false
        queue.async { [weak self] in
            self?.request = nil; self?.reconnect.stop(); self?.disconnect()
        }
    }

    private func publish(_ id: UUID, _ change: @escaping (CaptureController) -> Void) {
        guard let intent = request?.intent else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self, self.uiGeneration == intent, self.uiAttempt == id else { return }
            change(self)
        }
    }

    private func connect(_ request: Request) {
        guard admission.withIntent(request.intent, { true }) == true else { return }
        guard let id = reconnect.beginAttempt(for: request.intent) else { return }
        disconnect()
        generation = id
        let url = request.url, depth = request.depth, device = request.device
        DispatchQueue.main.async { [weak self] in
            guard let self, self.uiGeneration == request.intent else { return }
            self.uiAttempt = id; self.preview = nil; self.tracking = "not_available"
            self.sent = 0; self.skipped = 0; self.clockDiagnostic = "Capture clock not checked"
            self.geometryStatus = request.meshHost == nil ? "Phone 3D disabled" : "Starting phone 3D…"
            self.status = "Connecting to \(url.host ?? "Jetson")…"
        }
        sessionID = UUID().uuidString
        permitGate.start(sessionID: sessionID, mapEpoch: 1)
        capturePermitsNegotiated = false
        lastDiagnosticAt = -.infinity
        withDepth = depth
        helloSent = false
        inFlight = false
        cadence = CaptureCadence()
        frameID = 0
        sentCount = 0
        skippedCount = 0
        if let host = request.meshHost, let local = IPv4Address(host), let session = UUID(uuidString: sessionID) {
            geometryFeed.begin(session: session)
            geometryServer.start(local: local, context: id)
        }
        if depth {
            capture = ARSession(); capture.delegate = self; capture.delegateQueue = queue
            let configuration = ARWorldTrackingConfiguration()
            configuration.worldAlignment = .gravity
            configuration.frameSemantics = [.sceneDepth]
            if request.meshHost != nil { configuration.sceneReconstruction = .mesh }
            guard admission.withIntent(request.intent, { capture.run(configuration, options: [.resetTracking, .removeExistingAnchors]); return true }) == true else { disconnect(); return }
        } else {
            observeRGBCamera(id: id)
            do { try startRGBCapture() }
            catch { fail("RGB camera: \(error.localizedDescription)", id: id, retryable: false); return }
        }
        var socketRequest = URLRequest(url: url)
        socketRequest.timeoutInterval = 5
        let task = network.webSocketTask(with: socketRequest)
        task.maximumMessageSize = 65_536
        socket = task
        guard admission.withIntent(request.intent, { task.resume(); receive(task, id: id); return true }) == true else { disconnect(); return }
        intercomPlayback.beginAttempt(from: url)
        do {
            let hello: [String: Any] = ["type": "hello", "version": 1,
                "session_id": sessionID, "map_epoch": 1, "device": device,
                "supports_scene_depth": depth, "supports_mesh": request.meshHost != nil, "supports_capture_permits": true,
                "geometry_transport": request.meshHost != nil ? "separate_mesh_v1" : "none"]
            let json = try JSONSerialization.data(withJSONObject: hello)
            let text = String(decoding: json, as: UTF8.self)
            guard admission.withIntent(request.intent, {
            task.send(.string(text)) { [weak self] error in
                self?.queue.async { [weak self] in
                    guard let self, self.generation == id else { return }
                    if let error { self.fail(error.localizedDescription, id: id); return }
                    self.helloSent = true
                    self.publish(id) { $0.status = "Streaming locally over the selected connection" }
                }
            }
            return true
            }) == true else { disconnect(); return }
            armPermitWatchdog(id: id)
            armTimeout(id: id, seconds: 5, message: "Connection timed out. Verify the Jetson USB IP and server.")
        } catch { fail(error.localizedDescription, id: id) }
    }

    private func receive(_ task: URLSessionWebSocketTask, id: UUID) {
        task.receive { [weak self] result in
            let receivedAt = Self.hostTime()
            self?.queue.async { [weak self] in
                guard let self, self.generation == id else { return }
                switch result {
                case .failure(let error): self.fail("Connection closed: \(error.localizedDescription)", id: id)
                case .success(let message):
                    if case .string(let text) = message {
                        if let data = text.data(using: .utf8),
                           let message = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                           message["type"] as? String == "capture_permit" {
                            if self.permitGate.receive(message, at: receivedAt) {
                                self.capturePermitsNegotiated = true
                            } // Wrong-session/malformed permits never become current.
                        } else {
                            self.publish(id) { $0.status = "Jetson: \(text.prefix(180))" }
                        }
                    }
                    self.receive(task, id: id)
                }
            }
        }
    }

    private func armTimeout(id: UUID, seconds: Double, message: String) {
        timeout?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.generation == id else { return }
            self.fail(message, id: id)
        }
        timeout = work
        queue.asyncAfter(deadline: .now() + seconds, execute: work)
    }

    private func armPermitWatchdog(id: UUID) {
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.generation == id else { return }
            if self.capturePermitsNegotiated, let permit = self.permitGate.permit,
               Self.hostTime() - permit.receivedAt >= 1 {
                self.fail("Jetson capture permits stopped; replacing the connection", id: id)
                return
            }
            self.armPermitWatchdog(id: id)
        }
        permitWatchdog = work
        queue.asyncAfter(deadline: .now() + 0.25, execute: work)
    }

    private func disconnect() {
        intercomPlayback.disconnect()
        geometryRetry?.cancel(); geometryRetry = nil; geometryReady = false
        geometryServer.stop(); geometryFeed.stop()
        permitWatchdog?.cancel(); permitWatchdog = nil
        retryWork?.cancel(); retryWork = nil
        timeout?.cancel()
        timeout = nil
        generation = nil
        helloSent = false
        inFlight = false
        permitGate.clear()
        capturePermitsNegotiated = false
        capture.delegate = nil; capture.pause()
        for observer in cameraObservers { NotificationCenter.default.removeObserver(observer) }
        cameraObservers.removeAll()
        rgbOutput?.setSampleBufferDelegate(nil, queue: nil); rgbOutput = nil
        if rgbCapture.isRunning { rgbCapture.stopRunning() }
        socket?.cancel(with: .normalClosure, reason: nil)
        socket = nil
    }

    private func fail(_ message: String, id: UUID, retryable: Bool = true) {
        guard generation == id else { return }
        let ticket = reconnect.failed(id)
        disconnect()
        guard retryable, let ticket, let request else {
            publish(id) {
                $0.running = false; $0.preview = nil; $0.tracking = "not_available"
                $0.status = message + " Press Start after correcting the camera error."
                UIApplication.shared.isIdleTimerDisabled = false
            }
            if let intent = self.request?.intent { admission.revoke(intent) }
            self.request = nil; reconnect.stop()
            return
        }
        publish(id) {
            $0.preview = nil; $0.tracking = "not_available"
            $0.status = message + " Reconnecting in \(ticket.delay)s; Stop cancels."
        }
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.reconnect.mayRetry(ticket) else { return }
            self.connect(request)
        }
        retryWork = work
        queue.asyncAfter(deadline: .now() + ticket.delay, execute: work)
    }

    private static func hostTime() -> Double {
        CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))
    }

    private func startRGBCapture() throws {
        rgbCapture.beginConfiguration()
        defer { rgbCapture.commitConfiguration() }
        for output in rgbCapture.outputs { rgbCapture.removeOutput(output) }
        for input in rgbCapture.inputs { rgbCapture.removeInput(input) }
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
            throw NSError(domain: "FarsightCamera", code: 4, userInfo: [NSLocalizedDescriptionKey: "Rear RGB camera unavailable"])
        }
        let input = try AVCaptureDeviceInput(device: device)
        guard rgbCapture.canAddInput(input) else { throw NSError(domain: "FarsightCamera", code: 5) }
        rgbCapture.addInput(input)
        if rgbCapture.canSetSessionPreset(.vga640x480) { rgbCapture.sessionPreset = .vga640x480 }
        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        output.setSampleBufferDelegate(self, queue: queue)
        guard rgbCapture.canAddOutput(output) else { throw NSError(domain: "FarsightCamera", code: 6) }
        rgbCapture.addOutput(output)
        rgbOutput = output
        if let connection = output.connection(with: .video), connection.isVideoOrientationSupported {
            connection.videoOrientation = .landscapeRight
        }
        // startRunning must occur after commitConfiguration and off the main queue.
        let id = generation, intent = request?.intent
        queue.async { [weak self] in
            guard let self, let id, self.generation == id, self.rgbOutput === output, !self.withDepth else { return }
            guard let intent else { return }
            _ = self.admission.withIntent(intent) { self.rgbCapture.startRunning() }
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        let callbackHost = Self.hostTime()
        guard !withDepth, output === rgbOutput, let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        guard let clock = rgbCapture.synchronizationClock else {
            if let id = generation { publish(id) { $0.clockDiagnostic = "RGB synchronization clock unavailable; frames blocked" } }
            return
        }
        let raw = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let converted = CMSyncConvertTime(raw, from: clock, to: CMClockGetHostTimeClock())
        guard raw.isNumeric, converted.isNumeric else {
            if let id = generation { publish(id) { $0.clockDiagnostic = "RGB clock conversion unavailable; frames blocked" } }
            return
        }
        sendFrame(pixels: pixels, captureTime: CMTimeGetSeconds(converted),
                  rawTimestamp: CMTimeGetSeconds(raw), callbackHost: callbackHost,
                  clock: "avcapture_sync_to_host", frame: nil)
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard session === capture else { return }
        guard withDepth else { return }
        sendFrame(pixels: frame.capturedImage, captureTime: frame.timestamp,
                  rawTimestamp: frame.timestamp, callbackHost: Self.hostTime(),
                  clock: "arkit_unverified", frame: frame)
    }

    private func reportClock(id: UUID, captureTime: Double, hostTime: Double, message: String) {
        guard hostTime - lastDiagnosticAt >= 0.25 else { return }
        lastDiagnosticAt = hostTime
        let detail = String(format: "%@ · sensor %.6f · host %.6f · delta %.1f ms",
                            message, captureTime, hostTime, (hostTime - captureTime) * 1000)
        publish(id) { $0.clockDiagnostic = detail }
    }

    private func sendFrame(pixels: CVPixelBuffer, captureTime: Double, rawTimestamp: Double,
                           callbackHost: Double, clock: String, frame: ARFrame?) {
        let producerStarted = ContinuousClock.now
        guard let id = generation, helloSent, let task = socket else { return }
        let documentedClock = clock == "avcapture_sync_to_host"
        let plausible = documentedClock && permitGate.observe(captureTime: captureTime, hostTime: callbackHost)
        let selectedPermit = documentedClock ?
            permitGate.eligible(captureTime: captureTime, hostTime: callbackHost, clockQualified: true) : permitGate.permit
        let verifiedClock = documentedClock && plausible
        reportClock(id: id, captureTime: captureTime, hostTime: callbackHost,
                    message: documentedClock ? permitGate.diagnostic : "ARKit origin unverified · view only")
        if capturePermitsNegotiated && documentedClock && selectedPermit == nil {
            skippedCount += 1
            return
        }
        if inFlight {
            skippedCount += 1
            return
        }
        let fps = withDepth ? CaptureCadence.depthFPS : CaptureCadence.rgbFPS
        guard cadence.isDue(at: captureTime) else { return }
        if withDepth && frame?.sceneDepth == nil {
            publish(id) { $0.status = "Waiting for same-frame LiDAR depth; no synthetic depth is sent" }
            return
        }
        cadence.didCapture(at: captureTime, fps: fps)
        inFlight = true
        do {
            let image = CIImage(cvPixelBuffer: pixels)
            let scale = min(640 / image.extent.width, 1)
            let width = Int((image.extent.width * scale).rounded())
            let height = Int((image.extent.height * scale).rounded())
            let resized = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            guard let cgImage = context.createCGImage(resized,
                   from: CGRect(x: 0, y: 0, width: width, height: height)),
                  let jpeg = UIImage(cgImage: cgImage).jpegData(compressionQuality: 0.5) else {
                throw NSError(domain: "FarsightCamera", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "Camera JPEG encoding failed"])
            }
            let state = frame.map { trackingState($0.camera.trackingState) } ?? "not_available"
            var header: [String: Any] = ["version": 1, "type": withDepth ? "frame" : "rgb_frame",
                "session_id": sessionID, "frame_id": frameID, "t_capture": captureTime, "map_epoch": 1,
                "t_wall_ms": Int64(Date().timeIntervalSince1970 * 1000)]
            var payloads = [jpeg]
            if let frame, let scene = frame.sceneDepth, withDepth {
                let dw = CVPixelBufferGetWidth(scene.depthMap)
                let dh = CVPixelBufferGetHeight(scene.depthMap)
                guard CVPixelBufferGetPixelFormatType(scene.depthMap) == kCVPixelFormatType_DepthFloat32,
                      let confidence = scene.confidenceMap,
                      CVPixelBufferGetPixelFormatType(confidence) == kCVPixelFormatType_OneComponent8,
                      CVPixelBufferGetWidth(confidence) == dw,
                      CVPixelBufferGetHeight(confidence) == dh,
                      abs(Double(width) / Double(height) / (Double(dw) / Double(dh)) - 1) < 0.01 else {
                    throw NSError(domain: "FarsightCamera", code: 2,
                         userInfo: [NSLocalizedDescriptionKey: "Depth/RGB calibration aspect mismatch"])
                }
                let depthData = try rows(scene.depthMap, bytesPerPixel: 4)
                let confidenceData = try rows(confidence, bytesPerPixel: 1)
                let k = frame.camera.intrinsics
                let sx = Float(width) / Float(CVPixelBufferGetWidth(frame.capturedImage))
                let sy = Float(height) / Float(CVPixelBufferGetHeight(frame.capturedImage))
                let intrinsics: [Float] = [k[0][0] * sx, 0, 0, 0, k[1][1] * sy, 0,
                                          k[2][0] * sx, k[2][1] * sy, 1]
                let transform = frame.camera.transform
                let matrix = (0..<4).flatMap { column in (0..<4).map { row in transform[column][row] } }
                header["map_epoch"] = 1
                header["tracking"] = state
                header["transform"] = matrix
                header["image"] = ["width": width, "height": height, "jpeg_len": jpeg.count,
                                     "intrinsics": intrinsics, "orientation": "landscape_right"]
                header["depth"] = ["width": dw, "height": dh, "format": "float32_m", "len": depthData.count]
                header["confidence"] = ["width": dw, "height": dh, "format": "uint8_0_2", "len": confidenceData.count]
                payloads += [depthData, confidenceData]
            } else {
                header["jpeg_len"] = jpeg.count
                header["width"] = width
                header["height"] = height
                header["tracking"] = state
            }
            // Producer encoding interval is separate from sensor clock diagnostics.
            let elapsed = producerStarted.duration(to: ContinuousClock.now).components
            header["producer_delay_ms"] = max(0, (Double(elapsed.seconds)
                + Double(elapsed.attoseconds) / 1_000_000_000_000_000_000) * 1000)
            let finishedHost = Self.hostTime()
            let bound = verifiedClock && selectedPermit != nil
            header["capture_clock"] = clock
            header["capture_clock_verified"] = verifiedClock
            header["capture_bound"] = bound
            header["sensor_timestamp_raw_t"] = rawTimestamp
            header["callback_host_t"] = callbackHost
            header["sensor_host_delta_ms"] = (callbackHost - captureTime) * 1000
            if clock == "avcapture_sync_to_host" {
                header["sensor_timestamp_host_t"] = captureTime
                header["sensor_age_ms"] = (finishedHost - captureTime) * 1000
            }
            if let selectedPermit {
                header["capture_permit"] = selectedPermit.token
                header["capture_permit_received_t"] = selectedPermit.receivedAt
            }
            let packet = try WirePacket.bundle(header: header, payloads: payloads)
            if bound && !CapturePermitGate.validAtSend(selectedPermit!, captureTime: captureTime, hostTime: Self.hostTime()) {
                inFlight = false; skippedCount += 1
                reportClock(id: id, captureTime: captureTime, hostTime: Self.hostTime(), message: "Encoding exhausted capture permit; frame skipped")
                return
            }
            frameID += 1
            armTimeout(id: id, seconds: 0.5, message: "Frame send stalled for 500 ms; closed to discard stale frames.")
            guard let intent = request?.intent else { inFlight = false; return }
            guard admission.withIntent(intent, {
            task.send(.data(packet)) { [weak self] error in
                self?.queue.async { [weak self] in
                    guard let self, self.generation == id else { return }
                    self.timeout?.cancel()
                    self.inFlight = false
                    if let error { self.fail(error.localizedDescription, id: id); return }
                    self.reconnect.healthy(id)
                    self.sentCount += 1
                    let sent = self.sentCount, skipped = self.skippedCount
                    let depthMode = self.withDepth
                    self.publish(id) {
                        $0.sent = sent
                        $0.skipped = skipped
                        $0.tracking = state
                        $0.preview = UIImage(cgImage: cgImage)
                        $0.status = bound ? "Permit-bound RGB streaming — \(Int(fps)) fps target" :
                            depthMode ? "LiDAR streaming · AR clock unverified" : "Legacy view only · server capture permits unavailable"
                    }
                }
            }
            return true
            }) == true else { inFlight = false; return }
        } catch {
            inFlight = false
            fail(error.localizedDescription, id: id, retryable: false)
        }
    }

    private func rows(_ buffer: CVPixelBuffer, bytesPerPixel: Int) throws -> Data {
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else {
            throw NSError(domain: "FarsightCamera", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "Sensor buffer is unavailable"])
        }
        let height = CVPixelBufferGetHeight(buffer)
        let width = CVPixelBufferGetWidth(buffer)
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        var data = Data(capacity: width * height * bytesPerPixel)
        for row in 0..<height {
            // iPhone ARM stores Float32 little-endian. Strip CV buffer padding.
            data.append(base.advanced(by: row * stride).assumingMemoryBound(to: UInt8.self),
                        count: width * bytesPerPixel)
        }
        return data
    }

    private func trackingState(_ state: ARCamera.TrackingState) -> String {
        switch state {
        case .normal: return "normal"
        case .limited: return "limited"
        case .notAvailable: return "not_available"
        }
    }

    // Copy SDK-owned mesh buffers before returning from the delegate callback.
    // Compression and dirty-anchor coalescing happen on the feed worker queue.
    private func ingestMesh(_ anchors: [ARAnchor], session: ARSession, removing: Bool = false) {
        guard session === capture, withDepth, request?.meshHost != nil,
              generation != nil, let source = UUID(uuidString: sessionID) else { return }
        for anchor in anchors {
            guard let mesh = anchor as? ARMeshAnchor else { continue }
            if removing { geometryFeed.remove(session: source, anchor: mesh.identifier); continue }
            let vertices = mesh.geometry.vertices, faces = mesh.geometry.faces
            guard vertices.format == .float3, vertices.count > 0, vertices.count <= GeometryWire.maxVertices,
                  vertices.offset >= 0, vertices.stride >= 12, vertices.stride <= 256,
                  vertices.offset + (vertices.count - 1) * vertices.stride + 12 <= vertices.buffer.length,
                  faces.indexCountPerPrimitive == 3, faces.count > 0, faces.count <= GeometryWire.maxIndices / 3,
                  faces.bytesPerIndex == 2 || faces.bytesPerIndex == 4,
                  faces.count * 3 * faces.bytesPerIndex <= faces.buffer.length else {
                geometryFeed.reject(session: source); continue
            }
            let base = vertices.buffer.contents().advanced(by: vertices.offset)
            var xyz = [Float](); xyz.reserveCapacity(vertices.count * 3)
            for index in 0..<vertices.count {
                let vertex = base.advanced(by: index * vertices.stride)
                for axis in 0..<3 { xyz.append(vertex.advanced(by: axis * 4).loadUnaligned(as: Float.self)) }
            }
            let indexBase = faces.buffer.contents()
            var indices = [UInt32](); indices.reserveCapacity(faces.count * 3)
            for index in 0..<(faces.count * 3) {
                let pointer = indexBase.advanced(by: index * faces.bytesPerIndex)
                indices.append(faces.bytesPerIndex == 2 ? UInt32(pointer.loadUnaligned(as: UInt16.self)) : pointer.loadUnaligned(as: UInt32.self))
            }
            let transform = (0..<4).flatMap { column in (0..<4).map { row in mesh.transform[column][row] } }
            geometryFeed.replace(session: source, anchor: mesh.identifier, transform: transform, vertices: xyz, indices: indices)
        }
    }
    func session(_ session: ARSession, didAdd anchors: [ARAnchor]) { ingestMesh(anchors, session: session) }
    func session(_ session: ARSession, didUpdate anchors: [ARAnchor]) { ingestMesh(anchors, session: session) }
    func session(_ session: ARSession, didRemove anchors: [ARAnchor]) { ingestMesh(anchors, session: session, removing: true) }

    func session(_ session: ARSession, didFailWithError error: Error) {
        guard session === capture, let id = generation else { return }
        fail("Camera: \(error.localizedDescription)", id: id, retryable: false)
    }

    func sessionWasInterrupted(_ session: ARSession) {
        guard session === capture, let id = generation else { return }
        fail("Camera interrupted", id: id, retryable: false)
    }
}
