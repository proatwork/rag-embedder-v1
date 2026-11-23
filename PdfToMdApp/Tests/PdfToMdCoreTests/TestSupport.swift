// A scratch project for the runner tests: its venvs/bin/python is the real project's interpreter (the fake scripts use
// the standard library only), and each test writes the scripts it needs.

import Foundation
import PdfToMdCore
import XCTest

struct FakeProject {
    /// This file is PdfToMdApp/Tests/PdfToMdCoreTests/TestSupport.swift inside the project.
    static let realProject = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

    let root: URL

    /// The real project's interpreter; throws XCTSkip when there is none.
    static func realPython() throws -> URL {
        let python = realProject.appendingPathComponent(ProjectLocator.pythonRelativePath)
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw XCTSkip("no Python at \(python.path)")
        }
        return python
    }

    /// Throws XCTSkip when the real project has no Python.
    init() throws {
        let python = try Self.realPython()
        root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let fakePython = root.appendingPathComponent(ProjectLocator.pythonRelativePath)
        try FileManager.default.createDirectory(at: fakePython.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(at: fakePython, withDestinationURL: python)
    }

    func write(_ script: String, for step: PipelineStep) throws {
        try script.write(to: root.appendingPathComponent(step.scriptName), atomically: true, encoding: .utf8)
    }

    func remove() {
        try? FileManager.default.removeItem(at: root)
    }
}
