"""
first_run.py — la finestra del primo avvio: chiede la chiave Groq, la prova, la salva.

Si apre solo quando serve: eseguibile impacchettato senza groq_key.txt (vedi wavetype.ensure_key),
oppure a mano con `wavetype.py --setup`. Dal sorgente senza chiave Wavetype resta com'e' sempre
stato (motore locale), quindi questa finestra non compare.

Stile: quello della card Signal (card_styles.py, D_*): nero, Geist, etichette in Geist Mono,
un solo accento lime. Tutto cio' che si vede e' disegnato con PIL coi font di assets/fonts
(Tk non sa leggere i woff2); l'unico widget Tk vero e' il campo in cui si incolla la chiave.

La prova della chiave e' la chiamata piu' economica che Groq ha: GET /openai/v1/models
(nessun token consumato). 200 = buona, 401/403 = rifiutata, altro = rete.

Da riga di comando (solo per guardarla, niente viene salvato):
  python first_run.py --shot out.png [--state idle|typed|checking|invalid|network|ok]
"""
import ctypes
import os
import queue
import re
import sys
import threading
import webbrowser

import tkinter as tk
from PIL import Image, ImageDraw, ImageTk

import card_styles as cs
import paths

KEYS_URL = "https://console.groq.com/keys"
MODELS_URL = "https://api.groq.com/openai/v1/models"
KEY_RE = re.compile(r"gsk_[A-Za-z0-9]{20,}")

# ---- palette: la stessa della card Signal ----
BG = cs.D_BG
LINE = cs.D_LINE
RULE = cs.hexc("#1E2024")
TEXT = cs.D_TEXT
DIM = cs.D_DIM
SOFT = cs.hexc("#9AA0AA")
LIME = cs.D_LIME
CUT = cs.D_CUT
ORANGE = cs.D_ORANGE
FIELD = cs.hexc("#131519")
BTN = cs.hexc("#16181C")
BTN_HOVER = cs.hexc("#1D2025")
LIME_HOVER = cs.hexc("#D6FF6A")

# ---- copy (inglese, mercato US) ----
HEADLINE = "One key and you're talking."
SUB = ("Wavetype sends your voice to Groq and pastes back clean, punctuated text wherever "
       "you type. Groq keys are free and take a minute to make.")
STEP1 = "CREATE A KEY"
STEP2 = "PASTE IT HERE"
OPEN_BTN = "Open console.groq.com/keys"
PLACEHOLDER = "gsk_..."
CTA = "Start dictating"
STATUS = {
    "idle": ("", DIM),
    "typed": ("", DIM),
    "checking": ("CHECKING YOUR KEY WITH GROQ", SOFT),
    "invalid": ("GROQ DIDN'T ACCEPT THAT KEY. COPY IT AGAIN AND PASTE", CUT),
    "format": ("GROQ KEYS START WITH gsk_", ORANGE),
    "network": ("CAN'T REACH GROQ. CHECK YOUR CONNECTION AND RETRY", ORANGE),
    "ok": ("KEY SAVED. PRESS WIN + CTRL IN ANY APP TO DICTATE", LIME),
}

W = 440          # misure in px logici (96 dpi): si moltiplicano per la scala del monitor
PAD = 28


# ------------------------------------------------------------------ chiave
def validate_key(key, timeout=10.0):
    """("ok"|"invalid"|"format"|"network", dettaglio). Una sola GET, nessun token consumato."""
    key = (key or "").strip()
    if not KEY_RE.fullmatch(key):
        return "format", "not a gsk_ key"
    try:
        import httpx
        r = httpx.get(MODELS_URL, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except Exception as e:
        return "network", f"{type(e).__name__}: {e}"
    if r.status_code == 200:
        return "ok", "200"
    if r.status_code in (401, 403):
        return "invalid", str(r.status_code)
    return "network", f"HTTP {r.status_code}"


def save_key(key, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key.strip() + "\n")
    return path


def clipboard_key(root):
    """La chiave negli appunti, se ce n'e' una (chi torna dal browser l'ha appena copiata)."""
    try:
        m = KEY_RE.search(root.clipboard_get())
        return m.group(0) if m else ""
    except Exception:
        return ""


# ------------------------------------------------------------------ DPI e barra del titolo
_user = ctypes.windll.user32


class _SystemDpi:
    """Questo thread diventa DPI-aware (sistema) mentre la finestra e' aperta, poi torna com'era:
    il resto dell'app ragiona in pixel non scalati (caret.py) e non va toccato."""

    def __enter__(self):
        self.prev = None
        try:
            f = _user.SetThreadDpiAwarenessContext
            f.restype = ctypes.c_void_p
            f.argtypes = [ctypes.c_void_p]
            self.prev = f(ctypes.c_void_p(-2))          # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE
        except Exception:
            pass
        try:
            self.dpi = int(_user.GetDpiForSystem()) or 96
        except Exception:
            self.dpi = 96
        return self

    def __exit__(self, *a):
        if self.prev:
            try:
                _user.SetThreadDpiAwarenessContext(ctypes.c_void_p(self.prev))
            except Exception:
                pass


def _dark_titlebar(root):
    """Barra del titolo scura e dello stesso nero della finestra (Windows 11; su 10 solo scura)."""
    try:
        hwnd = _user.GetParent(root.winfo_id()) or root.winfo_id()
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)        # IMMERSIVE_DARK_MODE
        col = ctypes.c_int(BG[0] | (BG[1] << 8) | (BG[2] << 16))       # COLORREF 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(col), 4)       # CAPTION_COLOR
        txt = ctypes.c_int(TEXT[0] | (TEXT[1] << 8) | (TEXT[2] << 16))
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)       # TEXT_COLOR
    except Exception:
        pass


# ------------------------------------------------------------------ disegno
def _wrap(text, f, width):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if f.getlength(t) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _rrect(img, x, y, w, h, r, fill, border=None, bw=1):
    x, y, w, h = int(round(x)), int(round(y)), int(round(w)), int(round(h))
    if border is not None:
        img.alpha_composite(cs.solid((w, h), border, cs.rr_mask(w, h, r)), (x, y))
        img.alpha_composite(cs.solid((w - 2 * bw, h - 2 * bw), fill, cs.rr_mask(w - 2 * bw, h - 2 * bw, max(0, r - bw))),
                            (x + bw, y + bw))
    else:
        img.alpha_composite(cs.solid((w, h), fill, cs.rr_mask(w, h, r)), (x, y))


def _wave(img, x0, x1, cy, amp, k, color):
    """L'oscilloscopio della card Signal, fermo: piu' voci di seno, sfumato verso sinistra."""
    import math
    S = 3
    w, h = int(x1 - x0), int(amp * 2 + 8 * k)
    big = Image.new("L", (w * S, h * S), 0)
    d = ImageDraw.Draw(big)
    pts = []
    for i in range(w * S):
        t = i / (w * S)
        env = (0.25 + 0.75 * t) * (0.55 + 0.45 * math.sin(t * 9.0 + 0.6) ** 2)
        y = (math.sin(t * 61.0) * 0.6 + math.sin(t * 23.0 + 1.1) * 0.3 + math.sin(t * 131.0) * 0.1)
        pts.append((i, h * S / 2 + y * env * amp * S))
    d.line(pts, fill=255, width=max(2, int(1.8 * k * S)), joint="curve")
    m = big.resize((w, h), Image.LANCZOS)
    ramp = Image.linear_gradient("L").rotate(90, expand=True).resize((w, h))   # 0 a sinistra
    ramp = ramp.point(lambda v: int(255 * (v / 255.0) ** 0.8))
    from PIL import ImageChops
    m = ImageChops.multiply(m, ramp)
    img.alpha_composite(cs.solid((w, h), color, m), (int(x0), int(cy - h / 2)))


class KeyWindow:
    def __init__(self, save_path, log=print, dry=False):
        self.save_path = save_path
        self.log = log
        self.dry = dry
        self.result = None
        self.state = "idle"
        self.hover = None
        self.focus = False
        self.q = queue.Queue()
        self.busy = False

    # --- misure ---
    def px(self, v):
        return v * self.k

    def ipx(self, v):
        return int(round(v * self.k))

    def layout(self):
        k = self.k
        self.f_head = cs.font("geist", self.px(24), wght=600)
        self.f_sub = cs.font("geist", self.px(14.5), wght=400)
        self.f_lab = cs.font("gmono", self.px(10), wght=500)
        self.f_btn = cs.font("geist", self.px(14), wght=500)
        self.f_cta = cs.font("geist", self.px(15), wght=600)
        self.f_key = cs.font("gmono", self.px(10.5), wght=500)
        inner = W - 2 * PAD
        self.sub_lines = _wrap(SUB, self.f_sub, self.px(inner))
        y = 44 + 30
        self.y_head = y
        y += 40
        self.y_sub = y
        y += 21 * len(self.sub_lines) + 26
        self.y_s1 = y
        self.r_open = (PAD, y + 24, inner, 44)
        y += 24 + 44 + 22
        self.y_s2 = y
        self.r_field = (PAD, y + 24, inner, 46)
        self.r_paste = (PAD + inner - 8 - 62, y + 24 + 9, 62, 28)
        y += 24 + 46
        self.y_status = y + 15
        y += 36
        self.r_cta = (PAD, y, inner, 48)
        y += 48 + 26
        self.y_foot = y
        self.H = y + 46
        self.size = (self.ipx(W), self.ipx(self.H))

    def hit(self, x, y):
        for name, r in (("open", self.r_open), ("paste", self.r_paste), ("cta", self.r_cta)):
            rx, ry, rw, rh = (self.px(v) for v in r)
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return name
        return None

    def cta_enabled(self):
        return (bool(self.value()) and not self.busy) or self.state == "ok"

    # --- disegno di tutta la finestra (tranne il testo del campo, che e' un widget Tk) ---
    def render(self):
        k = self.k
        px = self.px
        img = Image.new("RGBA", self.size, BG + (255,))
        d = ImageDraw.Draw(img)
        one = max(1, self.ipx(1))
        # testata: il marchio a sinistra, l'oscilloscopio a destra (come il pie' della card Signal)
        cs.put_center(img, cs.glow_disc(px(3.5), px(6), LIME, 0.55), px(PAD + 4), px(22))
        cs.put_center(img, cs.disc(px(3.5), LIME), px(PAD + 4), px(22))
        cs.text_center(img, "WAVETYPE", self.f_lab, TEXT, px(PAD + 16), px(22), px(1.2))
        _wave(img, px(170), px(W - PAD), px(22), px(9), k, LIME)
        d.rectangle([0, self.ipx(44) - one, self.size[0], self.ipx(44) - 1], fill=RULE + (255,))
        # titolo e sottotitolo
        cs.text_center(img, HEADLINE, self.f_head, TEXT, px(PAD), px(self.y_head + 14), px(-0.3))
        for i, ln in enumerate(self.sub_lines):
            cs.text_center(img, ln, self.f_sub, SOFT, px(PAD), px(self.y_sub + 10 + 21 * i))
        # passo 1
        self._step(img, "01", STEP1, self.y_s1)
        x, y, w, h = self.r_open
        hov = self.hover == "open"
        _rrect(img, px(x), px(y), px(w), px(h), px(10), BTN_HOVER if hov else BTN,
               border=(LINE if not hov else cs.hexc("#3A3D44")), bw=one)
        cs.text_center(img, OPEN_BTN, self.f_btn, TEXT, px(x + 16), px(y + h / 2))
        self._arrow(img, px(x + w - 22), px(y + h / 2), LIME if hov else SOFT)
        # passo 2: il campo (il testo lo scrive il widget Entry sopra questo disegno)
        self._step(img, "02", STEP2, self.y_s2)
        x, y, w, h = self.r_field
        border = {"invalid": CUT, "format": ORANGE, "network": ORANGE, "ok": LIME}.get(self.state)
        if border is None:
            border = LIME if self.focus else LINE
        _rrect(img, px(x), px(y), px(w), px(h), px(10), FIELD, border=border, bw=one)
        if not self.value() and self.state != "ok":
            px_, py_, pw, ph = self.r_paste
            hov = self.hover == "paste"
            _rrect(img, px(px_), px(py_), px(pw), px(ph), px(6), BTN_HOVER if hov else BTN,
                   border=cs.hexc("#3A3D44") if hov else LINE, bw=one)
            tw = cs.label_width("PASTE", self.f_key, px(0.8))
            cs.text_center(img, "PASTE", self.f_key, LIME if hov else TEXT,
                           px(px_ + pw / 2) - tw / 2, px(py_ + ph / 2), px(0.8))
        elif self.state == "ok":
            self._check(img, px(x + w - 24), px(y + h / 2), LIME)
        # riga di stato
        msg, col = STATUS.get(self.state, ("", DIM))
        if self.state == "checking":
            dots = "." * (1 + (self.tick_n // 4) % 3)
            msg = msg + dots
        if msg:
            cs.put_center(img, cs.disc(px(2.5), col), px(PAD + 3), px(self.y_status))
            cs.text_center(img, msg, self.f_lab, col, px(PAD + 12), px(self.y_status), px(0.6))
        # bottone principale
        x, y, w, h = self.r_cta
        on = self.cta_enabled() or self.state == "checking"
        if on:
            fill = LIME_HOVER if (self.hover == "cta" and self.cta_enabled()) else LIME
            _rrect(img, px(x), px(y), px(w), px(h), px(10), fill)
            tcol = BG
        else:
            _rrect(img, px(x), px(y), px(w), px(h), px(10), cs.hexc("#1A1C20"))
            tcol = cs.hexc("#4A4E56")
        label = {"checking": "Checking...", "ok": "Got it"}.get(self.state, CTA)
        tw = cs.label_width(label, self.f_cta)
        cs.text_center(img, label, self.f_cta, tcol, px(x + w / 2) - tw / 2, px(y + h / 2))
        # pie': come si usa, a tasti
        d.rectangle([0, self.ipx(self.y_foot) - one, self.size[0], self.ipx(self.y_foot) - 1],
                    fill=RULE + (255,))
        cy = px(self.y_foot + 23)
        xx = px(PAD)
        for part in (("kbd", "Win"), ("txt", "+"), ("kbd", "Ctrl"), ("txt", "start and stop, in any app")):
            xx += self._foot_part(img, part, xx, cy) + px(6)
        self._foot_part(img, ("txt", "cancel"), px(W - PAD) - cs.label_width("cancel", self.f_key), cy)
        ew = cs.label_width("cancel", self.f_key) + px(6)
        self._foot_part(img, ("kbd", "Esc"), px(W - PAD) - ew - self._kbd_w("Esc"), cy)
        return img

    def _kbd_w(self, t):
        return cs.label_width(t, self.f_key) + self.px(12)

    def _foot_part(self, img, part, x, cy):
        kind, t = part
        if kind == "kbd":
            w = self._kbd_w(t)
            h = self.px(20)
            _rrect(img, x, cy - h / 2, w, h, self.px(4), BTN, border=LINE, bw=max(1, self.ipx(1)))
            cs.text_center(img, t, self.f_key, TEXT, x + self.px(6), cy)
            return w
        return cs.text_center(img, t, self.f_key, DIM, x, cy)

    def _step(self, img, num, label, y):
        px = self.px
        w = cs.text_center(img, num, self.f_lab, LIME, px(PAD), px(y + 8), px(0.8))
        cs.text_center(img, label, self.f_lab, DIM, px(PAD) + w + px(10), px(y + 8), px(1.0))

    def _arrow(self, img, cx, cy, col):
        """Freccia "si apre fuori" (in alto a destra), antialiasata."""
        S = 4
        s = int(self.px(12))
        big = Image.new("L", (s * S, s * S), 0)
        d = ImageDraw.Draw(big)
        lw = max(2, int(self.px(1.6) * S))
        m = lw
        d.line([(m, s * S - m), (s * S - m, m)], fill=255, width=lw)
        d.line([(s * S * 0.35, m), (s * S - m, m), (s * S - m, s * S * 0.65)], fill=255, width=lw,
               joint="curve")
        cs.put_center(img, cs.solid((s, s), col, big.resize((s, s), Image.LANCZOS)), cx, cy)

    def _check(self, img, cx, cy, col):
        S = 4
        s = int(self.px(14))
        big = Image.new("L", (s * S, s * S), 0)
        lw = max(2, int(self.px(2) * S))
        ImageDraw.Draw(big).line([(s * S * 0.12, s * S * 0.55), (s * S * 0.4, s * S * 0.82),
                                  (s * S * 0.9, s * S * 0.2)], fill=255, width=lw, joint="curve")
        cs.put_center(img, cs.solid((s, s), col, big.resize((s, s), Image.LANCZOS)), cx, cy)

    # --- stato ---
    def value(self):
        if self.placeholder:
            return ""
        return self.entry.get().strip()

    def redraw(self):
        self.photo = ImageTk.PhotoImage(self.render())
        self.canvas.itemconfig(self.bg_item, image=self.photo)
        self.canvas.configure(cursor="hand2" if self.hover and (self.hover != "cta" or self.cta_enabled())
                              else "")

    def set_placeholder(self, on):
        self.placeholder = on
        self.entry.delete(0, "end")
        if on:
            self.entry.configure(show="", fg=self._hex(DIM))
            self.entry.insert(0, PLACEHOLDER)
        else:
            self.entry.configure(show="•", fg=self._hex(TEXT))

    @staticmethod
    def _hex(c):
        return "#%02x%02x%02x" % tuple(c)

    def fill(self, key, check=True):
        self.set_placeholder(False)
        self.entry.insert(0, key)
        self.state = "typed"
        self.redraw()
        if check:
            self.submit()

    def submit(self):
        if self.state == "ok":                  # chiave salvata: il bottone chiude e l'app parte
            self.root.destroy()
            return
        if not self.cta_enabled():
            return
        key = self.value()
        if not KEY_RE.fullmatch(key):
            self.state = "format"
            self.redraw()
            return
        self.busy = True
        self.state = "checking"
        self.redraw()
        threading.Thread(target=lambda: self.q.put(validate_key(key)), daemon=True).start()

    def poll(self):
        self.tick_n += 1
        try:
            status, detail = self.q.get_nowait()
        except queue.Empty:
            if self.state == "checking":
                self.redraw()
            self.root.after(120, self.poll)
            return
        self.busy = False
        self.log(f"[setup] prova della chiave: {status} ({detail})")
        self.state = status
        if status == "ok":
            key = self.value()
            if not self.dry:
                try:
                    save_key(key, self.save_path)
                    self.log(f"[setup] chiave salvata in {self.save_path}")
                except Exception as e:
                    self.log(f"[setup] salvataggio fallito: {e}")
            self.result = key
            self.entry.configure(state="disabled", disabledbackground=self._hex(FIELD),
                                 disabledforeground=self._hex(TEXT))
            self.redraw()
            return
        self.redraw()
        self.root.after(120, self.poll)

    # --- eventi ---
    def on_motion(self, e):
        h = self.hit(e.x, e.y)
        if h == "paste" and (self.value() or self.state == "ok"):
            h = None
        if h != self.hover:
            self.hover = h
            self.redraw()

    def on_click(self, e):
        h = self.hit(e.x, e.y)
        if h == "open":
            webbrowser.open(KEYS_URL)
        elif h == "paste" and not self.value():
            k = clipboard_key(self.root)
            if k:
                self.fill(k)
            else:
                self.entry.focus_set()
                try:
                    txt = self.root.clipboard_get().strip()
                except Exception:
                    txt = ""
                if txt:
                    self.fill(txt, check=False)
                    self.state = "format"
                    self.redraw()
        elif h == "cta":
            self.submit()

    def on_focus_in(self, _e=None):
        if self.value() or self.busy or self.state == "ok":
            return
        k = clipboard_key(self.root)             # tornato dal browser con la chiave copiata
        if k:
            self.fill(k)

    def on_entry_focus(self, on):
        self.focus = on
        if on and self.placeholder:
            self.set_placeholder(False)
        elif not on and not self.entry.get().strip():
            self.set_placeholder(True)
        self.redraw()

    def on_key(self, _e=None):
        if self.state not in ("checking", "ok"):
            self.state = "typed" if self.value() else "idle"
        self.redraw()

    # --- finestra ---
    def run(self, preset=None):
        with _SystemDpi() as dpi:
            self.k = dpi.dpi / 96.0
            self.tick_n = 0
            self.layout()
            root = self.root = tk.Tk()
            root.withdraw()
            root.title("Wavetype")
            root.configure(bg=self._hex(BG))
            root.resizable(False, False)
            try:
                ico = paths.resource(os.path.join("assets", "wavetype.ico"),
                                     os.path.dirname(os.path.abspath(__file__)))
                if os.path.exists(ico):
                    root.iconbitmap(default=ico)
            except Exception:
                pass
            w, h = self.size
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - self.ipx(40))}")
            c = self.canvas = tk.Canvas(root, width=w, height=h, highlightthickness=0, bd=0,
                                        bg=self._hex(BG))
            c.pack()
            self.photo = None
            self.bg_item = c.create_image(0, 0, anchor="nw")
            x, y, fw, fh = self.r_field
            # font del campo: monospazio di sistema (Tk non legge i woff2 di assets/fonts)
            fam = "Cascadia Mono" if "Cascadia Mono" in root.tk.call("font", "families") else "Consolas"
            e = self.entry = tk.Entry(root, bd=0, relief="flat", highlightthickness=0,
                                      bg=self._hex(FIELD), fg=self._hex(TEXT),
                                      insertbackground=self._hex(LIME), insertwidth=max(2, self.ipx(1.5)),
                                      selectbackground=self._hex(cs.hexc("#2F3A12")),
                                      selectforeground=self._hex(TEXT), font=(fam, 11))
            c.create_window(self.px(x + 16), self.px(y + fh / 2), anchor="w", window=e,
                            width=self.px(fw - 16 - 8 - 62 - 10), height=self.px(24))
            self.placeholder = False
            self.set_placeholder(True)
            e.bind("<FocusIn>", lambda _e: self.on_entry_focus(True))
            e.bind("<FocusOut>", lambda _e: self.on_entry_focus(False))
            e.bind("<KeyRelease>", self.on_key)
            e.bind("<<Paste>>", lambda _e: root.after(1, self.on_key))
            root.bind("<Return>", lambda _e: self.submit())
            root.bind("<Escape>", lambda _e: root.destroy())
            root.bind("<FocusIn>", lambda ev: self.on_focus_in() if ev.widget is root else None)
            c.bind("<Motion>", self.on_motion)
            c.bind("<Leave>", lambda _e: (setattr(self, "hover", None), self.redraw()))
            c.bind("<Button-1>", self.on_click)
            self.redraw()
            root.deiconify()
            root.update_idletasks()
            _dark_titlebar(root)
            root.lift()
            root.attributes("-topmost", True)
            root.after(400, lambda: root.attributes("-topmost", False))
            root.focus_force()
            if preset:
                preset(self)
            root.after(120, self.poll)
            root.mainloop()
        return self.result


def ask_for_key(save_path, log=print):
    """Apre la finestra e aspetta. Torna la chiave (gia' provata e salvata) o None se chiusa."""
    return KeyWindow(save_path, log=log).run()


# ------------------------------------------------------------------ foto per la revisione
def _shot(path, state):
    """Apre la finestra in uno stato, la fotografa com'e' a schermo (ImageGrab), la chiude."""
    from PIL import ImageGrab

    def preset(win):
        fake = "gsk_" + "x" * 52
        if state in ("typed", "checking", "invalid", "network", "ok", "format"):
            win.set_placeholder(False)
            win.entry.insert(0, fake if state != "format" else "sk-proj-123")
            win.state = state
            if state == "checking":
                win.busy = True
            if state == "ok":
                win.entry.configure(state="disabled", disabledbackground=win._hex(FIELD),
                                    disabledforeground=win._hex(TEXT))
        if state == "hover":
            win.hover = "open"
        win.redraw()

        def grab():
            win.root.update()
            with _SystemDpi():
                hwnd = _user.GetParent(win.root.winfo_id()) or win.root.winfo_id()
                r = ctypes.wintypes.RECT()
                if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)):
                    _user.GetWindowRect(hwnd, ctypes.byref(r))     # 9 = EXTENDED_FRAME_BOUNDS
                ImageGrab.grab((r.left, r.top, r.right, r.bottom)).save(path)
            print(f"{path} {r.right - r.left}x{r.bottom - r.top}")
            win.root.destroy()
        win.root.after(700, grab)

    import ctypes.wintypes  # noqa: F401
    KeyWindow(os.devnull, log=print, dry=True).run(preset=preset)


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--shot" in args:
        out = args[args.index("--shot") + 1]
        st = args[args.index("--state") + 1] if "--state" in args else "idle"
        _shot(out, st)
    else:
        print(KeyWindow(os.devnull, dry=True).run())
