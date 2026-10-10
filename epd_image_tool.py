#!/usr/bin/env python3
"""
EPD Image Tool - resize any picture to an e-paper panel and quantise it to
16 gray levels, with a live preview of the result.

Standalone; nothing in the firmware depends on it.

    python epd_image_tool.py                      # GUI
    python epd_image_tool.py --selftest           # exercise the converter
    python epd_image_tool.py --cli IN --panel 6 --out out.bmp [--raw out.bin]

Outputs
    .bmp   24-bit, R=G=B, values 0,17,34..255 (16 steps). This is what the
           legacy PC tools (EPDdemo / EPDctrl) load.
    .bin   4 bits per pixel, two pixels per byte, first pixel in the HIGH
           nibble, 0 = black .. 15 = white. Exactly the stream the firmware
           pushes into the IT8951 image buffer.

Requires Pillow (pip install pillow). Tkinter ships with Python.
"""
import argparse
import os
import sys
import tempfile

from PIL import Image, ImageEnhance, ImageOps

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

FIT_MODES = ("fit (여백)", "fill (잘라냄)", "stretch (늘림)")
LEVELS = 16

# 16-entry gray palette image used for dithered quantisation
_PAL = Image.new("P", (1, 1))
_PAL.putpalette([v for i in range(LEVELS) for v in (i * 17,) * 3] + [0] * (768 - LEVELS * 3))


# ---------------------------------------------------------------------------
#  Conversion core (no GUI)
# ---------------------------------------------------------------------------
def convert(img, size, *, rotate=0, fit="fit", pad_white=True,
            brightness=0, contrast=0, gamma=1.0, dither=True, invert=False):
    """
    Return (levels_img, display_img):
      levels_img  - mode "L", each pixel is its 4-bit level 0..15
      display_img - mode "L", each pixel is level*17 (what you see / save)
    """
    w, h = size
    if w % 2:
        raise ValueError("width must be even (two pixels per byte)")

    im = ImageOps.exif_transpose(img).convert("RGB")
    if rotate:
        im = im.rotate(-rotate, expand=True)          # clockwise degrees

    if fit == "stretch":
        im = im.resize((w, h), Image.LANCZOS)
    elif fit == "fill":
        im = ImageOps.fit(im, (w, h), Image.LANCZOS, centering=(0.5, 0.5))
    else:                                              # fit
        im.thumbnail((w, h), Image.LANCZOS)
        canvas = Image.new("RGB", (w, h), (255, 255, 255) if pad_white else (0, 0, 0))
        canvas.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
        im = canvas

    gray = im.convert("L")
    if brightness:
        gray = ImageEnhance.Brightness(gray).enhance(1 + brightness / 100.0)
    if contrast:
        gray = ImageEnhance.Contrast(gray).enhance(1 + contrast / 100.0)
    if gamma and abs(gamma - 1.0) > 1e-3:
        lut = [round(255 * ((i / 255.0) ** (1.0 / gamma))) for i in range(256)]
        gray = gray.point(lut)
    if invert:
        gray = ImageOps.invert(gray)

    if dither:
        q = gray.convert("RGB").quantize(palette=_PAL, dither=Image.Dither.FLOYDSTEINBERG)
        # Go through the palette to the actual gray (0,17,..,255) and divide back
        # down to the level. This is robust to Pillow picking any of the padded
        # duplicate black entries (index >= 16) for a black pixel.
        levels = q.convert("L").point([round(v / 17) for v in range(256)])
    else:
        levels = gray.point([i // 16 for i in range(256)])

    display = levels.point([min(255, i * 17) for i in range(256)])
    return levels, display


def pack_4bpp(levels_img):
    """Two pixels per byte, first pixel in the high nibble."""
    data = levels_img.tobytes()
    hi = data[0::2]
    lo = data[1::2]
    return bytes(((a & 0x0F) << 4) | (b & 0x0F) for a, b in zip(hi, lo))


def save_bmp(display_img, path):
    display_img.convert("RGB").save(path, "BMP")       # 24-bit


def save_raw(levels_img, path):
    with open(path, "wb") as f:
        f.write(pack_4bpp(levels_img))


# ---------------------------------------------------------------------------
#  Self test
# ---------------------------------------------------------------------------
def selftest():
    src = Image.linear_gradient("L").resize((640, 480)).convert("RGB")
    for name, size in PANEL_BY_INCH.items():
        for dither in (False, True):
            lv, disp = convert(src, size, dither=dither)
            assert lv.size == size, (name, lv.size)
            uniq = set(lv.tobytes())
            assert uniq <= set(range(LEVELS)), "level out of range"
            raw = pack_4bpp(lv)
            assert len(raw) == size[0] * size[1] // 2, "raw size"
            print(f"  {name:>4}\"  dither={dither!s:5}  {size[0]}x{size[1]}  "
                  f"levels={len(uniq):2}  raw={len(raw):,} B")
    with tempfile.TemporaryDirectory() as d:
        lv, disp = convert(src, PANEL_BY_INCH["6"])
        p = os.path.join(d, "t.bmp"); save_bmp(disp, p)
        with Image.open(p) as back:            # close before the temp dir is removed
            assert back.size == PANEL_BY_INCH["6"] and back.mode == "RGB", "bmp roundtrip"
            print(f"  bmp roundtrip ok ({os.path.getsize(p):,} B, mode {back.mode})")
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
    lv, disp = convert(img, size, rotate=a.rotate, fit=a.fit, pad_white=not a.pad_black,
                       brightness=a.brightness, contrast=a.contrast, gamma=a.gamma,
                       dither=not a.no_dither, invert=a.invert)
    if a.out:
        save_bmp(disp, a.out); print(f"wrote {a.out}  ({size[0]}x{size[1]} 24-bit BMP)")
    if a.raw:
        save_raw(lv, a.raw);   print(f"wrote {a.raw}  ({size[0]*size[1]//2:,} B, 4bpp)")


# ---------------------------------------------------------------------------
#  GUI
# ---------------------------------------------------------------------------
def run_gui(initial=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from PIL import ImageTk

    root = tk.Tk()
    root.title("EPD Image Tool - 16계조 변환")
    root.geometry("1180x720")
    root.minsize(900, 560)

    state = {"src": None, "src_path": None, "levels": None, "display": None, "job": None}

    # ---- controls (left) --------------------------------------------------
    side = ttk.Frame(root, padding=10)
    side.pack(side="left", fill="y")

    def row(label):
        f = ttk.Frame(side); f.pack(fill="x", pady=3)
        ttk.Label(f, text=label, width=10).pack(side="left")
        return f

    ttk.Button(side, text="이미지 열기…", command=lambda: open_image()).pack(fill="x", pady=(0, 8))
    src_lbl = ttk.Label(side, text="(파일 없음)", foreground="#666", wraplength=230)
    src_lbl.pack(fill="x")

    ttk.Separator(side).pack(fill="x", pady=8)

    panel_var = tk.StringVar(value=list(PANELS)[0])
    f = row("패널")
    panel_cb = ttk.Combobox(f, textvariable=panel_var, values=list(PANELS), state="readonly", width=26)
    panel_cb.pack(side="left", fill="x", expand=True)

    f = row("크기")
    w_var = tk.StringVar(value="1072"); h_var = tk.StringVar(value="1448")
    w_ent = ttk.Entry(f, textvariable=w_var, width=7); w_ent.pack(side="left")
    ttk.Label(f, text=" x ").pack(side="left")
    h_ent = ttk.Entry(f, textvariable=h_var, width=7); h_ent.pack(side="left")

    f = row("회전")
    rot_var = tk.StringVar(value="0")
    ttk.Combobox(f, textvariable=rot_var, values=("0", "90", "180", "270"), state="readonly", width=6).pack(side="left")
    ttk.Label(f, text="° 시계방향").pack(side="left")

    f = row("맞춤")
    fit_var = tk.StringVar(value=FIT_MODES[0])
    ttk.Combobox(f, textvariable=fit_var, values=FIT_MODES, state="readonly", width=16).pack(side="left")
    pad_var = tk.StringVar(value="흰색")
    ttk.Combobox(f, textvariable=pad_var, values=("흰색", "검정"), state="readonly", width=5).pack(side="left", padx=(6, 0))

    def slider(label, var, lo, hi, res=1):
        f = row(label)
        s = ttk.Scale(f, from_=lo, to=hi, variable=var, orient="horizontal")
        s.pack(side="left", fill="x", expand=True)
        v = ttk.Label(f, width=5); v.pack(side="left")
        def upd(*_): v.config(text=f"{var.get():.2f}" if res != 1 else f"{int(var.get())}")
        var.trace_add("write", upd); upd()
        return s

    bri_var = tk.DoubleVar(value=0);   slider("밝기", bri_var, -100, 100)
    con_var = tk.DoubleVar(value=0);   slider("대비", con_var, -100, 100)
    gam_var = tk.DoubleVar(value=1.0); slider("감마", gam_var, 0.3, 3.0, res=0.01)

    dith_var = tk.BooleanVar(value=True)
    inv_var = tk.BooleanVar(value=False)
    f = ttk.Frame(side); f.pack(fill="x", pady=6)
    ttk.Checkbutton(f, text="디더링 (Floyd-Steinberg)", variable=dith_var).pack(anchor="w")
    ttk.Checkbutton(f, text="반전", variable=inv_var).pack(anchor="w")

    ttk.Button(side, text="초기화", command=lambda: reset_adjust()).pack(fill="x", pady=(2, 8))

    ttk.Separator(side).pack(fill="x", pady=8)
    ttk.Button(side, text="BMP 저장 (24-bit, 툴 입력용)…", command=lambda: save("bmp")).pack(fill="x", pady=2)
    ttk.Button(side, text="RAW 저장 (4bpp, 펌웨어 포맷)…", command=lambda: save("raw")).pack(fill="x", pady=2)

    info = ttk.Label(side, text="", foreground="#444", justify="left", wraplength=240)
    info.pack(fill="x", pady=(12, 0))

    # ---- previews (right) -------------------------------------------------
    right = ttk.Frame(root, padding=(0, 10, 10, 10))
    right.pack(side="right", fill="both", expand=True)
    hdr = ttk.Frame(right); hdr.pack(fill="x")
    ttk.Label(hdr, text="원본").pack(side="left", expand=True)
    ttk.Label(hdr, text="결과 (16계조, 실제 출력)").pack(side="left", expand=True)
    cv_frame = ttk.Frame(right); cv_frame.pack(fill="both", expand=True)
    cv_src = tk.Canvas(cv_frame, bg="#dcdcdc", highlightthickness=0)
    cv_out = tk.Canvas(cv_frame, bg="#dcdcdc", highlightthickness=0)
    cv_src.pack(side="left", fill="both", expand=True, padx=(0, 4))
    cv_out.pack(side="left", fill="both", expand=True, padx=(4, 0))
    status = ttk.Label(right, text="이미지를 열어 시작하세요.", anchor="w")
    status.pack(fill="x", pady=(6, 0))

    photos = {}

    def draw(canvas, img, key):
        canvas.delete("all")
        if img is None:
            return
        cw, ch = max(canvas.winfo_width(), 50), max(canvas.winfo_height(), 50)
        scale = min(cw / img.width, ch / img.height, 1.0)
        shown = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
        photos[key] = ImageTk.PhotoImage(shown)
        canvas.create_image(cw // 2, ch // 2, image=photos[key], anchor="center")

    # ---- logic --------------------------------------------------------------
    def target_size():
        try:
            w, h = int(w_var.get()), int(h_var.get())
        except ValueError:
            return None
        if w < 2 or h < 1:
            return None
        return (w - (w % 2), h)                         # keep width even

    def on_panel(*_):
        size = PANELS[panel_var.get()]
        custom = size is None
        for e in (w_ent, h_ent):
            e.config(state="normal" if custom else "readonly")
        if not custom:
            w_var.set(str(size[0])); h_var.set(str(size[1]))
        schedule()

    def open_image(path=None):
        p = path or filedialog.askopenfilename(filetypes=[("이미지", "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp"), ("모든 파일", "*.*")])
        if not p:
            return
        try:
            img = Image.open(p); img.load()
        except Exception as e:
            messagebox.showerror("열기 실패", str(e)); return
        state["src"], state["src_path"] = img, p
        src_lbl.config(text=f"{os.path.basename(p)}\n{img.width} x {img.height}  {img.mode}")
        draw(cv_src, img.convert("RGB"), "src")
        schedule()

    def reset_adjust():
        bri_var.set(0); con_var.set(0); gam_var.set(1.0)
        dith_var.set(True); inv_var.set(False); rot_var.set("0")
        fit_var.set(FIT_MODES[0]); pad_var.set("흰색")
        schedule()

    def schedule(*_):
        if state["job"]:
            root.after_cancel(state["job"])
        state["job"] = root.after(120, render)          # debounce slider drags

    def render():
        state["job"] = None
        if state["src"] is None:
            return
        size = target_size()
        if size is None:
            status.config(text="크기가 올바르지 않습니다 (가로는 짝수).")
            return
        fit = {"fit (여백)": "fit", "fill (잘라냄)": "fill", "stretch (늘림)": "stretch"}[fit_var.get()]
        try:
            lv, disp = convert(state["src"], size, rotate=int(rot_var.get()), fit=fit,
                               pad_white=(pad_var.get() == "흰색"),
                               brightness=bri_var.get(), contrast=con_var.get(),
                               gamma=gam_var.get(), dither=dith_var.get(), invert=inv_var.get())
        except Exception as e:
            status.config(text=f"변환 오류: {e}"); return
        state["levels"], state["display"] = lv, disp
        draw(cv_out, disp, "out")
        used = len(set(lv.tobytes()))
        info.config(text=f"출력 {size[0]} x {size[1]}\n16계조 중 사용 {used}단계\n"
                         f"BMP ≈ {size[0]*size[1]*3/1024:,.0f} KB   RAW = {size[0]*size[1]//2/1024:,.0f} KB")
        status.config(text=f"미리보기 갱신됨 - {size[0]}x{size[1]}, {'디더링' if dith_var.get() else '단순 양자화'}")

    def save(kind):
        if state["display"] is None:
            messagebox.showinfo("저장", "먼저 이미지를 열어주세요."); return
        base = os.path.splitext(os.path.basename(state["src_path"] or "image"))[0]
        w, h = state["display"].size
        if kind == "bmp":
            p = filedialog.asksaveasfilename(defaultextension=".bmp", initialfile=f"{base}_{w}x{h}.bmp",
                                             filetypes=[("24-bit BMP", "*.bmp")])
            if p: save_bmp(state["display"], p); status.config(text=f"저장됨: {p}")
        else:
            p = filedialog.asksaveasfilename(defaultextension=".bin", initialfile=f"{base}_{w}x{h}_4bpp.bin",
                                             filetypes=[("4bpp raw", "*.bin")])
            if p: save_raw(state["levels"], p); status.config(text=f"저장됨: {p}  ({w*h//2:,} B)")

    # ---- wiring -------------------------------------------------------------
    panel_var.trace_add("write", on_panel)
    for v in (w_var, h_var, rot_var, fit_var, pad_var, bri_var, con_var, gam_var, dith_var, inv_var):
        v.trace_add("write", schedule)
    cv_src.bind("<Configure>", lambda e: draw(cv_src, state["src"].convert("RGB") if state["src"] else None, "src"))
    cv_out.bind("<Configure>", lambda e: draw(cv_out, state["display"], "out"))
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
    ap.add_argument("--fit", default="fit", choices=("fit", "fill", "stretch"))
    ap.add_argument("--pad-black", action="store_true")
    ap.add_argument("--brightness", type=float, default=0)
    ap.add_argument("--contrast", type=float, default=0)
    ap.add_argument("--gamma", type=float, default=1.0)
    ap.add_argument("--no-dither", action="store_true")
    ap.add_argument("--invert", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.cli:
        run_cli(a)
    else:
        run_gui(a.open)
