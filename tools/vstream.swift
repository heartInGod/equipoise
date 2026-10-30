// 连续视频流截图：拉起一个输出 H.264 Annex-B 裸流的命令（默认 adb exec-out screenrecord），
// VideoToolbox 硬解，内存里只留最新一帧。
// screenrecord 单段最长 180s（系统写死）：每 ROTATE 秒提前起下一段，新段出首帧即切换并关掉旧段（无缝）；
// 段意外退出时立即重起。
// 用法：vstream -- <命令...>     例：vstream -- adb -s 59a4a919 exec-out screenrecord --output-format=h264 --bit-rate 20000000 -
// stdin 每行一条命令，stdout 每条回一行：
//   wait <seq> <ms>   阻塞到 出现序号 > seq 的新帧 / 超时；回 "<seq> <t_recv> <alive> <restarts> <errs>"
//   snap <path>       把最新帧写成 .raw（16 字节头 w,h,1,1 + RGBA，与 screencap 相同；原子替换）；回 "ok <seq> <w> <h>" 或 "none"
// 编译：cd tools && swiftc -O vstream.swift -o vstream
import Foundation
import VideoToolbox
import CoreMedia
import Accelerate

setvbuf(stdout, nil, _IOLBF, 0)
func elog(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

let argv = CommandLine.arguments
guard let dd = argv.firstIndex(of: "--"), dd + 1 < argv.count else { elog("usage: vstream -- <cmd...>"); exit(2) }
let cmd = Array(argv[(dd + 1)...])

final class Shared {
    let cond = NSCondition()
    var pb: CVPixelBuffer?
    var seq = 0, restarts = 0, errs = 0
    var tRecv = 0.0
    var alive = false
    var activeGen = 0, endedGen = 0
    var procs: [Int: Process] = [:]
}
let S = Shared()
let ROTATE = Double(ProcessInfo.processInfo.environment["VSTREAM_ROTATE"] ?? "") ?? 170.0   // 测试可调小

// ---------------- 解码 ----------------
final class Decoder {
    let gen: Int
    init(_ g: Int) { gen = g }
    var sps: [UInt8]?, pps: [UInt8]?
    var fmt: CMVideoFormatDescription?
    var session: VTDecompressionSession?
    var au: [[UInt8]] = []
    var tAU = 0.0

    func reset() {
        if let s = session { VTDecompressionSessionInvalidate(s) }
        session = nil; fmt = nil; sps = nil; pps = nil; au = []
    }

    func makeSession() {
        guard let s = sps, let p = pps else { return }
        var f: CMFormatDescription?
        let st: OSStatus = s.withUnsafeBufferPointer { sp in
            p.withUnsafeBufferPointer { pp in
                let ptrs: [UnsafePointer<UInt8>] = [sp.baseAddress!, pp.baseAddress!]
                let sizes: [Int] = [s.count, p.count]
                return CMVideoFormatDescriptionCreateFromH264ParameterSets(allocator: kCFAllocatorDefault, parameterSetCount: 2,
                    parameterSetPointers: ptrs, parameterSetSizes: sizes, nalUnitHeaderLength: 4, formatDescriptionOut: &f)
            }
        }
        guard st == noErr, let nf = f else { elog("fmt err \(st)"); return }
        if session != nil, let old = fmt, CMFormatDescriptionEqual(old, otherFormatDescription: nf) { return }
        if let o = session { VTDecompressionSessionInvalidate(o); session = nil }
        fmt = nf
        let attrs: [CFString: Any] = [kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA]
        var ss: VTDecompressionSession?
        let r = VTDecompressionSessionCreate(allocator: kCFAllocatorDefault, formatDescription: nf, decoderSpecification: nil,
                                             imageBufferAttributes: attrs as CFDictionary, outputCallback: nil, decompressionSessionOut: &ss)
        if r != noErr { elog("session err \(r)"); return }
        session = ss
        let d = CMVideoFormatDescriptionGetDimensions(nf)
        elog("format \(d.width)x\(d.height)")
    }

    func nal(_ n: [UInt8], _ t: Double) {
        guard !n.isEmpty else { return }
        switch n[0] & 0x1f {
        case 7: flush(); sps = n
        case 8: flush(); pps = n; makeSession()
        case 1, 5:
            if n.count > 1 && (n[1] & 0x80) != 0 { flush() }     // first_mb_in_slice == 0：新的一帧
            if au.isEmpty { tAU = t }
            au.append(n)
        default: break
        }
    }

    func flush() {
        guard !au.isEmpty else { return }
        let nals = au; au = []
        guard let ss = session, let f = fmt else { return }
        var data = [UInt8]()
        data.reserveCapacity(nals.reduce(0) { $0 + $1.count + 4 })
        for n in nals {
            let l = UInt32(n.count)
            data += [UInt8(l >> 24), UInt8((l >> 16) & 0xff), UInt8((l >> 8) & 0xff), UInt8(l & 0xff)]
            data += n
        }
        var bb: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(allocator: kCFAllocatorDefault, memoryBlock: nil, blockLength: data.count,
                blockAllocator: kCFAllocatorDefault, customBlockSource: nil, offsetToData: 0, dataLength: data.count,
                flags: 0, blockBufferOut: &bb) == noErr, let b = bb else { return }
        _ = data.withUnsafeBytes { CMBlockBufferReplaceDataBytes(with: $0.baseAddress!, blockBuffer: b, offsetIntoDestination: 0, dataLength: data.count) }
        var sb: CMSampleBuffer?
        var sz = data.count
        guard CMSampleBufferCreateReady(allocator: kCFAllocatorDefault, dataBuffer: b, formatDescription: f, sampleCount: 1,
                sampleTimingEntryCount: 0, sampleTimingArray: nil, sampleSizeEntryCount: 1, sampleSizeArray: &sz,
                sampleBufferOut: &sb) == noErr, let s = sb else { return }
        let t = tAU
        let r = VTDecompressionSessionDecodeFrame(ss, sampleBuffer: s, flags: [], infoFlagsOut: nil) { status, _, img, _, _ in
            S.cond.lock()
            if status == noErr, let img = img {
                let g = self.gen
                if g > S.activeGen {                             // 新段出首帧：切换，关掉旧段
                    S.activeGen = g
                    for (k, p) in S.procs where k < g { p.terminate() }
                    if g > 1 { elog("rotated to segment " + String(g)) }
                }
                if g == S.activeGen { S.pb = img; S.seq += 1; S.tRecv = t; S.alive = true }
            } else { S.errs += 1 }
            S.cond.broadcast(); S.cond.unlock()
        }
        if r != noErr { S.cond.lock(); S.errs += 1; S.cond.unlock() }
        VTDecompressionSessionWaitForAsynchronousFrames(ss)
    }
}

// ---------------- 读流 ----------------
func runGen(_ gen: Int) {
    let dec = Decoder(gen)
    var chunk = [UInt8](repeating: 0, count: 1 << 20)
    do {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = cmd
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch {
            elog("spawn err " + String(describing: error))
            S.cond.lock(); S.endedGen = max(S.endedGen, gen); S.cond.broadcast(); S.cond.unlock(); return
        }
        S.cond.lock(); S.procs[gen] = p; S.cond.unlock()
        let fd = pipe.fileHandleForReading.fileDescriptor
        var buf = [UInt8](), nalStart = -1, scan = 0
        while true {
            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let pr = poll(&pfd, 1, 30)
            let now = Date().timeIntervalSince1970
            if pr == 0 {                                         // 30ms 没数据：当前 NAL / 当前帧已完整
                if nalStart >= 0 && buf.count > nalStart { dec.nal(Array(buf[nalStart...]), now) }
                buf.removeAll(keepingCapacity: true); nalStart = -1; scan = 0
                dec.flush(); continue
            }
            if pr < 0 { if errno == EINTR { continue }; break }
            let n = chunk.withUnsafeMutableBytes { read(fd, $0.baseAddress!, $0.count) }
            if n <= 0 { break }
            buf.append(contentsOf: chunk[0..<n])
            var i = max(scan, 0)
            while i + 3 <= buf.count {
                if buf[i] == 0 && buf[i + 1] == 0 && buf[i + 2] == 1 {
                    if nalStart >= 0 {
                        var e = i
                        while e > nalStart && buf[e - 1] == 0 { e -= 1 }
                        dec.nal(Array(buf[nalStart..<e]), now)
                    }
                    nalStart = i + 3; i += 3
                } else { i += 1 }
            }
            scan = i
            if nalStart > 0 { buf.removeFirst(nalStart); scan -= nalStart; nalStart = 0 }
            else if nalStart < 0 && buf.count > 4 { let k = buf.count - 3; buf.removeFirst(k); scan = max(0, scan - k) }
        }
        p.terminate(); p.waitUntilExit()
        dec.flush(); dec.reset()
        S.cond.lock()
        S.procs[gen] = nil; S.endedGen = max(S.endedGen, gen)
        let wasActive = gen >= S.activeGen
        if wasActive { S.alive = false; S.restarts += 1 }
        let r = S.restarts
        S.cond.broadcast(); S.cond.unlock()
        if wasActive { elog("stream ended (restart " + String(r) + ")") }
    }
}

// 每 ROTATE 秒起下一段（旧段在新段出首帧时关闭）；当前段意外退出则立即起下一段
func supervisor() {
    var g = 0
    while true {
        g += 1
        let gen = g, started = Date()
        Thread { runGen(gen) }.start()
        let until = started.addingTimeInterval(ROTATE)
        S.cond.lock()
        while S.endedGen < gen && Date() < until { _ = S.cond.wait(until: until) }
        S.cond.unlock()
        if Date().timeIntervalSince(started) < 1 { usleep(500_000) }   // 起不来时别空转
    }
}

// ---------------- 写帧 ----------------
func snap(_ path: String) -> String {
    S.cond.lock(); let pb = S.pb, seq = S.seq; S.cond.unlock()
    guard let img = pb else { return "none" }
    CVPixelBufferLockBaseAddress(img, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(img, .readOnly) }
    let w = CVPixelBufferGetWidth(img), h = CVPixelBufferGetHeight(img), bpr = CVPixelBufferGetBytesPerRow(img)
    guard let base = CVPixelBufferGetBaseAddress(img) else { return "none" }
    var out = Data(count: 16 + w * h * 4)
    out.withUnsafeMutableBytes { (raw: UnsafeMutableRawBufferPointer) in
        let hdr = raw.bindMemory(to: UInt32.self)
        hdr[0] = UInt32(w).littleEndian; hdr[1] = UInt32(h).littleEndian; hdr[2] = 1; hdr[3] = 1
        var src = vImage_Buffer(data: base, height: vImagePixelCount(h), width: vImagePixelCount(w), rowBytes: bpr)
        var dst = vImage_Buffer(data: raw.baseAddress! + 16, height: vImagePixelCount(h), width: vImagePixelCount(w), rowBytes: w * 4)
        let map: [UInt8] = [2, 1, 0, 3]                         // BGRA -> RGBA
        vImagePermuteChannels_ARGB8888(&src, &dst, map, vImage_Flags(kvImageNoFlags))
    }
    do { try out.write(to: URL(fileURLWithPath: path), options: .atomic) } catch { return "err \(error)" }
    return "ok \(seq) \(w) \(h)"
}

// 退出（stdin 关闭 / SIGTERM / SIGINT / SIGHUP）时先关掉所有 screenrecord 子进程，避免手机上留孤儿录屏
func shutdown() -> Never {
    S.cond.lock(); let ps = Array(S.procs.values); S.cond.unlock()
    for p in ps { p.terminate() }
    exit(0)
}
var sigSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT, SIGHUP] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .global())
    src.setEventHandler { shutdown() }
    src.resume(); sigSources.append(src)
}

Thread { supervisor() }.start()

while let line = readLine() {
    let a = line.split(separator: " ").map(String.init)
    guard let c = a.first else { continue }
    if c == "wait" {
        let since = a.count > 1 ? Int(a[1]) ?? 0 : 0
        let ms = a.count > 2 ? Double(a[2]) ?? 0 : 0
        let deadline = Date(timeIntervalSinceNow: ms / 1000)
        S.cond.lock()
        while S.seq <= since && Date() < deadline { _ = S.cond.wait(until: deadline) }
        let r = "\(S.seq) \(String(format: "%.3f", S.tRecv)) \(S.alive ? 1 : 0) \(S.restarts) \(S.errs)"
        S.cond.unlock()
        print(r)
    } else if c == "snap", a.count > 1 {
        print(snap(a[1]))
    } else {
        print("bad")
    }
}
shutdown()
