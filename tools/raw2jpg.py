"""raw2jpg.py <in.raw> <out.jpg> [width]"""
import struct, subprocess, sys, zlib
src, dst = sys.argv[1], sys.argv[2]
w0 = sys.argv[3] if len(sys.argv) > 3 else "540"
b = open(src, 'rb').read(); w, h = struct.unpack('<II', b[:8]); px = b[len(b) - w * h * 4:]
raw = b''.join(b'\x00' + px[y * w * 4:(y + 1) * w * 4] for y in range(h))
def ch(t, d):
    c = t + d; return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
png = dst + ".tmp.png"
open(png, 'wb').write(b'\x89PNG\r\n\x1a\n' + ch(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) + ch(b'IDAT', zlib.compress(raw, 1)) + ch(b'IEND', b''))
subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "60", "--resampleWidth", w0, png, "--out", dst], capture_output=True)
import os; os.remove(png)
