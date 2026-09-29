#!/usr/bin/env python3
"""
UZip (Ultrazip) v3 - a max-ratio archiver that writes .uz files.

What it does that 7-Zip does not:
  * Recompression. Files already packed with older algorithms are turned
    back into raw data, and UZip records the exact settings needed to
    rebuild the original bytes. The raw data then gets a far stronger
    codec. On extract every file is rebuilt bit-for-bit and checked with
    SHA-256. Supported:
        Deflate  - ZIP, DOCX, XLSX, PPTX, ODT, JAR, APK, EPUB, PNG, GZ,
                   TGZ, PDF streams, SVGZ, zlib data inside any file
        bzip2    - .bz2, .tbz2, bzip2 streams inside any file
        xz       - .xz, .txz, xz streams inside any file
    Streams are found anywhere inside a file, so a .tar holding PNGs,
    a ZIP holding PNGs or a .tar.gz of Office files is handled too, and
    recompression nests two levels deep (a PNG inside a DOCX inside a
    .tar.gz is rebuilt).
  * PPMd for text. Text and source code go to a PPMd model (order 32/64),
    which beats LZMA2 on text by 10-20%. Needs: pip install pyppmd
  * Per-type blocks. Executables, text, recompressed data and everything
    else go into separate solid blocks, and each block races its own set
    of codecs; the smallest wins.
  * BCJ2-style executable filter written for UZip: x86 CALL, JMP and
    conditional-jump targets are made absolute and split into separate
    streams (like 7-Zip's BCJ2), so the code compresses better.
  * Whole-file dedup: identical files are stored once, however far apart
    they are (7-Zip only catches repeats inside its dictionary window).

Recompression note: rebuilding a Deflate stream exactly depends on the
zlib library. Archives record the zlib version they were made with. If
you extract on a machine whose Python uses a different zlib family (for
example zlib-ng, used by some Python 3.14 Windows builds), recompressed
files may fail their checksum. UZip then refuses to write a wrong file
and tells you. Use --no-rc when making archives that must open anywhere.

Format v3 (.uz, little-endian):
  "UZ01" | version u8 | zlib_version_len u8 | zlib_version | nblocks u16
  per block: codec u8 | class u8 | raw_size u64 | comp_size u64 | sha256
  then the block payloads in order.
  Each block's raw data is a list of entries:
    type u8 | path_len u16 | mode u32 | mtime i64 | size u64 | path | data
    types: 1 file, 2 dir, 3 recompressed file, 4 duplicate (data = path
    of the original); a type 0 entry ends the block.
  Versions 1 and 2 (single block) are still read.

Usage:
  python uzip.py c backup.uz folder file1 file2   # compress (max by default)
  python uzip.py c -l normal out.uz bigfolder      # faster, nearly as small
  python uzip.py c --no-rc out.uz folder           # skip recompression
  python uzip.py x backup.uz -o restore_dir        # extract
  python uzip.py l backup.uz                       # list
  python uzip.py t backup.uz                       # test integrity
  python uzip_gui.py [backup.uz]                   # graphical viewer

Optional extras (UZip works without them):
  pip install pyppmd zstandard brotli
Archives that used PPMd, zstd or brotli need that module to be extracted.

Limits: archives are built in memory, so inputs should fit comfortably
in RAM. No compressor can shrink every file. JPG, MP4, MP3, and 7z or
RAR archives barely move (see README notes in the chat for why).
"""

import argparse
import bz2
import hashlib
import lzma
import os
import posixpath
import re
import struct
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

try:
    import zstandard as _zstd
except ImportError:
    _zstd = None
try:
    import brotli as _brotli
except ImportError:
    _brotli = None
try:
    import pyppmd as _ppmd
except ImportError:
    _ppmd = None

MAGIC = b"UZ01"
VERSION = 3
HEADER_V1 = struct.Struct("<4sBBQ32s")
BLOCK = struct.Struct("<BBQQ32s")
ENTRY = struct.Struct("<BHIqQ")
T_END, T_FILE, T_DIR, T_FILE_RC, T_DUP = 0, 1, 2, 3, 4

CL_META, CL_EXE, CL_TEXT, CL_RC, CL_OTHER = 0, 1, 2, 3, 4
CLASS_NAMES = {CL_META: "folders+dups", CL_EXE: "executables", CL_TEXT: "text",
               CL_RC: "recompressed", CL_OTHER: "other"}

DICT_CAP = 64 << 20  # 64 MiB default LZMA dictionary cap
SAMPLE = 8 << 20     # big blocks race codecs on a sample first


class UZError(Exception):
    pass


def pct(a, b):
    return 100.0 * a / b if b else 100.0


# ================================================================ LZMA helpers

def _dict_size(n):
    d = 1 << 16
    while d < n and d < DICT_CAP:
        d <<= 1
    return min(d, DICT_CAP)


def _lzma2(n, **extra):
    f = {"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME,
         "dict_size": _dict_size(n), "mf": lzma.MF_BT4, "nice_len": 273}
    f.update(extra)
    return f


def _xzc(data, pre=(), **extra):
    return lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_NONE,
                         filters=list(pre) + [_lzma2(len(data), **extra)])


def _xz(pre=(), **extra):
    return lambda d: _xzc(d, pre, **extra)


def _xz_dec(data, raw_size):
    return lzma.decompress(data, format=lzma.FORMAT_XZ)


# ================================================================ BCJ2-style filter
#
# x86 CALL (E8), JMP (E9) and Jcc (0F 80..8F) carry a 32-bit relative
# target. When the target lands inside the data, the relative value is
# turned into an absolute big-endian address and moved to a side stream.
# Calls to the same function then look identical, which LZMA loves, and
# the main code stream loses the noisy address bytes.

_BCJ_ENC = re.compile(rb"[\xe8\xe9]|\x0f[\x80-\x8f]")
_BCJ_DEC = re.compile(rb"[\xe8\xe9\x80-\x8f]")
_BCJ_HEAD = struct.Struct("<5Q")


def bcj2_split(data):
    n = len(data)
    main, flags, calls, jumps = bytearray(), bytearray(), bytearray(), bytearray()
    i = 0
    for m in _BCJ_ENC.finditer(data):
        p = m.end() - 1
        if p < i:
            continue
        if p + 5 > n:
            break
        target = p + 5 + int.from_bytes(data[p + 1:p + 5], "little", signed=True)
        if 0 <= target < n:
            flags.append(1)
            main += data[i:p + 1]
            (calls if data[p] == 0xE8 else jumps).extend(target.to_bytes(4, "big"))
            i = p + 5
        else:
            flags.append(0)
    main += data[i:]
    return n, bytes(main), bytes(flags), bytes(calls), bytes(jumps)


def bcj2_join(n, main, flags, calls, jumps):
    out = bytearray()
    mpos = fi = ci = ji = 0
    while True:
        m = _BCJ_DEC.search(main, mpos)
        if not m:
            break
        p = m.start()
        op = main[p]
        out += main[mpos:p + 1]
        mpos = p + 1
        pos = len(out) - 1
        if 0x80 <= op <= 0x8F and (pos == 0 or out[pos - 1] != 0x0F):
            continue
        if pos + 5 > n:
            continue
        f = flags[fi]
        fi += 1
        if f:
            if op == 0xE8:
                target = int.from_bytes(calls[ci:ci + 4], "big")
                ci += 4
            else:
                target = int.from_bytes(jumps[ji:ji + 4], "big")
                ji += 4
            out += (target - (pos + 5)).to_bytes(4, "little", signed=True)
    out += main[mpos:]
    return bytes(out)


def _bcj2_comp(data):
    n, main, flags, calls, jumps = bcj2_split(data)
    side = {"lc": 0, "lp": 2, "pb": 2}
    parts = [_xzc(main), _xzc(flags), _xzc(calls, **side), _xzc(jumps, **side)]
    return _BCJ_HEAD.pack(n, *map(len, parts)) + b"".join(parts)


def _bcj2_dec(data, raw_size):
    n, *lens = _BCJ_HEAD.unpack_from(data, 0)
    pos, streams = _BCJ_HEAD.size, []
    for ln in lens:
        streams.append(lzma.decompress(data[pos:pos + ln]))
        pos += ln
    return bcj2_join(n, *streams)


# ================================================================ PPMd

# pyppmd is not thread-safe: two encoders running at once corrupt output
_PPMD_LOCK = threading.Lock()


def _ppmd_mem(n):
    m = 16 << 20
    while m < n * 64 and m < (192 << 20):
        m <<= 1
    return m


def _ppmd_comp(order):
    def comp(data):
        mem = _ppmd_mem(len(data))
        with _PPMD_LOCK:
            enc = _ppmd.Ppmd7Encoder(order, mem)
            body = enc.encode(data) + enc.flush(endmark=True)
        return struct.pack("<BI", order, mem) + body
    return comp


def _ppmd_dec(data, raw_size):
    if _ppmd is None:
        raise UZError("this archive uses PPMd: pip install pyppmd")
    order, mem = struct.unpack_from("<BI", data, 0)
    with _PPMD_LOCK:
        dec = _ppmd.Ppmd7Decoder(order, mem)
        out = bytearray(dec.decode(bytes(data[5:]), raw_size))
        while len(out) < raw_size and not dec.eof:
            more = dec.decode(b"", raw_size - len(out))
            if not more:
                break
            out += more
    return bytes(out)


# ================================================================ codec table

CODECS = {
    0: ("store", lambda d: bytes(d), lambda d, n: bytes(d)),
    1: ("zlib-9", lambda d: zlib.compress(d, 9), lambda d, n: zlib.decompress(d)),
    2: ("bzip2-9", lambda d: bz2.compress(d, 9), lambda d, n: bz2.decompress(d)),
    3: ("lzma2-ultra", _xz(), _xz_dec),
    4: ("lzma2-text", _xz(lc=4, lp=0, pb=0), _xz_dec),
    5: ("bcj-x86+lzma2", _xz(pre=[{"id": lzma.FILTER_X86}]), _xz_dec),
    6: ("delta4+lzma2", _xz(pre=[{"id": lzma.FILTER_DELTA, "dist": 4}]), _xz_dec),
    9: ("lzma2-fast", lambda d: lzma.compress(d, preset=6, check=lzma.CHECK_NONE),
        _xz_dec),
    11: ("bcj2+lzma2", _bcj2_comp, _bcj2_dec),
}
if _zstd is not None:
    CODECS[7] = ("zstd-22",
                 lambda d: _zstd.ZstdCompressor(level=22, write_content_size=True).compress(d),
                 lambda d, n: _zstd.ZstdDecompressor().decompress(d, max_output_size=n))
if _brotli is not None:
    CODECS[8] = ("brotli-11", lambda d: _brotli.compress(d, quality=11, lgwin=24),
                 lambda d, n: _brotli.decompress(d))
if _ppmd is not None:
    CODECS[10] = ("ppmd-o32", _ppmd_comp(32), _ppmd_dec)
    CODECS[12] = ("ppmd-o64", _ppmd_comp(64), _ppmd_dec)
# names for codecs that may be missing here but can appear in archives
CODEC_NAMES = {7: "zstd-22", 8: "brotli-11", 10: "ppmd-o32", 12: "ppmd-o64"}
CODEC_NAMES.update({k: v[0] for k, v in CODECS.items()})

PLAN = {
    "max": {CL_META: [3], CL_EXE: [11, 5, 3], CL_TEXT: [12, 10, 4, 3],
            CL_RC: [3, 4, 5, 6, 10, 12, 2, 7, 8],
            CL_OTHER: [3, 4, 5, 6, 11, 10, 2, 1, 7, 8]},
    "normal": {CL_META: [3], CL_EXE: [11], CL_TEXT: [12, 4], CL_RC: [3, 12],
               CL_OTHER: [3]},
    "fast": {CL_META: [9], CL_EXE: [9], CL_TEXT: [9], CL_RC: [9], CL_OTHER: [9]},
}
LEVELS = list(PLAN)


def codec_name(cid):
    return CODEC_NAMES.get(cid, "codec-%d" % cid)


def codec_by_name(name):
    for cid, (cname, _, _) in CODECS.items():
        if cname == name:
            return cid
    raise UZError("unknown or unavailable codec '%s'. available: %s"
                  % (name, ", ".join(c[0] for c in CODECS.values())))


def block_codecs(level, cl):
    ids = [c for c in PLAN[level][cl] if c in CODECS]
    return ids or [3]


def race(data, codec_ids, jobs, log=print):
    """Compress with each codec. Returns [(cid, payload)] smallest first,
    always ending with 'store' as a last resort."""
    codec_ids = [c for c in codec_ids if c in CODECS]

    def run(cid, buf):
        t = time.time()
        try:
            out = CODECS[cid][1](buf)
        except Exception:
            out = None
        return cid, out, time.time() - t

    def run_all(ids, buf):
        if len(ids) == 1:
            return [run(ids[0], buf)]
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            return list(ex.map(lambda c: run(c, buf), ids))

    if len(codec_ids) > 2 and len(data) > 2 * SAMPLE:
        trial = [r for r in run_all(codec_ids, data[:SAMPLE]) if r[1] is not None]
        trial.sort(key=lambda r: len(r[1]))
        keep = [r[0] for r in trial[:2]]
        # a sample cannot see long-distance repeats, so LZMA2 always gets a full run
        if 3 in codec_ids and 3 not in keep:
            keep.append(3)
        codec_ids = keep
        if log:
            log("    sample race picked: %s" % ", ".join(codec_name(c) for c in codec_ids))

    results = []
    for cid, out, secs in run_all(codec_ids, data):
        if out is None:
            continue
        if log:
            log("    %-15s %12d bytes  %6.2f%%  %6.2fs"
                % (codec_name(cid), len(out), pct(len(out), len(data)), secs))
        results.append((cid, out))
    results.append((0, bytes(data)))
    results.sort(key=lambda r: len(r[1]))
    return results


# ================================================================ recompression
#
# A file is described as a list of segments: literal bytes, or a
# compressed stream stored as its raw data plus the exact settings that
# rebuild it. A stream's raw data may itself be a segment list (nesting).

SEG_LIT, SEG_DEFLATE, SEG_PNG, SEG_BZ2, SEG_XZ = 0, 1, 2, 3, 4
NESTED = 0x80
SEG_NAMES = {SEG_DEFLATE: "deflate", SEG_PNG: "png", SEG_BZ2: "bzip2", SEG_XZ: "xz"}
RC_HEAD = struct.Struct("<2sQ32sI")      # magic, original size, sha256, nseg
RC_PARAMS = struct.Struct("<BBBb")       # level, memLevel, strategy, wbits
PNG_SIG = b"\x89PNG\r\n\x1a\n"
ZIP_LOCAL = struct.Struct("<4sHHHHHIIIHH")

SKIP_EXT = {
    ".jpg", ".jpeg", ".mp3", ".mp4", ".m4a", ".m4v", ".mkv", ".avi", ".mov",
    ".webm", ".webp", ".7z", ".zst", ".lz", ".lzma", ".rar", ".ace", ".uz",
    ".heic", ".avif", ".flac", ".ogg", ".opus", ".aac", ".wma", ".wmv",
}
RC_MAX_FILE = 512 << 20
RC_MAX_RAW = 1024 << 20
XZ_MAX_RAW = 128 << 20       # rebuilding xz is slow; skip giant xz streams
MIN_STREAM = 64
GIVE_UP_AFTER = 8
MAX_DEPTH = 2

LEVEL_ORDER = [6, 9, 1, 5, 4, 8, 7, 3, 2]
MEM_ORDER = [8, 9, 7, 6, 5, 4, 3, 2, 1]
FLEVEL_HINT = {0: [1], 1: [5, 4, 3, 2], 2: [6], 3: [9, 8, 7]}
XZ_CHECKS = {0: lzma.CHECK_NONE, 1: lzma.CHECK_CRC32, 4: lzma.CHECK_CRC64,
             10: lzma.CHECK_SHA256}
XZ_PRESETS = [6, 9, 1, 0, 2, 3, 4, 5, 7, 8]

RX_ZLIB = re.compile(rb"\x78[\x01\x5e\x9c\xda]")
RX_GZIP = re.compile(rb"\x1f\x8b\x08")
RX_BZ2 = re.compile(rb"BZh[1-9]1AY&SY")
RX_XZ = re.compile(rb"\xfd7zXZ\x00")


def _stream_matches(co, raw, target, step=1 << 16):
    """Feed raw to compressor object co; True if output equals target.
    Stops at the first differing byte, so wrong guesses are cheap."""
    pos, n = 0, len(target)
    for i in range(0, len(raw), step):
        out = co.compress(raw[i:i + step])
        if out:
            end = pos + len(out)
            if end > n or target[pos:end] != out:
                return False
            pos = end
    out = co.flush()
    return pos + len(out) == n and target[pos:] == out


def _decode_at(dec, view, pos, limit=RC_MAX_RAW):
    """Run a decompressor object from view[pos] to the end of its stream.
    Returns (raw, end) or None."""
    parts, total, i, n = [], 0, pos, len(view)
    try:
        while not dec.eof:
            if i >= n:
                return None
            chunk = view[i:i + 65536]
            out = dec.decompress(chunk)
            i += len(chunk)
            if out:
                total += len(out)
                if total > limit:
                    return None
                parts.append(out)
    except (zlib.error, OSError, EOFError, ValueError, lzma.LZMAError):
        return None
    return b"".join(parts), i - len(dec.unused_data)


def _deflate(raw, p):
    level, mem, strat, wbits = p
    co = zlib.compressobj(level, zlib.DEFLATED, wbits, mem, strat)
    return co.compress(raw) + co.flush()


def _find_deflate(raw, target, hints, wbits, strategies, last):
    levels = list(hints) + [lv for lv in LEVEL_ORDER if lv not in hints]
    combos = [(lv, mem, st, wbits)
              for mem in MEM_ORDER for st in strategies for lv in levels]
    if last in combos:
        combos.remove(last)
        combos.insert(0, last)
    for p in combos:
        try:
            co = zlib.compressobj(p[0], zlib.DEFLATED, p[3], p[1], p[2])
        except (ValueError, zlib.error):
            continue
        if _stream_matches(co, raw, target, 1 << 15):
            return p
    return None


def _gzip_header_end(data, pos):
    if pos + 18 > len(data):
        return None
    flg, xfl, p = data[pos + 3], data[pos + 8], pos + 10
    if flg & 0xE0:
        return None
    try:
        if flg & 4:
            p += 2 + struct.unpack_from("<H", data, p)[0]
        if flg & 8:
            p = data.index(b"\0", p) + 1
        if flg & 16:
            p = data.index(b"\0", p) + 1
        if flg & 2:
            p += 2
    except (ValueError, struct.error):
        return None
    hints = [9] if xfl == 2 else [1] if xfl == 4 else []
    return p, hints


def _candidates(data):
    """(start, kind, info) for every plausible stream in data, by position."""
    c = []
    for m in RX_GZIP.finditer(data):
        g = _gzip_header_end(data, m.start())
        if g:
            c.append((g[0], "gzip", g[1]))
    i = data.find(b"PK\x03\x04")
    while i != -1:
        if i + ZIP_LOCAL.size <= len(data):
            f = ZIP_LOCAL.unpack_from(data, i)
            if f[3] == 8:
                c.append((i + ZIP_LOCAL.size + f[9] + f[10], "zipdeflate", []))
        i = data.find(b"PK\x03\x04", i + 4)
    for m in RX_ZLIB.finditer(data):
        c.append((m.start() + 2, "zlib", FLEVEL_HINT[data[m.start() + 1] >> 6]))
    i = data.find(PNG_SIG)
    while i != -1:
        c.append((i, "png", None))
        i = data.find(PNG_SIG, i + 8)
    for m in RX_BZ2.finditer(data):
        c.append((m.start(), "bz2", None))
    for m in RX_XZ.finditer(data):
        c.append((m.start(), "xz", None))
    c.sort(key=lambda t: t[0])
    return c


def _try_deflate(data, view, start, kind, hints, lastp):
    r = _decode_at(zlib.decompressobj(-15), view, start)
    if not r:
        return None
    raw, end = r
    if len(raw) < MIN_STREAM:
        return None
    if kind == "zlib":
        if end + 4 > len(data) or struct.unpack_from(">I", data, end)[0] != zlib.adler32(raw):
            return None
    elif kind == "gzip":
        if (end + 8 > len(data)
                or struct.unpack_from("<II", data, end) != (zlib.crc32(raw),
                                                            len(raw) & 0xFFFFFFFF)):
            return None
    p = _find_deflate(raw, view[start:end], hints, -15, [zlib.Z_DEFAULT_STRATEGY], lastp)
    if p is None:
        return "miss"
    return start, end, [SEG_DEFLATE, p, raw]


def _try_png(data, view, start):
    pos, idat, first, run_end = start + 8, [], None, None
    while pos + 12 <= len(data):
        ln = struct.unpack_from(">I", data, pos)[0]
        typ = data[pos + 4:pos + 8]
        end = pos + 12 + ln
        if end > len(data) or not typ.isalpha():
            return None
        if typ == b"IDAT":
            if first is None:
                first = pos
            elif pos != run_end:
                return None
            if struct.unpack_from(">I", data, pos + 8 + ln)[0] != \
                    zlib.crc32(view[pos + 4:pos + 8 + ln]):
                return None
            idat.append((pos + 8, ln))
            run_end = end
        if typ == b"IEND":
            break
        pos = end
    if not idat:
        return None
    zs = b"".join(view[o:o + ln] for o, ln in idat)
    if len(zs) < 8:
        return None
    cmf, flg = zs[0], zs[1]
    if cmf & 0x0F != 8 or (cmf * 256 + flg) % 31 or flg & 0x20:
        return None
    wbits = -((cmf >> 4) + 8)
    r = _decode_at(zlib.decompressobj(wbits), memoryview(zs), 2)
    if not r:
        return None
    raw, end = r
    if end != len(zs) - 4 or struct.unpack_from(">I", zs, end)[0] != zlib.adler32(raw):
        return None
    p = _find_deflate(raw, memoryview(zs)[2:end], FLEVEL_HINT[flg >> 6], wbits,
                      [zlib.Z_FILTERED, zlib.Z_DEFAULT_STRATEGY], None)
    if p is None:
        return "miss"
    return first, run_end, [SEG_PNG, p, zs[:2], zs[-4:], [ln for _, ln in idat], raw]


def _try_bz2(data, view, start):
    r = _decode_at(bz2.BZ2Decompressor(), view, start)
    if not r:
        return None
    raw, end = r
    level = data[start + 3] - 48
    if len(raw) < MIN_STREAM:
        return None
    if not _stream_matches(bz2.BZ2Compressor(level), raw, view[start:end]):
        return "miss"
    return start, end, [SEG_BZ2, level, raw]


def _try_xz(data, view, start):
    r = _decode_at(lzma.LZMADecompressor(format=lzma.FORMAT_XZ), view, start)
    if not r:
        return None
    raw, end = r
    if len(raw) < MIN_STREAM or len(raw) > XZ_MAX_RAW:
        return None
    chk_id = data[start + 7] & 0x0F
    if chk_id not in XZ_CHECKS:
        return "miss"
    target = view[start:end]
    for extreme in (0, lzma.PRESET_EXTREME):
        for p in XZ_PRESETS:
            co = lzma.LZMACompressor(format=lzma.FORMAT_XZ, check=XZ_CHECKS[chk_id],
                                     preset=p | extreme)
            if _stream_matches(co, raw, target, 1 << 18):
                return start, end, [SEG_XZ, p | (0x80 if extreme else 0), chk_id, raw]
    return "miss"


def _analyze(data, stats, depth=0):
    """Segment list for data, or None if no stream could be reproduced."""
    view = memoryview(data)
    segs, last, misses, hits, lastp = [], 0, 0, 0, None
    for start, kind, info in _candidates(data):
        if start < last or start >= len(data):
            continue
        if kind in ("gzip", "zipdeflate", "zlib"):
            r = _try_deflate(data, view, start, kind, info, lastp)
        elif kind == "png":
            r = _try_png(data, view, start)
        elif kind == "bz2":
            r = _try_bz2(data, view, start)
        else:
            r = _try_xz(data, view, start)
        if r is None:
            continue
        if r == "miss":
            misses += 1
            stats["misses"] += 1
            if misses >= GIVE_UP_AFTER and not hits:
                break
            continue
        s0, s1, seg = r
        if seg[0] == SEG_DEFLATE:
            lastp = seg[1]
        raw = seg[-1]
        if depth + 1 < MAX_DEPTH and len(raw) >= 128:
            sub = _analyze(raw, stats, depth + 1)
            if sub:
                seg[0] |= NESTED
                seg[-1] = encode_rc(raw, sub)
                stats["nested"] += 1
        if s0 > last:
            segs.append([SEG_LIT, view[last:s0]])
        segs.append(seg)
        kname = SEG_NAMES[seg[0] & 0x7F]
        stats["kinds"][kname] = stats["kinds"].get(kname, 0) + 1
        stats["streams"] += 1
        stats["packed_bytes"] += s1 - s0
        last, hits = s1, hits + 1
    if not hits:
        return None
    if last < len(data):
        segs.append([SEG_LIT, view[last:]])
    return segs


def encode_rc(data, segs):
    out = bytearray(RC_HEAD.pack(b"RC", len(data), hashlib.sha256(data).digest(),
                                 len(segs)))
    for s in segs:
        kind = s[0]
        base = kind & 0x7F
        out.append(kind)
        if base == SEG_LIT:
            out += struct.pack("<Q", len(s[1]))
            out += s[1]
            continue
        if base == SEG_DEFLATE:
            out += RC_PARAMS.pack(*s[1])
        elif base == SEG_PNG:
            _, p, zhdr, adler, lens, _ = s
            out += RC_PARAMS.pack(*p) + zhdr + adler
            out += struct.pack("<I%dI" % len(lens), len(lens), *lens)
        elif base == SEG_BZ2:
            out.append(s[1])
        elif base == SEG_XZ:
            out += bytes([s[1], s[2]])
        payload = s[-1]
        out += struct.pack("<Q", len(payload))
        out += payload
    return bytes(out)


def decode_rc(buf):
    """Rebuild the original file from an RC container and verify SHA-256."""
    buf = memoryview(buf)
    magic, size, digest, nseg = RC_HEAD.unpack_from(buf, 0)
    if magic != b"RC":
        raise UZError("bad recompression container")
    pos, out = RC_HEAD.size, bytearray()
    for _ in range(nseg):
        kind = buf[pos]
        base = kind & 0x7F
        pos += 1
        if base == SEG_LIT:
            n = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
            out += buf[pos:pos + n]
            pos += n
            continue
        if base in (SEG_DEFLATE, SEG_PNG):
            p = RC_PARAMS.unpack_from(buf, pos)
            pos += RC_PARAMS.size
            if base == SEG_PNG:
                zhdr, adler = bytes(buf[pos:pos + 2]), bytes(buf[pos + 2:pos + 6])
                k = struct.unpack_from("<I", buf, pos + 6)[0]
                lens = struct.unpack_from("<%dI" % k, buf, pos + 10)
                pos += 10 + 4 * k
        elif base == SEG_BZ2:
            level = buf[pos]
            pos += 1
        elif base == SEG_XZ:
            preset, chk_id = buf[pos], buf[pos + 1]
            pos += 2
        else:
            raise UZError("unknown segment type %d" % kind)
        n = struct.unpack_from("<Q", buf, pos)[0]
        pos += 8
        raw = buf[pos:pos + n]
        pos += n
        raw = decode_rc(raw) if kind & NESTED else bytes(raw)
        if base == SEG_DEFLATE:
            out += _deflate(raw, p)
        elif base == SEG_PNG:
            zs = zhdr + _deflate(raw, p) + adler
            if sum(lens) != len(zs):
                raise UZError("PNG rebuild size mismatch (zlib version differs?)")
            o = 0
            for ln in lens:
                piece = zs[o:o + ln]
                out += struct.pack(">I", ln) + b"IDAT" + piece
                out += struct.pack(">I", zlib.crc32(b"IDAT" + piece))
                o += ln
        elif base == SEG_BZ2:
            out += bz2.compress(raw, level)
        else:
            out += lzma.compress(raw, format=lzma.FORMAT_XZ, check=XZ_CHECKS[chk_id],
                                 preset=(preset & 0x0F) |
                                 (lzma.PRESET_EXTREME if preset & 0x80 else 0))
    if len(out) != size or hashlib.sha256(out).digest() != digest:
        raise UZError("recompressed file failed its checksum. This usually means "
                      "this Python uses a different zlib/liblzma than the one "
                      "that made the archive.")
    return bytes(out)


def rc_original_size(buf):
    return RC_HEAD.unpack_from(buf, 0)[1]


def try_recompress(data, name, stats):
    """Return an RC container for data, or None if nothing was gained."""
    ext = os.path.splitext(name)[1].lower()
    if ext in SKIP_EXT or len(data) < 128 or len(data) > RC_MAX_FILE:
        return None
    try:
        segs = _analyze(data, stats)
        if not segs:
            return None
        rc = encode_rc(data, segs)
        if decode_rc(rc) != data:        # prove it round-trips before trusting it
            return None
    except (UZError, zlib.error, lzma.LZMAError, struct.error, ValueError,
            OSError, MemoryError):
        return None
    stats["files_rc"] += 1
    return rc


# ================================================================ classify / pack

def looks_text(data):
    sample = data[:65536]
    if not sample:
        return False
    nul = sample.count(0)
    if nul:
        # the only NULs tolerated are tar padding around text files
        if data[257:262] != b"ustar" or nul > len(sample) * 0.6:
            return False
        sample = sample.replace(b"\0", b"")
        if not sample:
            return False
    text = sample.decode("latin-1")
    good = sum(ch.isprintable() or ch in "\r\n\t\f" for ch in text)
    return good / len(text) > 0.95


def classify(data):
    try:
        if data[:4] == b"\x7fELF" and len(data) > 20:
            fmt = "<H" if data[5] == 1 else ">H"
            if struct.unpack_from(fmt, data, 18)[0] in (3, 62):
                return CL_EXE
        if data[:2] == b"MZ" and len(data) > 0x40:
            off = struct.unpack_from("<I", data, 0x3C)[0]
            if data[off:off + 4] == b"PE\0\0" and \
                    struct.unpack_from("<H", data, off + 4)[0] in (0x14C, 0x8664):
                return CL_EXE
        if data[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") and \
                struct.unpack_from("<I", data, 4)[0] & 0xFF == 7:
            return CL_EXE
    except struct.error:
        pass
    return CL_TEXT if looks_text(data) else CL_OTHER


def _arc(*parts):
    return posixpath.normpath(posixpath.join(*parts)).lstrip("/")


def collect(paths, log=print):
    dirs, files = [], []
    for p in paths:
        p = os.path.abspath(p)
        base = os.path.basename(p.rstrip(os.sep)) or "root"
        if os.path.islink(p):
            log("skip symlink: " + p)
        elif os.path.isdir(p):
            dirs.append((base, p))
            for root, dnames, fnames in os.walk(p):
                dnames.sort()
                rel = os.path.relpath(root, p).replace(os.sep, "/")
                for d in dnames:
                    full = os.path.join(root, d)
                    if not os.path.islink(full):
                        dirs.append((_arc(base, rel, d), full))
                for f in fnames:
                    full = os.path.join(root, f)
                    if os.path.isfile(full) and not os.path.islink(full):
                        files.append((_arc(base, rel, f), full))
        elif os.path.isfile(p):
            files.append((base, p))
        else:
            raise UZError("not found: " + p)
    files.sort(key=lambda it: (os.path.splitext(it[0])[1].lower(),
                               posixpath.basename(it[0]).lower(), it[0]))
    return dirs, files


def new_stats():
    return {"files_rc": 0, "streams": 0, "packed_bytes": 0, "misses": 0,
            "nested": 0, "kinds": {}, "dups": 0, "dup_bytes": 0}


def _entry(typ, name, mode, mtime, data):
    name = name.encode("utf-8")
    return ENTRY.pack(typ, len(name), mode & 0o7777, int(mtime), len(data)) + name + data


def build_blocks(dirs, files, recompress=True, dedup=True, stats=None, log=print):
    stats = stats if stats is not None else new_stats()
    blocks = {cl: bytearray() for cl in CLASS_NAMES}
    for arcname, full in dirs:
        st = os.stat(full)
        blocks[CL_META] += _entry(T_DIR, arcname, st.st_mode, st.st_mtime, b"")
    seen = {}
    for arcname, full in files:
        st = os.stat(full)
        with open(full, "rb") as fh:
            data = fh.read()
        if dedup:
            h = hashlib.sha256(data).digest()
            if h in seen:
                blocks[CL_META] += _entry(T_DUP, arcname, st.st_mode, st.st_mtime,
                                          seen[h].encode("utf-8"))
                stats["dups"] += 1
                stats["dup_bytes"] += len(data)
                if log:
                    log("  duplicate  %s = %s" % (arcname, seen[h]))
                continue
            seen[h] = arcname
        typ, cl = T_FILE, classify(data)
        if recompress:
            rc = try_recompress(data, arcname, stats)
            if rc is not None:
                typ, cl, data = T_FILE_RC, CL_RC, rc
                if log:
                    log("  recompressed %s" % arcname)
        blocks[cl] += _entry(typ, arcname, st.st_mode, st.st_mtime, data)
    out = []
    for cl in sorted(blocks):
        if blocks[cl]:
            blocks[cl] += ENTRY.pack(T_END, 0, 0, 0, 0)
            out.append((cl, bytes(blocks[cl])))
    return out, stats


def iter_entries(raw):
    pos, view = 0, memoryview(raw)
    while True:
        if pos + ENTRY.size > len(raw):
            raise UZError("truncated stream")
        typ, plen, mode, mtime, size = ENTRY.unpack_from(raw, pos)
        pos += ENTRY.size
        if typ == T_END:
            return
        path = bytes(view[pos:pos + plen]).decode("utf-8")
        pos += plen
        data = view[pos:pos + size]
        pos += size
        yield typ, path, mode, mtime, data


def safe_target(outdir, path):
    if (not path or path.startswith(("/", "\\")) or ":" in path
            or ".." in path.replace("\\", "/").split("/")):
        raise UZError("unsafe path in archive: %r" % path)
    root = os.path.realpath(outdir)
    target = os.path.realpath(os.path.join(root, *path.split("/")))
    if os.path.commonpath([root, target]) != root:
        raise UZError("path escapes output dir: %r" % path)
    return target


# ================================================================ archive IO

def write_archive(out, blocks, level="max", codec=None, jobs=2, log=print):
    results = []
    for cl, raw in blocks:
        ids = [codec_by_name(codec)] if codec else block_codecs(level, cl)
        if log:
            log("block %-13s %11d bytes, %d codec(s)" % (CLASS_NAMES[cl], len(raw), len(ids)))
        # never trust a codec blindly: take the smallest result that decodes
        # back to exactly the input (guards against bugs in codec libraries)
        for cid, payload in race(raw, ids, jobs, log):
            try:
                if CODECS[cid][2](payload, len(raw)) == raw:
                    break
            except Exception:
                pass
            if log:
                log("    %s failed its round-trip check, trying next best"
                    % codec_name(cid))
        results.append((cid, cl, raw, payload))
    zver = zlib.ZLIB_RUNTIME_VERSION.encode("ascii", "replace")[:255]
    with open(out, "wb") as fh:
        fh.write(MAGIC + bytes([VERSION, len(zver)]) + zver)
        fh.write(struct.pack("<H", len(results)))
        for cid, cl, raw, payload in results:
            fh.write(BLOCK.pack(cid, cl, len(raw), len(payload),
                                hashlib.sha256(raw).digest()))
        for r in results:
            fh.write(r[3])
    return [(codec_name(c), cl, len(raw), len(p)) for c, cl, raw, p in results], \
        os.path.getsize(out)


def _decompress_block(cid, payload, raw_size, digest):
    if cid not in CODECS:
        hint = {7: "zstandard", 8: "brotli", 10: "pyppmd", 12: "pyppmd"}.get(cid)
        raise UZError("archive uses %s which is not installed here%s"
                      % (codec_name(cid), " (pip install %s)" % hint if hint else ""))
    raw = CODECS[cid][2](payload, raw_size)
    if len(raw) != raw_size or hashlib.sha256(raw).digest() != digest:
        raise UZError("INTEGRITY FAILURE: checksum mismatch, archive is damaged")
    return raw


def read_archive(path):
    """Returns (blocks, zlib_version, format_version); blocks are dicts."""
    with open(path, "rb") as fh:
        blob = fh.read()
    if len(blob) < 6 or blob[:4] != MAGIC:
        raise UZError("not a .uz file")
    ver = blob[4]
    if ver > VERSION:
        raise UZError("archive version %d is newer than this tool" % ver)
    if ver <= 2:
        _, _, cid, raw_size, digest = HEADER_V1.unpack_from(blob, 0)
        pos, zver = HEADER_V1.size, ""
        if ver == 2:
            n = blob[pos]
            zver = blob[pos + 1:pos + 1 + n].decode("ascii", "replace")
            pos += 1 + n
        payload = blob[pos:]
        raw = _decompress_block(cid, payload, raw_size, digest)
        return [dict(cls=CL_OTHER, codec=cid, raw=raw, comp=len(payload))], zver, ver
    n = blob[5]
    zver = blob[6:6 + n].decode("ascii", "replace")
    pos = 6 + n
    nblocks = struct.unpack_from("<H", blob, pos)[0]
    pos += 2
    heads = []
    for _ in range(nblocks):
        heads.append(BLOCK.unpack_from(blob, pos))
        pos += BLOCK.size
    blocks = []
    for cid, cl, raw_size, comp_size, digest in heads:
        payload = blob[pos:pos + comp_size]
        pos += comp_size
        blocks.append(dict(cls=cl, codec=cid, comp=comp_size,
                           raw=_decompress_block(cid, payload, raw_size, digest)))
    return blocks, zver, ver


class Entry:
    __slots__ = ("typ", "path", "mode", "mtime", "data", "ref")

    def __init__(self, typ, path, mode, mtime, data):
        self.typ, self.path, self.mode, self.mtime, self.data = \
            typ, path, mode, mtime, data
        self.ref = None

    @property
    def is_dir(self):
        return self.typ == T_DIR

    @property
    def is_dup(self):
        return self.typ == T_DUP

    @property
    def recompressed(self):
        return self.typ == T_FILE_RC

    @property
    def size(self):
        if self.is_dup:
            return self.ref.size if self.ref else 0
        return rc_original_size(self.data) if self.recompressed else len(self.data)

    @property
    def unpacked_size(self):
        return len(self.data)

    @property
    def dup_of(self):
        return bytes(self.data).decode("utf-8") if self.is_dup else None


class Archive:
    """A .uz file loaded into memory (used by the CLI and the GUI)."""

    def __init__(self, path):
        self.path = path
        self.blocks, self.zlib_version, self.version = read_archive(path)
        self.entries = []
        for b in self.blocks:
            self.entries.extend(Entry(*e) for e in iter_entries(b["raw"]))
        by_path = {e.path: e for e in self.entries if not e.is_dir and not e.is_dup}
        for e in self.entries:
            if e.is_dup:
                e.ref = by_path.get(e.dup_of)
                if e.ref is None:
                    raise UZError("duplicate %s points at missing %s" % (e.path, e.dup_of))
        self.entries.sort(key=lambda e: e.path)
        self.file_size = os.path.getsize(path)

    @property
    def codec_name(self):
        names = []
        for b in self.blocks:
            n = codec_name(b["codec"])
            if n not in names:
                names.append(n)
        return " + ".join(names)

    @property
    def total_size(self):
        return sum(e.size for e in self.entries if not e.is_dir)

    @property
    def zlib_mismatch(self):
        uses_rc = any(e.recompressed for e in self.entries)
        return uses_rc and self.zlib_version and \
            self.zlib_version != zlib.ZLIB_RUNTIME_VERSION

    def read(self, entry):
        if entry.is_dup:
            entry = entry.ref
        if entry.recompressed:
            return decode_rc(entry.data)
        return bytes(entry.data)

    def extract(self, outdir, entries=None, force=False, log=print):
        entries = self.entries if entries is None else entries
        os.makedirs(outdir, exist_ok=True)
        dir_times, count, failed = [], 0, []
        for e in entries:
            target = safe_target(outdir, e.path)
            if e.is_dir:
                os.makedirs(target, exist_ok=True)
                dir_times.append((target, e.mtime))
                continue
            if os.path.exists(target) and not force:
                log("exists, skipping: " + e.path)
                continue
            try:
                data = self.read(e)
            except UZError as ex:
                failed.append(e.path)
                log("FAILED %s: %s" % (e.path, ex))
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
            try:
                os.chmod(target, e.mode & 0o777)
                os.utime(target, (e.mtime, e.mtime))
            except OSError:
                pass
            count += 1
            log("  " + e.path)
        for target, mtime in sorted(dir_times, reverse=True):
            try:
                os.utime(target, (mtime, mtime))
            except OSError:
                pass
        return count, failed

    def test(self):
        """Rebuild every recompressed file. Returns list of failed paths."""
        failed = []
        for e in self.entries:
            if e.recompressed:
                try:
                    decode_rc(e.data)
                except UZError:
                    failed.append(e.path)
        return failed


def default_jobs():
    return max(1, min(4, os.cpu_count() or 1))


def create_archive(out, inputs, level="max", codec=None, jobs=None,
                   recompress=True, dedup=True, dict_mb=64, log=print):
    global DICT_CAP
    DICT_CAP = dict_mb << 20
    jobs = jobs or default_jobs()
    if not out.lower().endswith(".uz"):
        out += ".uz"
    t0 = time.time()
    dirs, files = collect(inputs, log)
    in_bytes = sum(os.path.getsize(f) for _, f in files)
    log("UZip: %d files, %d dirs, %d bytes" % (len(files), len(dirs), in_bytes))
    blocks, stats = build_blocks(dirs, files, recompress, dedup, None, log)
    if dedup and stats["dups"]:
        log("dedup: %d duplicate file(s), %d bytes stored once"
            % (stats["dups"], stats["dup_bytes"]))
    if recompress:
        kinds = ", ".join("%d %s" % (v, k) for k, v in sorted(stats["kinds"].items()))
        log("recompression: %d file(s), %d stream(s) reversed (%s; %d nested), "
            "%d stream(s) not reproducible"
            % (stats["files_rc"], stats["streams"], kinds or "none", stats["nested"],
               stats["misses"]))
    summary, size = write_archive(out, blocks, level, codec, jobs, log)
    for name, cl, rawlen, clen in summary:
        log("  %-13s %-14s %11d -> %10d" % (CLASS_NAMES[cl], name, rawlen, clen))
    log("wrote %s  %d bytes  (%.2f%% of original, %.1fs)"
        % (out, size, pct(size, in_bytes), time.time() - t0))
    stats.update(out=out, size=size, in_bytes=in_bytes, blocks=summary)
    return stats


# ================================================================ commands

def cmd_compress(a):
    create_archive(a.output, a.inputs, a.level, a.codec, a.jobs,
                   not a.no_rc, not a.no_dedup, a.dict_mb)


def cmd_extract(a):
    arc = Archive(a.archive)
    if arc.zlib_mismatch:
        print("warning: archive made with zlib %s, this Python has %s"
              % (arc.zlib_version, zlib.ZLIB_RUNTIME_VERSION))
    log = print if a.verbose else (lambda s: None)
    count, failed = arc.extract(a.outdir, None, a.force, log)
    print("extracted %d files to %s (checksums OK)" % (count, a.outdir))
    if failed:
        raise SystemExit("%d file(s) could not be rebuilt" % len(failed))


def cmd_list(a):
    arc = Archive(a.archive)
    for e in arc.entries:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime))
        if e.is_dir:
            print("%12s    %s  %s/" % ("<dir>", stamp, e.path))
        else:
            flag = "R" if e.recompressed else "D" if e.is_dup else " "
            extra = "  (= %s)" % e.dup_of if e.is_dup else ""
            print("%12d %s  %s  %s%s" % (e.size, flag, stamp, e.path, extra))
    files = [e for e in arc.entries if not e.is_dir]
    print("%d files (%d recompressed, %d duplicates), %d bytes -> %d bytes (%.2f%%)"
          % (len(files), sum(e.recompressed for e in files),
             sum(e.is_dup for e in files), arc.total_size, arc.file_size,
             pct(arc.file_size, arc.total_size)))
    for b in arc.blocks:
        print("  block %-13s %-14s %11d -> %10d" % (CLASS_NAMES.get(b["cls"], "?"),
                                                    codec_name(b["codec"]),
                                                    len(b["raw"]), b["comp"]))


def cmd_test(a):
    arc = Archive(a.archive)
    failed = arc.test()
    if failed:
        raise SystemExit("FAILED to rebuild: " + ", ".join(failed))
    print("OK: %d entries, all checksums verified" % len(arc.entries))


def main():
    ap = argparse.ArgumentParser(prog="uzip", description="UZip (Ultrazip) .uz archiver")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("c", help="compress files/folders into a .uz archive")
    c.add_argument("output")
    c.add_argument("inputs", nargs="+")
    c.add_argument("-l", "--level", choices=LEVELS, default="max")
    c.add_argument("--codec", help="force one codec for every block")
    c.add_argument("--no-rc", action="store_true",
                   help="skip recompression (portable to any zlib)")
    c.add_argument("--no-dedup", action="store_true", help="store duplicate files again")
    c.add_argument("-j", "--jobs", type=int, default=None,
                   help="codecs to run in parallel (default: up to 4)")
    c.add_argument("--dict-mb", type=int, default=64,
                   help="LZMA dictionary cap in MiB (encoder RAM is about 11x this)")
    c.set_defaults(fn=cmd_compress)

    x = sub.add_parser("x", help="extract a .uz archive")
    x.add_argument("archive")
    x.add_argument("-o", "--outdir", default=".")
    x.add_argument("-f", "--force", action="store_true", help="overwrite files")
    x.add_argument("-v", "--verbose", action="store_true")
    x.set_defaults(fn=cmd_extract)

    l = sub.add_parser("l", help="list archive contents")
    l.add_argument("archive")
    l.set_defaults(fn=cmd_list)

    t = sub.add_parser("t", help="test archive integrity")
    t.add_argument("archive")
    t.set_defaults(fn=cmd_test)

    a = ap.parse_args()
    try:
        a.fn(a)
    except UZError as ex:
        raise SystemExit("error: %s" % ex)


if __name__ == "__main__":
    main()
