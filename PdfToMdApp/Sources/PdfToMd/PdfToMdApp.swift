import AppKit
import SwiftUI

@main
struct PdfToMdApp: App {
    static let mainWindowID = "main"

    @NSApplicationDelegateAdaptor private var delegate: AppDelegate

    init() {
        // Started from a terminal or a bare binary, the app would otherwise not take focus.
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    var body: some Scene {
        Window("PDF to Markdown", id: Self.mainWindowID) {
            WindowRoot(delegate: delegate)
        }
        .defaultSize(width: 1200, height: 800)
    }
}

/// The window's content; a view, because SwiftUI hands openWindow to views.
private struct WindowRoot: View {
    let delegate: AppDelegate
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        ContentView()
            .environmentObject(delegate.model)
            .onAppear {
                delegate.reopenWindow = { openWindow(id: PdfToMdApp.mainWindowID) }
            }
    }
}

/// Owns the model, so that quitting can stop a run first.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()
    /// Brings the window back when closing it was cancelled: by then it is already gone.
    var reopenWindow: (() -> Void)?

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard model.isRunning else {
            return .terminateNow
        }
        let alert = NSAlert()
        alert.messageText = "A conversion is running."
        alert.informativeText = "Quitting stops it. Files already converted are kept; with “Skip files already converted” a new run carries on from there."
        alert.addButton(withTitle: "Stop and Quit")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else {
            if sender.windows.allSatisfy({ !$0.isVisible }) {  // asked because the window was closed
                reopenWindow?()
            }
            return .terminateCancel
        }
        model.stopForQuitting()
        return .terminateNow
    }
}
