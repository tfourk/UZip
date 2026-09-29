# UZip (Ultrazip)

**A max-ratio archiver in pure Python that makes `.uz` files, and beats 7-Zip on ratio.**

UZip packs files smaller than 7-Zip's best settings, with the biggest wins on
files that are *already compressed*: PNG images, Office documents, ZIPs,
`.gz`, `.bz2` and `.xz` files. It does this by **recompression**. It
un-compresses those files back to raw data, records the exact settings needed
to rebuild them, and repacks the raw data with a much stronger codec. On
extract, every file is rebuilt **bit-for-bit** and verified with SHA-256.

It comes with **UZip Explorer**, a desktop GUI for browsing, previewing,
extracting and creating `.uz` archives.

--

## Benchmarks

All results are byte-exact round trips (every extracted file was compared
with the original).

### Whole folders

| Test set | Size | 7-Zip `-mx9` | 7-Zip ultra* | **UZip max** | UZip vs 7-Zip ultra |
|---|---:|---:|---:|---:|---:|
| Plain data (source code, text, executables) | 15,211,934 | 3,690,230 | 3,682,298 | **3,677,505** | 0.1% smaller (tie) |
| Already-compressed files (PNG, DOCX, XLSX, GZ, PDF) | 1,397,012 | 959,200 | 958,681 | **484,456** | **49% smaller** |
| Archives and containers (tar, tgz, tbz2, txz, zip, docx, 7z, zst) | 2,448,701 | 1,653,619 | 1,653,176 | **921,827** | **44% smaller** |

\* 7-Zip ultra = `-t7z -m0=lzma2 -mx9 -md=256m -mfb=273 -ms=on`

### Single files (the fair test for recompression)

Each file compressed on its own, so shared content between files cannot help.

| File | Original | 7-Zip `-mx9` | **UZip max** | UZip vs 7-Zip |
|---|---:|---:|---:|---:|
| `screens.zip` (ZIP of PNGs) | 237,311 | 237,481 | **64,813** | **-72.7%** |
| `images.tar` (tar of PNGs) | 307,200 | 230,670 | **64,493** | **-72.0%** |
| `report_with_images.docx` | 144,685 | 144,873 | **50,189** | **-65.4%** |
| `sources.tar.bz2` | 191,325 | 191,500 | **166,516** | **-13.0%** |
| `sources.tar` (Python source) | 880,640 | 189,234 | **166,440** | **-12.0%** |
| `sources.tar.xz` | 189,260 | 189,435 | **179,313** | **-5.3%** |
| `project.tar.gz` (made by GNU gzip) | 67,461 | 67,622 | 67,156 | -0.7% |
| `sources.7z` | 192,174 | 192,333 | 192,296 | tie |
| `sources.tar.zst` | 238,645 | 238,823 | 238,772 | tie |

### Speed

UZip is written in Python and 7-Zip is optimized C, so 7-Zip is much faster.
Times are for creating the archive.

| Test set | 7-Zip `-mx9` | UZip normal | UZip max |
|---|---:|---:|---:|
| Plain data (15.2 MB) | 3.0 s | 9.4 s | 16.5 s |
| Already-compressed (1.4 MB) | 0.1 s | 21.7 s | 66.1 s |
| Containers (2.4 MB) | 0.3 s | 9.0 s | 25.4 s |

`normal` mode lands within about 1% of `max` in roughly half the time. The
slow part on image-heavy sets is PNG pixel data, which expands many times
over before the final compression step.

**Test environment:** Linux, 2 CPU threads, Python 3.12, zlib 1.3, xz 5.4.5,
7-Zip 23.01, pyppmd 1.3.1.

### Per-codec measurements that shaped the design

| Data | 7-Zip | LZMA2 | Best UZip codec |
|---|---:|---:|---:|
| 1.2 MB of text/docs | 214,613 | 214,392 | **180,161** (PPMd order 64) |
| 14 MB of x86-64 executables | 3,474,292 (BCJ2) | 3,873,052 | 3,491,322 (UZip BCJ2-style, within 0.5%) |

---

## Features

- **Recompression** of files that were already compressed, rebuilt bit-exact:
  - **Deflate:** ZIP, DOCX, XLSX, PPTX, ODT, JAR, APK, EPUB, PNG, GZ, TGZ,
    SVGZ, PDF streams, and zlib data inside any file
  - **bzip2:** `.bz2`, `.tbz2`, and bzip2 streams inside any file
  - **xz:** `.xz`, `.txz`, and xz streams inside any file
  - Streams are found **anywhere** in a file, so a tar full of PNGs or a ZIP
    of screenshots is handled too
  - **Nesting two levels deep**, for example a PNG inside a DOCX inside a
    `.tar.gz`
- **Per-type solid blocks.** Executables, text, recompressed data and
  everything else are packed into separate blocks, and each block races its
  own set of codecs. The smallest result wins.
- **PPMd for text** (order 32 and 64), 10 to 20% better than LZMA2 on text
  and source code.
- **BCJ2-style executable filter** written for UZip. x86 CALL, JMP and
  conditional-jump targets are made absolute and split into separate
  streams, like 7-Zip's BCJ2.
- **Whole-file dedup.** Identical files are stored once, no matter how far
  apart they are.
- **Integrity everywhere:**
  - a SHA-256 for every block and every recompressed file
  - the winning codec is decoded and checked *before* the archive is written
  - path-traversal protection on extract
- **UZip Explorer GUI:** browse, filter, preview, extract, test and create
  archives.

---

## Installation

UZip needs **Python 3.10 or newer**. The core works with the standard library
alone.

```bash
git clone https://github.com/<you>/uzip.git
cd uzip

# optional, for the best ratio (strongly recommended):
pip install pyppmd zstandard brotli

# optional, for JPG/BMP/WEBP previews in the GUI:
pip install pillow
```

The GUI uses Tkinter. It ships with Python on Windows and macOS. On
Debian/Ubuntu, install it with `sudo apt install python3-tk`.

> Archives that used PPMd, zstd or brotli need that module to be extracted.
> If it is missing, UZip names the module to install rather than failing.

---

## Usage

### Command line

```bash
python uzip.py c backup.uz myfolder file.txt     # compress (max ratio)
python uzip.py c -l normal backup.uz myfolder    # about 2x faster, ~1% bigger
python uzip.py c -l fast backup.uz myfolder      # quickest
python uzip.py c --no-rc backup.uz myfolder      # skip recompression (portable)
python uzip.py x backup.uz -o restore            # extract
python uzip.py x backup.uz -o restore -f         # extract, overwrite existing
python uzip.py l backup.uz                       # list contents and blocks
python uzip.py t backup.uz                       # verify everything
```

| Option | Meaning |
|---|---|
| `-l max / normal / fast` | Effort level (default `max`) |
| `--no-rc` | Do not recompress; the archive then opens on any zlib |
| `--no-dedup` | Store duplicate files again |
| `--codec NAME` | Force one codec for every block |
| `-j N` | Codecs to run in parallel (default: up to 4) |
| `--dict-mb N` | LZMA dictionary cap in MiB (default 64; RAM use is about 11x this) |

In the `l` listing, `R` marks a recompressed file and `D` a duplicate.

### GUI (UZip Explorer)

```bash
python uzip_gui.py             # then File > Open, or New Archive
python uzip_gui.py backup.uz   # open an archive directly
```

- **Tree view** with a live filter. Green = recompressed, blue = duplicate.
- **Preview pane:**
  - images
  - text and code
  - the file list and readable text inside ZIP/DOCX/XLSX
  - the file list inside tar, `.tar.gz`, `.tar.bz2`, `.tar.xz` and `.tar.zst`
  - decoded PDF streams
  - a hex view for anything else
- **Archive summary** showing which codec won each block, dedup savings and
  file types.
- **Extract Selected / Extract All / Test / Open File** (double-click opens a
  file in its normal app).
- **New Archive** dialog with level, recompression toggle and a live log.

---

## How it works

```
files --> dedup --> classify --> recompress --> per-type solid blocks --> codec race --> verify --> .uz
                     |              |                                        |
                     |              +- ZIP/PNG/GZ/bz2/xz: unpack to raw,     +- exe:    BCJ2-style, BCJ, LZMA2
                     |                 record settings to rebuild exactly    +- text:   PPMd o64/o32, LZMA2
                     +- exe / text / other                                   +- recomp: LZMA2 variants, PPMd, ...
                                                                             +- other:  everything, incl. store
```

1. **Dedup.** Files are hashed with SHA-256. Repeats become a small reference
   to the first copy.
2. **Classify.** x86/x64 executables (ELF, PE, Mach-O), text (including
   tarballs of text) and other data.
3. **Recompress.** UZip scans each file for Deflate, PNG, gzip, bzip2 and xz
   streams. It unpacks each one and searches for the exact encoder settings
   that reproduce the original bytes. For Deflate that means level, memLevel,
   strategy and window. For xz it is the preset and check type. Streams that
   cannot be reproduced exactly are left untouched. Before a file is accepted,
   it is rebuilt in full and compared with the original.
4. **Race.** Each block is compressed by several codecs in parallel. Blocks
   over 16 MB race on an 8 MB sample first, and LZMA2 always gets a full run
   because a sample cannot see long-distance repeats.
5. **Verify.** Results are tried from smallest to largest, and the first one
   that decodes back to exactly the input is written. A codec library bug can
   cost some ratio, but it can never produce a broken archive.

### Recompression support

| Format | Status | Notes |
|---|---|---|
| ZIP family (DOCX, XLSX, PPTX, ODT, JAR, APK, EPUB) | Yes | When made with zlib-based tools (Office, Python, most ZIP libraries) |
| PNG | Yes | Including split IDAT chunks and PNGs inside other files |
| gzip / tgz | Partial | zlib-made files rebuild. GNU gzip 1.12 output differs from zlib, so those are stored as-is |
| zlib streams (PDF, SVGZ, others) | Yes | Validated with Adler-32 before use |
| bzip2 / tbz2 | Yes | Verified against `bzip2 -1` through `-9` |
| xz / txz | Yes | Verified against `xz -0` through `-9`, including `-T1` |
| tar | Scanned | tar is uncompressed. UZip recompresses the files inside it and routes text tarballs to PPMd |
| zstd | No | Output changes between zstd library versions, so exact rebuilds cannot be guaranteed |
| 7z | No | Packed with 7-Zip's own LZMA encoder, which Python cannot reproduce. Repacking LZMA with LZMA gains almost nothing |
| RAR | No | RAR's compressor is proprietary. Only WinRAR can create RAR data |
| ACE | No | Abandoned format. Its old unpacker had a serious flaw (CVE-2018-20250), so UZip does not open it |
| JPEG, MP3, MP4 | No | Already well compressed. JPEG repacking (Lepton/Brunsli style) is on the roadmap |

---

## Important: zlib versions

Rebuilding a Deflate stream exactly depends on the zlib library, and every
archive records the zlib version it was made with. Some builds use a
different zlib family, for example zlib-ng in some Python 3.14 Windows
builds. If you extract on one of those machines, recompressed files may not
rebuild. UZip then refuses to write a wrong file and tells you which files
failed.

For archives that must open on any machine, create them with `--no-rc`.

---

## File format (v3)

All integers are little-endian.

```
"UZ01" | version u8 (=3) | zlib_ver_len u8 | zlib_ver | nblocks u16
per block:  codec u8 | class u8 | raw_size u64 | comp_size u64 | sha256[32]
then the block payloads in order.

Each block's raw data is a sequence of entries:
  type u8 | path_len u16 | mode u32 | mtime i64 | size u64 | path | data
  types: 1 file, 2 dir, 3 recompressed file, 4 duplicate (data = original's path)
  a type 0 entry ends the block.

Recompressed file data ("RC" container):
  "RC" | original_size u64 | sha256[32] | nseg u32 | segments...
  segment kinds: literal, deflate, png, bzip2, xz
  (bit 0x80 = the segment's raw data is itself an RC container)
```

| Codec ID | Name | Needs |
|---:|---|---|
| 0 | store | |
| 1 | zlib-9 | |
| 2 | bzip2-9 | |
| 3 | lzma2-ultra | |
| 4 | lzma2-text | |
| 5 | bcj-x86+lzma2 | |
| 6 | delta4+lzma2 | |
| 7 | zstd-22 | `zstandard` |
| 8 | brotli-11 | `brotli` |
| 9 | lzma2-fast | |
| 10 | ppmd-o32 | `pyppmd` |
| 11 | bcj2+lzma2 | |
| 12 | ppmd-o64 | `pyppmd` |

Versions 1 and 2 (single block) are still read.

---

## Limitations

- **Speed:** UZip is 3 to 20x slower than 7-Zip. Use `-l normal` for large jobs.
- **Memory:** archives are built in memory, so inputs should fit comfortably
  in RAM. LZMA at max settings uses about 700 MB per parallel job on large
  inputs.
- **No magic:** no lossless compressor can shrink every file (see the
  pigeonhole principle). Gains come from undoing weak old compression and
  from better modelling, not from beating information theory.
- **Known library issue:** `pyppmd` 1.3.1 decodes incorrectly on roughly 3% of
  inputs in our tests, and is not thread-safe. UZip serializes PPMd calls and
  verifies every result, so this costs ratio on those blocks but never data.

## Roadmap

- **Preflate-style Deflate reconstruction:** store a small difference file so
  that *any* Deflate encoder (GNU gzip, 7-Zip, zopfli) can be rebuilt.
- **Lossless JPEG repacking,** for about 20% on photos.
- **Streaming mode** for inputs larger than RAM.
- **Optional unpack mode for RAR/7z,** which stores their contents instead of
  the archive file.

## License

Add your license of choice here (for example MIT).
