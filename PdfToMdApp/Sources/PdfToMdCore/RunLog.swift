// Reading the script's output: cutting it into lines, and following the status lines it prints for each file.

import Foundation

/// Cuts a byte stream into lines. A line is decoded only once it is whole, so a letter split across two reads (ă, ș in
/// a file name) is not lost. "\n", "\r\n" and a lone "\r" (progress bars) each end a line.
public struct LineSplitter {
    private static let newline = UInt8(ascii: "\n")
    private static let carriageReturn = UInt8(ascii: "\r")

    private var pending: [UInt8] = []
    private var afterCarriageReturn = false

    public init() {}

    public mutating func feed(_ data: Data) -> [String] {
        var lines: [String] = []
        for byte in data {
            if byte == Self.newline && afterCarriageReturn {  // the "\n" of a "\r\n": that line has ended already
                afterCarriageReturn = false
                continue
            }
            afterCarriageReturn = byte == Self.carriageReturn
            if byte == Self.newline || byte == Self.carriageReturn {
                lines.append(String(decoding: pending, as: UTF8.self))
                pending.removeAll(keepingCapacity: true)
            } else {
                pending.append(byte)
            }
        }
        return lines
    }

    /// The last line, when the output ended without a line break.
    public mutating func finish() -> [String] {
        defer { pending.removeAll() }
        return pending.isEmpty ? [] : [String(decoding: pending, as: UTF8.self)]
    }
}

/// What the script has reported so far, read from the status lines pipeline_common.run_batch prints:
///     == [2/12] name.pdf  ->  out/name.md
///        ok in 31s            (or: skipped (output exists) / FAILED (why) in 3s)
public struct RunProgress: Equatable {
    public enum LineKind: Equatable, Sendable {
        case fileStarted, converted, skipped, failed, other
    }

    enum Marker {
        static let fileStart = "== ["
        static let counterSeparator: Character = "/"
        static let counterEnd: Character = "]"
        static let arrow = "  ->  "
        static let converted = "   ok in "
        static let skipped = "   skipped "
        static let failed = "   FAILED ("
    }

    public private(set) var fileNumber = 0
    public private(set) var fileCount: Int?
    public private(set) var currentFile: String?
    public private(set) var converted = 0
    public private(set) var skipped = 0
    public private(set) var failedFiles: [String] = []

    public init() {}

    /// Files done with, one way or another.
    public var finishedCount: Int { converted + skipped + failedFiles.count }

    @discardableResult
    public mutating func read(_ line: String) -> LineKind {
        if let start = Self.fileStart(in: line) {
            fileNumber = start.number
            fileCount = start.count
            currentFile = start.name
            return .fileStarted
        }
        if line.hasPrefix(Marker.converted) {
            converted += 1
            return .converted
        }
        if line.hasPrefix(Marker.skipped) {
            skipped += 1
            return .skipped
        }
        if line.hasPrefix(Marker.failed) {
            failedFiles.append(currentFile ?? "")
            return .failed
        }
        return .other
    }

    static func fileStart(in line: String) -> (number: Int, count: Int, name: String)? {
        guard line.hasPrefix(Marker.fileStart), let counterEnd = line.firstIndex(of: Marker.counterEnd) else {
            return nil
        }
        let counter = line[line.index(line.startIndex, offsetBy: Marker.fileStart.count)..<counterEnd]
            .split(separator: Marker.counterSeparator)
        guard counter.count == 2, let number = Int(counter[0]), let count = Int(counter[1]) else {
            return nil
        }
        let rest = line[line.index(after: counterEnd)...].drop(while: { $0 == " " })
        let name = rest.range(of: Marker.arrow).map { String(rest[..<$0.lowerBound]) } ?? String(rest)
        return (number, count, name)
    }
}
