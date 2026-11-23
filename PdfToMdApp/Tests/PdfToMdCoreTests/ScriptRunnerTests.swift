// Tests of ScriptRunner against fake pdf_to_md.py scripts in a scratch project: the output as it streams, the exit
// status, and Stop. The fake project's Python is the real project's interpreter (stdlib only is used).

import Foundation
import PdfToMdCore
import XCTest

final class ScriptRunnerTests: XCTestCase {
    private static let timeout: TimeInterval = 30
    /// How long a test that failed midway gives its script to stop before it is terminated.
    private static let cleanupTimeout: TimeInterval = 2
    /// How long to watch that a run that never started reports nothing.
    private static let quietPeriod: TimeInterval = 1

    private var fake: FakeProject!
    private var project: URL { fake.root }

    override func setUpWithError() throws {
        fake = try FakeProject()
    }

    override func tearDownWithError() throws {
        fake?.remove()
    }

    private struct Run {
        var lines: [String] = []
        var outcome: RunOutcome?
    }

    /// Writes `script` as the project's pdf_to_md.py and makes a runner for it that records what it reports.
    /// `onLine` sees each line as it arrives.
    private func runner(
        script: String,
        environment: [String: String] = ChildEnvironment.make(from: ProcessInfo.processInfo.environment),
        stopGracePeriod: TimeInterval = ScriptRunner.defaultStopGracePeriod,
        onLine: @escaping (String) -> Void = { _ in }
    ) throws -> (ScriptRunner, () -> Run, XCTestExpectation) {
        try fake.write(script, for: .ocr)
        var options = ScriptOptions()
        options.inputDirectory = project.path
        options.outputDirectory = project.path
        var run = Run()
        let exited = expectation(description: "the run ends")
        let runner = ScriptRunner(
            invocation: try Invocation(projectRoot: project, options: options),
            environment: environment,
            stopGracePeriod: stopGracePeriod,
            onLines: { lines in
                XCTAssertNil(run.outcome, "lines after the end")
                run.lines += lines
                lines.forEach(onLine)
            },
            onExit: { outcome in
                XCTAssertNil(run.outcome, "two ends")
                run.outcome = outcome
                exited.fulfill()
            }
        )
        // A test that fails before its Stop must not leave its script (or the script's `sleep`) running.
        addTeardownBlock { runner.stopAndWait(timeout: Self.cleanupTimeout) }
        return (runner, { run }, exited)
    }

    func testOutputStreamsInOrderWithItsLetters() throws {
        let script = """
        import os, sys
        print("== [1/2] fișier ăîșțâ.pdf  ->  out/fișier ăîșțâ.md", flush=True)
        print("   ok in 1s", flush=True)
        sys.stderr.write("a warning on stderr\\n")
        print("PATH=" + os.environ["PATH"])
        print("ARGS=" + "|".join(sys.argv[1:5]))
        print("runs in the project:", os.path.samefile(os.getcwd(), sys.argv[2]))
        print("no line break at the end", end="")
        """
        let (runner, run, exited) = try runner(
            script: script, environment: ChildEnvironment.make(from: ["PATH": "/usr/bin:/bin"])
        )
        try runner.start()
        wait(for: [exited], timeout: Self.timeout)
        XCTAssertEqual(run().outcome, .succeeded)
        XCTAssertEqual(run().lines, [
            "== [1/2] fișier ăîșțâ.pdf  ->  out/fișier ăîșțâ.md",
            "   ok in 1s",
            "a warning on stderr",
            "PATH=/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "ARGS=--input-dir|\(project.path)|--output-dir|\(project.path)",
            "runs in the project: True",
            "no line break at the end",
        ])
    }

    func testAFailingRunReportsItsStatusAndMessage() throws {
        let (runner, run, exited) = try runner(script: "import sys\nsys.exit('no .pdf files in /in')\n")
        try runner.start()
        wait(for: [exited], timeout: Self.timeout)
        XCTAssertEqual(run().outcome, .failed(exitStatus: 1))
        XCTAssertEqual(run().lines, ["no .pdf files in /in"])
    }

    /// The script waits on a child through subprocess.run, as run_batch waits on `docling convert`. Stop must end
    /// both, and let the script's `finally` run (where run_batch deletes the half-written output).
    func testStopEndsTheScriptAndTheProcessItWaitsOn() throws {
        let childPID = project.appendingPathComponent("child.pid")
        let cleanedUp = project.appendingPathComponent("cleaned-up")
        let script = """
        import subprocess, sys
        try:
            print("started", flush=True)
            subprocess.run(["/bin/sh", "-c", 'echo $$ > "$0"; exec sleep 300', \(pythonString(childPID.path))])
        finally:
            open(\(pythonString(cleanedUp.path)), "w").close()
        """
        let started = expectation(description: "the child is running")
        let (runner, run, exited) = try runner(script: script) { line in
            if line == "started" {
                started.fulfill()
            }
        }
        try runner.start()
        wait(for: [started], timeout: Self.timeout)
        let pid = try waitForPID(in: childPID)
        XCTAssertEqual(kill(pid, 0), 0, "the child should be running before Stop")

        runner.stop()
        wait(for: [exited], timeout: Self.timeout)
        XCTAssertEqual(run().outcome, .stopped)
        let signalled = kill(pid, 0)
        let error = errno
        XCTAssertEqual(signalled, -1, "the child outlived Stop")
        XCTAssertEqual(error, ESRCH)
        XCTAssertTrue(FileManager.default.fileExists(atPath: cleanedUp.path), "the script's finally did not run")
        XCTAssertTrue(run().lines.contains("KeyboardInterrupt"), "\(run().lines)")
    }

    func testAScriptThatIgnoresCtrlCIsTerminatedAfterTheGracePeriod() throws {
        let script = """
        import signal, time
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        print("started", flush=True)
        time.sleep(300)
        """
        let started = expectation(description: "the script is running")
        let (runner, run, exited) = try runner(script: script, stopGracePeriod: 0.5) { line in
            if line == "started" {
                started.fulfill()
            }
        }
        try runner.start()
        wait(for: [started], timeout: Self.timeout)
        runner.stop()
        wait(for: [exited], timeout: Self.timeout)
        XCTAssertEqual(run().outcome, .stopped)
    }

    func testAMissingPythonDoesNotStart() throws {
        let (runner, run, exited) = try runner(script: "")
        exited.isInverted = true
        try FileManager.default.removeItem(at: project.appendingPathComponent(ProjectLocator.pythonRelativePath))
        XCTAssertThrowsError(try runner.start()) {
            XCTAssertEqual(($0 as? CocoaError)?.code, .fileNoSuchFile, "\($0)")
        }
        wait(for: [exited], timeout: Self.quietPeriod)
        XCTAssertNil(run().outcome)
    }

    private struct NoPID: Error {}

    private func waitForPID(in file: URL) throws -> pid_t {
        let deadline = Date().addingTimeInterval(Self.timeout)
        while Date() < deadline {
            if let text = try? String(contentsOf: file, encoding: .utf8),
               let pid = pid_t(text.trimmingCharacters(in: .whitespacesAndNewlines)) {
                return pid
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        throw NoPID()
    }

    private func pythonString(_ text: String) -> String {
        "'" + text.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "'", with: "\\'") + "'"
    }
}
