// ocr: read Android raw screencap (16-byte header + RGBA) or an image file, run Vision OCR.
// usage:
//   ocr <file> [fast]                      -> JSON [{"t","x","y","w","h","c"}] (pixel coords, origin top-left)
//   ocr <file> crops x,y,w,h ...           -> stack crops (2x) on white, OCR once; lines "L\t<x>\t<text>" + "i\t<text>"
//                                             env OCR_GAP (default 60), OCR_V (vertical stacking), OCR_DUMP
//   ocr serve                              -> long-lived: stdin one request per line, response then a line "\u{1}END"
//        request "<file>"                  -> full OCR JSON
//        request "C\t<file>\t<gap>\t<x,y,w,h>\t..." -> crops output (horizontal)
import Foundation
import Vision
import CoreGraphics
import ImageIO

func loadImage(_ path: String) -> CGImage? {
    let url = URL(fileURLWithPath: path)
    if path.hasSuffix(".raw") || path.hasSuffix(".bin") {
        guard let data = try? Data(contentsOf: url), data.count > 16 else { return nil }
        let w = Int(data[0]) | Int(data[1]) << 8 | Int(data[2]) << 16 | Int(data[3]) << 24
        let h = Int(data[4]) | Int(data[5]) << 8 | Int(data[6]) << 16 | Int(data[7]) << 24
        let hdr = data.count - w * h * 4
        guard hdr >= 0 else { return nil }
        let pix = data.subdata(in: hdr..<data.count)
        let provider = CGDataProvider(data: pix as CFData)!
        return CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4,
                       space: CGColorSpaceCreateDeviceRGB(),
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
                       provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    }
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}

func fullOCR(_ img: CGImage, fast: Bool) -> String {
    let W = Double(img.width), H = Double(img.height)
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = fast ? .fast : .accurate
    req.usesLanguageCorrection = false
    if !fast { req.recognitionLanguages = ["zh-Hans", "en-US"] }
    req.minimumTextHeight = 0.008
    do { try VNImageRequestHandler(cgImage: img, options: [:]).perform([req]) } catch { return "[]" }
    var out: [String] = []
    for obs in req.results ?? [] {
        guard let top = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox
        let x = (b.midX) * W, y = (1 - b.midY) * H
        let t = top.string.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        out.append(String(format: "{\"t\":\"%@\",\"x\":%.0f,\"y\":%.0f,\"w\":%.0f,\"h\":%.0f,\"c\":%.2f}",
                          t, x, y, b.width * W, b.height * H, top.confidence))
    }
    return "[" + out.joined(separator: ",") + "]"
}

func cropsOCR(_ img: CGImage, _ rects: [CGRect], gap: Double, horiz: Bool, dump: Bool) -> [String] {
    let sc = 2.0
    let cw = horiz ? rects.reduce(gap) { $0 + $1.width * sc + gap } : (rects.map { $0.width }.max() ?? 10) * sc + gap * 2
    let ch = horiz ? (rects.map { $0.height }.max() ?? 10) * sc + gap * 2 : rects.reduce(gap) { $0 + $1.height * sc + gap }
    let ctx = CGContext(data: nil, width: Int(cw), height: Int(ch), bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: cw, height: ch))
    ctx.interpolationQuality = .high
    var bands: [(Double, Double)] = []   // top-left-origin ranges
    var yTop = gap
    for r in rects {
        if let c = img.cropping(to: r) {
            let h = r.height * sc, w = r.width * sc
            if horiz {
                ctx.draw(c, in: CGRect(x: yTop, y: gap, width: w, height: h))
                bands.append((yTop, yTop + w)); yTop += w + gap
            } else {
                ctx.draw(c, in: CGRect(x: gap, y: ch - yTop - h, width: w, height: h))
                bands.append((yTop, yTop + h)); yTop += h + gap
            }
        }
    }
    let comp = ctx.makeImage()!
    let rq = VNRecognizeTextRequest()
    rq.recognitionLevel = .accurate
    rq.usesLanguageCorrection = false
    rq.recognitionLanguages = ["en-US"]
    rq.customWords = ["10", "J", "Q", "K", "A"]
    try? VNImageRequestHandler(cgImage: comp, options: [:]).perform([rq])
    var lines: [String] = []
    var res = Array(repeating: "", count: bands.count)
    for o in rq.results ?? [] {
        guard let cand = o.topCandidates(1).first else { continue }
        let str = cand.string
        if !horiz {
            let y = (1 - o.boundingBox.midY) * ch
            for (i, b) in bands.enumerated() where y >= b.0 - gap / 2 && y <= b.1 + gap / 2 { res[i] += str }
            continue
        }
        lines.append("L\t\(Int(o.boundingBox.minX * cw))\t\(str)")
        var idx = str.startIndex
        while idx < str.endIndex {
            let nx = str.index(after: idx)
            if str[idx] != " ", let bb = try? cand.boundingBox(for: idx..<nx) {
                let x = bb.boundingBox.midX * cw
                for (i, b) in bands.enumerated() where x >= b.0 - gap / 2 && x <= b.1 + gap / 2 { res[i] += String(str[idx]) }
            }
            idx = nx
        }
    }
    for (i, t) in res.enumerated() { lines.append("\(i)\t\(t)") }
    if dump {
        let dst = CGImageDestinationCreateWithURL(URL(fileURLWithPath: "/tmp/ocr_crops.png") as CFURL,
                                                  "public.png" as CFString, 1, nil)!
        CGImageDestinationAddImage(dst, comp, nil); CGImageDestinationFinalize(dst)
    }
    return lines
}

func parseRects(_ a: ArraySlice<String>) -> [CGRect] {
    return a.compactMap { s in
        let v = s.split(separator: ",").compactMap { Double($0) }
        return v.count == 4 ? CGRect(x: v[0], y: v[1], width: v[2], height: v[3]) : nil
    }
}

let args = CommandLine.arguments
guard args.count >= 2 else { fputs("usage: ocr <raw|png|jpg> [fast] | <file> crops x,y,w,h... | serve\n", stderr); exit(2) }

if args[1] == "serve" {
    // 常驻模式：省掉每次进程启动 + Vision 初始化
    while let line = readLine() {
        if line.isEmpty { continue }
        if line.hasPrefix("C\t") {
            let f = line.split(separator: "\t", omittingEmptySubsequences: false).map(String.init)
            if f.count >= 4, let im = loadImage(f[1]) {
                for l in cropsOCR(im, parseRects(f[3...]), gap: Double(f[2]) ?? 60, horiz: true, dump: false) { print(l) }
            }
        } else {
            if let im = loadImage(line.trimmingCharacters(in: .whitespaces)) { print(fullOCR(im, fast: false)) } else { print("[]") }
        }
        print("\u{1}END")
        fflush(stdout)
    }
    exit(0)
}

guard let img = loadImage(args[1]) else { fputs("cannot load image\n", stderr); exit(4) }
if args.count >= 4 && args[2] == "crops" {
    let env = ProcessInfo.processInfo.environment
    let lines = cropsOCR(img, parseRects(args[3...]), gap: Double(env["OCR_GAP"] ?? "60") ?? 60,
                         horiz: env["OCR_V"] == nil, dump: env["OCR_DUMP"] != nil)
    for l in lines { print(l) }
    exit(0)
}
print(fullOCR(img, fast: args.count >= 3 && args[2] == "fast"))
