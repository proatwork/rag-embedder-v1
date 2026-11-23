// The options of pdf_to_md.py as the app shows them, and the command line they make.
// The choices, defaults and rules are the script's own (pdf_to_md.py and pipeline_common.py): change them there first,
// then here.

import Foundation

public enum OutputFormat: String, CaseIterable, Identifiable, Codable, Sendable {
    case markdown = "md"
    case json
    public var id: String { rawValue }
}

public enum OCRMode: String, CaseIterable, Identifiable, Codable, Sendable {
    case fullPage = "full_page"
    case layoutRegions = "layout_regions"
    case pdfAwareLayoutRegions = "pdf_aware_layout_regions"
    case doclingDefault = "default"
    public var id: String { rawValue }
}

/// The devices pdf_to_md.py accepts (Docling knows more; the script refuses them).
public enum Device: String, CaseIterable, Identifiable, Codable, Sendable {
    case auto, cpu, mps
    public var id: String { rawValue }
}

public enum OCREngines {
    public static let defaultEngine = "tesseract"
    /// Docling 2.135's --ocr-engine values (`docling convert --help`), the two this project uses first.
    public static let all = ["tesseract", "ocrmac", "auto", "easyocr", "rapidocr", "tesserocr", "nemotron-ocr", "kserve_v2_ocr"]
    /// Romanian as each engine names it (pdf_to_md.py: "ron"; pdf_to_md_vision.py: "ro-RO" for ocrmac).
    private static let romanian = ["tesseract": "ron", "tesserocr": "ron", "ocrmac": "ro-RO", "easyocr": "ro"]

    /// The language a newly picked engine starts with; "" lets the engine choose, as Docling does with an empty --ocr-lang.
    public static func defaultLanguage(for engine: String) -> String {
        romanian[engine] ?? ""
    }
}

public enum OptionsProblem: LocalizedError, Equatable {
    case unclosedQuote
    case reservedDoclingOption(String)
    case optionSetAbove(String)

    public var errorDescription: String? {
        switch self {
        case .unclosedQuote:
            return "Extra Docling options: a quote is not closed."
        case .reservedDoclingOption(let option):
            return "Extra Docling options: \(option) is set by the script itself; leave it out."
        case .optionSetAbove(let option):
            return "Extra Docling options: \(option) is set in the form above; set it there."
        }
    }
}

/// The flags of pdf_to_md.py.
enum Flag {
    static let inputDirectory = "--input-dir"
    static let outputDirectory = "--output-dir"
    static let skipExisting = "--skip-existing"
    static let to = "--to"
    static let images = "--images"
    static let pageImages = "--page-images"
    static let ocrEngine = "--ocr-engine"
    static let ocrLanguage = "--ocr-lang"
    static let ocrMode = "--ocr-mode"
    static let device = "--device"
    /// What follows it goes to `docling convert` unchanged.
    static let passthroughSeparator = "--"

    /// The folder arguments every pipeline script takes (pipeline_common.add_dir_arguments).
    static func directories(input: String, output: String, skipExisting: Bool) -> [String] {
        [inputDirectory, input, outputDirectory, output] + (skipExisting ? [Flag.skipExisting] : [])
    }
}

public struct ScriptOptions: Equatable, Codable, Sendable {
    /// Options the script sets on `docling convert` itself; its docstring says not to pass them after "--".
    static let reservedDoclingOptions = [Flag.to, "--output", "--output-file", "--image-export-mode"]
    /// Options the form sets: a second copy after "--" would quietly win over the form.
    static let formDoclingOptions = [Flag.ocrEngine, Flag.ocrLanguage, Flag.ocrMode, Flag.device]

    public var inputDirectory = ""
    public var outputDirectory = ""
    public var skipExisting = false
    /// --images and --page-images need --to json, as the script requires; leaving json turns them off.
    public var format = OutputFormat.markdown {
        didSet {
            if !imagesAllowed {
                images = false
            }
        }
    }
    public var images = false {
        didSet {
            if !images {
                pageImages = false
            }
        }
    }
    public var pageImages = false
    /// Picking another engine resets the language to that engine's name for Romanian.
    public var ocrEngine = OCREngines.defaultEngine {
        didSet {
            if ocrEngine != oldValue {
                ocrLanguage = OCREngines.defaultLanguage(for: ocrEngine)
            }
        }
    }
    public var ocrLanguage = OCREngines.defaultLanguage(for: OCREngines.defaultEngine)
    public var ocrMode = OCRMode.fullPage
    public var device = Device.auto
    /// Typed as on a command line, e.g. "--page-range 1-4"; passed after "--".
    public var extraDoclingOptions = ""

    public init() {}

    public var imagesAllowed: Bool { format == .json }
    public var pageImagesAllowed: Bool { imagesAllowed && images }

    /// The arguments after the script's name: pdf_to_md.py's, or docx_to_md.py's (the same without the OCR ones).
    /// Throws an OptionsProblem when the extra Docling options cannot be used.
    public func scriptArguments(for source: SourceKind = .pdf) throws -> [String] {
        var arguments = Flag.directories(input: inputDirectory, output: outputDirectory, skipExisting: skipExisting)
        arguments += [Flag.to, format.rawValue]
        if imagesAllowed && images {
            arguments.append(Flag.images)
        }
        if pageImagesAllowed && pageImages {
            arguments.append(Flag.pageImages)
        }
        if source.hasOCR {
            arguments += [
                Flag.ocrEngine, ocrEngine, Flag.ocrLanguage, ocrLanguage, Flag.ocrMode, ocrMode.rawValue,
                Flag.device, device.rawValue,
            ]
        }
        let extra = try doclingArguments(refusingOCROptions: source.hasOCR)
        if !extra.isEmpty {
            arguments += [Flag.passthroughSeparator] + extra
        }
        return arguments
    }

    /// The extra Docling options as separate arguments. The OCR ones are refused where the form sets them (for PDFs);
    /// for Word files alone nothing else passes them on.
    public func doclingArguments(refusingOCROptions: Bool = true) throws -> [String] {
        let words = try ArgumentSplitter.split(extraDoclingOptions)
        for word in words {
            if let option = Self.reservedDoclingOptions.first(where: { Self.word(word, is: $0) }) {
                throw OptionsProblem.reservedDoclingOption(option)
            }
            if refusingOCROptions, let option = Self.formDoclingOptions.first(where: { Self.word(word, is: $0) }) {
                throw OptionsProblem.optionSetAbove(option)
            }
        }
        return words
    }

    /// What stops a run, in plain words; empty when the options can be run. `refusingOCROptions`: whether PDFs are
    /// converted, whose OCR options the form sets.
    public func problems(refusingOCROptions: Bool = true) -> [String] {
        var found: [String] = []
        if inputDirectory.isEmpty {
            found.append("Choose the folder with the PDFs or Word files.")
        } else if !Self.isFolder(inputDirectory) {
            found.append("The input is not a folder: \(inputDirectory)")
        }
        if outputDirectory.isEmpty {
            found.append("Choose the folder the results go to.")
        }
        do {
            _ = try doclingArguments(refusingOCROptions: refusingOCROptions)
        } catch {
            found.append(error.localizedDescription)
        }
        return found
    }

    private static func word(_ word: String, is option: String) -> Bool {
        word == option || word.hasPrefix(option + "=")
    }

    private static func isFolder(_ path: String) -> Bool {
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory) && isDirectory.boolValue
    }
}

/// Splits options typed as on a shell command line: words apart by spaces; '...' keeps everything as it is; "..."
/// keeps spaces, with \" and \\ for a quote and a backslash; outside quotes a backslash keeps the next character.
public enum ArgumentSplitter {
    private static let backslash: Character = "\\"
    private static let singleQuote: Character = "'"
    private static let doubleQuote: Character = "\""

    public static func split(_ text: String) throws -> [String] {
        var words: [String] = []
        var current = ""
        var inWord = false
        var quote: Character?
        var escaping = false
        for character in text {
            if escaping {
                escaping = false
                if quote == doubleQuote && character != doubleQuote && character != backslash {
                    current.append(backslash)
                }
                current.append(character)
            } else if let open = quote {
                if character == open {
                    quote = nil
                } else if character == backslash && open == doubleQuote {
                    escaping = true
                } else {
                    current.append(character)
                }
            } else if character == backslash {
                escaping = true
                inWord = true
            } else if character == singleQuote || character == doubleQuote {
                quote = character
                inWord = true
            } else if character.isWhitespace {
                if inWord {
                    words.append(current)
                    current = ""
                    inWord = false
                }
            } else {
                current.append(character)
                inWord = true
            }
        }
        if quote != nil {
            throw OptionsProblem.unclosedQuote
        }
        if escaping {
            current.append(backslash)  // a backslash at the very end has nothing to keep: it stays itself
        }
        if inWord {
            words.append(current)
        }
        return words
    }
}

/// Words written so that a shell, or ArgumentSplitter, reads them back unchanged.
public enum ShellQuoting {
    private static let plain = CharacterSet(
        charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./=:,+@%"
    )

    public static func quote(_ word: String) -> String {
        if !word.isEmpty && word.unicodeScalars.allSatisfy({ plain.contains($0) }) {
            return word
        }
        return "'" + word.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    public static func commandLine(_ words: [String]) -> String {
        words.map(quote).joined(separator: " ")
    }
}
