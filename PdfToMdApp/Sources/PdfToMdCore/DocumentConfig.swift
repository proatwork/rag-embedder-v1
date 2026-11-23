// The per-document settings in chunk-config.json, which json_to_chunks.py, embed_chunks.py, search.py, ask.py and the
// chatbot read: how each norm is cut into chunks, and what every chunk says of it (code, title, edition, status).
// The keys, values and rules are json_to_chunks.py's own (CONFIG_KEYS, OPTIONAL_KEYS, entry_problem, status_problem)
// and embed_chunks.py's (EMBED_FIELDS): change them there first, then here.

import Foundation

public enum DocumentStatus: String, CaseIterable, Identifiable, Codable, Sendable {
    case inForce = "in_force"
    /// For a norm nobody has checked yet.
    case unverified
    case repealed
    public var id: String { rawValue }
}

public enum ChunkProfile: String, CaseIterable, Identifiable, Codable, Sendable {
    /// A chunk starts at "Art. N".
    case art
    /// A chunk starts at a numbered paragraph, "3.7.4.2".
    case decimal
    /// No numbering: a chunk is the text under a heading.
    case headings
    public var id: String { rawValue }
}

/// Which field of a chunk embed_chunks.py turns into its vector.
public enum EmbedField: String, CaseIterable, Identifiable, Codable, Sendable {
    /// The breadcrumb and the text: the script's default.
    case embedText = "embed_text"
    case text
    public var id: String { rawValue }
}

public struct DocumentEntry: Equatable, Codable, Sendable {
    public var code: String
    public var title: String
    /// The edition's year.
    public var year: Int
    /// Only a repealed norm has the day it was repealed: leaving that status drops the day.
    public var status: DocumentStatus {
        didSet {
            if status != .repealed {
                repealedOn = nil
            }
        }
    }
    public var profile: ChunkProfile
    /// The PDF page the norm's text starts on; nil (or 0, as for the chunker) lets the chunker find it.
    public var firstBodyPage: Int?
    public var amendedBy: [String]?
    /// YYYY-MM-DD.
    public var repealedOn: String?
    public var gazette: String?
    /// nil: the script's default.
    public var embedField: EmbedField?

    /// In the order the file lists them (json_to_chunks.py's CONFIG_KEYS, then OPTIONAL_KEYS as the file has them).
    enum CodingKeys: String, CodingKey, CaseIterable {
        case code, title, year, status, profile
        case firstBodyPage = "first_body_page"
        case amendedBy = "amended_by"
        case repealedOn = "repealed_on"
        case gazette
        case embedField = "embed_field"
    }

    private static let yearInName = try! NSRegularExpression(pattern: "(?<![0-9])(19|20)[0-9]{2}(?![0-9])")
    private static let isoDay = try! NSRegularExpression(pattern: "^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    public init(code: String, title: String, year: Int, status: DocumentStatus, profile: ChunkProfile) {
        self.code = code
        self.title = title
        self.year = year
        self.status = status
        self.profile = profile
    }

    /// A start for a file that has no entry: its name as code and title, the last year in the name (else
    /// `currentYear`), unverified (nobody has checked it yet), and the profile that needs no numbering.
    public static func suggested(forKey key: String, currentYear: Int) -> DocumentEntry {
        let range = NSRange(key.startIndex..., in: key)
        let year = yearInName.matches(in: key, range: range).last
            .flatMap { Range($0.range, in: key) }
            .flatMap { Int(key[$0]) } ?? currentYear
        return DocumentEntry(code: key, title: key, year: year, status: .unverified, profile: .headings)
    }

    /// The entry as it is saved: spaces around the texts dropped, empty lines of `amendedBy` dropped, empty optional
    /// texts left out.
    public func normalized() -> DocumentEntry {
        var entry = self
        entry.code = code.trimmed
        entry.title = title.trimmed
        entry.amendedBy = amendedBy?.map(\.trimmed).filter { !$0.isEmpty }
        entry.gazette = gazette?.trimmed.nilIfEmpty
        entry.repealedOn = repealedOn?.trimmed.nilIfEmpty
        return entry
    }

    /// json_to_chunks.py's rules for an entry, in plain words; empty when the chunker takes it.
    public func problems() -> [String] {
        var found: [String] = []
        if !Self.isOneLine(code) {
            found.append("The code must be one line of text.")
        }
        if !Self.isOneLine(title) {
            found.append("The title must be one line of text.")
        }
        if let acts = amendedBy, !acts.allSatisfy(Self.isOneLine) {
            found.append("Each amending act must be one line of text.")
        }
        if let gazette, !Self.isOneLine(gazette) {
            found.append("The gazette must be one line of text.")
        }
        if (status == .repealed) != (repealedOn != nil) {
            found.append("A repealed norm needs the day it was repealed (and only a repealed one has it).")
        } else if let day = repealedOn, !Self.isRealDay(day) {
            found.append("The day it was repealed must be a real day, written YYYY-MM-DD.")
        }
        return found
    }

    /// json_to_chunks.one_line: some text, and no line break. Code points, as Python sees them: Swift takes "\r\n"
    /// for one Character, which contains neither "\n" nor "\r".
    private static func isOneLine(_ text: String) -> Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !text.unicodeScalars.contains { lineBreaks.contains($0) }
    }

    private static let lineBreaks: Set<Unicode.Scalar> = ["\n", "\r"]

    private static func isRealDay(_ text: String) -> Bool {
        guard isoDay.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil else {
            return false
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.isLenient = false
        return formatter.date(from: text).map { formatter.string(from: $0) == text } ?? false
    }
}

public enum DocumentConfigError: LocalizedError, Equatable {
    case notAnObject
    case entryNotAnObject(String)
    case unknownKeys(String, [String])
    case unreadable(String)

    public var errorDescription: String? {
        let file = DocumentConfigFile.fileName
        switch self {
        case .notAnObject:
            return "\(file) is not an object with one entry per document."
        case .entryNotAnObject(let name):
            return "\(file): \(name) is not an object with keys."
        case .unknownKeys(let name, let keys):
            return "\(file): \(name) has \(keys.joined(separator: ", ")), which no script reads."
        case .unreadable(let reason):
            return "\(file) cannot be read: \(reason)"
        }
    }
}

/// Reading and writing chunk-config.json, in the layout of Python's json.dumps(indent=2, ensure_ascii=False), so that
/// a save changes only the entries that changed. The documents are written in the order of their names.
public enum DocumentConfigFile {
    public static let fileName = "chunk-config.json"
    private static let indent = "  "

    public static func url(in projectRoot: URL) -> URL {
        projectRoot.appendingPathComponent(fileName)
    }

    /// The entries by document name (NFC, as json_to_chunks.load_config keys them); none when there is no file yet.
    public static func load(from url: URL) throws -> [String: DocumentEntry] {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return [:]
        }
        do {
            return try decode(Data(contentsOf: url))
        } catch let error as DocumentConfigError {
            throw error
        } catch {
            throw DocumentConfigError.unreadable(error.localizedDescription)
        }
    }

    /// The problems of every entry but those of `excluded`, as "name: problem", by name. json_to_chunks.load_config
    /// refuses the whole file for any one of them, so a single bad entry stops chunking for every document.
    public static func problems(in entries: [String: DocumentEntry], excluding excluded: Set<String> = []) -> [String] {
        entries.keys.sorted().filter { !excluded.contains($0) }.flatMap { name in
            entries[name]!.problems().map { "\(name): \($0)" }
        }
    }

    public static func save(_ entries: [String: DocumentEntry], to url: URL) throws {
        try encode(entries).write(to: url, atomically: true, encoding: .utf8)
    }

    public static func decode(_ data: Data) throws -> [String: DocumentEntry] {
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw DocumentConfigError.notAnObject
        }
        // An entry with keys this app does not know would lose them on the next save.
        let known = Set(DocumentEntry.CodingKeys.allCases.map(\.rawValue))
        for (name, value) in object {
            guard let entry = value as? [String: Any] else {
                throw DocumentConfigError.entryNotAnObject(name)
            }
            let unknown = entry.keys.filter { !known.contains($0) }.sorted()
            if !unknown.isEmpty {
                throw DocumentConfigError.unknownKeys(name, unknown)
            }
            // JSONDecoder would take 2013.0 or true for an Int; json_to_chunks wants a whole number.
            if let year = entry[DocumentEntry.CodingKeys.year.rawValue] as? NSNumber,
               CFGetTypeID(year) == CFBooleanGetTypeID() || CFNumberIsFloatType(year) {
                throw DocumentConfigError.unreadable("\(name): year \(year) is not a whole number")
            }
        }
        let typed: [String: DocumentEntry]
        do {
            typed = try JSONDecoder().decode([String: DocumentEntry].self, from: data)
        } catch let DecodingError.keyNotFound(key, context) {
            throw DocumentConfigError.unreadable("\(context.codingPath.first?.stringValue ?? "an entry") lacks \(key.stringValue)")
        } catch let DecodingError.typeMismatch(_, context), let DecodingError.dataCorrupted(context) {
            throw DocumentConfigError.unreadable("\(context.codingPath.map(\.stringValue).joined(separator: ".")): \(context.debugDescription)")
        }
        // Swift compares names by canonical equivalence, so two spellings of one name are already one key here, as
        // they are after load_config's NFC; it is written back in NFC.
        return Dictionary(uniqueKeysWithValues: typed.map { ($0.key.precomposedStringWithCanonicalMapping, $0.value) })
    }

    public static func encode(_ entries: [String: DocumentEntry]) -> String {
        guard !entries.isEmpty else {
            return "{}\n"
        }
        let documents = entries.keys.sorted().map { name in
            let fields = fields(of: entries[name]!).map { "\(indent)\(indent)\(quoted($0.key.rawValue)): \($0.value)" }
            return "\(indent)\(quoted(name)): {\n" + fields.joined(separator: ",\n") + "\n\(indent)}"
        }
        return "{\n" + documents.joined(separator: ",\n") + "\n}\n"
    }

    private static func fields(of entry: DocumentEntry) -> [(key: DocumentEntry.CodingKeys, value: String)] {
        var fields: [(DocumentEntry.CodingKeys, String)] = [
            (.code, quoted(entry.code)), (.title, quoted(entry.title)), (.year, String(entry.year)),
            (.status, quoted(entry.status.rawValue)), (.profile, quoted(entry.profile.rawValue)),
        ]
        if let page = entry.firstBodyPage {
            fields.append((.firstBodyPage, String(page)))
        }
        if let acts = entry.amendedBy {
            fields.append((.amendedBy, list(acts)))
        }
        if let day = entry.repealedOn {
            fields.append((.repealedOn, quoted(day)))
        }
        if let gazette = entry.gazette {
            fields.append((.gazette, quoted(gazette)))
        }
        if let field = entry.embedField {
            fields.append((.embedField, quoted(field.rawValue)))
        }
        return fields
    }

    private static func list(_ items: [String]) -> String {
        guard !items.isEmpty else {
            return "[]"
        }
        let inner = String(repeating: indent, count: 3)
        return "[\n" + items.map { inner + quoted($0) }.joined(separator: ",\n") + "\n\(indent)\(indent)]"
    }

    /// A JSON string as Python writes it with ensure_ascii=False: letters as they are, "/" not escaped.
    private static func quoted(_ text: String) -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = .withoutEscapingSlashes
        return String(decoding: try! encoder.encode(text), as: UTF8.self)
    }
}

/// A file a run converts, with the name its document has in chunk-config.json.
public struct DocumentFile: Identifiable, Equatable, Sendable {
    public let name: String
    public let key: String
    public let kind: SourceKind
    public var id: String { name }
}

public enum DocumentFiles {
    /// pipeline_common's names: what collect_files leaves out, and what output_path drops from the end of a name.
    private static let hiddenPrefix = "."
    private static let lockFilePrefix = "~$"
    private static let unsafeNameTail = CharacterSet(charactersIn: ". ")

    /// The PDFs and Word files the conversion scripts convert in `folder` (pipeline_common.collect_files), by name.
    public static func files(in folder: URL) -> [DocumentFile] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
        return names.sorted().compactMap { name in
            var isDirectory: ObjCBool = false
            let path = folder.appendingPathComponent(name).path
            guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory), !isDirectory.boolValue,
                  let kind = SourceKind(rawValue: (name as NSString).pathExtension.lowercased()),
                  !name.hasPrefix(hiddenPrefix), !name.hasPrefix(lockFilePrefix) else {
                return nil
            }
            return DocumentFile(name: name, key: key(forFileName: name), kind: kind)
        }
    }

    /// Files that would write the same results (a.pdf and a.docx both make a.json): the second conversion would
    /// overwrite the first, or skip it. Names compared as collect_files compares them, case left aside; a file with
    /// no name left is `nameless`, not a clash.
    public static func clashes(in files: [DocumentFile]) -> [[DocumentFile]] {
        // uppercased().lowercased() comes closer to Python's casefold() than lowercased() alone: "ß" becomes "ss".
        Dictionary(grouping: files.filter { !$0.key.isEmpty }) { $0.key.uppercased().lowercased() }.values
            .filter { $0.count > 1 }
            .sorted { $0[0].name < $1[0].name }
    }

    /// Files whose name is nothing once its trailing dots and spaces are dropped (" .pdf"): collect_files refuses
    /// them, and stops the whole conversion.
    public static func nameless(in files: [DocumentFile]) -> [DocumentFile] {
        files.filter { $0.key.isEmpty }
    }

    /// The name json_to_chunks.py looks the document up by: the name of the JSON that pdf_to_md.py writes for it
    /// (output_path drops trailing dots and spaces), without its suffix, in NFC.
    public static func key(forFileName name: String) -> String {
        var stem = (name as NSString).deletingPathExtension
        while let last = stem.unicodeScalars.last, unsafeNameTail.contains(last) {
            stem.unicodeScalars.removeLast()
        }
        return stem.precomposedStringWithCanonicalMapping
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
