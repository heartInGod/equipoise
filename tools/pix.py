"""Pixel helpers: load Android raw screencap or PNG (stdlib only)."""
import struct, zlib


class Img:
    def __init__(self, w, h, data, bpp):
        self.w, self.h, self.d, self.bpp = w, h, data, bpp

    def px(self, x, y):
        o = (y * self.w + x) * self.bpp
        d = self.d
        return d[o], d[o + 1], d[o + 2]


def load_raw(path):
    b = open(path, 'rb').read()
    w, h = struct.unpack('<II', b[:8])
    hdr = len(b) - w * h * 4
    return Img(w, h, memoryview(b)[hdr:], 4)


def load_png(path):
    b = open(path, 'rb').read()
    assert b[:8] == b'\x89PNG\r\n\x1a\n'
    i, idat = 8, []
    while i < len(b):
        n = struct.unpack('>I', b[i:i + 4])[0]
        t = b[i + 4:i + 8]
        c = b[i + 8:i + 8 + n]
        if t == b'IHDR':
            w, h, bd, ct = struct.unpack('>IIBB', c[:10])
        elif t == b'IDAT':
            idat.append(c)
        i += 12 + n
    bpp = {2: 3, 6: 4}[ct]
    raw = zlib.decompress(b''.join(idat))
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if f == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                bb = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                pa, pb, pc = abs(bb - c), abs(a - c), abs(a + bb - 2 * c)
                pr = a if pa <= pb and pa <= pc else (bb if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return Img(w, h, out, bpp)


def load(path):
    return load_png(path) if path.endswith('.png') else load_raw(path)
