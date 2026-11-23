// Runs pdf_to_md.py as a child process: its output comes back line by line, and Stop ends it the way Ctrl-C does.

import Foundation

public enum RunOutcome: Equatable, Sendable {
    case succeeded
    case failed(exitStatus: Int32)
    case killed(signal: Int32)
    case stopped
}

public final class ScriptRunner {
    /// After Stop, how long the script gets to end on its own before it is terminated.
    public static let defaultStopGracePeriod: TimeInterval = 10
    /// After the script has ended, how long to wait for the end of its output: a process it left behind can hold the
    /// pipe open.
    static let outputDrainTimeout: TimeInterval = 5
    /// How often stopAndWait looks whether the script has ended.
    private static let exitPollInterval: TimeInterval = 0.05

    private let process = Process()
    private let pipe = Pipe()
    private let stopGracePeriod: TimeInterval
    private let callbackQueue: DispatchQueue
    private let onLines: ([String]) -> Void
    private let onExit: (RunOutcome) -> Void

    // Touched only on `queue`.
    private let queue = DispatchQueue(label: "PdfToMd.ScriptRunner")
    private var splitter = LineSplitter()
    private var stopRequested = false
    private var outputEnded = false
    private var exited = false
    private var reported = false

    /// `onLines` gets the output (stdout and stderr together, as a terminal shows them) and `onExit` how the run
    /// ended, once, after the last lines; both on `callbackQueue`.
    public init(
        invocation: Invocation,
        environment: [String: String],
        stopGracePeriod: TimeInterval = defaultStopGracePeriod,
        callbackQueue: DispatchQueue = .main,
        onLines: @escaping ([String]) -> Void,
        onExit: @escaping (RunOutcome) -> Void
    ) {
        self.stopGracePeriod = stopGracePeriod
        self.callbackQueue = callbackQueue
        self.onLines = onLines
        self.onExit = onExit
        process.executableURL = invocation.executable
        process.arguments = invocation.arguments
        process.currentDirectoryURL = invocation.workingDirectory
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = pipe
        process.standardError = pipe
    }

    /// Starts the script. Throws when it cannot be started (no Python at that path, say); the callbacks then never run.
    public func start() throws {
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            self?.queue.async { self?.receive(data) }
        }
        process.terminationHandler = { [weak self] _ in
            self?.queue.async { self?.processExited() }
        }
        do {
            try process.run()
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            process.terminationHandler = nil
            throw error
        }
    }

    /// Ctrl-C (SIGINT): Python stops with KeyboardInterrupt, subprocess.run kills the `docling convert` it is waiting
    /// for, and run_batch deletes the half-written output. SIGTERM would end Python alone and leave Docling running,
    /// so it is sent only to a script still running after the grace period.
    public func stop() {
        queue.async { [self] in
            // isRunning: a process never started (or already gone) must not be signalled; Process raises on that.
            guard !exited, !stopRequested, process.isRunning else {
                return
            }
            stopRequested = true
            process.interrupt()
            queue.asyncAfter(deadline: .now() + stopGracePeriod) { [self] in
                if !exited && process.isRunning {
                    process.terminate()
                }
            }
        }
    }

    /// Stop, then wait up to `timeout` for the script to end, and terminate it if it has not: for quitting the app.
    public func stopAndWait(timeout: TimeInterval) {
        stop()
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: Self.exitPollInterval)
        }
        if process.isRunning {
            process.terminate()
        }
    }

    private func receive(_ data: Data) {
        guard !outputEnded else {
            return
        }
        if data.isEmpty {  // end of file: every writer has closed the pipe
            endOutput()
            return
        }
        deliver(splitter.feed(data))
    }

    private func endOutput() {
        guard !outputEnded else {
            return
        }
        outputEnded = true
        pipe.fileHandleForReading.readabilityHandler = nil
        deliver(splitter.finish())
        reportIfDone()
    }

    private func processExited() {
        exited = true
        queue.asyncAfter(deadline: .now() + Self.outputDrainTimeout) { [self] in
            endOutput()
        }
        reportIfDone()
    }

    private func deliver(_ lines: [String]) {
        guard !lines.isEmpty else {
            return
        }
        callbackQueue.async { [onLines] in
            onLines(lines)
        }
    }

    private func reportIfDone() {
        guard exited, outputEnded, !reported else {
            return
        }
        reported = true
        let outcome = outcome()
        callbackQueue.async { [onExit] in
            onExit(outcome)
        }
    }

    private func outcome() -> RunOutcome {
        let status = process.terminationStatus
        if process.terminationReason == .exit && status == 0 {
            return .succeeded
        }
        if stopRequested {
            return .stopped
        }
        return process.terminationReason == .uncaughtSignal ? .killed(signal: status) : .failed(exitStatus: status)
    }
}
