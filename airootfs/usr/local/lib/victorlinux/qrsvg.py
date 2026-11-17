"""Minimal QR Code encoder (byte mode, error correction M, versions 1-15) rendering to inline SVG.

Rendered locally in the Control Center so a pairing QR never goes through any web service. Standard library only.
"""
# (EC codewords per block, blocks in group 1, data codewords per block g1, blocks in group 2, data codewords g2)
_M = {1: (10, 1, 16, 0, 0), 2: (16, 1, 28, 0, 0), 3: (26, 1, 44, 0, 0), 4: (18, 2, 32, 0, 0), 5: (24, 2, 43, 0, 0),
      6: (16, 4, 27, 0, 0), 7: (18, 4, 31, 0, 0), 8: (22, 2, 38, 2, 39), 9: (22, 3, 36, 2, 37), 10: (26, 4, 43, 1, 44),
      11: (30, 1, 50, 4, 51), 12: (22, 6, 36, 2, 37), 13: (22, 8, 37, 1, 38), 14: (24, 4, 40, 5, 41), 15: (24, 5, 41, 5, 42)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
          10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62], 14: [6, 26, 46, 66], 15: [6, 26, 48, 70]}
_REMAINDER = {v: (0 if v == 1 else 7 if v <= 6 else 0 if v <= 13 else 3) for v in _M}

_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gmul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_ec(data, n):
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= _gmul(g, _EXP[i])
        gen = nxt
    res = list(data) + [0] * n
    for i in range(len(data)):
        c = res[i]
        if c:
            for j in range(1, len(gen)):
                res[i + j] ^= _gmul(gen[j], c)
    return res[len(data):]


def _capacity(v):
    ec, b1, d1, b2, d2 = _M[v]
    return b1 * d1 + b2 * d2


def _codewords(data, v):
    cc_bits = 8 if v <= 9 else 16
    bits = [0, 1, 0, 0] + [int(b) for b in format(len(data), f"0{cc_bits}b")]
    for byte in data:
        bits += [int(b) for b in format(byte, "08b")]
    cap = _capacity(v) * 8
    bits += [0] * min(4, cap - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    i = 0
    while len(words) < _capacity(v):
        words.append(pad[i % 2])
        i += 1
    ec, b1, d1, b2, d2 = _M[v]
    blocks, pos = [], 0
    for count, size in ((b1, d1), (b2, d2)):
        for _ in range(count):
            blocks.append(words[pos:pos + size])
            pos += size
    ecs = [_rs_ec(b, ec) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec):
        out += [e[i] for e in ecs]
    return out


def _bch(value, poly, bits):
    v = value << bits
    plen = poly.bit_length()
    while v.bit_length() >= plen:
        v ^= poly << (v.bit_length() - plen)
    return (value << bits) | v


_MASKS = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0, lambda r, c: (r + c) % 3 == 0,
          lambda r, c: (r // 2 + c // 3) % 2 == 0, lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
          lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0, lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]


def _base(v):
    n = 17 + 4 * v
    m = [[None] * n for _ in range(n)]

    def finder(r, c):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                rr, cc = r + dr, c + dc
                if 0 <= rr < n and 0 <= cc < n:
                    inside = 0 <= dr <= 6 and 0 <= dc <= 6
                    ring = inside and (dr in (0, 6) or dc in (0, 6))
                    core = 2 <= dr <= 4 and 2 <= dc <= 4
                    m[rr][cc] = bool(ring or core)
    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)
    for i in range(8, n - 8):
        m[6][i] = m[i][6] = i % 2 == 0
    pos = _ALIGN[v]
    last = len(pos) - 1
    for i, r in enumerate(pos):
        for j, c in enumerate(pos):
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = max(abs(dr), abs(dc)) != 1
    m[n - 8][8] = True                      # dark module
    for i in range(9):                      # reserve format areas
        for (r, c) in ((8, i), (i, 8)):
            if m[r][c] is None:
                m[r][c] = False
    for i in range(8):
        if m[8][n - 1 - i] is None:
            m[8][n - 1 - i] = False
        if m[n - 1 - i][8] is None:
            m[n - 1 - i][8] = False
    if v >= 7:
        info = _bch(v, 0x1F25, 12)
        for i in range(18):
            bit = bool((info >> i) & 1)
            r, c = i // 3, i % 3 + n - 11
            m[r][c] = bit
            m[c][r] = bit
    return m


def _place(m, words, v, mask):
    n = len(m)
    reserved = [[cell is not None for cell in row] for row in m]
    out = [row[:] for row in m]
    bits = []
    for w in words:
        bits += [(w >> (7 - i)) & 1 for i in range(8)]
    bits += [0] * _REMAINDER[v]
    k, up, c = 0, True, n - 1
    while c > 0:
        if c == 6:
            c -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for cc in (c, c - 1):
                if not reserved[r][cc]:
                    b = bits[k] if k < len(bits) else 0
                    k += 1
                    out[r][cc] = bool(b) ^ _MASKS[mask](r, cc)
        up = not up
        c -= 2
    fmt = _bch((0b00 << 3) | mask, 0x537, 10) ^ 0x5412      # EC level M = 00
    bit = [bool((fmt >> i) & 1) for i in range(15)]
    for i in range(6):
        out[i][8] = bit[i]
    out[7][8], out[8][8], out[8][7] = bit[6], bit[7], bit[8]
    for i in range(9, 15):
        out[8][14 - i] = bit[i]
    for i in range(8):
        out[8][n - 1 - i] = bit[i]
    for i in range(8, 15):
        out[n - 15 + i][8] = bit[i]
    out[n - 8][8] = True
    return out


def _penalty(m):
    n, p = len(m), 0
    for grid in (m, [list(x) for x in zip(*m)]):
        for row in grid:
            run, prev = 0, None
            for x in row:
                if x == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run, prev = 1, x
            if run >= 5:
                p += run - 2
            s = "".join("1" if x else "0" for x in row)
            p += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                p += 3
    dark = sum(sum(1 for x in row if x) for row in m)
    p += 10 * (abs(dark * 100 // (n * n) - 50) // 5)
    return p


def matrix(text, mask=None):
    data = text.encode("utf-8")
    for v in sorted(_M):
        header = 4 + (8 if v <= 9 else 16)
        if header + 8 * len(data) <= _capacity(v) * 8:
            break
    else:
        raise ValueError("text too long for a QR code here")
    base = _base(v)
    words = _codewords(data, v)
    masks = [mask] if mask is not None else range(8)
    best = min((_place(base, words, v, k) for k in masks), key=_penalty)
    return best


def svg(text, scale=6, quiet=4, dark="#0b0f1a", light="#ffffff"):
    """QR codes need high contrast, so the code itself is dark-on-white inside a small framed tile."""
    m = matrix(text)
    n = len(m)
    size = (n + 2 * quiet) * scale
    path = "".join(f"M{(c + quiet) * scale},{(r + quiet) * scale}h{scale}v{scale}h-{scale}z" for r in range(n) for c in range(n) if m[r][c])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" '
            f'aria-label="Pairing QR code"><rect width="{size}" height="{size}" fill="{light}"/><path fill="{dark}" d="{path}"/></svg>')
