#!/usr/bin/env python3
"""
UZip Explorer - graphical viewer and builder for .uz archives.

Put this file next to uzip.py and run:
    python uzip_gui.py              (then File > Open)
    python uzip_gui.py backup.uz    (open an archive directly)

Features:
  * Folder tree of everything inside the archive, with a live filter
  * Preview pane: images (PNG/GIF natively, JPG/BMP/WEBP if Pillow is
    installed), text and code, the file list inside ZIP/DOCX/XLSX files,
    the text inside .gz files, and a hex dump for anything else
  * Shows which files were recompressed (old Deflate reversed) and how
    big each file really is
  * Extract selected files/folders or everything, open a file in its
    normal app, test the archive, and create new .uz archives
Only needs the Python standard library (Tkinter). Pillow is optional.
"""

import base64
import bz2
import gzip
import io
import lzma
import math
import os
import posixpath
import queue
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import zipfile
import zlib
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uzip  # noqa: E402

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None
try:
    import zstandard as _zstd
except ImportError:
    _zstd = None

TK_IMG = {".png", ".gif", ".ppm", ".pgm"}
PIL_IMG = {".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".ico"}
MAX_TEXT = 256 * 1024
MAX_HEX = 16 * 1024

BG = "#f4f5f7"
PANEL = "#ffffff"
ACCENT = "#1f6feb"
MUTED = "#6b7280"
RC_GREEN = "#0a7d38"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


def looks_text(data):
    sample = data[:8192]
    if not sample:
        return True
    if b"\0" in sample:
        return False
    text = sample.decode("latin-1")
    good = sum(ch.isprintable() or ch in "\r\n\t" for ch in text)
    return good / len(text) > 0.92


def decode_text(data):
    for enc in ("utf-8", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def hexdump(data, limit=MAX_HEX):
    lines = []
    for off in range(0, min(len(data), limit), 16):
        chunk = data[off:off + 16]
        hexs = " ".join("%02x" % b for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append("%08x  %-47s  %s" % (off, hexs, asc))
    if len(data) > limit:
        lines.append("... (%s more bytes not shown)" % human(len(data) - limit))
    return "\n".join(lines)


PARA_END = re.compile(r"</(?:w:p|a:p|text:p|text:h|si|row)>")
TAG = re.compile(r"<[^>]+>")


def xml_to_text(xml):
    """Readable text from Word/Excel/PowerPoint/OpenDocument XML."""
    xml = PARA_END.sub("\n", xml)
    xml = re.sub(r"<w:tab/>|<w:br/>", " ", xml)
    text = TAG.sub("", xml)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(a, b)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def pdf_summary(data):
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    rows = ["PDF, %d bytes, about %d page object(s)" % (len(data), pages), ""]
    shown = 0
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        try:
            d = zlib.decompressobj()
            out = d.decompress(data[start:start + 4 * 1024 * 1024], 200000)
        except zlib.error:
            continue
        if not out:
            continue
        shown += 1
        body = decode_text(out[:3000]) if looks_text(out) else hexdump(out, 256)
        rows += ["---- decoded stream %d (%s) ----" % (shown, human(len(out))), body, ""]
        if shown >= 8:
            rows.append("(more streams not shown)")
            break
    if not shown:
        rows.append("No readable compressed streams found.")
    return "\n".join(rows)


def is_tar(data):
    return len(data) > 512 and data[257:262] == b"ustar"


def tar_listing(data):
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            members = t.getmembers()
    except (tarfile.TarError, OSError, EOFError):
        return hexdump(data)
    rows = ["Contents of this tar archive (%d entries):" % len(members), "",
            "%10s  %-16s  %s" % ("size", "modified", "name"),
            "%10s  %-16s  %s" % ("-" * 10, "-" * 16, "-" * 40)]
    for m in members[:5000]:
        rows.append("%10s  %-16s  %s%s" % (
            "<dir>" if m.isdir() else human(m.size),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(m.mtime)),
            m.name, "/" if m.isdir() else ""))
    if len(members) > 5000:
        rows.append("... (%d more)" % (len(members) - 5000))
    return "\n".join(rows)


def _unzstd(data):
    if _zstd is None:
        raise ValueError("zstandard not installed")
    return _zstd.ZstdDecompressor().decompressobj().decompress(data)


STREAM_FORMATS = [
    (b"\x1f\x8b", "gzip", gzip.decompress),
    (b"BZh", "bzip2", bz2.decompress),
    (b"\xfd7zXZ\x00", "xz", lzma.decompress),
    (b"\x28\xb5\x2f\xfd", "zstd", _unzstd),
]

FOREIGN_ARCHIVES = [
    (b"7z\xbc\xaf\x27\x1c", 0, "7-Zip archive",
     "Kept as-is. Its contents are already packed with LZMA by 7-Zip's own "
     "encoder, which cannot be reproduced bit-exact from Python, and "
     "re-packing LZMA with LZMA gains almost nothing anyway."),
    (b"Rar!\x1a\x07", 0, "RAR archive",
     "Kept as-is. RAR's compressor is proprietary (only WinRAR can create "
     "RAR data), so it cannot be rebuilt bit-exact."),
    (b"**ACE**", 7, "ACE archive",
     "Kept as-is. ACE is an abandoned format and its old unpacker had a "
     "serious security flaw (CVE-2018-20250), so UZip does not open it."),
]


def open_with_system(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: pylint (Windows only)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ====================================================================== app

class App(tk.Tk):
    def __init__(self, path=None):
        super().__init__()
        self.title("UZip Explorer")
        self.geometry("1180x720")
        self.minsize(820, 480)
        self.configure(bg=BG)

        self.arc = None
        self.items = {}          # tree iid -> uzip.Entry
        self.folders = {}        # tree iid -> folder path
        self.q = queue.Queue()
        self.preview_token = 0
        self.create_dlg = None
        self._photo = None
        self._tmpdir = tempfile.mkdtemp(prefix="uzip_")

        self._style()
        self._build()
        self._bind_keys()
        self.after(80, self._poll)
        self.show_message("Open a .uz archive, or create a new one.",
                          "Use File > Open (Ctrl+O) or New Archive (Ctrl+N).")
        if path:
            self.open_archive(path)

    # ------------------------------------------------------------ layout

    def _style(self):
        st = ttk.Style(self)
        if "clam" in st.theme_names():
            st.theme_use("clam")
        base = tkfont.nametofont("TkDefaultFont")
        self.bold = base.copy()
        self.bold.configure(weight="bold")
        self.title_font = base.copy()
        self.title_font.configure(size=base.cget("size") + 3, weight="bold")
        self.mono = tkfont.nametofont("TkFixedFont")

        st.configure(".", background=BG)
        st.configure("Toolbar.TFrame", background=PANEL)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TButton", padding=(10, 5))
        st.configure("Accent.TButton", foreground="white", background=ACCENT)
        st.map("Accent.TButton", background=[("active", "#1a5fd0")])
        st.configure("Treeview", rowheight=24, background=PANEL,
                     fieldbackground=PANEL)
        st.configure("Treeview.Heading", font=self.bold)
        st.configure("Info.TLabel", background=PANEL, foreground=MUTED)
        st.configure("Title.TLabel", background=PANEL, font=self.title_font)
        st.configure("Meta.TLabel", background=PANEL, foreground=MUTED)
        st.configure("Status.TLabel", background=BG, foreground=MUTED)

    def _build(self):
        # menu
        mb = tk.Menu(self)
        fm = tk.Menu(mb, tearoff=0)
        fm.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_dialog)
        fm.add_command(label="New Archive...", accelerator="Ctrl+N",
                       command=self.new_archive)
        fm.add_separator()
        fm.add_command(label="Extract Selected...", accelerator="Ctrl+E",
                       command=self.extract_selected)
        fm.add_command(label="Extract All...", command=self.extract_all)
        fm.add_command(label="Test Archive", command=self.test_archive)
        fm.add_separator()
        fm.add_command(label="Quit", command=self.destroy)
        mb.add_cascade(label="File", menu=fm)
        self.config(menu=mb)

        # toolbar
        tb = ttk.Frame(self, style="Toolbar.TFrame", padding=(8, 6))
        tb.pack(fill="x")
        ttk.Button(tb, text="Open", command=self.open_dialog).pack(side="left")
        ttk.Button(tb, text="New Archive", style="Accent.TButton",
                   command=self.new_archive).pack(side="left", padx=(6, 0))
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=10)
        self.btn_extract = ttk.Button(tb, text="Extract Selected",
                                      command=self.extract_selected)
        self.btn_extract.pack(side="left")
        self.btn_all = ttk.Button(tb, text="Extract All", command=self.extract_all)
        self.btn_all.pack(side="left", padx=(6, 0))
        self.btn_openfile = ttk.Button(tb, text="Open File", command=self.open_external)
        self.btn_openfile.pack(side="left", padx=(6, 0))
        self.btn_test = ttk.Button(tb, text="Test", command=self.test_archive)
        self.btn_test.pack(side="left", padx=(6, 0))

        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.populate())
        ent = ttk.Entry(tb, textvariable=self.filter_var, width=28)
        ent.pack(side="right")
        ttk.Label(tb, text="Filter:", background=PANEL).pack(side="right", padx=(0, 6))

        # archive info line
        self.info = ttk.Label(self, text="No archive open", style="Info.TLabel",
                              padding=(10, 4))
        self.info.pack(fill="x")

        # main split
        pw = ttk.Panedwindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=8, pady=(6, 0))

        left = ttk.Frame(pw, style="Panel.TFrame")
        cols = ("size", "packed", "modified")
        self.tree = ttk.Treeview(left, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("packed", text="Storage", anchor="w")
        self.tree.heading("modified", text="Modified", anchor="w")
        self.tree.column("#0", width=260, stretch=True)
        self.tree.column("size", width=80, anchor="e", stretch=False)
        self.tree.column("packed", width=110, stretch=False)
        self.tree.column("modified", width=140, stretch=False)
        self.tree.tag_configure("dir", font=self.bold)
        self.tree.tag_configure("rc", foreground=RC_GREEN)
        self.tree.tag_configure("dup", foreground="#5b6bc0")
        ys = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", lambda e: self.open_external())
        self.tree.bind("<Button-3>", self.on_right_click)
        pw.add(left, weight=2)

        right = ttk.Frame(pw, style="Panel.TFrame", padding=(12, 10))
        self.pv_title = ttk.Label(right, text="", style="Title.TLabel")
        self.pv_title.pack(anchor="w")
        self.pv_meta = ttk.Label(right, text="", style="Meta.TLabel", wraplength=520,
                                 justify="left")
        self.pv_meta.pack(anchor="w", pady=(2, 8))
        self.pv_body = ttk.Frame(right, style="Panel.TFrame")
        self.pv_body.pack(fill="both", expand=True)

        self.text_frame = ttk.Frame(self.pv_body, style="Panel.TFrame")
        self.text = tk.Text(self.text_frame, wrap="none", font=self.mono,
                            relief="flat", bg="#fbfbfc", padx=8, pady=6,
                            borderwidth=1, highlightthickness=0)
        tys = ttk.Scrollbar(self.text_frame, orient="vertical", command=self.text.yview)
        txs = ttk.Scrollbar(self.text_frame, orient="horizontal",
                            command=self.text.xview)
        self.text.configure(yscrollcommand=tys.set, xscrollcommand=txs.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        tys.grid(row=0, column=1, sticky="ns")
        txs.grid(row=1, column=0, sticky="ew")
        self.text_frame.rowconfigure(0, weight=1)
        self.text_frame.columnconfigure(0, weight=1)

        self.image_label = tk.Label(self.pv_body, bg="#e9ebef", anchor="center")
        pw.add(right, weight=3)
        self.after(60, lambda: pw.sashpos(0, 640))

        # context menu
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Open", command=self.open_external)
        self.menu.add_command(label="Extract...", command=self.extract_selected)

        # status bar
        sb = ttk.Frame(self, padding=(10, 4))
        sb.pack(fill="x")
        self.status = ttk.Label(sb, text="Ready", style="Status.TLabel")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(sb, mode="indeterminate", length=160)
        self._set_actions(False)

    def _bind_keys(self):
        self.bind_all("<Control-o>", lambda e: self.open_dialog())
        self.bind_all("<Control-n>", lambda e: self.new_archive())
        self.bind_all("<Control-e>", lambda e: self.extract_selected())

    def _set_actions(self, enabled):
        state = ["!disabled"] if enabled else ["disabled"]
        for b in (self.btn_extract, self.btn_all, self.btn_openfile, self.btn_test):
            b.state(state)

    # ------------------------------------------------------------ background work

    def run_bg(self, label, fn, done):
        self.set_busy(True, label)

        def work():
            try:
                res, err = fn(), None
            except Exception as ex:  # report any failure in the UI
                res, err = None, ex
            self.q.put(("call", done, res, err))
        threading.Thread(target=work, daemon=True).start()

    def set_busy(self, busy, label=None):
        if busy:
            self.status.config(text=label or "Working...")
            self.progress.pack(side="right")
            self.progress.start(12)
            self.config(cursor="watch")
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.config(cursor="")

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "call":
                    _, done, res, err = msg
                    self.set_busy(False)
                    done(res, err)
                elif msg[0] == "status":
                    self.status.config(text=msg[1])
                elif msg[0] == "clog" and self.create_dlg:
                    self.create_dlg.log(msg[1])
        except queue.Empty:
            pass
        self.after(80, self._poll)

    # ------------------------------------------------------------ open / tree

    def open_dialog(self):
        path = filedialog.askopenfilename(
            title="Open UZip archive",
            filetypes=[("UZip archives", "*.uz"), ("All files", "*.*")])
        if path:
            self.open_archive(path)

    def open_archive(self, path):
        def done(arc, err):
            if err:
                messagebox.showerror("Could not open archive", str(err))
                self.status.config(text="Open failed")
                return
            self.arc = arc
            self.title("UZip Explorer - " + os.path.basename(path))
            self.filter_var.set("")
            self.populate()
            self._set_actions(True)
            files = [e for e in arc.entries if not e.is_dir]
            nrc = sum(e.recompressed for e in files)
            self.info.config(text="%s    |    %d files (%d recompressed)    |    "
                             "%s packed to %s (%.1f%%)    |    codec %s    |    format v%d"
                             % (os.path.basename(path), len(files), nrc,
                                human(arc.total_size), human(arc.file_size),
                                uzip.pct(arc.file_size, arc.total_size),
                                arc.codec_name, arc.version))
            self.show_summary()
            msg = "Opened %s, checksum OK" % path
            if arc.zlib_mismatch:
                msg += ("   WARNING: made with zlib %s, this Python has %s; "
                        "recompressed files may not rebuild"
                        % (arc.zlib_version, zlib.ZLIB_RUNTIME_VERSION))
            self.status.config(text=msg)
        self.run_bg("Opening %s..." % os.path.basename(path),
                    lambda: uzip.Archive(path), done)

    def populate(self):
        if not self.arc:
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.folders.clear()
        flt = self.filter_var.get().strip().lower()
        by_path = {}

        def folder(path):
            if not path:
                return ""
            if path in by_path:
                return by_path[path]
            parent = folder(posixpath.dirname(path))
            iid = self.tree.insert(parent, "end", text="  " + posixpath.basename(path),
                                   values=("", "folder", ""), tags=("dir",),
                                   open=bool(flt) or parent == "")
            by_path[path] = iid
            self.folders[iid] = path
            return iid

        for e in self.arc.entries:
            if e.is_dir:
                if not flt:
                    folder(e.path)
                continue
            if flt and flt not in e.path.lower():
                continue
            parent = folder(posixpath.dirname(e.path))
            storage = ("recompressed" if e.recompressed else
                       "duplicate" if e.is_dup else "stored")
            iid = self.tree.insert(
                parent, "end", text="  " + posixpath.basename(e.path),
                values=(human(e.size), storage,
                        time.strftime("%Y-%m-%d %H:%M", time.localtime(e.mtime))),
                tags=("rc",) if e.recompressed else ("dup",) if e.is_dup else ())
            self.items[iid] = e

    def selected_entries(self):
        out, seen = [], set()
        for iid in self.tree.selection():
            if iid in self.items:
                cands = [self.items[iid]]
            elif iid in self.folders:
                prefix = self.folders[iid] + "/"
                cands = [e for e in self.arc.entries
                         if e.path == self.folders[iid] or e.path.startswith(prefix)]
            else:
                cands = []
            for e in cands:
                if e.path not in seen:
                    seen.add(e.path)
                    out.append(e)
        return out

    def on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------ preview

    def show_message(self, title, body):
        self.pv_title.config(text=title)
        self.pv_meta.config(text="")
        self._show_text(body)

    def show_summary(self):
        arc = self.arc
        files = [e for e in arc.entries if not e.is_dir]
        rc = [e for e in files if e.recompressed]
        dups = [e for e in files if e.is_dup]
        by_ext = {}
        for e in files:
            ext = os.path.splitext(e.path)[1].lower() or "(none)"
            n, s = by_ext.get(ext, (0, 0))
            by_ext[ext] = (n + 1, s + e.size)
        lines = [
            "Archive      %s" % arc.path,
            "Stored as    %s  (%.2f%% of %s)" % (human(arc.file_size),
                                                 uzip.pct(arc.file_size, arc.total_size),
                                                 human(arc.total_size)),
            "Format       UZip v%d" % arc.version,
            "Made with    zlib %s  (this Python: zlib %s)"
            % (arc.zlib_version or "n/a", zlib.ZLIB_RUNTIME_VERSION),
            "",
            "Blocks (each type of data raced its own codecs):",
        ]
        for b in arc.blocks:
            lines.append("  %-13s %-14s %10s -> %10s"
                         % (uzip.CLASS_NAMES.get(b["cls"], "?"),
                            uzip.codec_name(b["codec"]), human(len(b["raw"])),
                            human(b["comp"])))
        lines += [
            "",
            "Recompressed %d of %d files: their old ZIP/PNG/GZ/bzip2/xz data was"
            % (len(rc), len(files)),
            "             reversed and is rebuilt bit-exact on extract or preview.",
            "Duplicates   %d file(s) stored once (%s saved before compression)"
            % (len(dups), human(sum(e.size for e in dups))),
            "",
            "By type:",
        ]
        for ext, (n, s) in sorted(by_ext.items(), key=lambda kv: -kv[1][1]):
            lines.append("  %-10s %5d file(s)  %10s" % (ext, n, human(s)))
        lines += ["", "Select a file on the left to preview it.",
                  "Double-click a file to open it in its normal app."]
        self.pv_title.config(text=os.path.basename(arc.path))
        self.pv_meta.config(text="Archive summary")
        self._show_text("\n".join(lines))

    def on_select(self, event=None):
        sel = self.tree.selection()
        if not sel or not self.arc:
            return
        iid = sel[0]
        if iid in self.folders:
            path = self.folders[iid]
            inside = [e for e in self.arc.entries
                      if not e.is_dir and e.path.startswith(path + "/")]
            self.pv_title.config(text=posixpath.basename(path) + "/")
            self.pv_meta.config(text="Folder  |  %d files  |  %s"
                                % (len(inside), human(sum(e.size for e in inside))))
            self._show_text("\n".join("%10s  %s" % (human(e.size), e.path[len(path) + 1:])
                                      for e in inside) or "(empty folder)")
            return
        e = self.items.get(iid)
        if not e:
            return
        self.preview_token += 1
        token = self.preview_token

        def done(data, err):
            if token != self.preview_token:
                return
            self.status.config(text="Ready")
            if err:
                self.show_message(posixpath.basename(e.path), "Could not read file:\n%s" % err)
                return
            self.render_preview(e, data)
        self.run_bg("Reading %s..." % e.path, lambda: self.arc.read(e), done)

    def render_preview(self, e, data):
        name = posixpath.basename(e.path)
        ext = os.path.splitext(name)[1].lower()
        meta = ["%s" % human(e.size),
                time.strftime("modified %Y-%m-%d %H:%M", time.localtime(e.mtime))]
        if e.recompressed:
            meta.append("recompressed: old compression reversed (%s raw), "
                        "rebuilt bit-exact" % human(e.unpacked_size))
        elif e.is_dup:
            meta.append("duplicate of %s (stored once)" % e.dup_of)
        else:
            meta.append("stored as-is")
        self.pv_title.config(text=name)

        is_png = data.startswith(b"\x89PNG")
        is_gif = data[:6] in (b"GIF87a", b"GIF89a")
        if is_png or is_gif or ext in TK_IMG or (Image and ext in PIL_IMG):
            info = self._show_image(data)
            if info:
                self.pv_meta.config(text="    |    ".join(meta + [info]))
                return
        kind, body = self.describe(data, ext)
        self.pv_meta.config(text="    |    ".join(meta + [kind]))
        self._show_text(body)

    def describe(self, data, ext):
        bio = io.BytesIO(data)
        if data[:4] == b"PK\x03\x04" and zipfile.is_zipfile(bio):
            try:
                with zipfile.ZipFile(bio) as z:
                    infos = z.infolist()
                    rows = ["Contents of this ZIP container (%d entries):" % len(infos), "",
                            "%10s  %10s  %s" % ("size", "packed", "name"),
                            "%10s  %10s  %s" % ("-" * 10, "-" * 10, "-" * 40)]
                    for i in infos:
                        rows.append("%10s  %10s  %s" % (human(i.file_size),
                                                         human(i.compress_size),
                                                         i.filename))
                    # show readable text from office documents
                    for main in ("word/document.xml", "xl/sharedStrings.xml",
                                 "content.xml", "ppt/slides/slide1.xml"):
                        if main in z.namelist():
                            txt = xml_to_text(decode_text(z.read(main)))
                            if len(txt) > 20000:
                                txt = txt[:20000] + "\n..."
                            rows += ["", "---- text of %s ----" % main, "", txt]
                            break
                return "ZIP container", "\n".join(rows)
            except zipfile.BadZipFile:
                pass
        if data[:5] == b"%PDF-":
            return "PDF document", pdf_summary(data)
        if is_tar(data):
            return "tar archive", tar_listing(data)
        for magic, label, fn in STREAM_FORMATS:
            if data.startswith(magic) and fn is not None:
                try:
                    inner = fn(data)
                except Exception:
                    continue
                if is_tar(inner):
                    return ("%s containing a tar archive (%s unpacked)"
                            % (label, human(len(inner))), tar_listing(inner))
                if looks_text(inner):
                    return ("%s, %s uncompressed text" % (label, human(len(inner))),
                            decode_text(inner[:MAX_TEXT]))
                return ("%s, %s uncompressed binary" % (label, human(len(inner))),
                        hexdump(inner))
        for magic, off, label, why in FOREIGN_ARCHIVES:
            if data[off:off + len(magic)] == magic:
                return label, ("%s\n\n%s\n\n" % (label, textwrap.fill(why, 64))) + hexdump(data, 1024)
        if looks_text(data):
            body = decode_text(data[:MAX_TEXT])
            if len(data) > MAX_TEXT:
                body += "\n\n... (showing first %s)" % human(MAX_TEXT)
            return "text", body
        return "binary (hex view)", hexdump(data)

    def _show_text(self, body):
        self.image_label.pack_forget()
        self.text_frame.pack(fill="both", expand=True)
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", body)
        self.text.config(state="disabled")

    def _show_image(self, data):
        self.update_idletasks()
        maxw = max(200, self.pv_body.winfo_width() - 10)
        maxh = max(200, self.pv_body.winfo_height() - 10)
        try:
            if Image is not None:
                im = Image.open(io.BytesIO(data))
                size = im.size
                im.thumbnail((maxw, maxh))
                photo = ImageTk.PhotoImage(im)
            else:
                photo = tk.PhotoImage(data=base64.b64encode(data))
                size = (photo.width(), photo.height())
                k = math.ceil(max(size[0] / maxw, size[1] / maxh, 1))
                if k > 1:
                    photo = photo.subsample(k)
        except Exception:
            return None
        self._photo = photo
        self.text_frame.pack_forget()
        self.image_label.config(image=photo)
        self.image_label.pack(fill="both", expand=True)
        return "image %d x %d" % size

    # ------------------------------------------------------------ actions

    def _need_archive(self):
        if not self.arc:
            messagebox.showinfo("UZip", "Open an archive first.")
            return False
        return True

    def extract_selected(self):
        if not self._need_archive():
            return
        entries = self.selected_entries()
        if not entries:
            messagebox.showinfo("UZip", "Select files or folders in the tree first.")
            return
        self._extract(entries)

    def extract_all(self):
        if self._need_archive():
            self._extract(None)

    def _extract(self, entries):
        outdir = filedialog.askdirectory(title="Extract to folder")
        if not outdir:
            return
        skipped = []

        def log(s):
            if s.startswith("exists"):
                skipped.append(s)

        def done(res, err):
            if err:
                messagebox.showerror("Extract failed", str(err))
                return
            count, failed = res
            msg = "Extracted %d file(s) to\n%s" % (count, outdir)
            if skipped:
                msg += "\n\nSkipped %d file(s) that already exist." % len(skipped)
            if failed:
                msg += "\n\n%d file(s) could not be rebuilt:\n%s" % (
                    len(failed), "\n".join(failed[:10]))
                messagebox.showwarning("Extract finished with errors", msg)
            else:
                messagebox.showinfo("Extract complete", msg)
            self.status.config(text="Extracted %d file(s)" % count)
        self.run_bg("Extracting...", lambda: self.arc.extract(outdir, entries, False, log),
                    done)

    def open_external(self):
        if not self.arc:
            return
        sel = [self.items[i] for i in self.tree.selection() if i in self.items]
        if not sel:
            return
        e = sel[0]

        def work():
            target = os.path.join(self._tmpdir, str(time.time_ns()),
                                  posixpath.basename(e.path))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(self.arc.read(e))
            return target

        def done(target, err):
            if err:
                messagebox.showerror("Could not open", str(err))
                return
            try:
                open_with_system(target)
                self.status.config(text="Opened a temporary copy of " + e.path)
            except Exception as ex:
                messagebox.showerror("Could not open", str(ex))
        self.run_bg("Preparing %s..." % e.path, work, done)

    def test_archive(self):
        if not self._need_archive():
            return

        def done(failed, err):
            if err:
                messagebox.showerror("Test failed", str(err))
            elif failed:
                messagebox.showwarning("Test failed", "%d file(s) could not be rebuilt:\n%s"
                                       % (len(failed), "\n".join(failed[:15])))
            else:
                messagebox.showinfo("Test passed",
                                    "All %d entries verified.\nArchive checksum OK and "
                                    "every recompressed file rebuilt bit-exact."
                                    % len(self.arc.entries))
        self.run_bg("Testing archive...", self.arc.test, done)

    def new_archive(self):
        if self.create_dlg and self.create_dlg.winfo_exists():
            self.create_dlg.lift()
            return
        self.create_dlg = CreateDialog(self)


# ====================================================================== create dialog

class CreateDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("New UZip Archive")
        self.geometry("640x560")
        self.configure(bg=BG)
        self.inputs = []

        pad = {"padx": 12, "pady": 4}
        ttk.Label(self, text="Files and folders to pack", font=app.bold).pack(
            anchor="w", padx=12, pady=(12, 4))
        lf = ttk.Frame(self)
        lf.pack(fill="x", **pad)
        self.listbox = tk.Listbox(lf, height=7, activestyle="none", relief="flat",
                                  highlightthickness=1, highlightcolor="#cbd5e1")
        self.listbox.pack(side="left", fill="both", expand=True)
        bf = ttk.Frame(lf)
        bf.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(bf, text="Add Files...", command=self.add_files).pack(fill="x")
        ttk.Button(bf, text="Add Folder...", command=self.add_folder).pack(fill="x", pady=4)
        ttk.Button(bf, text="Remove", command=self.remove).pack(fill="x")

        opt = ttk.Frame(self)
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="Level:").pack(side="left")
        self.level = ttk.Combobox(opt, values=["max", "normal", "fast"], width=8,
                                  state="readonly")
        self.level.set("max")
        self.level.pack(side="left", padx=(6, 16))
        self.rc = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Recompress ZIP / DOCX / PNG / GZ / BZ2 / XZ / PDF",
                        variable=self.rc).pack(side="left")

        of = ttk.Frame(self)
        of.pack(fill="x", **pad)
        ttk.Label(of, text="Save as:").pack(side="left")
        self.out = tk.StringVar()
        ttk.Entry(of, textvariable=self.out).pack(side="left", fill="x", expand=True,
                                                  padx=6)
        ttk.Button(of, text="Browse...", command=self.browse).pack(side="left")

        self.btn = ttk.Button(self, text="Create Archive", style="Accent.TButton",
                              command=self.create)
        self.btn.pack(anchor="e", padx=12, pady=(6, 6))

        ttk.Label(self, text="Log", font=app.bold).pack(anchor="w", padx=12)
        self.logtext = tk.Text(self, height=12, font=app.mono, relief="flat",
                               bg="#fbfbfc", state="disabled")
        self.logtext.pack(fill="both", expand=True, padx=12, pady=(2, 12))

    def _refresh(self):
        self.listbox.delete(0, "end")
        for p in self.inputs:
            self.listbox.insert("end", p)
        if self.inputs and not self.out.get():
            first = self.inputs[0].rstrip("/\\")
            self.out.set(os.path.join(os.path.dirname(first),
                                      os.path.basename(first).split(".")[0] + ".uz"))

    def add_files(self):
        for p in filedialog.askopenfilenames(parent=self, title="Add files"):
            if p not in self.inputs:
                self.inputs.append(p)
        self._refresh()

    def add_folder(self):
        p = filedialog.askdirectory(parent=self, title="Add folder")
        if p and p not in self.inputs:
            self.inputs.append(p)
        self._refresh()

    def remove(self):
        for i in reversed(self.listbox.curselection()):
            del self.inputs[i]
        self._refresh()

    def browse(self):
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".uz",
                                         filetypes=[("UZip archive", "*.uz")])
        if p:
            self.out.set(p)

    def log(self, s):
        self.logtext.config(state="normal")
        self.logtext.insert("end", s + "\n")
        self.logtext.see("end")
        self.logtext.config(state="disabled")

    def create(self):
        if not self.inputs:
            messagebox.showinfo("UZip", "Add at least one file or folder.", parent=self)
            return
        out = self.out.get().strip()
        if not out:
            messagebox.showinfo("UZip", "Choose where to save the archive.", parent=self)
            return
        self.btn.state(["disabled"])
        q = self.app.q
        level, rc, inputs = self.level.get(), self.rc.get(), list(self.inputs)

        def work():
            return uzip.create_archive(out, inputs, level=level, recompress=rc,
                                       log=lambda s: q.put(("clog", s)))

        def done(stats, err):
            if self.winfo_exists():
                self.btn.state(["!disabled"])
            if err:
                messagebox.showerror("Create failed", str(err), parent=self)
                return
            self.app.open_archive(stats["out"])
        self.app.run_bg("Creating archive...", work, done)


def main():
    app = App(sys.argv[1] if len(sys.argv) > 1 else None)
    app.mainloop()


if __name__ == "__main__":
    main()
