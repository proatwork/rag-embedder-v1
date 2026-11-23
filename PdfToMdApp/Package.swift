// swift-tools-version: 5.9
// The Mac app around pdf_to_md.py (PdfToMd), the code it is built on (PdfToMdCore) and that code's tests (swift test).
// build_app.sh wraps the app as PdfToMd.app.
import PackageDescription

let package = Package(
    name: "PdfToMdApp",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "PdfToMd", targets: ["PdfToMd"])],
    targets: [
        .target(name: "PdfToMdCore"),
        .executableTarget(name: "PdfToMd", dependencies: ["PdfToMdCore"]),
        .testTarget(name: "PdfToMdCoreTests", dependencies: ["PdfToMdCore"]),
    ]
)
