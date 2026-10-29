import UIKit
import CryptoKit

@main final class AppDelegate: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication, configurationForConnecting session: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let configuration = UISceneConfiguration(name: "Default", sessionRole: session.role)
        configuration.delegateClass = SceneDelegate.self
        return configuration
    }
}

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?
    let bridge = BridgeView()

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options: UIScene.ConnectionOptions) {
        guard let scene = scene as? UIWindowScene else { return }
        window = UIWindow(windowScene: scene)
        window?.rootViewController = bridge
        window?.makeKeyAndVisible()
        for context in options.urlContexts { bridge.receive(context.url) }
        #if DEBUG
        // Reproducible synthetic payload tests; only this app's Fixtures directory.
        let args = ProcessInfo.processInfo.arguments
        if let index = args.firstIndex(of: "--usb-bridge-fixture"), index + 1 < args.count {
            let name = args[index + 1]
            if !name.isEmpty && name != "." && name != ".." && !name.contains("/") && !name.contains("\\") {
                let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
                bridge.receive(documents.appendingPathComponent("Fixtures/" + name))
            }
        }
        #endif
    }

    func scene(_ scene: UIScene, openURLContexts contexts: Set<UIOpenURLContext>) {
        for context in contexts { bridge.receive(context.url) }
    }
}

final class BridgeView: UIViewController {
    private let status = UILabel()
    private let details = UILabel()
    private let root = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    private let imports = DispatchQueue(label: "usb-file-bridge.import")
    private var currentID: String?
    private var timer: Timer?
    private let maximumBytes = 64 * 1024 * 1024

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        let title = UILabel()
        title.text = "USB File Bridge"
        title.font = .boldSystemFont(ofSize: 30)
        status.text = "Ready for a shared file"
        status.font = .preferredFont(forTextStyle: .headline)
        status.numberOfLines = 0
        details.numberOfLines = 0
        details.text = "Share a file using Open In → USB File Bridge. Arm the Mac receiver first.\n\nOne file, up to 64 MiB. No clipboard access or Handoff changes."
        let stack = UIStackView(arrangedSubviews: [title, status, details])
        stack.axis = .vertical
        stack.spacing = 24
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 40),
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -24)
        ])
        do {
            let pointer = root.appendingPathComponent("latest.json")
            let previous = (try? Data(contentsOf: pointer)).flatMap {
                try? JSONSerialization.jsonObject(with: $0) as? [String: Any]
            }
            if previous?["version"] as? Int != 1 {
                try writeJSON(["version": 1, "id": NSNull()], to: pointer)
            }
        } catch {
            status.text = "Inbox initialization failed"
            details.text = error.localizedDescription
        }
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.refreshReceipt()
        }
    }

    deinit { timer?.invalidate() }

    private func writeJSON(_ value: [String: Any], to url: URL) throws {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    private func refreshReceipt() {
        guard let id = currentID,
              let data = try? Data(contentsOf: root.appendingPathComponent("receipt.json")),
              let receipt = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              receipt["id"] as? String == id else { return }
        status.text = "Received and verified on Mac"
        details.text = "\(receipt["name"] as? String ?? "File")\n\nUSB transfer complete. System clipboard settings were not changed."
    }

    func receive(_ url: URL) {
        loadViewIfNeeded()
        guard url.isFileURL else { status.text = "Only file URLs are accepted"; return }
        status.text = "Preparing file…"
        let scoped = url.startAccessingSecurityScopedResource()
        imports.async { [self] in
            defer { if scoped { url.stopAccessingSecurityScopedResource() } }
            let id = UUID().uuidString
            let directory = root.appendingPathComponent("Transfers/" + id, isDirectory: true)
            do {
                let name = url.lastPathComponent
                guard !name.isEmpty, name.utf8.count <= 240,
                      !name.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }),
                      !name.contains("\\") else { throw CocoaError(.fileReadInvalidFileName) }
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                let payload = directory.appendingPathComponent("payload")
                var failure: Error?
                var manifest: [String: Any]?
                var coordinatorError: NSError?
                NSFileCoordinator().coordinate(readingItemAt: url, options: [], error: &coordinatorError) { source in
                    do {
                        let values = try source.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
                        guard values.isRegularFile == true, (values.fileSize ?? maximumBytes + 1) <= maximumBytes else {
                            throw CocoaError(.fileReadTooLarge)
                        }
                        guard FileManager.default.createFile(atPath: payload.path, contents: nil) else {
                            throw CocoaError(.fileWriteUnknown)
                        }
                        let input = try FileHandle(forReadingFrom: source)
                        defer { try? input.close() }
                        let output = try FileHandle(forWritingTo: payload)
                        defer { try? output.close() }
                        var digest = SHA256()
                        var size = 0
                        while let data = try input.read(upToCount: 1024 * 1024), !data.isEmpty {
                            size += data.count
                            guard size <= maximumBytes else { throw CocoaError(.fileReadTooLarge) }
                            try output.write(contentsOf: data)
                            digest.update(data: data)
                        }
                        try output.synchronize()
                        manifest = ["version": 1, "id": id, "name": name, "bytes": size,
                                    "sha256": digest.finalize().map { String(format: "%02x", $0) }.joined()]
                    } catch { failure = error }
                }
                if let error = coordinatorError { throw error }
                if let error = failure { throw error }
                guard let manifest else { throw CocoaError(.fileReadUnknown) }
                try writeJSON(manifest, to: directory.appendingPathComponent("manifest.json"))
                // Publish the pointer only after both immutable payload and manifest exist.
                try writeJSON(manifest, to: root.appendingPathComponent("latest.json"))
                DispatchQueue.main.async { [self] in
                    currentID = id
                    status.text = "Ready; waiting for Mac"
                    details.text = "\(name)\n\(manifest["bytes"] ?? 0) bytes\n\n\(id)\n\nTransfer is complete only after the Mac acknowledges verification."
                }
            } catch {
                try? FileManager.default.removeItem(at: directory)
                DispatchQueue.main.async { [self] in
                    status.text = "Import failed"
                    details.text = error.localizedDescription
                }
            }
        }
    }
}
