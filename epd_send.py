#!/usr/bin/env python3
"""
EPD Send - push a converted image to the EPD controller over TCP, at any
panel size (unlike the 6"-only EPDdemo).

It speaks the exact legacy wire protocol the firmware implements:
  10-byte little-endian header  { u8 command, u8 color, u16 x, u16 y, u16 w, u16 h }
  commands: 1 fill, 2 load (header + w*h/2 bytes of 4bpp pixels), 4 display
and replays EPDdemo's sequence:  fill white -> display -> load -> display.

    python epd_send.py image.bmp  --ip 192.168.45.154
    python epd_send.py image.bin  --ip 192.168.45.154 --w 1872 --h 1404
    python epd_send.py image.bmp  --ip 192.168.45.154 --no-clear
    python epd_send.py --selftest

Input
  .bmp / .png / ...  must ALREADY be the panel size (use epd_image_tool.py
                     to resize + quantise to 16 gray first). Converted to
                     4bpp here, first pixel in the high nibble.
  .bin               raw 4bpp packed, needs --w/--h (or --panel).
"""
import argparse
import os
import socket
import struct
import sys
import threading
import time

PORT = 8319
PANEL_BY_INCH = {"6": (1072, 1448), "7.8": (1872, 1404), "10.3": (1872, 1404)}


def header(cmd, color, x, y, w, h):
    return struct.pack("<BBHHHH", cmd, color, x, y, w, h)


def pack_4bpp_from_image(path, size):
    from PIL import Image
    im = Image.open(path).convert("L")
    if im.size != size:
        raise SystemExit(f"image is {im.size}, panel is {size}. "
                         f"Resize it with epd_image_tool.py first.")
    # quantise to the 16 levels, two pixels per byte, first pixel high nibble
    levels = im.point([i // 16 for i in range(256)])
    data = levels.tobytes()
    hi, lo = data[0::2], data[1::2]
    return bytes(((a & 0x0F) << 4) | (b & 0x0F) for a, b in zip(hi, lo))


def load_payload(path, size):
    if path.lower().endswith(".bin"):
        raw = open(path, "rb").read()
        need = size[0] * size[1] // 2
        if len(raw) != need:
            raise SystemExit(f".bin is {len(raw):,} B, panel {size} needs {need:,} B")
        return raw
    return pack_4bpp_from_image(path, size)


def send_image(ip, size, payload, *, clear=True, timeout=10, log=print):
    w, h = size
    log(f"connecting to {ip}:{PORT} ...")
    with socket.create_connection((ip, PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        if clear:
            s.sendall(header(1, 0x0F, 0, 0, w, h))     # fill white
            s.sendall(header(4, 0, 0, 0, w, h))        # display
            log("cleared to white")
        s.sendall(header(2, 0x0F, 0, 0, w, h))         # load
        s.sendall(payload)                             # w*h/2 bytes
        log(f"sent image payload ({len(payload):,} B)")
        s.sendall(header(4, 0, 0, 0, w, h))            # display
        log("display triggered")
    log("done (connection closed)")


# ---------------------------------------------------------------------------
#  Self test - a mock server that mimics the firmware's recv, verify bytes
# ---------------------------------------------------------------------------
def selftest():
    size = (1872, 1404)
    payload = bytes((i * 37) & 0xFF for i in range(size[0] * size[1] // 2))
    got = {"cmds": [], "payload_len": 0, "err": None}

    def recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            c = conn.recv(n - len(buf))
            if not c:
                break
            buf += c
        return buf

    def server(sock):
        try:
            conn, _ = sock.accept()
            with conn:
                while True:
                    hdr = recv_exact(conn, 10)
                    if len(hdr) < 10:
                        break
                    cmd, color, x, y, w, h = struct.unpack("<BBHHHH", hdr)
                    got["cmds"].append((cmd, color, x, y, w, h))
                    if cmd == 2:                        # load -> read pixels
                        body = recv_exact(conn, w * h // 2)
                        got["payload_len"] = len(body)
                        assert body == payload, "payload mismatch"
        except Exception as e:                          # noqa
            got["err"] = e

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(1)
    th = threading.Thread(target=server, args=(srv,), daemon=True)
    th.start()
    time.sleep(0.1)
    send_image("127.0.0.1", size, payload, clear=True, log=lambda *_: None)
    th.join(timeout=5)
    srv.close()

    assert got["err"] is None, f"server error: {got['err']}"
    cmds = [c[0] for c in got["cmds"]]
    assert cmds == [1, 4, 2, 4], f"command sequence wrong: {cmds}"
    assert got["cmds"][2] == (2, 0x0F, 0, 0, size[0], size[1]), got["cmds"][2]
    assert got["payload_len"] == size[0] * size[1] // 2, got["payload_len"]
    print(f"  sequence {cmds}  payload {got['payload_len']:,} B  header {got['cmds'][2]}")
    print("selftest OK")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", help=".bmp/.png (panel size) or .bin (4bpp)")
    ap.add_argument("--ip", help="board IP (see 'wifi' in the CLI or the boot log)")
    ap.add_argument("--panel", help="6 | 7.8 | 10.3 (sets w/h)")
    ap.add_argument("--w", type=int, help="panel width")
    ap.add_argument("--h", type=int, help="panel height")
    ap.add_argument("--no-clear", action="store_true", help="skip the white fill before load")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        selftest(); sys.exit(0)
    if not a.image or not a.ip:
        ap.error("image and --ip are required (or use --selftest)")

    size = None
    if a.panel:
        size = PANEL_BY_INCH.get(a.panel)
    if a.w and a.h:
        size = (a.w, a.h)
    if size is None:
        if a.image.lower().endswith(".bin"):
            ap.error(".bin needs --panel or --w/--h")
        from PIL import Image
        size = Image.open(a.image).size                 # trust the bitmap's size
    if size[0] % 2:
        ap.error("panel width must be even")

    payload = load_payload(a.image, size)
    send_image(a.ip, size, payload, clear=not a.no_clear)
