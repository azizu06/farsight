import SwiftUI

@main
struct FarsightCaptureApp: App {
    var body: some Scene {
        WindowGroup { CaptureView() }
    }
}

struct CaptureView: View {
    @StateObject private var capture = CaptureController()
    @AppStorage("jetsonEndpoint") private var endpoint = "ws://172.20.10.2:8443"
    @State private var rgbOnly = false
    @Environment(\.scenePhase) private var phase

    var body: some View {
        HStack(spacing: 20) {
            ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("FARSIGHT CAPTURE").font(.title2.bold())
                Text(capture.supportsDepth ? "LiDAR available · calibrated RGB-D" : "RGB camera · no LiDAR on this device")
                    .font(.subheadline).foregroundStyle(.secondary)
                TextField("ws://JETSON_USB_IP:8443", text: $endpoint)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .textFieldStyle(.roundedBorder)
                    .disabled(capture.running)
                if capture.supportsDepth {
                    Toggle("Send RGB only", isOn: $rgbOnly).disabled(capture.running)
                }
                Button(capture.running ? "Stop" : "Start capture") {
                    if capture.running { capture.stop() }
                    else { capture.start(endpoint: endpoint, rgbOnly: rgbOnly) }
                }
                .buttonStyle(.borderedProminent)
                .tint(capture.running ? .red : .green)
                Text(capture.source).font(.caption.bold())
                Text(capture.status).font(.callout)
                if !capture.resolvedEndpoint.isEmpty {
                    Text(capture.resolvedEndpoint).font(.caption.monospaced()).textSelection(.enabled)
                }
                Text("Sent \(capture.sent) frames · skipped \(capture.skipped)\nTracking: \(capture.tracking)")
                    .font(.caption.monospaced())
                Text("Phone USB → Jetson. Keep Personal Hotspot enabled and allow Local Network access. No venue Wi-Fi required.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            }
            .frame(maxWidth: 370, alignment: .leading)
            ZStack {
                RoundedRectangle(cornerRadius: 14).fill(Color.black)
                if let image = capture.preview {
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
            if newPhase == .background && capture.running { capture.stop() }
        }
    }
}
