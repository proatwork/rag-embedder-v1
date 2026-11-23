// Tests of chunk-config.json as the app reads and writes it: the layout, the chunker's rules, and the names of the PDFs.

import Foundation
import PdfToMdCore
import XCTest

/// A path in the temporary folder that nothing uses yet.
private func scratchURL(_ suffix: String = "") -> URL {
    FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + suffix)
}

final class DocumentConfigTests: XCTestCase {
    private static let currentYear = 2026
    /// The keys every entry needs, with values the chunker takes.
    private static let validEntryJSON = #""code": "X", "title": "Y", "year": 2020, "status": "in_force", "profile": "art""#
    /// Two entries as Python's json.dumps(indent=2, ensure_ascii=False) writes them, every optional key in one.
    private static let pythonLayout = #"""
    {
      "A": {
        "code": "P1/2020",
        "title": "Titlu ăîșț \"q\" a/b",
        "year": 2020,
        "status": "repealed",
        "profile": "headings",
        "first_body_page": 2,
        "amended_by": [
          "Ordin 1/2021",
          "Lege 2/2022"
        ],
        "repealed_on": "2024-01-31",
        "gazette": "M. Of. nr. 1/2020",
        "embed_field": "text"
      },
      "B": {
        "code": "X",
        "title": "Y",
        "year": 1999,
        "status": "in_force",
        "profile": "art",
        "amended_by": []
      }
    }

    """#

    private func decode(_ text: String) throws -> [String: DocumentEntry] {
        try DocumentConfigFile.decode(Data(text.utf8))
    }

    private func entry(status: DocumentStatus = .inForce) -> DocumentEntry {
        DocumentEntry(code: "P1", title: "T", year: 2020, status: status, profile: .art)
    }

    func testTheProjectsOwnFileComesBackUnchanged() throws {
        let url = DocumentConfigFile.url(in: FakeProject.realProject)
        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertEqual(DocumentConfigFile.encode(try DocumentConfigFile.load(from: url)), text)
    }

    func testTheLayoutIsPythons() throws {
        let entries = try decode(Self.pythonLayout)
        XCTAssertEqual(entries["A"]?.amendedBy, ["Ordin 1/2021", "Lege 2/2022"])
        XCTAssertEqual(entries["A"]?.embedField, .text)
        XCTAssertEqual(entries["B"]?.firstBodyPage, nil)
        XCTAssertEqual(DocumentConfigFile.encode(entries), Self.pythonLayout)
    }

    func testTheRealChunkerTakesWhatTheAppWrites() throws {
        let python = try FakeProject.realPython()
        var entries = try decode(Self.pythonLayout)
        entries["CATUC"] = .suggested(forKey: "CATUC", currentYear: Self.currentYear)
        let file = scratchURL(".json")
        defer { try? FileManager.default.removeItem(at: file) }
        try DocumentConfigFile.save(entries, to: file)

        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = FakeProject.realProject
        process.arguments = ["-c", "import sys, pathlib, json_to_chunks; print(sorted(json_to_chunks.load_config(pathlib.Path(sys.argv[1]))))", file.path]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let text = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0, text)
        XCTAssertEqual(text.trimmingCharacters(in: .whitespacesAndNewlines), "['A', 'B', 'CATUC']")
    }

    func testANameIsKeptInNFC() throws {
        let entries = try decode(#"{"Aa\#u{0306}": {\#(Self.validEntryJSON)}}"#)
        XCTAssertEqual(entries.keys.first?.unicodeScalars.count, 2, "written back precomposed")
        // Two spellings of one name are one document, as after json_to_chunks.load_config's NFC.
        XCTAssertEqual(try decode(#"{"A\#u{0103}": {\#(Self.validEntryJSON)}, "Aa\#u{0306}": {\#(Self.validEntryJSON)}}"#).count, 1)
    }

    func testNoFileYetIsNoEntries() throws {
        XCTAssertEqual(try DocumentConfigFile.load(from: scratchURL()), [:])
        XCTAssertEqual(DocumentConfigFile.encode([:]), "{}\n")
    }

    func testFilesTheAppCannotKeepAreRefused() throws {
        let cases: [(String, DocumentConfigError)] = [
            ("[]", .notAnObject),
            (#"{"A": 1}"#, .entryNotAnObject("A")),
            (#"{"A": {\#(Self.validEntryJSON), "note": "x"}}"#, .unknownKeys("A", ["note"])),
        ]
        for (text, expected) in cases {
            XCTAssertThrowsError(try decode(text), text) {
                XCTAssertEqual($0 as? DocumentConfigError, expected)
            }
        }
        let unreadable = [
            #"{"A": {"code": "X"}}"#,
            #"{"A": {\#(Self.validEntryJSON.replacingOccurrences(of: "2020", with: "\"2020\""))}}"#,
            #"{"A": {\#(Self.validEntryJSON.replacingOccurrences(of: "2020", with: "2020.0"))}}"#,
            #"{"A": {\#(Self.validEntryJSON.replacingOccurrences(of: "2020", with: "true"))}}"#,
            #"{"A": {\#(Self.validEntryJSON.replacingOccurrences(of: "\"art\"", with: "\"chapters\""))}}"#,
            #"{"A": {\#(Self.validEntryJSON.replacingOccurrences(of: "in_force", with: "valid"))}}"#,
            #"{"A": {\#(Self.validEntryJSON), "embed_field": "summary"}}"#,
        ]
        for text in unreadable {
            XCTAssertThrowsError(try decode(text), text) {
                guard case .unreadable? = $0 as? DocumentConfigError else {
                    return XCTFail("\(text): \($0)")
                }
            }
        }
        let broken = scratchURL(".json")
        defer { try? FileManager.default.removeItem(at: broken) }
        try "{\"A\": ".write(to: broken, atomically: true, encoding: .utf8)
        XCTAssertThrowsError(try DocumentConfigFile.load(from: broken)) {
            guard case .unreadable? = $0 as? DocumentConfigError else {
                return XCTFail("\($0)")
            }
        }
    }

    func testTheChunkersRules() {
        var entry = entry()
        XCTAssertEqual(entry.problems(), [])
        entry.firstBodyPage = 0  // the chunker reads 0 as "not set"
        XCTAssertEqual(entry.problems(), [])

        entry.code = " "
        entry.title = "two\nlines"
        entry.amendedBy = ["Ordin 1", ""]
        entry.gazette = "a\r\nb"  // one Character for Swift, two line breaks for Python
        XCTAssertEqual(entry.problems(), [
            "The code must be one line of text.", "The title must be one line of text.",
            "Each amending act must be one line of text.", "The gazette must be one line of text.",
        ])

        var repealed = self.entry(status: .repealed)
        XCTAssertEqual(repealed.problems(), ["A repealed norm needs the day it was repealed (and only a repealed one has it)."])
        for day in ["2024-02-30", "2024-1-31", "31.01.2024"] {
            repealed.repealedOn = day
            XCTAssertEqual(repealed.problems(), ["The day it was repealed must be a real day, written YYYY-MM-DD."], day)
        }
        repealed.repealedOn = "2024-02-29"
        XCTAssertEqual(repealed.problems(), [])
        repealed.status = .inForce
        XCTAssertNil(repealed.repealedOn, "leaving repealed drops the day")
    }

    func testTheProblemsOfAWholeFile() {
        var bad = entry()
        bad.title = " "
        let entries = ["B": bad, "A": entry(), "C": entry(status: .repealed)]
        XCTAssertEqual(DocumentConfigFile.problems(in: entries), [
            "B: The title must be one line of text.",
            "C: A repealed norm needs the day it was repealed (and only a repealed one has it).",
        ])
        XCTAssertEqual(DocumentConfigFile.problems(in: entries, excluding: ["B"]).count, 1)
    }

    func testASuggestionForANewFile() {
        let found = DocumentEntry.suggested(forKey: "Normativ_P118_2_2013", currentYear: Self.currentYear)
        XCTAssertEqual(found, DocumentEntry(code: "Normativ_P118_2_2013", title: "Normativ_P118_2_2013", year: 2013, status: .unverified, profile: .headings))
        XCTAssertEqual(DocumentEntry.suggested(forKey: "CATUC", currentYear: Self.currentYear).year, Self.currentYear)
        XCTAssertEqual(DocumentEntry.suggested(forKey: "Ordin 1995 rev 2003", currentYear: Self.currentYear).year, 2003)
        XCTAssertEqual(DocumentEntry.suggested(forKey: "cod 12345", currentYear: Self.currentYear).year, Self.currentYear)
        XCTAssertEqual(DocumentEntry.suggested(forKey: "CATUC", currentYear: Self.currentYear).problems(), [])
    }

    func testNormalizingWhatWasTyped() {
        var entry = DocumentEntry(code: " P1 ", title: "T ", year: 2020, status: .inForce, profile: .art)
        entry.amendedBy = ["Ordin 1 ", "", " "]
        entry.gazette = " "
        let normalized = entry.normalized()
        XCTAssertEqual(normalized.code, "P1")
        XCTAssertEqual(normalized.title, "T")
        XCTAssertEqual(normalized.amendedBy, ["Ordin 1"])
        XCTAssertNil(normalized.gazette)
        XCTAssertEqual(normalized.problems(), [])
    }
}

final class DocumentFilesTests: XCTestCase {
    func testTheFilesARunConverts() throws {
        let folder = scratchURL()
        try FileManager.default.createDirectory(at: folder.appendingPathComponent("sub.pdf"), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        for name in ["b.PDF", "a.pdf", ".hidden.pdf", "~$lock.pdf", "notes.txt", "Normativ..pdf", "c.docx", "~$c.docx", "D.DOCX"] {
            FileManager.default.createFile(atPath: folder.appendingPathComponent(name).path, contents: Data())
        }
        let files = DocumentFiles.files(in: folder)
        XCTAssertEqual(files.map(\.name), ["D.DOCX", "Normativ..pdf", "a.pdf", "b.PDF", "c.docx"], "lock and hidden files left out")
        XCTAssertEqual(files.map(\.key), ["D", "Normativ", "a", "b", "c"])
        XCTAssertEqual(files.map(\.kind), [.docx, .pdf, .pdf, .pdf, .docx])
    }

    func testFilesThatWouldWriteTheSameResults() throws {
        let folder = scratchURL()
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        for name in ["a.pdf", "A.docx", "b.pdf", "c.docx", "c. .pdf", "Straße.pdf", "STRASSE.docx", " .pdf"] {
            FileManager.default.createFile(atPath: folder.appendingPathComponent(name).path, contents: Data())
        }
        let files = DocumentFiles.files(in: folder)
        let clashes = DocumentFiles.clashes(in: files).map { Set($0.map(\.name)) }
        XCTAssertEqual(Set(clashes), [["a.pdf", "A.docx"], ["c.docx", "c. .pdf"], ["Straße.pdf", "STRASSE.docx"]], "as Python's casefold")
        XCTAssertEqual(DocumentFiles.nameless(in: files).map(\.name), [" .pdf"])
    }

    func testTheNameTheChunkerLooksUp() {
        XCTAssertEqual(DocumentFiles.key(forFileName: "Normativ_P118_2_2013.pdf"), "Normativ_P118_2_2013")
        XCTAssertEqual(DocumentFiles.key(forFileName: "construcțiilor. .pdf"), "construcțiilor")
        XCTAssertEqual(DocumentFiles.key(forFileName: "a\u{0306}.pdf"), "\u{0103}", "NFC")
        XCTAssertEqual(DocumentFiles.key(forFileName: "v1.2.pdf"), "v1.2")
    }
}
