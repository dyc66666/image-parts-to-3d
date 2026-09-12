// vision_mask.swift — 用 macOS Vision 生成前景遮罩（PNG，白=前景）
//
// 用法：swift vision_mask.swift <input-image> <output-mask.png>
// 需要 macOS 12+。脚本失败时会以非 0 退出，调用方应回退到颜色去背。

import Foundation
import Vision
import CoreGraphics
import CoreImage
import ImageIO

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: vision_mask <input> <output>\n".data(using: .utf8)!)
    exit(1)
}
let inPath = args[1]
let outPath = args[2]

guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: inPath) as CFURL, nil),
      let cgImage = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
    FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!)
    exit(2)
}

let request = VNGenerateForegroundInstanceMaskRequest()
let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("vision request failed: \(error)\n".data(using: .utf8)!)
    exit(3)
}

guard let obs = request.results?.first else {
    FileHandle.standardError.write("no foreground found\n".data(using: .utf8)!)
    exit(4)
}

do {
    let maskBuffer = try obs.generateScaledMaskForImage(forInstances: obs.allInstances,
                                                        from: handler)
    let ci = CIImage(cvPixelBuffer: maskBuffer)
    let ctx = CIContext()
    guard let cg = ctx.createCGImage(ci, from: ci.extent) else {
        FileHandle.standardError.write("mask conversion failed\n".data(using: .utf8)!)
        exit(5)
    }
    let url = URL(fileURLWithPath: outPath) as CFURL
    guard let dest = CGImageDestinationCreateWithURL(url, "public.png" as CFString, 1, nil) else {
        FileHandle.standardError.write("cannot create output\n".data(using: .utf8)!)
        exit(6)
    }
    CGImageDestinationAddImage(dest, cg, nil)
    if !CGImageDestinationFinalize(dest) {
        FileHandle.standardError.write("write failed\n".data(using: .utf8)!)
        exit(7)
    }
} catch {
    FileHandle.standardError.write("mask generation failed: \(error)\n".data(using: .utf8)!)
    exit(8)
}
