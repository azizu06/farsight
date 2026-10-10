import SwiftUI

@main
struct FarsightCaptureApp: App {
    var body: some Scene {
        WindowGroup { CaptureView() }
    }
}

struct CaptureView: View {
    @StateObject private var capture = CaptureController()
    @StateObject private var bridge = PhoneBridge()
    @AppStorage("bridgeRole") private var bridgeRole: PhoneBridgeRole = .off
    @AppStorage("bridgeLocalTailIP") private var localTailIP = ""
    @AppStorage("bridgeRoverPeerIP") private var roverPeerIP = ""
    @AppStorage("bridgeFallbackEnabled") private var fallbackEnabled = false
    @AppStorage("bridgeDualRoutes") private var dualRoutes = false
    @AppStorage("bridgeFallbackPeerIP") private var fallbackPeerIP = "100.103.169.40"
    @AppStorage("bridgeJetsonUSBIP") private var jetsonUSBIP = "172.20.10.3"
    @AppStorage("jetsonEndpoint") private var endpoint = "ws://172.20.10.3:8443"
    @AppStorage("rgbOnly") private var rgbOnly = false
    @AppStorage("phoneMeshEnabled") private var phoneMeshEnabled = true
    @AppStorage("operatorGeometryEnabled") private var operatorGeometryEnabled = true
    @Environment(\.scenePhase) private var phase

    var body: some View {
        HStack(spacing: 20) {
            ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("FARSIGHT CAPTURE").font(.title2.bold())
                Picker("Phone bridge role", selection: $bridgeRole) {
                    ForEach(PhoneBridgeRole.allCases) { role in Text(role.title).tag(role) }
                }.disabled(bridge.running || capture.running)
                if bridgeRole != .off {
                    if bridgeRole == .rover {
                        bridgeAddress("This phone's Tailscale IPv4", text: $localTailIP)
                        bridgeAddress("Jetson USB IPv4", text: $jetsonUSBIP)
                        Text("Listen on this phone's tailnet IP:8081 → Jetson USB:8443. Capture can run at the same time.")
                            .font(.caption)
                    } else {
                        bridgeAddress("Primary rover phone's Tailscale IPv4", text: $roverPeerIP)
                        Toggle("Forward phone 3D over USB", isOn: $operatorGeometryEnabled)
                            .disabled(bridge.running || bridge.operatorRecoveryWanted)
                        Toggle("Enable fallback Jetson path", isOn: $fallbackEnabled)
                            .disabled(bridge.running || bridge.operatorRecoveryWanted || dualRoutes)
                        Toggle("Pinned dual routes for host broker", isOn: $dualRoutes)
                            .disabled(bridge.running || bridge.operatorRecoveryWanted)
                        if fallbackEnabled || dualRoutes { bridgeAddress("Fallback Jetson Tailscale IPv4", text: $fallbackPeerIP) }
                        Text(dualRoutes ? "USB phone ports: 8081 pinned primary, 8082 pinned fallback. Host broker selects logical control. No phone racing or replay." : "127.0.0.1:8081 → primary or fallback:8081. Selection happens before bytes are sent. Existing sessions close on failure; they never migrate or replay.")
                            .font(.caption)
                    }
                    Button(bridge.running ? "Stop bridge" : "Start bridge") {
                        if bridge.running { bridge.stop() }
                        else { bridge.start(role: bridgeRole, localTailIP: localTailIP,
                                            roverPeerIP: roverPeerIP, jetsonUSBIP: jetsonUSBIP,
                                            fallbackPeerIP: bridgeRole == .operator && (fallbackEnabled || dualRoutes) ? fallbackPeerIP : nil,
                                            dualRoutes: bridgeRole == .operator && dualRoutes,
                                            geometry: bridgeRole == .operator && operatorGeometryEnabled) }
                        updateIdleTimer()
                    }.buttonStyle(.borderedProminent).tint(bridge.running ? .red : .green)
                    Text(bridge.status).font(.callout)
                    Text(bridge.counters).font(.caption.monospaced())
                    if bridgeRole == .operator && operatorGeometryEnabled { Text(bridge.geometryStatus).font(.caption.monospaced()) }
                    if bridgeRole == .operator && dualRoutes {
                        Text(bridge.primaryRouteStatus).font(.caption.monospaced())
                        Text(bridge.secondaryRouteStatus).font(.caption.monospaced())
                    }
                    if bridge.operatorRecoveryWanted && !bridge.running {
                        Button("Cancel bridge recovery") { bridge.stop(); updateIdleTimer() }
                    }
                    Text("PHONE VPN / USB BRIDGE UNQUALIFIED. Background closes all sessions and capture. Operator listeners recover in foreground. Capture retries network faults while started; background requires Start again. Motion needs a fresh trigger press after a fault.")
                        .font(.caption.bold()).foregroundStyle(.orange)
                }
                if bridgeRole != .operator {
                Text(capture.supportsDepth ? "LiDAR available · calibrated RGB-D" : "RGB camera · no LiDAR on this device")
                    .font(.subheadline).foregroundStyle(.secondary)
                if bridgeRole == .rover {
                    Text("Camera → ws://\(jetsonUSBIP):8443").font(.caption.monospaced())
                } else {
                TextField("ws://JETSON_USB_IP:8443", text: $endpoint)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .textFieldStyle(.roundedBorder)
                    .disabled(capture.running)
                }
                if capture.supportsDepth {
                    Toggle("Send RGB only", isOn: $rgbOnly).disabled(capture.running)
                    if capture.supportsMesh && bridgeRole == .rover {
                        Toggle("Build 3D on this phone", isOn: $phoneMeshEnabled).disabled(capture.running)
                    }
                }
                Button(capture.running ? "Stop" : "Start capture") {
                    if capture.running { capture.stop() }
                    else if bridgeRole == .rover {
                        do {
                            _ = try PhoneBridgeConfig.make(role: .rover, localTailIP: localTailIP,
                                roverPeerIP: roverPeerIP, jetsonUSBIP: jetsonUSBIP)
                            let host = jetsonUSBIP.trimmingCharacters(in: .whitespacesAndNewlines)
                            capture.start(endpoint: "ws://\(host):8443", rgbOnly: rgbOnly,
                                          meshHost: phoneMeshEnabled && !rgbOnly ? localTailIP : nil)
                        } catch { capture.status = error.localizedDescription }
                    } else { capture.start(endpoint: endpoint, rgbOnly: rgbOnly) }
                    updateIdleTimer()
                }
                .buttonStyle(.borderedProminent)
                .tint(capture.running ? .red : .green)
                Text(capture.source).font(.caption.bold())
                Text(capture.status).font(.callout)
                Text(capture.geometryStatus).font(.caption.monospaced())
                Text(capture.clockDiagnostic).font(.caption.monospaced()).foregroundStyle(.secondary)
                if !capture.resolvedEndpoint.isEmpty {
                    Text(capture.resolvedEndpoint).font(.caption.monospaced()).textSelection(.enabled)
                }
                Text("Sent \(capture.sent) frames · skipped \(capture.skipped)\nTracking: \(capture.tracking)")
                    .font(.caption.monospaced())
                Text("Phone USB → Jetson. Keep Personal Hotspot enabled and allow Local Network access. No venue Wi-Fi required.")
                    .font(.caption).foregroundStyle(.secondary)
                }
            }
            }
            .frame(maxWidth: 370, alignment: .leading)
            ZStack {
                RoundedRectangle(cornerRadius: 14).fill(Color.black)
                if bridgeRole == .operator {
                    Text("Operator phone bridge\nCamera not required\n\(bridge.status)").foregroundStyle(.white).multilineTextAlignment(.center).padding()
                } else if let image = capture.preview {
                    Image(uiImage: image).resizable().scaledToFit()
                } else {
                    Text("Live camera preview").foregroundStyle(.white.opacity(0.6))
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 14))
        }
        .padding(24)
        .background(Color(white: 0.09))
        .preferredColorScheme(.dark)
        .onChange(of: phase) { _, newPhase in
            // Permission prompts briefly make the scene inactive; only actual
            // backgrounding should stop the user's requested capture.
            if newPhase == .background {
                if capture.running { capture.stop() }
                bridge.suspendForBackground()
            } else if newPhase == .active {
                bridge.recoverOperatorOnActive(role: bridgeRole)
            }
            updateIdleTimer()
        }
        .onChange(of: capture.running) { _, _ in updateIdleTimer() }
        .onChange(of: bridge.running) { _, _ in updateIdleTimer() }
        .onChange(of: bridgeRole) { _, role in
            bridge.stop()
            if capture.running { capture.stop() }
            updateIdleTimer()
        }
        .onChange(of: dualRoutes) { _, enabled in if enabled { fallbackEnabled = false } }
        .onChange(of: fallbackEnabled) { _, enabled in if enabled { dualRoutes = false } }
        .onDisappear {
            if capture.running { capture.stop() }
            bridge.suspendForBackground()
            UIApplication.shared.isIdleTimerDisabled = false
        }
        .onAppear {
            if phase == .active { bridge.recoverOperatorOnActive(role: bridgeRole) }
            updateIdleTimer()
        }
    }

    private func updateIdleTimer() {
        UIApplication.shared.isIdleTimerDisabled = phase != .background && (capture.running || bridge.running || bridge.operatorRecoveryWanted)
    }

    private func bridgeAddress(_ label: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading) {
            Text(label).font(.caption)
            TextField(label, text: text).textInputAutocapitalization(.never)
                .autocorrectionDisabled().keyboardType(.numbersAndPunctuation)
                .textFieldStyle(.roundedBorder).disabled(bridge.running || bridge.operatorRecoveryWanted || capture.running)
        }
    }
}
