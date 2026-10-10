#!/usr/bin/env python3
"""
EPD Image Tool - resize any picture to an e-paper panel and quantise it to
16 gray levels, with a live preview you can zoom and drag into place.

Standalone; nothing in the firmware depends on it.

    python epd_image_tool.py                      # GUI
    python epd_image_tool.py --open IN            # GUI with an image loaded
    python epd_image_tool.py --selftest           # exercise the converter
    python epd_image_tool.py --cli IN --panel 6 --out out.bmp [--raw out.bin]

Fit modes
    fit       whole image inside the panel, letterboxed (upscales small images)
    fill      cover the panel, crop the overflow
    width     match the panel width exactly (height may crop or letterbox)
    height    match the panel height exactly
    stretch   distort to the exact panel size
    manual    free zoom + position (set by the zoom slider or by dragging)

Outputs
    .bmp   24-bit, R=G=B, values 0,17,34..255 (16 steps). What the legacy PC
           tools (EPDdemo / EPDctrl) load.
    .bin   4 bits per pixel, two pixels per byte, first pixel in the HIGH
           nibble, 0 = black .. 15 = white. The stream the firmware pushes
           into the IT8951 image buffer.

Requires Pillow (pip install pillow). Tkinter ships with Python.
"""
import argparse
import os
import socket
import struct
import sys
import tempfile
import threading

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

TCP_PORT = 8319                                        # firmware listens here
IP_MEMO = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_ip")


def _hdr(cmd, color, x, y, w, h):
    """10-byte little-endian command header the firmware expects."""
    return struct.pack("<BBHHHH", cmd, color, x, y, w, h)

# ---------------------------------------------------------------------------
#  Panel presets - bitmap W x H as the panel vendors state them
# ---------------------------------------------------------------------------
PANELS = {
    '6"   (OPM060E9)   1072 x 1448': (1072, 1448),
    '7.8" (ED078KC2)   1872 x 1404': (1872, 1404),
    '10.3"(ED103TC2)   1872 x 1404': (1872, 1404),
    "사용자 정의": None,
}
PANEL_BY_INCH = {"6": (1072, 1448), "7.8": (1872, 1404), "10.3": (1872, 1404)}

MODES = {
    "fit (여백)":       "fit",
    "fill (잘라냄)":    "fill",
    "가로 (width)":     "width",
    "세로 (height)":    "height",
    "stretch (늘림)":   "stretch",
    "수동 (줌/드래그)": "manual",
}
MODE_MANUAL = "수동 (줌/드래그)"
LEVELS = 16

# 16-entry gray palette image used for dithered quantisation
_PAL = Image.new("P", (1, 1))
_PAL.putpalette([v for i in range(LEVELS) for v in (i * 17,) * 3] + [0] * (768 - LEVELS * 3))


# ---------------------------------------------------------------------------
#  Layout: resolve (scale, offset) for a mode, or accept manual values
# ---------------------------------------------------------------------------
def base_scale(iw, ih, size, mode):
    """Uniform scale a preset mode would use (manual/stretch excluded)."""
    w, h = size
    if mode == "fill":
        return max(w / iw, h / ih)
    if mode == "width":
        return w / iw
    if mode == "height":
        return h / ih
    return min(w / iw, h / ih)                         # fit


def resolve_layout(iw, ih, size, mode, scale=None, off=None):
    """Return (scale, off_x, off_y). Centre presets; pass manual through."""
    w, h = size
    if mode == "manual":
        s = scale if scale else base_scale(iw, ih, size, "fit")
        if off is not None:
            return s, off[0], off[1]
        return s, round((w - iw * s) / 2), round((h - ih * s) / 2)
    s = base_scale(iw, ih, size, mode)
    return s, round((w - iw * s) / 2), round((h - ih * s) / 2)


# ---------------------------------------------------------------------------
#  Conversion core (no GUI)
# ---------------------------------------------------------------------------
def convert(img, size, *, rotate=0, mode="fit", scale=None, off=None, pad_white=True,
            brightness=0, contrast=0, gamma=1.0, autocontrast=False, sharpen=0,
            dither=True, invert=False):
    """
    Return (levels_img, display_img, meta):
      levels_img  - "L", each pixel is its 4-bit level 0..15
      display_img - "L", each pixel is level*17 (what you see / save)
      meta        - dict(scale, off, src=(iw,ih)) actually used
    """
    w, h = size
    if w % 2:
        raise ValueError("width must be even (two pixels per byte)")

    im = ImageOps.exif_transpose(img).convert("RGB")
    if rotate:
        im = im.rotate(-rotate, expand=True)           # clockwise degrees
    iw, ih = im.size

    pad = (255, 255, 255) if pad_white else (0, 0, 0)
    if mode == "stretch":
        placed = im.resize((w, h), Image.LANCZOS)
        meta = {"scale": None, "off": (0, 0), "src": (iw, ih)}
    else:
        s, ox, oy = resolve_layout(iw, ih, size, mode, scale, off)
        sw, sh = max(1, round(iw * s)), max(1, round(ih * s))
        scaled = im.resize((sw, sh), Image.LANCZOS)
        placed = Image.new("RGB", (w, h), pad)
        placed.paste(scaled, (ox, oy))
        meta = {"scale": s, "off": (ox, oy), "src": (iw, ih)}

    gray = placed.convert("L")
    # Stretch the histogram first so the rest works on full 0..255 range. The
    # padding border is excluded from the measurement (cutoff) so a big white
    # letterbox doesn't flatten the stretch.
    if autocontrast:
        gray = ImageOps.autocontrast(gray, cutoff=1)
    if brightness:
        gray = ImageEnhance.Brightness(gray).enhance(1 + brightness / 100.0)
    if contrast:
        gray = ImageEnhance.Contrast(gray).enhance(1 + contrast / 100.0)
    if gamma and abs(gamma - 1.0) > 1e-3:
        lut = [round(255 * ((i / 255.0) ** (1.0 / gamma))) for i in range(256)]
        gray = gray.point(lut)
    # Sharpen last, before quantisation: e-paper softens edges, so an unsharp
    # mask makes text and detail read far better. percent scales the amount.
    if sharpen > 0:
        gray = gray.filter(ImageFilter.UnsharpMask(radius=2, percent=int(sharpen * 1.5), threshold=2))
    if invert:
        gray = ImageOps.invert(gray)

    if dither:
        q = gray.convert("RGB").quantize(palette=_PAL, dither=Image.Dither.FLOYDSTEINBERG)
        # Through the palette gray (0,17,..) back to the level; robust to Pillow
        # choosing any of the padded duplicate black entries (index >= 16).
        levels = q.convert("L").point([round(v / 17) for v in range(256)])
    else:
        levels = gray.point([i // 16 for i in range(256)])

    display = levels.point([min(255, i * 17) for i in range(256)])
    return levels, display, meta


def pack_4bpp(levels_img):
    """Two pixels per byte, first pixel in the high nibble."""
    data = levels_img.tobytes()
    hi, lo = data[0::2], data[1::2]
    return bytes(((a & 0x0F) << 4) | (b & 0x0F) for a, b in zip(hi, lo))


def save_bmp(display_img, path):
    display_img.convert("RGB").save(path, "BMP")        # 24-bit


def save_raw(levels_img, path):
    with open(path, "wb") as f:
        f.write(pack_4bpp(levels_img))


# ---------------------------------------------------------------------------
#  Self test
# ---------------------------------------------------------------------------
def selftest():
    small = Image.new("RGB", (161, 161))
    for x in range(161):
        for y in range(161):
            small.putpixel((x, y), ((x + y) % 256,) * 3)
    big = Image.linear_gradient("L").resize((640, 480)).convert("RGB")

    for name, size in PANEL_BY_INCH.items():
        for mode in ("fit", "fill", "width", "height", "stretch", "manual"):
            lv, disp, meta = convert(small if mode in ("fit", "width") else big,
                                     size, mode=mode, dither=(mode != "manual"))
            assert lv.size == size, (name, mode, lv.size)
            assert set(lv.tobytes()) <= set(range(LEVELS)), "level out of range"
            assert len(pack_4bpp(lv)) == size[0] * size[1] // 2, "raw size"
    # the 161x161 bug: fit/width must now enlarge to fill at least one axis
    lv, disp, meta = convert(small, (1072, 1448), mode="width")
    assert meta["scale"] > 6.0, f"small image not upscaled (scale={meta['scale']})"
    print(f"  161x161 width-fill scale = {meta['scale']:.2f} (fills 1072 px)")
    # manual offset/scale honoured
    lv, disp, meta = convert(big, (1072, 1448), mode="manual", scale=2.0, off=(10, 20))
    assert abs(meta["scale"] - 2.0) < 1e-6 and meta["off"] == (10, 20), "manual not honoured"
    with tempfile.TemporaryDirectory() as d:
        lv, disp, meta = convert(big, PANEL_BY_INCH["6"], mode="fit")
        p = os.path.join(d, "t.bmp"); save_bmp(disp, p)
        with Image.open(p) as back:
            assert back.size == PANEL_BY_INCH["6"] and back.mode == "RGB", "bmp roundtrip"
    print("selftest OK")


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------
def run_cli(a):
    size = PANEL_BY_INCH.get(a.panel)
    if size is None:
        if not a.size:
            sys.exit("--panel must be 6 / 7.8 / 10.3, or give --size WxH")
        size = tuple(int(v) for v in a.size.lower().split("x"))
    img = Image.open(a.cli)
    off = (a.offx, a.offy) if (a.offx or a.offy) else None
    scale = (a.scale / 100.0) if a.scale else None
    lv, disp, meta = convert(img, size, rotate=a.rotate, mode=a.mode, scale=scale, off=off,
                             pad_white=not a.pad_black, brightness=a.brightness,
                             contrast=a.contrast, gamma=a.gamma, autocontrast=a.autocontrast,
                             sharpen=a.sharpen, dither=not a.no_dither, invert=a.invert)
    if a.out:
        save_bmp(disp, a.out); print(f"wrote {a.out}  ({size[0]}x{size[1]} 24-bit BMP, scale={meta['scale']})")
    if a.raw:
        save_raw(lv, a.raw);   print(f"wrote {a.raw}  ({size[0]*size[1]//2:,} B, 4bpp)")


# ---------------------------------------------------------------------------
#  GUI
# ---------------------------------------------------------------------------
def _load_ip():
    try:
        with open(IP_MEMO) as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_ip(ip):
    try:
        with open(IP_MEMO, "w") as f:
            f.write(ip.strip())
    except OSError:
        pass


def run_gui(initial=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from PIL import ImageTk

    root = tk.Tk()
    root.title("EPD Image Tool - 16계조 변환")
    root.geometry("1180x740")
    root.minsize(960, 580)

    st = {"src": None, "src_path": None, "src_gray": None, "levels": None,
          "display": None, "job": None, "out_scale": 1.0, "out_pos": (0, 0),
          "man_scale": 1.0, "man_off": None, "cur_scale": 1.0, "cur_off": (0, 0),
          "drag": None, "dragging": False, "sync": False}

    side = ttk.Frame(root, padding=10); side.pack(side="left", fill="y")

    def row(label):
        f = ttk.Frame(side); f.pack(fill="x", pady=3)
        ttk.Label(f, text=label, width=9).pack(side="left"); return f

    ttk.Button(side, text="이미지 열기…", command=lambda: open_image()).pack(fill="x", pady=(0, 6))
    src_lbl = ttk.Label(side, text="(파일 없음)", foreground="#666", wraplength=240); src_lbl.pack(fill="x")
    ttk.Separator(side).pack(fill="x", pady=8)

    panel_var = tk.StringVar(value=list(PANELS)[0])
    f = row("패널")
    ttk.Combobox(f, textvariable=panel_var, values=list(PANELS), state="readonly", width=26).pack(side="left", fill="x", expand=True)

    f = row("크기")
    w_var, h_var = tk.StringVar(value="1072"), tk.StringVar(value="1448")
    w_ent = ttk.Entry(f, textvariable=w_var, width=7); w_ent.pack(side="left")
    ttk.Label(f, text=" x ").pack(side="left")
    h_ent = ttk.Entry(f, textvariable=h_var, width=7); h_ent.pack(side="left")

    f = row("회전")
    rot_var = tk.StringVar(value="0")
    ttk.Combobox(f, textvariable=rot_var, values=("0", "90", "180", "270"), state="readonly", width=6).pack(side="left")
    ttk.Label(f, text="° 시계방향").pack(side="left")

    f = row("맞춤")
    mode_var = tk.StringVar(value=list(MODES)[0])
    ttk.Combobox(f, textvariable=mode_var, values=list(MODES), state="readonly", width=16).pack(side="left")
    pad_var = tk.StringVar(value="흰색")
    ttk.Combobox(f, textvariable=pad_var, values=("흰색", "검정"), state="readonly", width=5).pack(side="left", padx=(6, 0))

    f = row("줌 %")
    zoom_var = tk.DoubleVar(value=100)
    zoom_scale = ttk.Scale(f, from_=10, to=400, variable=zoom_var, orient="horizontal")
    zoom_scale.pack(side="left", fill="x", expand=True)
    zoom_lbl = ttk.Label(f, width=5); zoom_lbl.pack(side="left")
    ttk.Label(side, text="결과 미리보기를 드래그해 위치 이동 · 더블클릭=가운데",
              foreground="#777", wraplength=240).pack(fill="x", pady=(0, 2))

    def slider(label, var, lo, hi, res=1):
        f = row(label)
        ttk.Scale(f, from_=lo, to=hi, variable=var, orient="horizontal").pack(side="left", fill="x", expand=True)
        v = ttk.Label(f, width=5); v.pack(side="left")
        def upd(*_): v.config(text=f"{var.get():.2f}" if res != 1 else f"{int(var.get())}")
        var.trace_add("write", upd); upd()

    bri_var = tk.DoubleVar(value=0);   slider("밝기", bri_var, -100, 100)
    con_var = tk.DoubleVar(value=0);   slider("대비", con_var, -100, 100)
    gam_var = tk.DoubleVar(value=1.0); slider("감마", gam_var, 0.3, 3.0, res=0.01)
    shp_var = tk.DoubleVar(value=0);   slider("샤픈", shp_var, 0, 100)

    ac_var = tk.BooleanVar(value=False)
    dith_var = tk.BooleanVar(value=True); inv_var = tk.BooleanVar(value=False)
    f = ttk.Frame(side); f.pack(fill="x", pady=6)
    ttk.Checkbutton(f, text="자동 대비 (히스토그램)", variable=ac_var).pack(anchor="w")
    ttk.Checkbutton(f, text="디더링 (Floyd-Steinberg)", variable=dith_var).pack(anchor="w")
    ttk.Checkbutton(f, text="반전", variable=inv_var).pack(anchor="w")
    ttk.Button(side, text="초기화", command=lambda: reset_adjust()).pack(fill="x", pady=(2, 8))

    ttk.Separator(side).pack(fill="x", pady=8)
    ttk.Button(side, text="BMP 저장 (24-bit, 툴 입력용)…", command=lambda: save("bmp")).pack(fill="x", pady=2)
    ttk.Button(side, text="RAW 저장 (4bpp, 펌웨어 포맷)…", command=lambda: save("raw")).pack(fill="x", pady=2)

    ttk.Separator(side).pack(fill="x", pady=8)
    f = row("보드 IP")
    ip_var = tk.StringVar(value=_load_ip())
    ttk.Entry(f, textvariable=ip_var, width=16).pack(side="left", fill="x", expand=True)
    clr_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(side, text="전송 전 흰색으로 지우기", variable=clr_var).pack(anchor="w")
    send_btn = ttk.Button(side, text="▶ 보드로 전송", command=lambda: do_send())
    send_btn.pack(fill="x", pady=(2, 0))
    send_lbl = ttk.Label(side, text="IP 입력 후 전송 (포트 8319)", foreground="#777", wraplength=244)
    send_lbl.pack(fill="x", pady=(2, 0))

    info = ttk.Label(side, text="", foreground="#444", justify="left", wraplength=244); info.pack(fill="x", pady=(12, 0))

    right = ttk.Frame(root, padding=(0, 10, 10, 10)); right.pack(side="right", fill="both", expand=True)
    hdr = ttk.Frame(right); hdr.pack(fill="x")
    ttk.Label(hdr, text="원본").pack(side="left", expand=True)
    ttk.Label(hdr, text="결과 (16계조, 실제 출력 - 드래그 이동)").pack(side="left", expand=True)
    cvf = ttk.Frame(right); cvf.pack(fill="both", expand=True)
    cv_src = tk.Canvas(cvf, bg="#dcdcdc", highlightthickness=0)
    cv_out = tk.Canvas(cvf, bg="#dcdcdc", highlightthickness=0)
    cv_src.pack(side="left", fill="both", expand=True, padx=(0, 4))
    cv_out.pack(side="left", fill="both", expand=True, padx=(4, 0))
    status = ttk.Label(right, text="이미지를 열어 시작하세요.", anchor="w"); status.pack(fill="x", pady=(6, 0))

    photos = {}

    def draw(canvas, img, key):
        canvas.delete("all")
        if img is None:
            return 1.0, (0, 0)
        cw, ch = max(canvas.winfo_width(), 50), max(canvas.winfo_height(), 50)
        scale = min(cw / img.width, ch / img.height, 1.0)
        shown = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.NEAREST if key == "out" else Image.LANCZOS)
        photos[key] = ImageTk.PhotoImage(shown)
        x0, y0 = (cw - shown.width) // 2, (ch - shown.height) // 2
        canvas.create_image(x0, y0, image=photos[key], anchor="nw")
        return scale, (x0, y0)

    def cur_mode():
        return MODES[mode_var.get()]

    def rebuild_gray():
        """Rotated grayscale source kept for the fast drag preview."""
        if st["src"] is None:
            st["src_gray"] = None
            return
        g = ImageOps.exif_transpose(st["src"]).convert("L")
        r = int(rot_var.get())
        if r:
            g = g.rotate(-r, expand=True)
        st["src_gray"] = g

    def target_size():
        try:
            w, h = int(w_var.get()), int(h_var.get())
        except ValueError:
            return None
        return (w - (w % 2), h) if (w >= 2 and h >= 1) else None

    def on_panel(*_):
        size = PANELS[panel_var.get()]
        custom = size is None
        for e in (w_ent, h_ent):
            e.config(state="normal" if custom else "readonly")
        if not custom:
            w_var.set(str(size[0])); h_var.set(str(size[1]))
        schedule()

    def open_image(path=None):
        p = path or filedialog.askopenfilename(
            filetypes=[("이미지", "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp"), ("모든 파일", "*.*")])
        if not p:
            return
        try:
            img = Image.open(p); img.load()
        except Exception as e:
            messagebox.showerror("열기 실패", str(e)); return
        st["src"], st["src_path"] = img, p
        st["man_off"] = None
        rebuild_gray()
        src_lbl.config(text=f"{os.path.basename(p)}\n{img.width} x {img.height}  {img.mode}")
        draw(cv_src, img.convert("RGB"), "src")
        schedule()

    def reset_adjust():
        st["sync"] = True
        bri_var.set(0); con_var.set(0); gam_var.set(1.0); shp_var.set(0); zoom_var.set(100)
        ac_var.set(False); dith_var.set(True); inv_var.set(False); rot_var.set("0")
        mode_var.set(list(MODES)[0]); pad_var.set("흰색")
        st["man_off"] = None; st["sync"] = False
        schedule()

    def go_manual_from_current():
        """Switch to manual WITHOUT changing size or position: seed the manual
        scale/offset from whatever the current mode just resolved to."""
        if st["src"] is None:
            return
        st["man_scale"] = st["cur_scale"] or st["man_scale"]
        st["man_off"] = st["cur_off"] if st["cur_off"] is not None else st["man_off"]
        st["sync"] = True
        mode_var.set(MODE_MANUAL)
        st["sync"] = False

    def on_zoom(*_):
        if st["sync"]:
            return
        # user turned the zoom knob -> manual, keep current offset
        if cur_mode() != "manual":
            go_manual_from_current()
        st["man_scale"] = zoom_var.get() / 100.0
        schedule()

    def render():
        st["job"] = None
        if st["src"] is None:
            return
        size = target_size()
        if size is None:
            status.config(text="크기가 올바르지 않습니다 (가로는 짝수)."); return
        mode = cur_mode()
        scale = st["man_scale"] if mode == "manual" else None
        off = st["man_off"] if mode == "manual" else None
        use_dither = dith_var.get() and not st["dragging"]
        try:
            lv, disp, meta = convert(st["src"], size, rotate=int(rot_var.get()), mode=mode,
                                     scale=scale, off=off, pad_white=(pad_var.get() == "흰색"),
                                     brightness=bri_var.get(), contrast=con_var.get(),
                                     gamma=gam_var.get(), autocontrast=ac_var.get(),
                                     sharpen=shp_var.get(), dither=use_dither, invert=inv_var.get())
        except Exception as e:
            status.config(text=f"변환 오류: {e}"); return
        st["levels"], st["display"] = lv, disp
        st["out_scale"], st["out_pos"] = draw(cv_out, disp, "out")
        # Remember what this mode actually resolved to, so a later switch to
        # manual (drag / zoom) can start from the exact same size and place.
        iw, ih = meta["src"]
        st["cur_scale"] = meta["scale"] if meta["scale"] else base_scale(iw, ih, size, "fit")
        st["cur_off"] = meta["off"]
        # reflect the resolved scale back onto the zoom knob (without recursing)
        if meta["scale"]:
            st["sync"] = True; zoom_var.set(round(meta["scale"] * 100)); st["sync"] = False
        if mode == "manual" and meta["off"]:
            st["man_off"] = meta["off"]
        used = len(set(lv.tobytes()))
        info.config(text=f"출력 {size[0]} x {size[1]}   배율 {('%.0f%%' % (meta['scale']*100)) if meta['scale'] else '늘림'}\n"
                         f"16계조 중 사용 {used}단계\n"
                         f"BMP ≈ {size[0]*size[1]*3/1024:,.0f} KB   RAW = {size[0]*size[1]//2/1024:,.0f} KB")
        status.config(text=f"미리보기 - {mode}, {'디더링' if dith_var.get() else '단순 양자화'}")

    def schedule(*_):
        if st["sync"]:
            return
        if st["job"]:
            root.after_cancel(st["job"])
        st["job"] = root.after(100, render)

    def drag_preview():
        """Cheap canvas-resolution preview + red outline while dragging.
        Avoids the full resize+quantise of the panel-sized output per motion."""
        g = st["src_gray"]
        size = target_size()
        if g is None or size is None:
            return
        w, h = size
        cw, ch = max(cv_out.winfo_width(), 50), max(cv_out.winfo_height(), 50)
        view = min(cw / w, ch / h, 1.0)
        pw, ph = max(1, round(w * view)), max(1, round(h * view))
        pad = 255 if pad_var.get() == "흰색" else 0
        prev = Image.new("L", (pw, ph), pad)
        iw, ih = g.size
        s = st["man_scale"] * view
        sw, sh = max(1, round(iw * s)), max(1, round(ih * s))
        scaled = g.resize((sw, sh), Image.BILINEAR)         # fast
        ox, oy = st["man_off"]
        px, py = round(ox * view), round(oy * view)
        prev.paste(scaled, (px, py))

        cv_out.delete("all")
        photos["out"] = ImageTk.PhotoImage(prev)
        x0, y0 = (cw - pw) // 2, (ch - ph) // 2
        cv_out.create_image(x0, y0, image=photos["out"], anchor="nw")
        cv_out.create_rectangle(x0, y0, x0 + pw - 1, y0 + ph - 1, outline="#9a9a9a")
        cv_out.create_rectangle(x0 + px, y0 + py, x0 + px + sw, y0 + py + sh,
                                outline="red", width=2)
        st["out_scale"], st["out_pos"] = view, (x0, y0)

    # ---- drag on the result canvas ---------------------------------------
    def drag_start(ev):
        if st["src"] is None:
            return
        if cur_mode() != "manual":
            go_manual_from_current()
        if st["man_off"] is None:                      # seed offset from current layout
            size = target_size() or (1, 1)
            iw, ih = ImageOps.exif_transpose(st["src"]).size
            if int(rot_var.get()) in (90, 270):
                iw, ih = ih, iw
            _, ox, oy = resolve_layout(iw, ih, size, "manual", st["man_scale"], None)
            st["man_off"] = (ox, oy)
        st["drag"] = (ev.x, ev.y, st["man_off"])
        st["dragging"] = True
        if st["job"]:                                  # cancel any pending full render
            root.after_cancel(st["job"]); st["job"] = None
        drag_preview()

    def drag_move(ev):
        if not st["drag"]:
            return
        x0, y0, (ox, oy) = st["drag"]
        sc = st["out_scale"] or 1.0
        st["man_off"] = (round(ox + (ev.x - x0) / sc), round(oy + (ev.y - y0) / sc))
        drag_preview()                                 # cheap; no full convert

    def drag_end(ev):
        st["drag"] = None
        if st["dragging"]:
            st["dragging"] = False
            render()                                   # final full render (dither, 16-level)

    def center_image(ev=None):
        if st["src"] is None:
            return
        if cur_mode() != "manual":
            go_manual_from_current()
        size = target_size() or (1, 1)
        iw, ih = ImageOps.exif_transpose(st["src"]).size
        if int(rot_var.get()) in (90, 270):
            iw, ih = ih, iw
        _, ox, oy = resolve_layout(iw, ih, size, "manual", st["man_scale"], None)
        st["man_off"] = (ox, oy); schedule()

    cv_out.bind("<Button-1>", drag_start)
    cv_out.bind("<B1-Motion>", drag_move)
    cv_out.bind("<ButtonRelease-1>", drag_end)
    cv_out.bind("<Double-Button-1>", center_image)

    def save(kind):
        if st["display"] is None:
            messagebox.showinfo("저장", "먼저 이미지를 열어주세요."); return
        base = os.path.splitext(os.path.basename(st["src_path"] or "image"))[0]
        w, h = st["display"].size
        if kind == "bmp":
            p = filedialog.asksaveasfilename(defaultextension=".bmp", initialfile=f"{base}_{w}x{h}.bmp",
                                             filetypes=[("24-bit BMP", "*.bmp")])
            if p: save_bmp(st["display"], p); status.config(text=f"저장됨: {p}")
        else:
            p = filedialog.asksaveasfilename(defaultextension=".bin", initialfile=f"{base}_{w}x{h}_4bpp.bin",
                                             filetypes=[("4bpp raw", "*.bin")])
            if p: save_raw(st["levels"], p); status.config(text=f"저장됨: {p}  ({w*h//2:,} B)")

    # ---- send to the board (background thread) ---------------------------
    def send_post(msg, color="#444"):
        send_lbl.config(text=msg, foreground=color)

    def send_done():
        send_btn.config(state="normal", text="▶ 보드로 전송")

    def send_worker(ip, w, h, payload, clear):
        try:
            root.after(0, send_post, f"접속 중 {ip}:{TCP_PORT} …")
            with socket.create_connection((ip, TCP_PORT), timeout=8) as s:
                s.settimeout(20)
                if clear:
                    s.sendall(_hdr(1, 0x0F, 0, 0, w, h))
                    s.sendall(_hdr(4, 0, 0, 0, w, h))
                    root.after(0, send_post, "흰색으로 지움 …")
                s.sendall(_hdr(2, 0x0F, 0, 0, w, h))
                total, sent, CH = len(payload), 0, 8192
                while sent < total:
                    s.sendall(payload[sent:sent + CH]); sent += CH
                    sent = min(sent, total)
                    if sent % (CH * 32) == 0 or sent == total:
                        root.after(0, send_post, f"전송 {sent*100//total}%  ({sent:,}/{total:,} B)")
                s.sendall(_hdr(4, 0, 0, 0, w, h))
            root.after(0, send_post, "완료 ✓ 화면 갱신됨", "#1a7a1a")
        except Exception as e:                          # noqa
            root.after(0, send_post, f"전송 실패: {e}", "#c0392b")
        finally:
            root.after(0, send_done)

    def do_send():
        if st["levels"] is None:
            messagebox.showinfo("전송", "먼저 이미지를 열어주세요."); return
        ip = ip_var.get().strip()
        if not ip:
            messagebox.showinfo("전송", "보드 IP를 입력하세요 (부팅 로그 / CLI의 wifi)."); return
        _save_ip(ip)
        w, h = st["levels"].size
        payload = pack_4bpp(st["levels"])
        send_btn.config(state="disabled", text="전송 중…")
        send_post(f"{w}x{h}, {len(payload):,} B 전송 시작…")
        threading.Thread(target=send_worker, args=(ip, w, h, payload, clr_var.get()),
                         daemon=True).start()

    def on_mode(*_):
        if st["sync"]:
            return
        if cur_mode() != "manual":
            st["man_off"] = None                       # presets recentre
        schedule()

    def on_zoom_lbl(*_):
        zoom_lbl.config(text=f"{int(zoom_var.get())}%")

    rot_var.trace_add("write", lambda *_: rebuild_gray())
    panel_var.trace_add("write", on_panel)
    mode_var.trace_add("write", on_mode)
    zoom_var.trace_add("write", on_zoom)
    zoom_var.trace_add("write", on_zoom_lbl); on_zoom_lbl()
    for v in (w_var, h_var, rot_var, pad_var, bri_var, con_var, gam_var, shp_var,
              ac_var, dith_var, inv_var):
        v.trace_add("write", schedule)
    cv_src.bind("<Configure>", lambda e: draw(cv_src, st["src"].convert("RGB") if st["src"] else None, "src"))
    cv_out.bind("<Configure>", lambda e: schedule())
    on_panel()
    if initial:
        root.after(200, lambda: open_image(initial))
    root.mainloop()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--cli", metavar="INPUT", help="convert without the GUI")
    ap.add_argument("--open", metavar="INPUT", help="start the GUI with this image loaded")
    ap.add_argument("--panel", default="6", help="6 | 7.8 | 10.3")
    ap.add_argument("--size", help="custom WxH (overrides --panel)")
    ap.add_argument("--out", help="output .bmp")
    ap.add_argument("--raw", help="output .bin (4bpp packed)")
    ap.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270))
    ap.add_argument("--mode", default="fit", choices=("fit", "fill", "width", "height", "stretch", "manual"))
    ap.add_argument("--scale", type=float, default=0, help="manual zoom percent (mode=manual)")
    ap.add_argument("--offx", type=int, default=0, help="manual x offset in output pixels")
    ap.add_argument("--offy", type=int, default=0, help="manual y offset in output pixels")
    ap.add_argument("--pad-black", action="store_true")
    ap.add_argument("--brightness", type=float, default=0)
    ap.add_argument("--contrast", type=float, default=0)
    ap.add_argument("--gamma", type=float, default=1.0)
    ap.add_argument("--autocontrast", action="store_true", help="stretch histogram to full range")
    ap.add_argument("--sharpen", type=float, default=0, help="unsharp amount 0..100")
    ap.add_argument("--no-dither", action="store_true")
    ap.add_argument("--invert", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.cli:
        run_cli(a)
    else:
        run_gui(a.open)
