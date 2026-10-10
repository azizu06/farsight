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
    @State private var bridgeRole: PhoneBridgeRole = .off
    @AppStorage("bridgeLocalTailIP") private var localTailIP = ""
    @AppStorage("bridgeRoverPeerIP") private var roverPeerIP = ""
    @AppStorage("bridgeJetsonUSBIP") private var jetsonUSBIP = "172.20.10.3"
    @AppStorage("jetsonEndpoint") private var endpoint = "ws://172.20.10.2:8443"
    @State private var rgbOnly = true
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
                        bridgeAddress("Rover phone's Tailscale IPv4", text: $roverPeerIP)
                        Text("Listen only on 127.0.0.1:8081 → rover phone:8081. Mac access requires tested USB/iproxy forwarding.")
                            .font(.caption)
                    }
                    Button(bridge.running ? "Stop bridge" : "Start bridge") {
                        if bridge.running { bridge.stop() }
                        else { bridge.start(role: bridgeRole, localTailIP: localTailIP,
                                            roverPeerIP: roverPeerIP, jetsonUSBIP: jetsonUSBIP) }
                        updateIdleTimer()
                    }.buttonStyle(.borderedProminent).tint(bridge.running ? .red : .green)
                    Text(bridge.status).font(.callout)
                    Text(bridge.counters).font(.caption.monospaced())
                    Text("PHONE VPN / USB BRIDGE UNQUALIFIED. Keep this app foreground. Backgrounding stops both bridge and capture.")
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
                }
                Button(capture.running ? "Stop" : "Start capture") {
                    if capture.running { capture.stop() }
                    else if bridgeRole == .rover {
                        do {
                            _ = try PhoneBridgeConfig.make(role: .rover, localTailIP: localTailIP,
                                roverPeerIP: roverPeerIP, jetsonUSBIP: jetsonUSBIP)
                            let host = jetsonUSBIP.trimmingCharacters(in: .whitespacesAndNewlines)
                            capture.start(endpoint: "ws://\(host):8443", rgbOnly: rgbOnly)
                        } catch { capture.status = error.localizedDescription }
                    } else { capture.start(endpoint: endpoint, rgbOnly: rgbOnly) }
                    updateIdleTimer()
                }
                .buttonStyle(.borderedProminent)
                .tint(capture.running ? .red : .green)
                Text(capture.source).font(.caption.bold())
                Text(capture.status).font(.callout)
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
                bridge.stop()
            }
            updateIdleTimer()
        }
        .onChange(of: capture.running) { _, _ in updateIdleTimer() }
        .onChange(of: bridge.running) { _, _ in updateIdleTimer() }
        .onChange(of: bridgeRole) { _, role in
            if role == .operator && capture.running { capture.stop() }
            updateIdleTimer()
        }
        .onDisappear {
            if capture.running { capture.stop() }
            bridge.stop()
            UIApplication.shared.isIdleTimerDisabled = false
        }
    }

    private func updateIdleTimer() {
        UIApplication.shared.isIdleTimerDisabled = phase != .background && (capture.running || bridge.running)
    }

    private func bridgeAddress(_ label: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading) {
            Text(label).font(.caption)
            TextField(label, text: text).textInputAutocapitalization(.never)
                .autocorrectionDisabled().keyboardType(.numbersAndPunctuation)
                .textFieldStyle(.roundedBorder).disabled(bridge.running || capture.running)
        }
    }
}
